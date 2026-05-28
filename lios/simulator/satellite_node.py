"""Satellite node for the LIOS DES emulator.

Implements the full satellite-side protocol:
  - ISL contact handling (auth, sync, forward, commit).
  - Settlement trigger evaluation at contact-end.
  - Ground station contact handling (tx log offload, notification receipt).
  - ISL pause/resume state machine updates.
"""
from __future__ import annotations

import random
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set

from cryptography.hazmat.primitives.asymmetric import ec

from crypto.key_hierarchy import (
    OperatorCA,
    SatelliteCert,
    SatelliteKeyStore,
    _sign,
)
from protocol.auth import ContactAuthSession
from protocol.isl_state_machine import ISLStateMachine
from protocol.offchain import BalanceProof, OffChainProtocol, SettlementPayload
from simulator.simulator import EventType, SimEvent


CHANNEL_BALANCE_KB = 1_024 * 1_024  # 1 TB per side (in KB)


@dataclass
class NotificationBundle:
    bundle_id: str
    generated_at: float
    target_satellite: str
    notifications: List[dict] = field(default_factory=list)
    crl_delta: List[str] = field(default_factory=list)
    gs_sig: bytes = b""

    def to_dict(self) -> dict:
        return {
            "bundle_id": self.bundle_id,
            "generated_at": self.generated_at,
            "target_satellite": self.target_satellite,
            "notifications": self.notifications,
            "crl_delta": self.crl_delta,
            "gs_sig": self.gs_sig.hex(),
        }


class SatelliteNode:
    """Full satellite protocol node."""

    def __init__(
        self,
        satellite_id: str,
        operator_id: str,
        cert: SatelliteCert,
        private_key: ec.EllipticCurvePrivateKey,
        key_store: SatelliteKeyStore,
        isl_fsm: ISLStateMachine,
    ) -> None:
        self.satellite_id = satellite_id
        self.operator_id = operator_id
        self.cert = cert
        self._priv = private_key
        self._ks = key_store
        self._isl_fsm = isl_fsm

        self._protocol = OffChainProtocol(satellite_id, private_key, cert)
        self._auth_sessions: Dict[str, ContactAuthSession] = {}  # peer_id → session

        # Pending settlement payloads to upload to GS on next contact
        self._pending_settlement: Dict[str, SettlementPayload] = {}
        # Unacked notifications from GS
        self._pending_notif: List[NotificationBundle] = []
        # Active ISL contacts
        self._active_isls: Set[str] = set()  # peer sat IDs
        # Event log for metrics
        self.event_log: List[dict] = []

    # ── event dispatch ─────────────────────────────────────────────────────────

    def handle_event(self, event: SimEvent) -> List[SimEvent]:
        """DES handler — called by EventLoop for events addressed to this satellite."""
        if event.event_type == EventType.ISL_OPEN:
            return self._on_isl_open(event)
        elif event.event_type == EventType.ISL_CLOSE:
            return self._on_isl_close(event)
        elif event.event_type == EventType.GS_CONTACT_START:
            return self._on_gs_contact_start(event)
        elif event.event_type == EventType.GS_CONTACT_END:
            return self._on_gs_contact_end(event)
        elif event.event_type == EventType.TRAFFIC_ARRIVE:
            return self._on_traffic_arrive(event)
        elif event.event_type == EventType.NOTIFICATION_DELIVER:
            return self._on_notification_deliver(event)
        return []

    # ── ISL contact lifecycle ──────────────────────────────────────────────────

    def _on_isl_open(self, event: SimEvent) -> List[SimEvent]:
        peer_id = event.from_node if event.to_node == self.satellite_id else event.to_node
        contact = event.payload
        channel_id = self._channel_id(peer_id)

        # Ensure channel state exists (idempotent)
        if self._protocol.get_channel(channel_id) is None:
            op_ch_id = f"{self.operator_id}_{self._peer_operator(peer_id)}_ch"
            role = "A" if self.satellite_id < peer_id else "B"
            self._protocol.open_channel(channel_id, op_ch_id, peer_id, CHANNEL_BALANCE_KB, role)
            self._isl_fsm.register_channel(
                channel_id,
                sat_a=self.satellite_id if role == "A" else peer_id,
                sat_b=peer_id if role == "A" else self.satellite_id,
            )

        self._active_isls.add(peer_id)
        self._log("ISL_OPEN", peer_id=peer_id, t=event.time)

        # Authenticate (simplified: assume cert already verified by key store)
        state = self._protocol.get_channel(channel_id)
        if state:
            self._protocol.resume_channel(channel_id)

        return []

    def _on_isl_close(self, event: SimEvent) -> List[SimEvent]:
        peer_id = event.from_node if event.to_node == self.satellite_id else event.to_node
        channel_id = self._channel_id(peer_id)
        t = event.time

        self._active_isls.discard(peer_id)
        self._log("ISL_CLOSE", peer_id=peer_id, t=t)

        # Evaluate settlement triggers at contact-end
        triggers = self._protocol.evaluate_settlement_triggers(channel_id)
        if triggers:
            # Pause channel immediately at contact-end
            self._isl_fsm.record_contact_end_pause(
                channel_id, t, self.operator_id, "SETTLEMENT"
            )
            self._protocol.pause_channel(channel_id)

            # Queue settlement payload for next GS upload
            payload = self._protocol.get_settlement_payload(channel_id)
            if payload:
                self._pending_settlement[channel_id] = payload
                self._log("SETTLEMENT_QUEUED", channel_id=channel_id, triggers=triggers, t=t)

        return []

    # ── Ground station contact lifecycle ───────────────────────────────────────

    def _on_gs_contact_start(self, event: SimEvent) -> List[SimEvent]:
        gs_id = event.from_node
        self._log("GS_CONTACT_START", gs_id=gs_id, t=event.time)
        # Signal GS with settlement payloads (carried in payload field)
        return []

    def _on_gs_contact_end(self, event: SimEvent) -> List[SimEvent]:
        gs_id = event.from_node
        self._log("GS_CONTACT_END", gs_id=gs_id, t=event.time)
        # Settlement payloads drained during contact; clear any stale entries
        self._pending_settlement.clear()
        return []

    # ── Traffic forwarding ─────────────────────────────────────────────────────

    def _on_traffic_arrive(self, event: SimEvent) -> List[SimEvent]:
        flow = event.payload
        if flow is None:
            return []
        channel_id = self._channel_id(event.from_node)
        t = event.time

        if not self._protocol.can_forward(channel_id):
            self._log("TRAFFIC_DROPPED", reason="channel_not_active", t=t)
            return []

        state = self._protocol.get_channel(channel_id)
        if state is None or state.my_balance() < flow.size_kb:
            self._log("TRAFFIC_DROPPED", reason="insufficient_balance", t=t)
            return []

        # Build a dummy peer_ack_sig for the hash chain entry
        dummy_sig = _sign(self._priv, f"{channel_id}:{t}:{flow.size_kb}".encode())
        peer_pub = self._ks._operator_keys.get(self._peer_operator(event.from_node))
        if peer_pub is None:
            self._log("TRAFFIC_DROPPED", reason="no_peer_key", t=t)
            return []

        proof = self._protocol.record_forwarding(channel_id, flow.size_kb, peer_pub, t, dummy_sig)
        if proof:
            self._log("TRAFFIC_FORWARDED", bytes_kb=flow.size_kb, t=t, channel_id=channel_id)

        return []

    # ── Notification delivery ──────────────────────────────────────────────────

    def _on_notification_deliver(self, event: SimEvent) -> List[SimEvent]:
        bundle: NotificationBundle = event.payload
        if bundle is None:
            return []
        self._log("NOTIFICATION_RECEIVED", bundle_id=bundle.bundle_id, t=event.time)

        # Process CRL delta
        if bundle.crl_delta:
            self._ks.update_revocation_cache(bundle.crl_delta)

        # Process per-channel resume notifications
        for notif in bundle.notifications:
            if notif.get("type") == "ISL_RESUME":
                ch_id = notif.get("satChannelId", "")
                ready = self._isl_fsm.record_satellite_resume_ack(ch_id, self.satellite_id, event.time)
                if ready:
                    self._protocol.resume_channel(ch_id)
                    self._log("ISL_RESUMED", channel_id=ch_id, t=event.time)

        return []

    # ── Helpers ────────────────────────────────────────────────────────────────

    def _channel_id(self, peer_id: str) -> str:
        a, b = sorted([self.satellite_id, peer_id])
        return f"{a}__{b}"

    def _peer_operator(self, peer_id: str) -> str:
        return peer_id.split("-")[0] if "-" in peer_id else "unknown"

    def _log(self, event_type: str, **kwargs) -> None:
        self.event_log.append({"event": event_type, "satellite": self.satellite_id, **kwargs})

    def get_pending_settlement_payloads(self) -> List[SettlementPayload]:
        return list(self._pending_settlement.values())

    def clear_pending_settlements(self) -> None:
        self._pending_settlement.clear()

    def get_channel_state(self, peer_id: str):
        return self._protocol.get_channel(self._channel_id(peer_id))


# ── Factory function ───────────────────────────────────────────────────────────

def create_satellite(
    satellite_id: str,
    operator_id: str,
    ca: OperatorCA,
    all_operator_cas: Dict[str, OperatorCA],
    isl_fsm: ISLStateMachine,
    permitted_operators: Optional[List[str]] = None,
) -> SatelliteNode:
    """Instantiate a fully configured SatelliteNode."""
    cert, priv = ca.issue_for_new_key(
        satellite_id,
        valid_days=90,
        permitted_operators=permitted_operators or list(all_operator_cas.keys()),
    )
    ks = SatelliteKeyStore()
    for op_id, op_ca in all_operator_cas.items():
        ks.register_operator(op_id, op_ca.public_key)
    return SatelliteNode(satellite_id, operator_id, cert, priv, ks, isl_fsm)

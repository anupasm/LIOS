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
from config import cfg
from protocol.auth import ContactAuthSession
from protocol.isl_state_machine import ISLStateMachine
from protocol.offchain import BalanceProof, OffChainProtocol, SettlementPayload
from simulator.simulator import EventType, SimEvent


CHANNEL_BALANCE_KB = cfg.protocol.channel_balance_kb  # 5 GB per side; see config.toml


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
        # Settlement payloads waiting for the peer's PROOF_ACK before being queued.
        self._pending_cosign: Dict[str, SettlementPayload] = {}

        # Pending settlement payloads to upload to GS on next contact
        self._pending_settlement: Dict[str, SettlementPayload] = {}
        # ISL metadata stashed at trigger-fire time, consumed by
        # _queue_and_maybe_upload to measure the complete proof-signing exchange.
        self._settlement_meta: Dict[str, dict] = {}
        # Unacked notifications from GS
        self._pending_notif: List[NotificationBundle] = []
        # Active ISL contacts: peer_id → contact_id
        self._active_isls: Dict[str, str] = {}
        # Active ISL geometry used to schedule per-update proof exchanges.
        self._active_isl_info: Dict[str, dict] = {}
        # Active GS contacts: gs_id → {start_time, range_km, prop_delay_sec}
        # prop_delay_sec = range_km / c  (one-way propagation, Bhattacherjee & Singla CoNEXT 2019 §3.1)
        self._active_gs: Dict[str, dict] = {}
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
        elif event.event_type == EventType.TRAFFIC_RETRY:
            return self._on_traffic_arrive(event)
        elif event.event_type == EventType.NOTIFICATION_DELIVER:
            return self._on_notification_deliver(event)
        elif event.event_type == EventType.PROOF_PROP:
            return self._on_proof_prop(event)
        elif event.event_type == EventType.PROOF_ACK:
            return self._on_proof_ack(event)
        return []

    # ── ISL contact lifecycle ──────────────────────────────────────────────────

    def _on_isl_open(self, event: SimEvent) -> List[SimEvent]:
        peer_id = event.from_node if event.to_node == self.satellite_id else event.to_node
        contact = event.payload
        contact_id = contact.contact_id if contact else ""
        self._active_isls[peer_id] = contact_id

        range_km = getattr(contact, "range_km", 0.0) if contact else 0.0
        isl_prop_delay_sec = range_km / cfg.link.c_km_s if range_km > 0 else 0.0
        self._active_isl_info[peer_id] = {
            "range_km": range_km,
            "prop_delay_sec": isl_prop_delay_sec,
        }
        self._log("ISL_OPEN", peer_id=peer_id, contact_id=contact_id, t=event.time,
                  isl_range_km=round(range_km, 3),
                  isl_prop_delay_sec=round(isl_prop_delay_sec, 6))

        # No settlement channel between satellites of the same operator.
        if self._is_same_operator(peer_id):
            return []

        channel_id = self._channel_id(peer_id)
        if self._protocol.get_channel(channel_id) is None:
            op_a, op_b = sorted([self.operator_id, self._peer_operator(peer_id)])
            op_ch_id = f"{op_a}_{op_b}_ch"
            role = "A" if self.satellite_id < peer_id else "B"
            self._protocol.open_channel(channel_id, op_ch_id, peer_id, CHANNEL_BALANCE_KB, role)
            self._isl_fsm.register_channel(
                channel_id,
                sat_a=self.satellite_id if role == "A" else peer_id,
                sat_b=peer_id if role == "A" else self.satellite_id,
            )

        state = self._protocol.get_channel(channel_id)
        if state and state.status != "PAUSED":
            self._protocol.resume_channel(channel_id)

        return []

    def _on_isl_close(self, event: SimEvent) -> List[SimEvent]:
        peer_id = event.from_node if event.to_node == self.satellite_id else event.to_node
        t = event.time

        contact_id = self._active_isls.pop(peer_id, "")
        self._active_isl_info.pop(peer_id, None)

        contact = event.payload
        range_km = getattr(contact, "range_km", 0.0) if contact else 0.0
        isl_prop_delay_sec = range_km / cfg.link.c_km_s if range_km > 0 else 0.0
        self._log("ISL_CLOSE", peer_id=peer_id, contact_id=contact_id, t=t,
                  isl_range_km=round(range_km, 3),
                  isl_prop_delay_sec=round(isl_prop_delay_sec, 6))

        # No settlement between same-operator satellites.
        if self._is_same_operator(peer_id):
            return []

        channel_id = self._channel_id(peer_id)

        # Evaluate settlement triggers at contact-end; skip if channel already paused.
        ch_state = self._protocol.get_channel(channel_id)
        triggers = self._protocol.evaluate_settlement_triggers(channel_id)
        if triggers and (ch_state is None or ch_state.status != "PAUSED"):
            # Pause both satellites immediately at contact-end (traffic stops).
            self._isl_fsm.record_contact_end_pause(
                channel_id, t, self.operator_id, "SETTLEMENT"
            )
            self._protocol.pause_channel(channel_id)

            payload = self._protocol.get_settlement_payload(channel_id)
            if payload:
                payload.contact_id = contact_id
                payload.queued_at = t
                proof = payload.latest_proof
                ch = self._protocol.get_channel(channel_id)
                self._log(
                    "SETTLEMENT_TRIGGERED",
                    channel_id=channel_id,
                    contact_id=contact_id,
                    triggers=triggers,
                    seq_num=ch.seq_num if ch else None,
                    balance_a_kb=ch.balance_a_kb if ch else None,
                    balance_b_kb=ch.balance_b_kb if ch else None,
                    isl_range_km=round(range_km, 3),
                    t=t,
                )
                # Stash ISL geometry for SETTLEMENT_QUEUED log emitted in _queue_and_maybe_upload
                self._settlement_meta[channel_id] = {
                    "isl_prop_delay_sec": isl_prop_delay_sec,
                    "isl_range_km": range_km,
                    "proof_exchange_started_at": t,
                }

                if proof and proof.sig_a and proof.sig_b:
                    # Already co-signed — queue immediately.
                    return self._queue_and_maybe_upload(channel_id, payload, t)

                # Not yet co-signed: send PROOF_PROP to peer so it can cosign.
                # The peer returns PROOF_ACK (also over the ISL) and we queue
                # the settlement payload once both signatures are present.
                self._pending_cosign[channel_id] = payload
                self._log(
                    "PROOF_PROP_SENT",
                    channel_id=channel_id,
                    seq_num=proof.seq_num,
                    peer_id=peer_id,
                    t=t,
                )
                return [SimEvent(
                    time=t + isl_prop_delay_sec,
                    event_type=EventType.PROOF_PROP,
                    from_node=self.satellite_id,
                    to_node=peer_id,
                    payload={
                        "channel_id": channel_id,
                        "proof": proof,
                        "sender_pub_key": self._priv.public_key(),
                        "isl_prop_delay_sec": isl_prop_delay_sec,
                        "triggers": triggers,  # forwarded so peer labels its payload correctly
                    },
                )]

        return []

    # ── Ground station contact lifecycle ───────────────────────────────────────

    def _on_gs_contact_start(self, event: SimEvent) -> List[SimEvent]:
        gs_id = event.from_node
        t = event.time
        contact = event.payload
        range_km = getattr(contact, "range_km", 0.0) if contact else 0.0
        # One-way propagation delay: d/c (Bhattacherjee & Singla, CoNEXT 2019 §3.1)
        prop_delay_sec = range_km / cfg.link.c_km_s if range_km > 0 else 0.0
        self._active_gs[gs_id] = {
            "start_time": t,
            "range_km": range_km,
            "prop_delay_sec": prop_delay_sec,
        }
        self._log("GS_CONTACT_START", gs_id=gs_id, t=t,
                  gs_range_km=round(range_km, 3),
                  gs_prop_delay_sec=round(prop_delay_sec, 6))
        events = self._upload_pending_settlements(gs_id, t + prop_delay_sec)
        self._pending_settlement.clear()
        return events

    def _on_gs_contact_end(self, event: SimEvent) -> List[SimEvent]:
        gs_id = event.from_node
        t = event.time
        gs_info = self._active_gs.get(gs_id, {})
        prop_delay_sec = gs_info.get("prop_delay_sec", 0.0)
        self._log("GS_CONTACT_END", gs_id=gs_id, t=t)
        # Final sweep: upload anything queued during this contact window.
        new_events = self._upload_pending_settlements(gs_id, t + prop_delay_sec)
        self._active_gs.pop(gs_id, None)
        self._pending_settlement.clear()
        return new_events

    def _upload_pending_settlements(self, gs_id: str, t: float) -> List[SimEvent]:
        # Route through get_pending_settlement_payloads so subclass overrides
        # (e.g. MaliciousSatelliteNode rollback substitution) are respected.
        return [
            SimEvent(
                time=t,
                event_type=EventType.SETTLEMENT_UPLOAD,
                from_node=self.satellite_id,
                to_node=gs_id,
                payload=payload,
            )
            for payload in self.get_pending_settlement_payloads()
        ]

    # ── Traffic forwarding ─────────────────────────────────────────────────────

    def _on_traffic_arrive(self, event: SimEvent) -> List[SimEvent]:
        flow = event.payload
        if flow is None:
            return []
        t = event.time

        if flow.src_satellite != self.satellite_id:
            self._log(
                "TRAFFIC_DROPPED",
                reason="wrong_source_satellite",
                flow_id=flow.flow_id,
                t=t,
            )
            return []

        next_hop = flow.dst_satellite
        channel_id = self._channel_id(next_hop)

        if next_hop not in self._active_isls:
            self._log(
                "TRAFFIC_DROPPED",
                reason="no_active_isl_contact",
                flow_id=flow.flow_id,
                contact_id=flow.contact_id,
                channel_id=channel_id,
                next_hop=next_hop,
                t=t,
            )
            return []

        # Same-operator hops have no settlement channel — forward freely.
        if self._is_same_operator(next_hop):
            self._log(
                "TRAFFIC_FORWARDED",
                flow_id=flow.flow_id,
                peer_id=next_hop,
                contact_id=flow.contact_id,
                bytes_kb=flow.size_kb,
                t=t,
                channel_id=channel_id,
            )
            return []

        isl_delay = self._active_isl_info.get(next_hop, {}).get(
            "prop_delay_sec", 0.0
        )
        busy_until = self._isl_fsm.proof_exchange_busy_until(channel_id)
        if t < busy_until:
            self._log(
                "TRAFFIC_DEFERRED",
                reason="proof_exchange_in_flight",
                flow_id=flow.flow_id,
                channel_id=channel_id,
                retry_at=busy_until + 1e-6,
                t=t,
            )
            return [SimEvent(
                time=busy_until + 1e-6,
                event_type=EventType.TRAFFIC_RETRY,
                from_node=self.satellite_id,
                to_node=self.satellite_id,
                payload=flow,
            )]

        if not self._protocol.can_forward(channel_id):
            self._log("TRAFFIC_DROPPED", reason="channel_not_active",
                      flow_id=flow.flow_id, contact_id=flow.contact_id,
                      channel_id=channel_id, next_hop=next_hop, t=t)
            return []

        state = self._protocol.get_channel(channel_id)
        if state is None or state.my_balance() < flow.size_kb:
            self._log("TRAFFIC_DROPPED", reason="insufficient_balance",
                      flow_id=flow.flow_id, contact_id=flow.contact_id,
                      channel_id=channel_id, t=t)
            return []

        dummy_sig = _sign(self._priv, f"{channel_id}:{t}:{flow.size_kb}".encode())
        peer_pub = self._ks._operator_keys.get(self._peer_operator(next_hop))
        if peer_pub is None:
            self._log("TRAFFIC_DROPPED", reason="no_peer_key",
                      flow_id=flow.flow_id, contact_id=flow.contact_id,
                      channel_id=channel_id, next_hop=next_hop, t=t)
            return []

        contact_id = self._active_isls.get(next_hop, "")
        proof = self._protocol.record_forwarding(channel_id, flow.size_kb, peer_pub, t, dummy_sig)
        if proof:
            self._log(
                "TRAFFIC_FORWARDED",
                flow_id=flow.flow_id,
                peer_id=next_hop,
                contact_id=flow.contact_id,
                bytes_kb=flow.size_kb,
                t=t,
                channel_id=channel_id,
            )
            self._log(
                "OFFCHAIN_PROOF_UPDATE",
                channel_id=channel_id,
                contact_id=contact_id,
                seq_num=proof.seq_num,
                balance_a_kb=proof.balance_a_kb,
                balance_b_kb=proof.balance_b_kb,
                bytes_kb=flow.size_kb,
                t=t,
            )
            self._isl_fsm.reserve_proof_exchange(
                channel_id, t + 2.0 * isl_delay
            )
            self._log(
                "PROOF_PROP_SENT",
                channel_id=channel_id,
                seq_num=proof.seq_num,
                peer_id=next_hop,
                exchange_type="balance_update",
                t=t,
            )
            return [SimEvent(
                time=t + isl_delay,
                event_type=EventType.PROOF_PROP,
                from_node=self.satellite_id,
                to_node=next_hop,
                payload={
                    "channel_id": channel_id,
                    "proof": proof,
                    "sender_pub_key": self._priv.public_key(),
                    "isl_prop_delay_sec": isl_delay,
                    "balance_update": True,
                },
            )]

        return []

    # ── PROOF_PROP / PROOF_ACK (ISL cosign exchange) ──────────────────────────

    def _on_proof_prop(self, event: SimEvent) -> List[SimEvent]:
        """Peer sent its half-signed proof for us to cosign (COMMIT phase PROOF_PROP).

        Cosigns and returns PROOF_ACK.  After cosigning, re-evaluates settlement
        triggers because the adopted state may differ from what was visible at
        ISL_CLOSE (e.g. peer's balance was depleted → T1 now fires for us too).
        """
        data = event.payload or {}
        channel_id = data.get("channel_id", "")
        proof = data.get("proof")
        sender_pub_key = data.get("sender_pub_key")
        isl_delay = data.get("isl_prop_delay_sec", 0.0)
        peer_triggers: List[str] = data.get("triggers", [])
        balance_update = bool(data.get("balance_update"))
        t = event.time

        if proof is None or sender_pub_key is None:
            return []

        cosigned = self._protocol.cosign_proof(channel_id, proof, sender_pub_key)
        if cosigned is None:
            self._log("PROOF_PROP_REJECTED", channel_id=channel_id, t=t)
            return []

        self._log("PROOF_PROP_COSIGNED", channel_id=channel_id,
                  seq_num=cosigned.seq_num, peer_id=event.from_node,
                  exchange_type="balance_update" if balance_update else "settlement",
                  t=t)
        self._log("PROOF_ACK_SENT", channel_id=channel_id,
                  seq_num=cosigned.seq_num, peer_id=event.from_node,
                  exchange_type="balance_update" if balance_update else "settlement",
                  t=t)

        new_events: List[SimEvent] = [SimEvent(
            time=t + isl_delay,
            event_type=EventType.PROOF_ACK,
            from_node=self.satellite_id,
            to_node=event.from_node,
            payload={"channel_id": channel_id, "proof": cosigned,
                     "balance_update": balance_update},
        )]

        if balance_update:
            return new_events

        # PROOF_PROP is only sent when the peer triggered settlement at contact-end.
        # Always cooperate: pause our channel and submit the co-signed proof to our GS
        # so that both GS submit the same seq_num → MUTUAL_FINALIZED (no Tch wait).
        # Also catches T1/T7 conditions that became visible from the peer's proof state.
        ch_state = self._protocol.get_channel(channel_id)
        if ch_state and ch_state.status != "PAUSED":
            self._isl_fsm.record_contact_end_pause(
                channel_id, t, self.operator_id, "SETTLEMENT"
            )
            self._protocol.pause_channel(channel_id)
            payload = self._protocol.get_settlement_payload(channel_id)
            if payload:
                payload.latest_proof = cosigned  # use the fully co-signed proof
                # Use peer's triggers so GS log shows the actual settlement cause.
                payload.triggers_fired = peer_triggers or payload.triggers_fired
                payload.queued_at = t
                new_events += self._queue_and_maybe_upload(channel_id, payload, t)

        return new_events

    def _on_proof_ack(self, event: SimEvent) -> List[SimEvent]:
        """Peer returned the co-signed proof (COMMIT phase PROOF_ACK).

        Adopts the co-signed proof as our latest_proof and queues the pending
        settlement payload that was waiting on this cosign.
        """
        data = event.payload or {}
        channel_id = data.get("channel_id", "")
        cosigned = data.get("proof")
        balance_update = bool(data.get("balance_update"))
        t = event.time

        if cosigned is None:
            return []

        ch_state = self._protocol.get_channel(channel_id)
        if ch_state and ch_state.latest_proof and \
                ch_state.latest_proof.seq_num == cosigned.seq_num:
            ch_state.latest_proof = cosigned

        self._log("PROOF_ACK_RECEIVED", channel_id=channel_id,
                  seq_num=cosigned.seq_num,
                  exchange_type="balance_update" if balance_update else "settlement",
                  t=t)
        if balance_update:
            return []

        payload = self._pending_cosign.pop(channel_id, None)
        if payload is None:
            return []

        payload.latest_proof = cosigned
        return self._queue_and_maybe_upload(channel_id, payload, t)

    def _queue_and_maybe_upload(
        self, channel_id: str, payload: "SettlementPayload", t: float
    ) -> List[SimEvent]:
        """Store payload and flush to GS immediately if a contact is already open."""
        meta = self._settlement_meta.pop(channel_id, {})
        # Only emit on the first call per settlement cycle (meta present).
        # _on_proof_prop and _on_proof_ack can both call this method; only the
        # initiating node owns metadata for the complete proposal/ACK exchange.
        if meta:
            exchange_started_at = meta.get("proof_exchange_started_at", t)
            self._log(
                "SETTLEMENT_QUEUED",
                channel_id=channel_id,
                seq_num=payload.latest_proof.seq_num,
                isl_prop_delay_sec=round(meta["isl_prop_delay_sec"], 6),
                offchain_latency_sec=round(t - exchange_started_at, 6),
                isl_range_km=round(meta["isl_range_km"], 3),
                t=t,
            )
        self._pending_settlement[channel_id] = payload
        if self._active_gs:
            gs_id = next(iter(self._active_gs))
            gs_delay = self._active_gs[gs_id].get("prop_delay_sec", 0.0)
            events = self._upload_pending_settlements(gs_id, t + gs_delay)
            self._pending_settlement.clear()
            return events
        return []

    # ── Notification delivery ──────────────────────────────────────────────────

    def _on_notification_deliver(self, event: SimEvent) -> List[SimEvent]:
        bundle: NotificationBundle = event.payload
        if bundle is None:
            return []
        # downlink_prop_delay_sec = event.time - bundle.generated_at (GS sent → satellite received)
        downlink_prop_delay_sec = round(event.time - bundle.generated_at, 6)
        self._log("NOTIFICATION_RECEIVED", bundle_id=bundle.bundle_id, t=event.time,
                  gs_sent_at=bundle.generated_at,
                  downlink_prop_delay_sec=downlink_prop_delay_sec)

        # Process CRL delta
        if bundle.crl_delta:
            self._ks.update_revocation_cache(bundle.crl_delta)

        # Process per-channel resume notifications
        for notif in bundle.notifications:
            if notif.get("type") == "ISL_RESUME":
                ch_id = notif.get("satChannelId", "")
                # Proof is co-signed — mutual consent already given.  Each satellite
                # resets its own channel state independently when its GS signals resume.
                self._isl_fsm.record_satellite_resume_ack(ch_id, self.satellite_id, event.time)
                self._protocol.resume_channel(ch_id)
                self._log("ISL_RESUMED", channel_id=ch_id, t=event.time,
                          seq_num=self._protocol.get_channel(ch_id).seq_num,
                          gs_sent_at=bundle.generated_at,
                          downlink_prop_delay_sec=downlink_prop_delay_sec)

        return []

    # ── Helpers ────────────────────────────────────────────────────────────────

    def _channel_id(self, peer_id: str) -> str:
        a, b = sorted([self.satellite_id, peer_id])
        return f"{a}__{b}"

    def _peer_operator(self, peer_id: str) -> str:
        return peer_id.split("-")[0] if "-" in peer_id else "unknown"

    def _is_same_operator(self, peer_id: str) -> bool:
        return self.operator_id == self._peer_operator(peer_id)

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
    node_class: Optional[type] = None,
) -> SatelliteNode:
    """Instantiate a fully configured SatelliteNode (or subclass).

    Pass node_class to create a baseline-protocol variant, e.g.
    GreedySatelliteNode, TitForTatNode, or CentralSatelliteNode.
    """
    cert, priv = ca.issue_for_new_key(
        satellite_id,
        valid_days=90,
        permitted_operators=permitted_operators or list(all_operator_cas.keys()),
    )
    ks = SatelliteKeyStore()
    for op_id, op_ca in all_operator_cas.items():
        ks.register_operator(op_id, op_ca.public_key)
    cls = node_class if node_class is not None else SatelliteNode
    return cls(satellite_id, operator_id, cert, priv, ks, isl_fsm)

"""Ground station node for the LIOS DES emulator.

Responsibilities:
  - Receive settlement TX logs from satellites during ground contacts.
  - Submit/monitor settlement transactions on Hyperledger Fabric (mocked in simulation).
  - Relay settlement notifications and CRL updates to satellites on next contact.
  - Subscribe to Fabric chaincode events.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from contact_plan.gs_loader import GroundStation
from protocol.isl_state_machine import ISLStateMachine
from protocol.offchain import BalanceProof, SettlementPayload
from simulator.satellite_node import NotificationBundle, SatelliteNode
from simulator.simulator import EventType, SimEvent


@dataclass
class FabricSubmission:
    """Records a Fabric transaction for audit trail."""
    tx_id: str
    fn_name: str
    payload: dict
    submitted_at: float
    status: str = "PENDING"  # PENDING | CONFIRMED | FAILED


class FabricMock:
    """In-process Fabric mock for simulation.

    In production this would be replaced with a real Fabric SDK client.
    """

    def __init__(self) -> None:
        self._channels: Dict[str, dict] = {}       # on-chain channel state
        self._settlements: Dict[str, dict] = {}    # pending settlements
        self._notifications: List[dict] = []
        self._tx_log: List[FabricSubmission] = []

    def open_operator_channel(self, op_a: str, op_b: str, bal_a: float, bal_b: float, reserve: float) -> str:
        ch_id = f"{op_a}_{op_b}_ch"
        self._channels[ch_id] = {
            "operatorA": op_a,
            "operatorB": op_b,
            "balanceA": bal_a,
            "balanceB": bal_b,
            "penaltyReserve": reserve,
            "status": "OPEN",
        }
        return ch_id

    def initiate_settlement(self, sat_channel_id: str, proof: BalanceProof, submitted_by: str, t: float) -> str:
        tx_id = str(uuid.uuid4())
        self._settlements[sat_channel_id] = {
            "proof": proof.to_dict(),
            "submittedBy": submitted_by,
            "submittedAt": t,
            "status": "PENDING_CHALLENGE",
            "tx_id": tx_id,
        }
        self._push_notification("SETTLEMENT_INITIATED", {"satChannelId": sat_channel_id}, t)
        return tx_id

    def challenge_settlement(self, sat_channel_id: str, counter_proof: BalanceProof, t: float) -> bool:
        rec = self._settlements.get(sat_channel_id)
        if rec is None:
            return False
        submitted_seq = rec["proof"]["seq_num"]
        if counter_proof.seq_num > submitted_seq:
            rec["proof"] = counter_proof.to_dict()
            rec["status"] = "CHALLENGED"
            self._push_notification("SETTLEMENT_CHALLENGED", {"satChannelId": sat_channel_id}, t)
            return True
        return False

    def finalize_settlement(self, sat_channel_id: str, t: float) -> bool:
        rec = self._settlements.get(sat_channel_id)
        if rec is None:
            return False
        rec["status"] = "FINALIZED"
        self._push_notification("SETTLEMENT_FINALIZED", {"satChannelId": sat_channel_id}, t)
        return True

    def get_pending_notifications(self, operator_id: str) -> List[dict]:
        return [n for n in self._notifications if not n.get("acknowledged")]

    def acknowledge_notification(self, notif_id: str) -> None:
        for n in self._notifications:
            if n["id"] == notif_id:
                n["acknowledged"] = True

    def get_peer_balance_proofs(self, satellite_id: str) -> List[dict]:
        """Return latest finalized balance proofs for channels involving satellite_id."""
        proofs = []
        for ch_id, rec in self._settlements.items():
            if satellite_id in ch_id and rec["status"] == "FINALIZED":
                proofs.append({"channel_id": ch_id, "proof": rec["proof"]})
        return proofs

    def _push_notification(self, notif_type: str, payload: dict, t: float) -> None:
        self._notifications.append({
            "id": str(uuid.uuid4()),
            "type": notif_type,
            "payload": payload,
            "created_at": t,
            "acknowledged": False,
        })


class GroundStationNode:
    """Ground station that manages settlement and satellite notification."""

    def __init__(
        self,
        gs: GroundStation,
        fabric: FabricMock,
        isl_fsm: ISLStateMachine,
        t_challenge_sec: float = 172_800.0,  # 48 hours
    ) -> None:
        self.gs = gs
        self.gs_id = gs.gs_id
        self.operator_id = gs.operator_id
        self._fabric = fabric
        self._isl_fsm = isl_fsm
        self._t_challenge = t_challenge_sec

        self._pending_to_satellites: Dict[str, List[dict]] = {}  # sat_id → notifications
        self._crl_delta: List[str] = []
        self._challenge_timers: Dict[str, float] = {}  # sat_channel_id → deadline
        self.event_log: List[dict] = []

    # ── event dispatch ─────────────────────────────────────────────────────────

    def handle_event(self, event: SimEvent) -> List[SimEvent]:
        if event.event_type == EventType.GS_CONTACT_START:
            return self._on_satellite_contact_start(event)
        elif event.event_type == EventType.GS_CONTACT_END:
            return self._on_satellite_contact_end(event)
        elif event.event_type == EventType.CHALLENGE_WINDOW_EXPIRE:
            return self._on_challenge_expire(event)
        elif event.event_type == EventType.SETTLEMENT_TRIGGER:
            return self._on_settlement_trigger(event)
        elif event.event_type == EventType.KEY_REVOKED:
            return self._on_key_revoked(event)
        return []

    # ── satellite contact ──────────────────────────────────────────────────────

    def _on_satellite_contact_start(self, event: SimEvent) -> List[SimEvent]:
        sat_id = event.to_node
        t = event.time
        self._log("GS_CONTACT_START", sat_id=sat_id, t=t)

        new_events: List[SimEvent] = []

        # Deliver pending notifications to satellite
        pending = self._pending_to_satellites.pop(sat_id, [])
        if pending or self._crl_delta:
            bundle = NotificationBundle(
                bundle_id=str(uuid.uuid4()),
                generated_at=t,
                target_satellite=sat_id,
                notifications=pending,
                crl_delta=list(self._crl_delta),
            )
            new_events.append(SimEvent(
                time=t + 0.01,  # near-instant delivery during contact
                event_type=EventType.NOTIFICATION_DELIVER,
                from_node=self.gs_id,
                to_node=sat_id,
                payload=bundle,
            ))

        return new_events

    def _on_satellite_contact_end(self, event: SimEvent) -> List[SimEvent]:
        sat_id = event.to_node
        self._log("GS_CONTACT_END", sat_id=sat_id, t=event.time)
        return []

    # ── settlement ─────────────────────────────────────────────────────────────

    def receive_settlement_payload(
        self,
        sat_node: SatelliteNode,
        payload: SettlementPayload,
        t: float,
    ) -> List[SimEvent]:
        """Called when satellite uploads a settlement payload during GS contact."""
        ch_id = payload.channel_id
        self._log("SETTLEMENT_RECEIVED", channel_id=ch_id, triggers=payload.triggers_fired, t=t)

        tx_id = self._fabric.initiate_settlement(ch_id, payload.latest_proof, self.operator_id, t)
        challenge_deadline = t + self._t_challenge

        # Schedule challenge window expiry event
        expire_event = SimEvent(
            time=challenge_deadline,
            event_type=EventType.CHALLENGE_WINDOW_EXPIRE,
            from_node=self.gs_id,
            to_node=self.gs_id,
            payload={"sat_channel_id": ch_id, "tx_id": tx_id},
        )
        self._challenge_timers[ch_id] = challenge_deadline

        # Notify counterpart operator's GS (via Fabric event subscription — mocked)
        self._fabric._push_notification(
            "SETTLEMENT_INITIATED",
            {"satChannelId": ch_id, "seqNum": payload.latest_proof.seq_num},
            t,
        )
        return [expire_event]

    def _on_challenge_expire(self, event: SimEvent) -> List[SimEvent]:
        payload = event.payload or {}
        ch_id = payload.get("sat_channel_id", "")
        t = event.time
        self._log("CHALLENGE_EXPIRED", channel_id=ch_id, t=t)

        # Finalize settlement
        self._fabric.finalize_settlement(ch_id, t)
        self._isl_fsm.on_settlement_finalized(ch_id)

        # Queue resume notifications for both satellites
        self._queue_resume_notification(ch_id, t)
        return []

    def challenge_settlement(
        self,
        ch_id: str,
        counter_proof: BalanceProof,
        t: float,
    ) -> bool:
        """Submit a newer proof to challenge a pending settlement."""
        result = self._fabric.challenge_settlement(ch_id, counter_proof, t)
        if result:
            self._log("CHALLENGE_SUBMITTED", channel_id=ch_id, seq=counter_proof.seq_num, t=t)
        return result

    def _queue_resume_notification(self, ch_id: str, t: float) -> None:
        """Queue an ISL_RESUME notification for both satellites in the channel."""
        # Extract satellite IDs from channel_id convention: "satA__satB"
        parts = ch_id.split("__")
        if len(parts) == 2:
            for sat_id in parts:
                self._pending_to_satellites.setdefault(sat_id, []).append({
                    "type": "ISL_RESUME",
                    "satChannelId": ch_id,
                    "timestamp": t,
                })

    # ── key revocation ─────────────────────────────────────────────────────────

    def _on_key_revoked(self, event: SimEvent) -> List[SimEvent]:
        sat_id = event.payload.get("satellite_id", "")
        if sat_id:
            self._crl_delta.append(sat_id)
            self._log("KEY_REVOKED_PROPAGATED", sat_id=sat_id, t=event.time)
        return []

    def _on_settlement_trigger(self, event: SimEvent) -> List[SimEvent]:
        """Handle a settlement trigger relayed from satellite."""
        return []

    # ── Helpers ────────────────────────────────────────────────────────────────

    def _log(self, event_type: str, **kwargs) -> None:
        self.event_log.append({"event": event_type, "gs": self.gs_id, **kwargs})

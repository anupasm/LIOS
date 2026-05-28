"""Ground station settlement manager (§11, §13).

Orchestrates:
  - Receiving TX logs from satellites during ground contacts.
  - Evaluating settlement triggers.
  - Submitting initiateSettlement(), challengeSettlement(), finalizeSettlement() to Fabric.
  - Constructing and transmitting notification bundles to satellites.
  - Monitoring challenge windows and triggering finalization.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from contact_plan.gs_loader import GroundStation
from contact_plan.window_calculator import ContactPlan
from protocol.isl_state_machine import ISLStateMachine
from protocol.offchain import BalanceProof, SettlementPayload
from simulator.ground_station_node import FabricMock, GroundStationNode
from simulator.satellite_node import NotificationBundle, SatelliteNode
from simulator.simulator import EventType, SimEvent


T_CHALLENGE_SEC = 172_800.0     # 48 hours
T_TOPUP_CONFIRM_SEC = 86_400.0  # 24 hours
T_LOW_FRACTION = 0.05


@dataclass
class TopUpRecord:
    topup_id: str
    op_channel_id: str
    requested_by: str
    amount_a_kb: float
    amount_b_kb: float
    expires_at: float
    status: str = "PENDING"  # PENDING | CONFIRMED | EXPIRED


@dataclass
class ChainEvent:
    """A Fabric ledger event received by this GS."""
    event_type: str        # e.g. "SETTLEMENT_INITIATED"
    channel_id: str
    payload: dict = field(default_factory=dict)
    notif_id: str = ""


class SettlementManager:
    """Full settlement orchestration layer for one ground station."""

    def __init__(
        self,
        gs_node: GroundStationNode,
        fabric: FabricMock,
        isl_fsm: ISLStateMachine,
        contact_plan: Optional[ContactPlan] = None,
    ) -> None:
        self._gs = gs_node
        self._fabric = fabric
        self._isl_fsm = isl_fsm
        self._cp = contact_plan

        # channel_id → settlement payloads waiting to be submitted (no trigger yet)
        self._queued: Dict[str, SettlementPayload] = {}
        # channel_id → challenge deadline (sim time)
        self._challenge_deadlines: Dict[str, float] = {}
        # topup_id → record
        self._topups: Dict[str, TopUpRecord] = {}
        # CRL satellites to push to satellites on next contact
        self._pending_crl: List[str] = []
        self.event_log: List[dict] = []

    # ── satellite contact integration ──────────────────────────────────────────

    def on_satellite_contact(
        self,
        sat_node: SatelliteNode,
        t_contact: float,
    ) -> List[SimEvent]:
        """Called when a satellite establishes ground contact.

        Drains pending settlement payloads from the satellite and submits them.
        Also processes pending Fabric events and transmits notifications.
        """
        new_events: List[SimEvent] = []

        # 1. Receive TX logs from satellite
        for payload in sat_node.get_pending_settlement_payloads():
            new_events.extend(self._handle_payload(payload, t_contact))
        sat_node.clear_pending_settlements()

        # 2. Check and respond to any Fabric events (counterpart settlements, key revocations, etc.)
        new_events.extend(self._process_fabric_events(sat_node, t_contact))

        return new_events

    def _handle_payload(self, payload: SettlementPayload, t: float) -> List[SimEvent]:
        ch_id = payload.channel_id
        self._log("PAYLOAD_RECEIVED", channel_id=ch_id, triggers=payload.triggers_fired, t=t)

        if payload.triggers_fired:
            # Triggers fired: submit settlement immediately
            return self.initiate_settlement(ch_id, payload.latest_proof, t)
        else:
            # No trigger: record for later (may batch with future payload)
            self._queued[ch_id] = payload
            return []

    def initiate_settlement(
        self,
        channel_id: str,
        proof: BalanceProof,
        t: float,
    ) -> List[SimEvent]:
        """Submit settlement to Fabric and arm the T_challenge timer.

        Steps:
          1. Submit initiateSettlement() to Fabric.
          2. Set local T_challenge deadline.
          3. Record ISL pause in FSM.
        """
        tx_id = self._fabric.initiate_settlement(channel_id, proof, self._gs.operator_id, t)
        deadline = t + T_CHALLENGE_SEC
        self._challenge_deadlines[channel_id] = deadline

        # Pause ISL at contact-end — FSM guard ensures this is called at boundary
        self._isl_fsm.record_contact_end_pause(channel_id, t, self._gs.operator_id)

        self._log("SETTLEMENT_INITIATED", channel_id=channel_id, tx_id=tx_id, t=t)
        return [SimEvent(
            time=deadline,
            event_type=EventType.CHALLENGE_WINDOW_EXPIRE,
            from_node=self._gs.gs_id,
            to_node=self._gs.gs_id,
            payload={"sat_channel_id": channel_id, "tx_id": tx_id},
        )]

    # ── Fabric event handler ────────────────────────────────────────────────────

    def on_fabric_event(self, event: ChainEvent, sat_node: Optional[SatelliteNode], t: float) -> None:
        """Dispatch a Fabric ledger event.

        Routes to the correct handler based on event_type.
        """
        etype = event.event_type
        ch_id = event.channel_id

        if etype == "SETTLEMENT_INITIATED":
            self._on_settlement_initiated(event, sat_node, t)
        elif etype == "SETTLEMENT_FINALIZED":
            self._on_settlement_finalized(ch_id, sat_node, t)
        elif etype == "DISPUTE_RESOLVED":
            self._on_dispute_resolved(event, t)
        elif etype == "TOPUP_REQUESTED":
            self._on_topup_requested(event, t)
        elif etype == "KEY_REVOKED":
            self._on_key_revoked(event, t)

        if event.notif_id:
            self._fabric.acknowledge_notification(event.notif_id)

    def _on_settlement_initiated(
        self,
        event: ChainEvent,
        sat_node: Optional[SatelliteNode],
        t: float,
    ) -> None:
        """Counterpart initiated settlement — challenge if we have a newer proof."""
        ch_id = event.channel_id
        notif_seq = event.payload.get("seqNum", -1)
        if sat_node is None:
            return
        ch_state = sat_node._protocol.get_channel(ch_id)
        if ch_state and ch_state.latest_proof and ch_state.latest_proof.seq_num > notif_seq:
            self._fabric.challenge_settlement(ch_id, ch_state.latest_proof, t)
            self._log("CHALLENGE_SUBMITTED", channel_id=ch_id, t=t)

    def _on_settlement_finalized(
        self,
        ch_id: str,
        sat_node: Optional[SatelliteNode],
        t: float,
    ) -> None:
        """Settlement finalized — advance FSM and queue resume notification."""
        self._isl_fsm.on_settlement_finalized(ch_id)
        self._log("SETTLEMENT_FINALIZED", channel_id=ch_id, t=t)

    def _on_dispute_resolved(self, event: ChainEvent, t: float) -> None:
        self._log("DISPUTE_RESOLVED", channel_id=event.channel_id,
                  outcome=event.payload.get("outcome", ""), t=t)

    def _on_topup_requested(self, event: ChainEvent, t: float) -> None:
        """Evaluate top-up need; confirm if our operator is the counterpart."""
        ch_id = event.channel_id
        topup_id = event.payload.get("topUpId", "")
        requested_by = event.payload.get("requestedBy", "")
        # Only confirm if we are NOT the requestor
        if requested_by != self._gs.operator_id + "MSP":
            self._log("TOPUP_CONFIRM", topup_id=topup_id, t=t)
        else:
            self._log("TOPUP_SKIP_OWN_REQUEST", topup_id=topup_id, t=t)

    def _on_key_revoked(self, event: ChainEvent, t: float) -> None:
        """A satellite key was revoked — queue the satellite ID for CRL push."""
        sat_id = event.payload.get("satelliteId", "")
        if sat_id and sat_id not in self._pending_crl:
            self._pending_crl.append(sat_id)
        self._log("KEY_REVOKED_RECEIVED", satellite_id=sat_id, t=t)

    def _process_fabric_events(
        self,
        sat_node: SatelliteNode,
        t: float,
    ) -> List[SimEvent]:
        """Poll Fabric for pending notifications and dispatch as ChainEvents."""
        new_events: List[SimEvent] = []
        pending = self._fabric.get_pending_notifications(self._gs.operator_id)

        for notif in pending:
            if notif.get("acknowledged"):
                continue
            ch_event = ChainEvent(
                event_type=notif.get("type", ""),
                channel_id=notif.get("payload", {}).get("satChannelId", ""),
                payload=notif.get("payload", {}),
                notif_id=notif["id"],
            )
            self.on_fabric_event(ch_event, sat_node, t)

        return new_events

    # ── challenge window expiry ─────────────────────────────────────────────────

    def on_challenge_expire(self, ch_id: str, t: float) -> None:
        """Finalize settlement after T_challenge expires with no dispute."""
        self._fabric.finalize_settlement(ch_id, t)
        self._isl_fsm.on_settlement_finalized(ch_id)
        self._log("SETTLEMENT_FINALIZED", channel_id=ch_id, t=t)

    # ── top-up management ──────────────────────────────────────────────────────

    def request_topup(
        self,
        op_channel_id: str,
        amount_a_kb: float,
        amount_b_kb: float,
        t: float,
    ) -> TopUpRecord:
        record = TopUpRecord(
            topup_id=str(uuid.uuid4()),
            op_channel_id=op_channel_id,
            requested_by=self._gs.operator_id,
            amount_a_kb=amount_a_kb,
            amount_b_kb=amount_b_kb,
            expires_at=t + T_TOPUP_CONFIRM_SEC,
        )
        self._topups[record.topup_id] = record
        self._log("TOPUP_REQUESTED", topup_id=record.topup_id, t=t)
        return record

    def confirm_topup(self, topup_id: str, t: float) -> bool:
        record = self._topups.get(topup_id)
        if record is None or record.expires_at < t:
            if record:
                record.status = "EXPIRED"
            return False
        record.status = "CONFIRMED"
        self._log("TOPUP_CONFIRMED", topup_id=topup_id, t=t)
        return True

    def evaluate_topup_need(
        self,
        ch_id: str,
        balance_a_kb: float,
        balance_b_kb: float,
        initial_capacity_kb: float,
        t: float,
        peer_gs_node_id: Optional[str] = None,
    ) -> bool:
        """Return True only if balance is low AND both operators can meet within T_TOPUP_CONFIRM.

        Condition 1: balance below T_LOW threshold.
        Condition 2 (if contact_plan available): both operators have a GS contact
                    within the next T_TOPUP_CONFIRM seconds, so the confirmation
                    won't expire before the counterpart can ACK.
        """
        t_low = initial_capacity_kb * T_LOW_FRACTION
        if balance_a_kb >= t_low and balance_b_kb >= t_low:
            return False  # balance still healthy

        if self._cp is None or peer_gs_node_id is None:
            return True  # can't check window; assume topup is viable

        # Check that peer GS has a satellite contact within T_TOPUP_CONFIRM window
        peer_reachable = self._cp.get_reachable_satellites(
            peer_gs_node_id, t, t + T_TOPUP_CONFIRM_SEC
        )
        return bool(peer_reachable)

    # ── notification bundle construction ───────────────────────────────────────

    def transmit_notifications(
        self,
        sat_node: SatelliteNode,
        t: float,
    ) -> NotificationBundle:
        """Build and deliver a notification bundle to a satellite.

        Includes:
          1. Pending Fabric notifications for this operator.
          2. CRL deltas for revoked satellites.
          3. Latest peer BalProofs (so satellite can update channel state).
        Bundle is queued for ACK on the satellite's next ground contact.
        """
        return self.build_notification_bundle(
            target_sat_id=sat_node.satellite_id,
            t=t,
            crl_delta=list(self._pending_crl),
        )

    def build_notification_bundle(
        self,
        target_sat_id: str,
        t: float,
        crl_delta: Optional[List[str]] = None,
    ) -> NotificationBundle:
        """Construct a notification bundle for a satellite.

        Queries Fabric for unacknowledged notifications, adds CRL deltas and
        any latest peer BalProofs, then marks notifications as acknowledged.
        """
        pending = self._fabric.get_pending_notifications(self._gs.operator_id)
        notifications = [
            {"type": n["type"], **n["payload"]}
            for n in pending
            if not n.get("acknowledged")
        ]

        # Include latest peer BalProofs for all channels involving this satellite
        peer_proofs = self._fabric.get_peer_balance_proofs(target_sat_id)
        if peer_proofs:
            notifications.append({"type": "PEER_BALANCE_PROOFS", "proofs": peer_proofs})

        bundle = NotificationBundle(
            bundle_id=str(uuid.uuid4()),
            generated_at=t,
            target_satellite=target_sat_id,
            notifications=notifications,
            crl_delta=crl_delta or [],
        )
        for n in pending:
            self._fabric.acknowledge_notification(n["id"])

        self._pending_crl.clear()
        self._log("BUNDLE_SENT", target=target_sat_id, bundle_id=bundle.bundle_id, t=t)
        return bundle

    def _log(self, event_type: str, **kwargs) -> None:
        self.event_log.append({"event": event_type, "gs": self._gs.gs_id, **kwargs})

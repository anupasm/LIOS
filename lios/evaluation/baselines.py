"""Baseline protocol implementations for LIOS comparative evaluation.

Baselines, each a minimal SatelliteNode (or ground-node) subclass:

  GreedySatelliteNode      — no accounting, no settlement (anarchy lower bound).
  TitForTatNode            — per-contact forwarding cap, no cross-contact credit.
  CentralSatelliteNode     — settlement without ISL pause (trusted central authority).
  GroundResetSatelliteNode — contact-end ground reset; blocks until both sats report.
  CentralFabricMock        — accepts any proof and finalises immediately.
  CentralGroundStationNode — submits to Fabric without queuing ISL_RESUME.
  GroundResetFabricMock    — central ledger for the ground-reset baseline.
  GroundResetGroundStationNode — waits for both endpoint uploads before reset.

Usage in run_experiments.py: set ExperimentConfig.baseline_protocol to one of
  'greedy' | 't4t' | 'central' | 'ground_reset'
  (default 'lios' uses the normal SatelliteNode).
"""
from __future__ import annotations

from typing import Dict, List, Set, Tuple

from config import cfg
from crypto.key_hierarchy import _sign
from protocol.isl_state_machine import ISLChannelStatus
from protocol.offchain import BalanceProof, SettlementPayload
from simulator.ground_station_node import FabricMock, GroundStationNode
from simulator.satellite_node import SatelliteNode
from simulator.simulator import EventType, SimEvent


# ── A.1  No-Protocol Greedy ────────────────────────────────────────────────────

class GreedySatelliteNode(SatelliteNode):
    """Forward all cross-operator traffic with no balance accounting or settlement.

    Expected: Jain fairness degrades over time; free-riders are never penalised;
    OOS fraction = 0 % (ISL never paused); protocol overhead = 0.
    """

    def _on_isl_open(self, event: SimEvent) -> List[SimEvent]:
        peer_id = event.from_node if event.to_node == self.satellite_id else event.to_node
        contact = event.payload
        contact_id = contact.contact_id if contact else ""
        range_km = getattr(contact, "range_km", 0.0) if contact else 0.0
        self._active_isls[peer_id] = contact_id
        self._log("ISL_OPEN", peer_id=peer_id, contact_id=contact_id, t=event.time,
                  isl_range_km=round(range_km, 3), isl_prop_delay_sec=0.0)
        return []

    def _on_isl_close(self, event: SimEvent) -> List[SimEvent]:
        peer_id = event.from_node if event.to_node == self.satellite_id else event.to_node
        contact = event.payload
        contact_id = self._active_isls.pop(peer_id, "")
        range_km = getattr(contact, "range_km", 0.0) if contact else 0.0
        self._log("ISL_CLOSE", peer_id=peer_id, contact_id=contact_id, t=event.time,
                  isl_range_km=round(range_km, 3))
        return []

    def _on_traffic_arrive(self, event: SimEvent) -> List[SimEvent]:
        flow = event.payload
        if flow is None:
            return []
        t = event.time
        if flow.src_satellite != self.satellite_id:
            self._log("TRAFFIC_DROPPED", reason="wrong_source_satellite",
                      flow_id=flow.flow_id, t=t)
            return []

        next_hop = flow.dst_satellite
        if next_hop not in self._active_isls:
            self._log("TRAFFIC_DROPPED", reason="no_active_isl_contact",
                      flow_id=flow.flow_id, contact_id=flow.contact_id, t=t)
            return []

        # Forward freely — no payment channel, no balance deduction.
        channel_id = self._channel_id(next_hop)
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


# ── A.3  Tit-for-Tat ──────────────────────────────────────────────────────────

class TitForTatNode(SatelliteNode):
    """Per-contact reciprocation: choke forwarding once deficit_threshold_kb is
    reached in a single ISL contact window.  No settlement between contacts.

    The balance is reset to equal-split at every ISL open (via the parent's
    _on_isl_open → OffChainProtocol.resume_channel), giving each contact a fresh
    quota.  The per-contact byte counter is the sole T4T enforcement mechanism.

    Expected: fairness enforced within each contact but degrades over a 24 h
    horizon because no credit carries over; OOS = 0 % (no settlement pause).
    """

    def __init__(self, *args, deficit_threshold_kb: float = 500.0, **kwargs):
        super().__init__(*args, **kwargs)
        self.deficit_threshold_kb = deficit_threshold_kb
        # peer_id → KB forwarded to that peer in the current contact window
        self._contact_fwd: Dict[str, float] = {}

    def _on_isl_open(self, event: SimEvent) -> List[SimEvent]:
        peer_id = event.from_node if event.to_node == self.satellite_id else event.to_node
        self._contact_fwd[peer_id] = 0.0
        # Super: opens/resumes the payment channel (resets balance to equal-split)
        # and registers the channel in the ISL FSM.
        return super()._on_isl_open(event)

    def _on_traffic_arrive(self, event: SimEvent) -> List[SimEvent]:
        flow = event.payload
        if flow is None:
            return []
        if flow.src_satellite != self.satellite_id:
            self._log("TRAFFIC_DROPPED", reason="wrong_source_satellite",
                      flow_id=flow.flow_id, t=event.time)
            return []

        next_hop = flow.dst_satellite
        if not self._is_same_operator(next_hop):
            fwd = self._contact_fwd.get(next_hop, 0.0)
            if fwd + flow.size_kb > self.deficit_threshold_kb:
                self._log("TRAFFIC_DROPPED", reason="t4t_choke",
                          flow_id=flow.flow_id, contact_id=flow.contact_id,
                          fwd_kb=round(fwd, 2),
                          threshold_kb=self.deficit_threshold_kb,
                          t=event.time)
                return []
            self._contact_fwd[next_hop] = fwd + flow.size_kb

        # Delegate to the parent for balance deduction and forwarding logs.
        return super()._on_traffic_arrive(event)

    def _on_isl_close(self, event: SimEvent) -> List[SimEvent]:
        peer_id = event.from_node if event.to_node == self.satellite_id else event.to_node
        self._contact_fwd.pop(peer_id, None)
        # Close the contact with no settlement and no ISL pause.
        contact = event.payload
        contact_id = self._active_isls.pop(peer_id, "")
        range_km = getattr(contact, "range_km", 0.0) if contact else 0.0
        self._log("ISL_CLOSE", peer_id=peer_id, contact_id=contact_id, t=event.time,
                  isl_range_km=round(range_km, 3))
        return []


# ── A.2  Centralised Authority ────────────────────────────────────────────────

class CentralFabricMock(FabricMock):
    """Trusted central authority: every settlement is finalised immediately.

    No challenge window; single-signed proofs accepted without bilateral
    verification.  Models a scenario where a trusted third party (e.g. a
    consortium clearing house) has complete visibility and instant settlement.
    """

    def initiate_settlement(self, sat_channel_id: str, proof, submitted_by: str, t: float) -> str:
        tx_id = super().initiate_settlement(sat_channel_id, proof, submitted_by, t)
        # Immediate finalization — no 48 h challenge window.
        self.finalize_settlement(sat_channel_id, t)
        return tx_id


class CentralSatelliteNode(SatelliteNode):
    """Satellite under a trusted central authority.

    Queues the settlement payload at ISL close (so the GS can report to the
    authority) but does NOT pause the ISL channel.  The channel balance is
    reset optimistically: since the authority finalises immediately, there is
    no risk of a stale proof being accepted.

    Expected: Jain ≈ same as LIOS; OOS = 0 % (no ISL pause); settlement
    latency = wait_for_gs only (no challenge window, no peer-GS wait).
    """

    def _on_isl_close(self, event: SimEvent) -> List[SimEvent]:
        peer_id = event.from_node if event.to_node == self.satellite_id else event.to_node
        channel_id = self._channel_id(peer_id)
        t = event.time
        contact_id = self._active_isls.pop(peer_id, "")
        contact = event.payload
        range_km = getattr(contact, "range_km", 0.0) if contact else 0.0
        isl_prop_delay = range_km / cfg.link.c_km_s if range_km > 0 else 0.0

        self._log("ISL_CLOSE", peer_id=peer_id, contact_id=contact_id, t=t,
                  isl_range_km=round(range_km, 3),
                  isl_prop_delay_sec=round(isl_prop_delay, 6))

        if self._is_same_operator(peer_id):
            return []

        triggers = self._protocol.evaluate_settlement_triggers(channel_id)
        if not triggers:
            return []

        payload = self._protocol.get_settlement_payload(channel_id)
        if payload is None:
            return []

        payload.contact_id = contact_id
        payload.queued_at = t
        self._settlement_meta[channel_id] = {
            "isl_prop_delay_sec": isl_prop_delay,
            "isl_range_km": range_km,
        }

        # Optimistic balance reset — no ISL pause, no waiting for GS ack.
        # The authority will finalise immediately; the channel carries on.
        self._protocol.resume_channel(channel_id)

        return self._queue_and_maybe_upload(channel_id, payload, t)


class CentralGroundStationNode(GroundStationNode):
    """GS node for the central-authority baseline.

    Submits the settlement payload to CentralFabricMock (which finalises
    immediately) and skips the ISL_RESUME notification — the satellite never
    paused its channel, so no resume signal is needed.
    """

    def receive_settlement_payload(
        self,
        sat_id: str,
        payload: SettlementPayload,
        t: float,
    ) -> List[SimEvent]:
        ch_id = payload.channel_id
        proof = payload.latest_proof
        info = self._sat_contact_info.get(sat_id, {})
        gs_range_km = info.get("range_km", 0.0)
        uplink_prop_delay_sec = info.get("prop_delay_sec", 0.0)
        queued_at = getattr(payload, "queued_at", None)
        wait_for_gs_sec = (
            round(t - uplink_prop_delay_sec - queued_at, 3) if queued_at is not None else None
        )

        self._log(
            "SETTLEMENT_RECEIVED",
            channel_id=ch_id,
            triggers=payload.triggers_fired,
            seq_num=proof.seq_num,
            t=t,
            wait_for_gs_sec=wait_for_gs_sec,
            gs_range_km=round(gs_range_km, 3),
            uplink_prop_delay_sec=round(uplink_prop_delay_sec, 6),
        )

        # CentralFabricMock.initiate_settlement immediately calls finalize_settlement.
        self._fabric.initiate_settlement(ch_id, proof, self.operator_id, t)
        self._isl_fsm.on_settlement_finalized(ch_id)
        self._log("SETTLEMENT_FINALIZED", channel_id=ch_id, t=t)

        # No ISL_RESUME queued: CentralSatelliteNode already reset balance optimistically.
        return []


# ── Contact-end Ground Reset ─────────────────────────────────────────────────

class GroundResetFabricMock(FabricMock):
    """Ledger model for the contact-end ground-reset baseline.

    A channel reset is committed only after both endpoint satellites have uploaded
    the same contact-end balance proof through a ground station.  Until that
    happens, the satellite channel remains paused and future ISLs are blocked.
    """

    def __init__(self) -> None:
        super().__init__()
        self._ground_reset_pending: Dict[str, dict] = {}

    def submit_ground_reset_upload(
        self,
        sat_pair_id: str,
        sat_id: str,
        proof: BalanceProof,
        submitted_by: str,
        t: float,
    ) -> Tuple[str, Set[str]]:
        pending = self._ground_reset_pending.setdefault(
            sat_pair_id,
            {
                "proof": proof,
                "submittedBy": submitted_by,
                "submittedAt": t,
                "satellites": set(),
            },
        )
        pending["satellites"].add(sat_id)

        endpoints = set(sat_pair_id.split("__"))
        if not endpoints.issubset(pending["satellites"]):
            return "WAITING_FOR_PEER", set(pending["satellites"])

        self.submit_balance_reset(sat_pair_id, proof, submitted_by, t)
        self._settlements[sat_pair_id]["status"] = "RESET"
        self._settlements[sat_pair_id]["ground_reset_sats"] = sorted(pending["satellites"])
        self._ground_reset_pending.pop(sat_pair_id, None)
        return "RESET_COMMITTED", endpoints


class GroundResetSatelliteNode(SatelliteNode):
    """Baseline: every cross-operator contact-end requires a ground reset.

    When contact traffic changed the bilateral balance, both satellites pause the
    channel and upload the co-signed proof to ground.  No subsequent ISL with the
    peer is established until the ledger reset has committed and both satellites
    receive their ground resume notification.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._last_ground_reset_seq: Dict[str, int] = {}

    def _on_isl_open(self, event: SimEvent) -> List[SimEvent]:
        peer_id = event.from_node if event.to_node == self.satellite_id else event.to_node

        if not self._is_same_operator(peer_id):
            channel_id = self._channel_id(peer_id)
            state = self._protocol.get_channel(channel_id)
            fsm_status = self._isl_fsm.get_status(channel_id)
            if state is not None and (
                state.status == "PAUSED" or fsm_status != ISLChannelStatus.ACTIVE
            ):
                contact = event.payload
                contact_id = contact.contact_id if contact else ""
                self._log(
                    "ISL_OPEN_BLOCKED",
                    peer_id=peer_id,
                    contact_id=contact_id,
                    channel_id=channel_id,
                    fsm_status=fsm_status.value if fsm_status else None,
                    t=event.time,
                )
                return []

        return super()._on_isl_open(event)

    def _on_isl_close(self, event: SimEvent) -> List[SimEvent]:
        peer_id = event.from_node if event.to_node == self.satellite_id else event.to_node
        t = event.time
        contact = event.payload
        range_km = getattr(contact, "range_km", 0.0) if contact else 0.0
        isl_prop_delay_sec = range_km / cfg.link.c_km_s if range_km > 0 else 0.0
        contact_id = self._active_isls.pop(peer_id, "")

        self._log("ISL_CLOSE", peer_id=peer_id, contact_id=contact_id, t=t,
                  isl_range_km=round(range_km, 3),
                  isl_prop_delay_sec=round(isl_prop_delay_sec, 6))

        if self._is_same_operator(peer_id):
            return []

        channel_id = self._channel_id(peer_id)
        ch_state = self._protocol.get_channel(channel_id)
        if ch_state is None or ch_state.status == "PAUSED":
            return []

        latest = ch_state.latest_proof
        last_reset_seq = self._last_ground_reset_seq.get(channel_id, 0)
        if latest is None or latest.seq_num <= last_reset_seq:
            if self.satellite_id > peer_id:
                self._log(
                    "GROUND_RESET_WAITING_FOR_PEER_PROOF",
                    channel_id=channel_id,
                    contact_id=contact_id,
                    seq_num=latest.seq_num if latest else None,
                    t=t,
                )
                return []
            latest = self._build_contact_end_proof(ch_state)

        self._isl_fsm.record_contact_end_pause(
            channel_id, t, self.operator_id, "GROUND_RESET"
        )
        self._protocol.pause_channel(channel_id)

        payload = self._protocol.get_settlement_payload(channel_id)
        if payload is None:
            return []
        payload.latest_proof = latest
        payload.contact_id = contact_id
        payload.queued_at = t
        payload.triggers_fired = ["CONTACT_END_GROUND_RESET"]
        payload.reset_requested = True

        self._settlement_meta[channel_id] = {
            "isl_prop_delay_sec": isl_prop_delay_sec,
            "isl_range_km": range_km,
        }
        self._log(
            "GROUND_RESET_TRIGGERED",
            channel_id=channel_id,
            contact_id=contact_id,
            seq_num=ch_state.seq_num,
            balance_a_kb=ch_state.balance_a_kb,
            balance_b_kb=ch_state.balance_b_kb,
            t=t,
        )

        if latest.is_fully_signed():
            return self._queue_and_maybe_upload(channel_id, payload, t)

        self._pending_cosign[channel_id] = payload
        return [SimEvent(
            time=t + isl_prop_delay_sec,
            event_type=EventType.PROOF_PROP,
            from_node=self.satellite_id,
            to_node=peer_id,
            payload={
                "channel_id": channel_id,
                "proof": latest,
                "sender_pub_key": self._priv.public_key(),
                "isl_prop_delay_sec": isl_prop_delay_sec,
                "triggers": ["CONTACT_END_GROUND_RESET"],
                "ground_reset": True,
            },
        )]

    def _build_contact_end_proof(self, ch_state) -> BalanceProof:
        """Create a signed same-balance snapshot for a zero-traffic contact."""
        proof = BalanceProof(
            channel_id=ch_state.channel_id,
            seq_num=ch_state.seq_num + 1,
            balance_a_kb=ch_state.balance_a_kb,
            balance_b_kb=ch_state.balance_b_kb,
        )
        sig = _sign(self._priv, proof.signing_payload())
        if ch_state.my_role == "A":
            proof.sig_a = sig
        else:
            proof.sig_b = sig
        ch_state.seq_num = proof.seq_num
        ch_state.latest_proof = proof
        self._log(
            "GROUND_RESET_CONTACT_SNAPSHOT",
            channel_id=ch_state.channel_id,
            seq_num=proof.seq_num,
            balance_a_kb=proof.balance_a_kb,
            balance_b_kb=proof.balance_b_kb,
        )
        return proof

    def _on_proof_prop(self, event: SimEvent) -> List[SimEvent]:
        data = event.payload or {}
        if not data.get("ground_reset"):
            return super()._on_proof_prop(event)

        channel_id = data.get("channel_id", "")
        proof = data.get("proof")
        sender_pub_key = data.get("sender_pub_key")
        isl_delay = data.get("isl_prop_delay_sec", 0.0)
        t = event.time
        if proof is None or sender_pub_key is None:
            return []

        cosigned = self._protocol.cosign_proof(channel_id, proof, sender_pub_key)
        if cosigned is None:
            self._log("PROOF_PROP_REJECTED", channel_id=channel_id, t=t)
            return []

        self._log("PROOF_PROP_COSIGNED", channel_id=channel_id,
                  seq_num=cosigned.seq_num, t=t)

        new_events: List[SimEvent] = [SimEvent(
            time=t + isl_delay,
            event_type=EventType.PROOF_ACK,
            from_node=self.satellite_id,
            to_node=event.from_node,
            payload={"channel_id": channel_id, "proof": cosigned},
        )]

        ch_state = self._protocol.get_channel(channel_id)
        if ch_state and ch_state.status != "PAUSED":
            self._isl_fsm.record_contact_end_pause(
                channel_id, t, self.operator_id, "GROUND_RESET"
            )
            self._protocol.pause_channel(channel_id)
            payload = self._protocol.get_settlement_payload(channel_id)
            if payload:
                payload.latest_proof = cosigned
                payload.triggers_fired = ["CONTACT_END_GROUND_RESET"]
                payload.reset_requested = True
                payload.queued_at = t
                new_events += self._queue_and_maybe_upload(channel_id, payload, t)

        return new_events

    def _on_proof_ack(self, event: SimEvent) -> List[SimEvent]:
        events = super()._on_proof_ack(event)
        data = event.payload or {}
        channel_id = data.get("channel_id", "")
        payload = self._pending_settlement.get(channel_id)
        if payload is not None:
            payload.triggers_fired = ["CONTACT_END_GROUND_RESET"]
            payload.reset_requested = True
        return events

    def _on_notification_deliver(self, event: SimEvent) -> List[SimEvent]:
        bundle = event.payload
        if bundle:
            for notif in bundle.notifications:
                if notif.get("type") == "ISL_RESUME":
                    ch_id = notif.get("satChannelId", "")
                    state = self._protocol.get_channel(ch_id)
                    if state:
                        self._last_ground_reset_seq[ch_id] = state.seq_num
        return super()._on_notification_deliver(event)


class GroundResetGroundStationNode(GroundStationNode):
    """Ground node for the contact-end reset baseline.

    The first endpoint upload is retained in the central mock ledger.  The second
    endpoint upload commits the ledger reset and queues resume notifications for
    both satellites.
    """

    def receive_settlement_payload(
        self,
        sat_id: str,
        payload: SettlementPayload,
        t: float,
    ) -> List[SimEvent]:
        ch_id = payload.channel_id
        proof = payload.latest_proof
        settlement_operator = self._operator_for_satellite(sat_id)
        self._latest_sat_proofs[ch_id] = proof

        info = self._sat_contact_info.get(sat_id or "", {})
        gs_range_km = info.get("range_km", 0.0)
        uplink_prop_delay_sec = info.get("prop_delay_sec", 0.0)
        queued_at = getattr(payload, "queued_at", None)
        wait_for_gs_sec = round(t - uplink_prop_delay_sec - queued_at, 3) if queued_at else None
        self._log(
            "SETTLEMENT_RECEIVED",
            channel_id=ch_id,
            contact_id=payload.contact_id,
            triggers=payload.triggers_fired,
            reset_requested=True,
            seq_num=proof.seq_num,
            balance_a_kb=proof.balance_a_kb,
            balance_b_kb=proof.balance_b_kb,
            t=t,
            queued_at=queued_at,
            gs_range_km=round(gs_range_km, 3),
            uplink_prop_delay_sec=round(uplink_prop_delay_sec, 6),
            wait_for_gs_sec=wait_for_gs_sec,
        )

        if not isinstance(self._fabric, GroundResetFabricMock):
            raise TypeError("GroundResetGroundStationNode requires GroundResetFabricMock")

        status, sats_seen = self._fabric.submit_ground_reset_upload(
            ch_id, sat_id, proof, settlement_operator, t
        )
        if status == "WAITING_FOR_PEER":
            self._log(
                "GROUND_RESET_WAITING_FOR_PEER",
                channel_id=ch_id,
                sat_id=sat_id,
                sats_seen=sorted(sats_seen),
                t=t,
            )
            return []

        commit_latency = self._fabric.commit_latency_sec
        self._isl_fsm.on_settlement_finalized(ch_id)
        self._log("BALANCE_RESET_SUBMITTED", channel_id=ch_id, t=t)
        self._log("SETTLEMENT_FINALIZED", channel_id=ch_id, t=t + commit_latency,
                  via="ground_reset_both_sats")

        for endpoint_sat in ch_id.split("__"):
            self._pending_to_satellites.setdefault(endpoint_sat, []).append({
                "type": "ISL_RESUME", "satChannelId": ch_id, "timestamp": t,
            })
        return []

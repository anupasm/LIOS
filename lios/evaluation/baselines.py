"""Baseline protocol implementations for LIOS comparative evaluation.

Three baselines, each a minimal SatelliteNode (or ground-node) subclass:

  GreedySatelliteNode      — no accounting, no settlement (anarchy lower bound).
  TitForTatNode            — per-contact forwarding cap, no cross-contact credit.
  CentralSatelliteNode     — settlement without ISL pause (trusted central authority).
  CentralFabricMock        — accepts any proof and finalises immediately.
  CentralGroundStationNode — submits to Fabric without queuing ISL_RESUME.

Usage in run_experiments.py: set ExperimentConfig.baseline_protocol to one of
  'greedy' | 't4t' | 'central'   (default 'lios' uses the normal SatelliteNode).
"""
from __future__ import annotations

from typing import Dict, List

from config import cfg
from protocol.offchain import SettlementPayload
from simulator.ground_station_node import FabricMock, GroundStationNode
from simulator.satellite_node import SatelliteNode
from simulator.simulator import SimEvent


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

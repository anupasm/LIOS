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
from typing import Dict, List, Optional, Set

from config import cfg
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

    Settlement collision semantics (enforces Tch challenge window):
      - First submission:          PENDING_CHALLENGE  (Tch clock starts)
      - Second, same seq_num:      MUTUAL_FINALIZED   (immediate finalize, no penalty)
      - Second, higher seq_num:    CHALLENGED         (rollback detected, penalty, immediate finalize)
      - Second, lower  seq_num:    REJECTED_STALE     (rollback attempt by second submitter, penalty)
      - After FINALIZED, lower seq: REJECTED_STALE    (late rollback attempt, penalty)
    """

    # Simulated Fabric block-commit delay.  Read from config so it stays in
    # sync with config.toml without needing to reconstruct the mock.
    @property
    def commit_latency_sec(self) -> float:
        return cfg.blockchain.commit_latency_sec

    def __init__(self) -> None:
        self._channels: Dict[str, dict] = {}
        self._settlements: Dict[str, dict] = {}
        self._notifications: List[dict] = []
        self._tx_log: List[FabricSubmission] = []
        # Security tracking
        self._penalty_events: List[dict] = []

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

    def register_satellite_key(self, op_id: str, sat_id: str, pub_key_pem: str) -> None:
        pass  # No-op in mock; real Fabric client verifies against on-chain registry.

    def initiate_settlement(self, sat_channel_id: str, proof: BalanceProof, submitted_by: str, t: float) -> dict:
        """Submit a settlement proof.  Returns a status dict with 'status' and 'tx_id'.

        Status codes:
          NEW_PENDING      — first submission, Tch window started
          MUTUAL_FINALIZED — second submission with same seq; finalized immediately
          CHALLENGED       — second submission with higher seq; rollback penalised, finalized
          REJECTED_STALE   — stale submission; penalty applied, not recorded
        """
        existing = self._settlements.get(sat_channel_id)

        if existing:
            ex_status = existing.get("status", "")
            ex_seq = existing["proof"]["seq_num"]

            if ex_status == "PENDING_CHALLENGE":
                if proof.seq_num > ex_seq:
                    # Rollback detected: second submitter has the honest proof.
                    detection_latency = t - existing["submittedAt"]
                    self._penalty_events.append({
                        "cheating_operator": existing["submittedBy"],
                        "honest_operator": submitted_by,
                        "channel_id": sat_channel_id,
                        "attack_seq": ex_seq,
                        "honest_seq": proof.seq_num,
                        "t_attack": existing["submittedAt"],
                        "t_detected": t,
                        "detection_latency": detection_latency,
                    })
                    existing["proof"] = proof.to_dict()
                    existing["status"] = "FINALIZED"
                    existing["finalizedAt"] = t
                    self._push_notification("SETTLEMENT_CHALLENGED", {"satChannelId": sat_channel_id,
                                                                       "honest_seq": proof.seq_num}, t)
                    self._push_notification("SETTLEMENT_FINALIZED", {"satChannelId": sat_channel_id}, t)
                    return {"status": "CHALLENGED", "tx_id": existing["tx_id"]}

                elif proof.seq_num == ex_seq:
                    if submitted_by == existing["submittedBy"]:
                        # Same operator submitting again — duplicate, not mutual agreement.
                        return {"status": "ALREADY_PENDING", "tx_id": existing["tx_id"]}
                    # Mutual: peer operator confirms same seq — finalize immediately.
                    existing["status"] = "FINALIZED"
                    existing["finalizedAt"] = t
                    self._push_notification("SETTLEMENT_FINALIZED", {"satChannelId": sat_channel_id}, t)
                    return {"status": "MUTUAL_FINALIZED", "tx_id": existing["tx_id"]}

                else:
                    # Second submitter sent a *lower* seq — rollback attempt by second submitter.
                    self._penalty_events.append({
                        "cheating_operator": submitted_by,
                        "honest_operator": existing["submittedBy"],
                        "channel_id": sat_channel_id,
                        "attack_seq": proof.seq_num,
                        "honest_seq": ex_seq,
                        "t_attack": t,
                        "t_detected": t,
                        "detection_latency": 0.0,
                    })
                    self._push_notification("SETTLEMENT_CHALLENGED", {"satChannelId": sat_channel_id}, t)
                    return {"status": "REJECTED_STALE", "tx_id": ""}

            elif ex_status == "FINALIZED":
                if proof.seq_num < existing["proof"]["seq_num"]:
                    # Late stale submission after finalization.
                    self._penalty_events.append({
                        "cheating_operator": submitted_by,
                        "honest_operator": existing.get("submittedBy", ""),
                        "channel_id": sat_channel_id,
                        "attack_seq": proof.seq_num,
                        "honest_seq": existing["proof"]["seq_num"],
                        "t_attack": t,
                        "t_detected": t,
                        "detection_latency": 0.0,
                    })
                    return {"status": "REJECTED_STALE", "tx_id": ""}
                return {"status": "ALREADY_FINALIZED", "tx_id": existing["tx_id"]}

        # First submission — start Tch window.
        tx_id = str(uuid.uuid4())
        self._settlements[sat_channel_id] = {
            "proof": proof.to_dict(),
            "submittedBy": submitted_by,
            "submittedAt": t,
            "status": "PENDING_CHALLENGE",
            "tx_id": tx_id,
        }
        self._push_notification("SETTLEMENT_INITIATED", {
            "satChannelId": sat_channel_id,
            "seq_num": proof.seq_num,
            "submitted_by": submitted_by,
        }, t)
        return {"status": "NEW_PENDING", "tx_id": tx_id}

    def challenge_settlement(self, sat_channel_id: str, counter_proof: BalanceProof, t: float,
                             submitted_by: str = "") -> bool:
        """Explicitly challenge a pending settlement with a newer proof.

        Used by the peer GS when it receives a SETTLEMENT_INITIATED trigger and
        already holds a higher-seq proof from its own satellite.
        """
        rec = self._settlements.get(sat_channel_id)
        if rec is None or rec.get("status") != "PENDING_CHALLENGE":
            return False
        submitted_seq = rec["proof"]["seq_num"]
        if counter_proof.seq_num > submitted_seq:
            detection_latency = t - rec["submittedAt"]
            self._penalty_events.append({
                "cheating_operator": rec["submittedBy"],
                "honest_operator": submitted_by or "unknown",
                "channel_id": sat_channel_id,
                "attack_seq": submitted_seq,
                "honest_seq": counter_proof.seq_num,
                "t_attack": rec["submittedAt"],
                "t_detected": t,
                "detection_latency": detection_latency,
            })
            rec["proof"] = counter_proof.to_dict()
            rec["status"] = "FINALIZED"
            rec["finalizedAt"] = t
            self._push_notification("SETTLEMENT_CHALLENGED", {"satChannelId": sat_channel_id}, t)
            self._push_notification("SETTLEMENT_FINALIZED", {"satChannelId": sat_channel_id}, t)
            return True
        return False

    def finalize_settlement(self, sat_channel_id: str, t: float) -> bool:
        """Finalize a PENDING_CHALLENGE settlement after Tch expiry (no challenge filed)."""
        rec = self._settlements.get(sat_channel_id)
        if rec is None:
            return False
        if rec["status"] == "FINALIZED":
            return True  # Already finalized (e.g. via MUTUAL or CHALLENGED path).
        rec["status"] = "FINALIZED"
        rec["finalizedAt"] = t
        self._push_notification("SETTLEMENT_FINALIZED", {"satChannelId": sat_channel_id}, t)
        return True

    def get_pending_settlement(self, sat_channel_id: str) -> dict:
        """Return the current settlement record for a channel, or {} if none."""
        return self._settlements.get(sat_channel_id, {})

    def get_penalty_events(self) -> List[dict]:
        return list(self._penalty_events)

    def get_attack_detection_stats(self) -> dict:
        """Aggregate detection metrics across all penalty events."""
        import numpy as _np
        events = self._penalty_events
        if not events:
            return {"total_detected": 0, "detection_latency_sec": {}}
        latencies = [e["detection_latency"] for e in events]
        return {
            "total_detected": len(events),
            "detection_latency_sec": {
                "mean": float(_np.mean(latencies)),
                "p50":  float(_np.percentile(latencies, 50)),
                "p95":  float(_np.percentile(latencies, 95)),
                "max":  float(_np.max(latencies)),
            },
        }

    def submit_balance_reset(self, sat_pair_id: str, proof: BalanceProof, submitted_by: str, t: float) -> str:
        """Submit a co-signed balance reset.  Records the net transfer and marks the pair as RESET."""
        import uuid as _uuid
        tx_id = str(_uuid.uuid4())
        half = (proof.balance_a_kb + proof.balance_b_kb) / 2.0
        delta = half - proof.balance_a_kb
        self._settlements[sat_pair_id] = {
            "proof": proof.to_dict(),
            "submittedBy": submitted_by,
            "submittedAt": t,
            "status": "RESET",
            "delta": delta,
            "tx_id": tx_id,
        }
        self._push_notification("BALANCE_RESET", {"satChannelId": sat_pair_id, "delta": delta}, t)
        return tx_id

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
        shared_pending: Optional[Dict[str, List[dict]]] = None,
    ) -> None:
        self.gs = gs
        self.gs_id = gs.gs_id
        self.operator_id = gs.operator_id
        self._fabric = fabric
        self._isl_fsm = isl_fsm

        # Shared across all GS of the same operator so that any GS can deliver
        # an ISL_RESUME notification regardless of which GS queued it originally.
        self._pending_to_satellites: Dict[str, List[dict]] = (
            shared_pending if shared_pending is not None else {}
        )
        self._crl_delta: List[str] = []
        # Channels for which this GS has already queued ISL_RESUME (prevents re-delivery).
        self._notified_channels: Set[str] = set()
        # operator_id → gs_id registry — injected after all GS nodes are built.
        self.gs_registry: Dict[str, str] = {}
        # sat_id → {range_km, prop_delay_sec} for the current contact window
        self._sat_contact_info: Dict[str, dict] = {}
        # Latest proof received from each satellite — used for challenge detection.
        self._latest_sat_proofs: Dict[str, "BalanceProof"] = {}  # ch_id → proof
        self.event_log: List[dict] = []

    # ── event dispatch ─────────────────────────────────────────────────────────

    def handle_event(self, event: SimEvent) -> List[SimEvent]:
        if event.event_type == EventType.GS_CONTACT_START:
            return self._on_satellite_contact_start(event)
        elif event.event_type == EventType.GS_CONTACT_END:
            return self._on_satellite_contact_end(event)
        elif event.event_type == EventType.SETTLEMENT_UPLOAD:
            return self._on_settlement_upload(event)
        elif event.event_type == EventType.CHALLENGE_WINDOW_EXPIRE:
            return self._on_challenge_expire(event)
        elif event.event_type == EventType.SETTLEMENT_TRIGGER:
            return self._on_settlement_trigger(event)
        elif event.event_type == EventType.KEY_REVOKED:
            return self._on_key_revoked(event)
        return []

    # ── satellite contact ──────────────────────────────────────────────────────
    #
    # GS_CONTACT_START/END are sent to the GS with from_node=sat, to_node=gs_id
    # (see simulator.seed_contacts_from_plan).  sat_id is always event.from_node.

    def _on_satellite_contact_start(self, event: SimEvent) -> List[SimEvent]:
        sat_id = event.from_node
        t = event.time
        contact = event.payload
        range_km = getattr(contact, "range_km", 0.0) if contact else 0.0
        # One-way propagation delay: d/c (Bhattacherjee & Singla, CoNEXT 2019 §3.1)
        prop_delay_sec = range_km / cfg.link.c_km_s if range_km > 0 else 0.0
        self._sat_contact_info[sat_id] = {"range_km": range_km, "prop_delay_sec": prop_delay_sec}
        self._log("GS_CONTACT_START", sat_id=sat_id, t=t,
                  gs_range_km=round(range_km, 3),
                  gs_prop_delay_sec=round(prop_delay_sec, 6))
        # Deliver notifications queued before this contact window.
        return self._drain_notifications(sat_id, t)

    def _on_satellite_contact_end(self, event: SimEvent) -> List[SimEvent]:
        sat_id = event.from_node
        t = event.time
        self._log("GS_CONTACT_END", sat_id=sat_id, t=t)
        # Drain notifications queued during (or before) this contact window.
        result = self._drain_notifications(sat_id, t)
        self._sat_contact_info.pop(sat_id, None)
        return result

    def _drain_notifications(self, sat_id: str, t: float) -> List[SimEvent]:
        """Bundle and deliver all queued notifications for sat_id.

        Applies physical downlink propagation delay (range_km / c) to the delivery time.
        """
        pending = self._pending_to_satellites.pop(sat_id, [])
        if not pending and not self._crl_delta:
            return []
        info = self._sat_contact_info.get(sat_id, {})
        prop_delay_sec = info.get("prop_delay_sec", 0.0)
        bundle = NotificationBundle(
            bundle_id=str(uuid.uuid4()),
            generated_at=t,               # time GS generates/sends the bundle
            target_satellite=sat_id,
            notifications=pending,
            crl_delta=list(self._crl_delta),
        )
        return [SimEvent(
            time=t + prop_delay_sec,      # physical downlink propagation delay
            event_type=EventType.NOTIFICATION_DELIVER,
            from_node=self.gs_id,
            to_node=sat_id,
            payload=bundle,
        )]

    # ── settlement ─────────────────────────────────────────────────────────────

    def receive_settlement_payload(
        self,
        sat_id: str,
        payload: SettlementPayload,
        t: float,
    ) -> List[SimEvent]:
        """Called when satellite uploads a settlement payload during GS contact.

        Settlement flow (Tch challenge-window model):
          T1 reset  — co-signed balance reset; both sides finalise via RESET_TRIGGER.
          T2/T7     — submit to Fabric via initiate_settlement:
                        MUTUAL_FINALIZED / CHALLENGED → immediate finalize + ISL_RESUME
                        NEW_PENDING                   → schedule CHALLENGE_WINDOW_EXPIRE;
                                                        push SETTLEMENT_TRIGGER(INITIATED)
                                                        to peer GS so it can challenge.
        """
        ch_id = payload.channel_id
        proof = payload.latest_proof

        # Always store the latest proof from our satellite for future challenge detection.
        self._latest_sat_proofs[ch_id] = proof

        # Proactive rollback defence: if the peer already submitted a PENDING_CHALLENGE
        # settlement with a lower seq_num, challenge it immediately with our honest proof.
        # This covers the T1-reset path where the honest GS calls submit_balance_reset
        # rather than initiate_settlement, bypassing the normal collision detection.
        ch_rec = self._fabric.get_pending_settlement(ch_id)
        if ch_rec.get("status") == "PENDING_CHALLENGE":
            pending_seq = ch_rec["proof"]["seq_num"]
            if proof.seq_num > pending_seq:
                challenged = self._fabric.challenge_settlement(
                    ch_id, proof, t, submitted_by=self.operator_id
                )
                if challenged:
                    self._isl_fsm.on_settlement_finalized(ch_id)
                    self._log("CHALLENGE_SUBMITTED", channel_id=ch_id,
                              honest_seq=proof.seq_num, attack_seq=pending_seq, t=t)

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
            reset_requested=payload.reset_requested,
            seq_num=proof.seq_num,
            balance_a_kb=proof.balance_a_kb,
            balance_b_kb=proof.balance_b_kb,
            t=t,
            queued_at=queued_at,
            gs_range_km=round(gs_range_km, 3),
            uplink_prop_delay_sec=round(uplink_prop_delay_sec, 6),
            wait_for_gs_sec=wait_for_gs_sec,
        )

        new_events: List[SimEvent] = []

        if payload.reset_requested:
            # T1 balance-reset path: co-signed reset.  Only the FIRST GS to process
            # this channel submits on-chain; the peer GS sends SETTLEMENT_TRIGGER
            # FINALIZED which puts the channel in _notified_channels.  If that trigger
            # already arrived before our satellite uploaded, skip the duplicate call.
            if ch_id not in self._notified_channels:
                self._fabric.submit_balance_reset(ch_id, proof, self.operator_id, t)
                commit_latency = self._fabric.commit_latency_sec
                self._isl_fsm.on_settlement_finalized(ch_id)
                self._log("BALANCE_RESET_SUBMITTED", channel_id=ch_id, t=t)
                self._log("SETTLEMENT_FINALIZED", channel_id=ch_id, t=t + commit_latency, via="balance_reset")
                self._notified_channels.add(ch_id)
                for other_sat in ch_id.split("__"):
                    other_op = other_sat.split("-")[0] if "-" in other_sat else ""
                    if other_op and other_op != self.operator_id:
                        peer_gs_id = self.gs_registry.get(other_op)
                        if peer_gs_id:
                            new_events.append(SimEvent(
                                time=t + 0.01,
                                event_type=EventType.SETTLEMENT_TRIGGER,
                                from_node=self.gs_id,
                                to_node=peer_gs_id,
                                payload={"sat_channel_id": ch_id, "reset": True, "trigger_type": "FINALIZED"},
                            ))
            else:
                self._log("BALANCE_RESET_ALREADY_COMMITTED", channel_id=ch_id, t=t)
            # Always queue ISL_RESUME for our own satellite (idempotent via set).
            if sat_id:
                self._pending_to_satellites.setdefault(sat_id, []).append({
                    "type": "ISL_RESUME", "satChannelId": ch_id, "timestamp": t,
                })
            return new_events

        # Normal settlement path (T2/T7).
        result = self._fabric.initiate_settlement(ch_id, proof, self.operator_id, t)
        commit_latency = self._fabric.commit_latency_sec
        status = result["status"] if isinstance(result, dict) else "NEW_PENDING"

        if status == "REJECTED_STALE":
            # Peer already finalized this proof on-chain (co-signed path won the race).
            # The channel is settled — unblock our satellite so it can resume.
            self._isl_fsm.on_settlement_finalized(ch_id)
            self._log("SETTLEMENT_ALREADY_COMMITTED", channel_id=ch_id, t=t + commit_latency)
            if sat_id:
                self._pending_to_satellites.setdefault(sat_id, []).append({
                    "type": "ISL_RESUME", "satChannelId": ch_id, "timestamp": t,
                })
            return new_events

        if status in ("MUTUAL_FINALIZED", "CHALLENGED"):
            # Immediate finalization — both parties agree (or rollback caught).
            self._isl_fsm.on_settlement_finalized(ch_id)
            self._log("SETTLEMENT_FINALIZED", channel_id=ch_id, t=t + commit_latency, via=status)
            if sat_id:
                self._pending_to_satellites.setdefault(sat_id, []).append({
                    "type": "ISL_RESUME", "satChannelId": ch_id, "timestamp": t,
                })
            self._notified_channels.add(ch_id)
            for other_sat in ch_id.split("__"):
                other_op = other_sat.split("-")[0] if "-" in other_sat else ""
                if other_op and other_op != self.operator_id:
                    peer_gs_id = self.gs_registry.get(other_op)
                    if peer_gs_id:
                        new_events.append(SimEvent(
                            time=t + 0.01,
                            event_type=EventType.SETTLEMENT_TRIGGER,
                            from_node=self.gs_id,
                            to_node=peer_gs_id,
                            payload={"sat_channel_id": ch_id, "reset": False, "trigger_type": "FINALIZED"},
                        ))

        elif status == "NEW_PENDING":
            # Tch window active — schedule expiry and notify peer so it can challenge.
            tch = cfg.protocol.t_challenge_sec
            new_events.append(SimEvent(
                time=t + tch,
                event_type=EventType.CHALLENGE_WINDOW_EXPIRE,
                from_node=self.gs_id,
                to_node=self.gs_id,
                payload={"sat_channel_id": ch_id, "sat_id": sat_id},
            ))
            for other_sat in ch_id.split("__"):
                other_op = other_sat.split("-")[0] if "-" in other_sat else ""
                if other_op and other_op != self.operator_id:
                    peer_gs_id = self.gs_registry.get(other_op)
                    if peer_gs_id:
                        new_events.append(SimEvent(
                            time=t + 0.01,
                            event_type=EventType.SETTLEMENT_TRIGGER,
                            from_node=self.gs_id,
                            to_node=peer_gs_id,
                            payload={
                                "sat_channel_id": ch_id,
                                "reset": False,
                                "trigger_type": "INITIATED",
                                "submitted_seq": proof.seq_num,
                            },
                        ))

        elif status == "REJECTED_STALE":
            self._log("SETTLEMENT_REJECTED_STALE", channel_id=ch_id, t=t)

        return new_events

    def _on_settlement_upload(self, event: SimEvent) -> List[SimEvent]:
        """Satellite uploaded a settlement payload during a GS contact."""
        sat_node_id = event.from_node
        payload = event.payload
        if payload is None:
            return []
        return self.receive_settlement_payload(sat_node_id, payload, event.time)

    def _on_challenge_expire(self, event: SimEvent) -> List[SimEvent]:
        """Tch expired — finalize the pending settlement (if not already resolved) and
        send ISL_RESUME to our satellite plus a FINALIZED trigger to the peer GS."""
        payload = event.payload or {}
        ch_id = payload.get("sat_channel_id", "")
        sat_id = payload.get("sat_id")
        if not ch_id:
            return []
        t = event.time

        rec = self._fabric.get_pending_settlement(ch_id)
        if rec.get("status") == "PENDING_CHALLENGE":
            # No challenge arrived within Tch — finalize with the (possibly stale) proof.
            self._fabric.finalize_settlement(ch_id, t)
            self._isl_fsm.on_settlement_finalized(ch_id)
            self._log("SETTLEMENT_FINALIZED", channel_id=ch_id,
                      t=t + self._fabric.commit_latency_sec, via="tch_expiry")

        # Queue ISL_RESUME for our satellite (idempotent guard via _notified_channels).
        new_events: List[SimEvent] = []
        if ch_id not in self._notified_channels:
            if sat_id:
                self._pending_to_satellites.setdefault(sat_id, []).append({
                    "type": "ISL_RESUME", "satChannelId": ch_id, "timestamp": t,
                })
            self._notified_channels.add(ch_id)
            for other_sat in ch_id.split("__"):
                other_op = other_sat.split("-")[0] if "-" in other_sat else ""
                if other_op and other_op != self.operator_id:
                    peer_gs_id = self.gs_registry.get(other_op)
                    if peer_gs_id:
                        new_events.append(SimEvent(
                            time=t + 0.01,
                            event_type=EventType.SETTLEMENT_TRIGGER,
                            from_node=self.gs_id,
                            to_node=peer_gs_id,
                            payload={"sat_channel_id": ch_id, "reset": False, "trigger_type": "FINALIZED"},
                        ))
        return new_events

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

    # ── key revocation ─────────────────────────────────────────────────────────

    def _on_key_revoked(self, event: SimEvent) -> List[SimEvent]:
        sat_id = event.payload.get("satellite_id", "")
        if sat_id:
            self._crl_delta.append(sat_id)
            self._log("KEY_REVOKED_PROPAGATED", sat_id=sat_id, t=event.time)
        return []

    def _on_settlement_trigger(self, event: SimEvent) -> List[SimEvent]:
        """Peer GS notified us of a settlement event.

        trigger_type == "INITIATED": peer GS started a Tch window with submitted_seq.
            Check _latest_sat_proofs — if we hold a newer proof, challenge immediately.
        trigger_type == "FINALIZED" (or legacy payload without type): queue ISL_RESUME.
        """
        payload = event.payload or {}
        ch_id = payload.get("sat_channel_id", "")
        if not ch_id:
            return []
        t = event.time
        trigger_type = payload.get("trigger_type", "FINALIZED")  # backward-compat default

        if trigger_type == "INITIATED":
            submitted_seq = payload.get("submitted_seq", -1)
            latest = self._latest_sat_proofs.get(ch_id)
            if latest and latest.seq_num > submitted_seq:
                # We hold a newer proof — challenge the pending settlement.
                challenged = self._fabric.challenge_settlement(
                    ch_id, latest, t, submitted_by=self.operator_id
                )
                if challenged:
                    self._log("CHALLENGE_SUBMITTED", channel_id=ch_id,
                              honest_seq=latest.seq_num, attack_seq=submitted_seq, t=t)
                    self._log("SETTLEMENT_FINALIZED", channel_id=ch_id, t=t, via="CHALLENGED")
                    # Finalize immediately from our side and queue ISL_RESUME.
                    self._isl_fsm.on_settlement_finalized(ch_id)
                    if ch_id not in self._notified_channels:
                        for sat in ch_id.split("__"):
                            if sat.split("-")[0] == self.operator_id:
                                self._pending_to_satellites.setdefault(sat, []).append({
                                    "type": "ISL_RESUME", "satChannelId": ch_id, "timestamp": t,
                                })
                        self._notified_channels.add(ch_id)
            return []

        # FINALIZED (or legacy): queue ISL_RESUME for our satellite.
        if ch_id in self._notified_channels:
            return []
        is_reset = payload.get("reset", False)
        self._log(
            "PEER_RESET_NOTIFIED" if is_reset else "PEER_SETTLEMENT_NOTIFIED",
            channel_id=ch_id, from_gs=event.from_node, t=t,
        )
        for sat in ch_id.split("__"):
            if sat.split("-")[0] == self.operator_id:
                self._pending_to_satellites.setdefault(sat, []).append({
                    "type": "ISL_RESUME", "satChannelId": ch_id, "timestamp": t,
                })
                self._notified_channels.add(ch_id)
        return []

    # ── Helpers ────────────────────────────────────────────────────────────────

    def _log(self, event_type: str, **kwargs) -> None:
        self.event_log.append({"event": event_type, "gs": self.gs_id, **kwargs})

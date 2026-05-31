"""Metrics collector and fairness analysis for LIOS protocol evaluation (§16.2)."""
from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np


# ── Data structures ────────────────────────────────────────────────────────────

@dataclass
class ForwardingRecord:
    from_sat: str
    to_sat: str
    from_op: str
    to_op: str
    bytes_kb: float
    sim_time: float


@dataclass
class SettlementRecord:
    sat_channel_id: str
    duration_sec: float
    was_disputed: bool
    sim_time: float


@dataclass
class PenaltyRecord:
    cheating_operator: str
    honest_operator: str
    penalty_amount: float
    attack_type: str
    sim_time: float


@dataclass
class OOSRecord:
    sat_channel_id: str
    pause_time: float
    resume_time: float
    duration_sec: float


# ── Security-specific records ──────────────────────────────────────────────────

@dataclass
class RollbackRecord:
    """One rollback attempt by a malicious satellite."""
    channel_id: str
    attack_seq: int        # stale seq_num submitted
    honest_seq: int        # true latest seq_num
    gain_kb: float         # KB the attacker would gain if unchallenged
    t_attack: float        # sim time of submission
    detected: bool = False
    t_detected: float = 0.0
    detection_latency_sec: float = 0.0


@dataclass
class SelectiveDropRecord:
    """One selective-forward drop event."""
    channel_id: str
    bytes_kb: float
    sim_time: float
    # False-claim: traffic counted in balance but not actually forwarded.


# ── MetricsCollector ───────────────────────────────────────────────────────────

class MetricsCollector:
    """Collects and computes all evaluation metrics (§16.2)."""

    def __init__(self, operators: List[str]) -> None:
        self.operators = operators
        self.bytes_forwarded_by: Dict[str, float] = {op: 0.0 for op in operators}
        self.bytes_received_by: Dict[str, float] = {op: 0.0 for op in operators}
        self.payments_received_by: Dict[str, float] = {op: 0.0 for op in operators}
        self.settlement_events: List[SettlementRecord] = []
        self.penalty_events: List[PenaltyRecord] = []
        self.oos_records: List[OOSRecord] = []
        self.forwarding_timeline: List[ForwardingRecord] = []
        self.rollback_attempts: int = 0
        self._balance_snapshots: Dict[str, List[Tuple[float, float, float]]] = {}
        # Security records
        self.rollback_records: List[RollbackRecord] = []
        self.selective_drop_records: List[SelectiveDropRecord] = []

    # ── record methods ─────────────────────────────────────────────────────────

    def record_forwarding(
        self, from_sat: str, to_sat: str, bytes_kb: float, sim_time: float,
        from_op: str = "", to_op: str = "",
    ) -> None:
        if from_op:
            self.bytes_forwarded_by[from_op] = self.bytes_forwarded_by.get(from_op, 0) + bytes_kb
        if to_op:
            self.bytes_received_by[to_op] = self.bytes_received_by.get(to_op, 0) + bytes_kb
        self.forwarding_timeline.append(
            ForwardingRecord(from_sat, to_sat, from_op, to_op, bytes_kb, sim_time)
        )

    def record_settlement(
        self, sat_channel_id: str, duration_sec: float, was_disputed: bool, sim_time: float
    ) -> None:
        self.settlement_events.append(SettlementRecord(sat_channel_id, duration_sec, was_disputed, sim_time))

    def record_penalty(
        self, cheating_op: str, honest_op: str, amount: float, attack_type: str, sim_time: float
    ) -> None:
        self.penalty_events.append(PenaltyRecord(cheating_op, honest_op, amount, attack_type, sim_time))

    def record_oos(
        self, sat_channel_id: str, pause_time: float, resume_time: float
    ) -> None:
        self.oos_records.append(OOSRecord(sat_channel_id, pause_time, resume_time, resume_time - pause_time))

    def record_rollback_attempt(self) -> None:
        self.rollback_attempts += 1

    def record_rollback(
        self,
        channel_id: str,
        attack_seq: int,
        honest_seq: int,
        gain_kb: float,
        t_attack: float,
        detected: bool = False,
        t_detected: float = 0.0,
    ) -> None:
        detection_latency = (t_detected - t_attack) if detected else 0.0
        self.rollback_records.append(RollbackRecord(
            channel_id=channel_id,
            attack_seq=attack_seq,
            honest_seq=honest_seq,
            gain_kb=gain_kb,
            t_attack=t_attack,
            detected=detected,
            t_detected=t_detected,
            detection_latency_sec=detection_latency,
        ))
        self.rollback_attempts += 1

    def record_selective_drop(self, channel_id: str, bytes_kb: float, sim_time: float) -> None:
        self.selective_drop_records.append(SelectiveDropRecord(channel_id, bytes_kb, sim_time))

    def record_payment(self, from_op: str, to_op: str, amount_kb: float) -> None:
        """Record a monetary transfer (in KB-equivalent) from one operator to another."""
        if amount_kb <= 0:
            return
        self.payments_received_by[to_op] = self.payments_received_by.get(to_op, 0.0) + amount_kb

    def record_balance_snapshot(
        self, op_pair: str, t: float, bal_a: float, bal_b: float
    ) -> None:
        self._balance_snapshots.setdefault(op_pair, []).append((t, bal_a, bal_b))

    # ── computed metrics ───────────────────────────────────────────────────────

    def compute_jain_fairness_index(self) -> float:
        """J = (Σx_i)² / (n·Σx_i²) where x_i = forwarded/received ratio per operator."""
        ratios: List[float] = []
        for op in self.operators:
            fwd = self.bytes_forwarded_by.get(op, 0.0)
            rcv = self.bytes_received_by.get(op, 0.0)
            if rcv > 0:
                ratios.append(fwd / rcv)
            elif fwd > 0:
                ratios.append(fwd)  # penalise operators that only forward, never receive
        if not ratios:
            return 1.0
        n = len(ratios)
        return (sum(ratios) ** 2) / (n * sum(r ** 2 for r in ratios))

    def compute_utility_jain(self, price_per_kb: float = 1.0) -> float:
        """Jain index on gross utility = traffic_received * price + payments_received.

        Allows fair comparison across protocols: for Central/T4T/Greedy payments are
        zero, so utility reduces to traffic received.  For LIOS, on-chain compensation
        is included, showing fairness even when raw traffic is unbalanced.
        """
        utilities = [
            self.bytes_received_by.get(op, 0.0) * price_per_kb
            + self.payments_received_by.get(op, 0.0)
            for op in self.operators
        ]
        total = sum(utilities)
        if total == 0:
            return 1.0
        n = len(utilities)
        return (total ** 2) / (n * sum(u ** 2 for u in utilities))

    def compute_free_rider_prevention_rate(self) -> float:
        """Fraction of rollback attempts that resulted in a penalty."""
        if self.rollback_attempts == 0:
            return 1.0
        return len(self.penalty_events) / self.rollback_attempts

    def compute_settlement_latency_stats(self) -> dict:
        durations = [s.duration_sec for s in self.settlement_events]
        if not durations:
            return {"mean": 0, "p50": 0, "p95": 0, "p99": 0, "max": 0, "count": 0}
        arr = np.array(durations)
        return {
            "mean": float(np.mean(arr)),
            "p50": float(np.percentile(arr, 50)),
            "p95": float(np.percentile(arr, 95)),
            "p99": float(np.percentile(arr, 99)),
            "max": float(np.max(arr)),
            "count": len(durations),
        }

    def compute_oos_fraction(self, total_contact_sec: float) -> float:
        """Total OOS seconds / total potential contact seconds."""
        if total_contact_sec <= 0:
            return 0.0
        total_oos = sum(r.duration_sec for r in self.oos_records)
        return min(1.0, total_oos / total_contact_sec)

    # ── security metrics ───────────────────────────────────────────────────────

    def compute_detection_rate(self) -> float:
        """Fraction of rollback attempts that were detected and penalised."""
        if not self.rollback_records:
            return 1.0  # No attacks → vacuously 100 %
        return sum(1 for r in self.rollback_records if r.detected) / len(self.rollback_records)

    def compute_detection_latency_stats(self) -> dict:
        latencies = [r.detection_latency_sec for r in self.rollback_records if r.detected]
        if not latencies:
            return {"mean": 0, "p50": 0, "p95": 0, "max": 0, "count": 0}
        arr = np.array(latencies)
        return {
            "mean": float(np.mean(arr)),
            "p50":  float(np.percentile(arr, 50)),
            "p95":  float(np.percentile(arr, 95)),
            "max":  float(np.max(arr)),
            "count": len(latencies),
        }

    def compute_economic_deterrence(self) -> dict:
        """KB gain prevented across all detected rollback attempts."""
        detected = [r for r in self.rollback_records if r.detected]
        attempted_gain = sum(r.gain_kb for r in self.rollback_records)
        prevented_gain = sum(r.gain_kb for r in detected)
        return {
            "total_attempted_gain_kb": attempted_gain,
            "total_prevented_gain_kb": prevented_gain,
            "prevention_fraction": prevented_gain / attempted_gain if attempted_gain > 0 else 1.0,
        }

    def compute_false_claim_rate(self) -> dict:
        """Fraction of total forwarding events that were selective-forward drops (false claims)."""
        total_fwd = len(self.forwarding_timeline)
        drops = len(self.selective_drop_records)
        claimed_kb = sum(r.bytes_kb for r in self.selective_drop_records)
        return {
            "drop_count": drops,
            "total_fwd_events": total_fwd,
            "false_claim_fraction": drops / (total_fwd + drops) if (total_fwd + drops) > 0 else 0.0,
            "falsely_claimed_kb": claimed_kb,
        }

    def compute_tch_sensitivity(self, tch_values_sec: List[float]) -> List[dict]:
        """Post-hoc E3 analysis: for each Tch value, what fraction of detected attacks
        would still be caught (detection_latency ≤ Tch)?"""
        if not self.rollback_records:
            return [{"tch_sec": tch, "detection_rate": 1.0} for tch in tch_values_sec]
        total = len(self.rollback_records)
        results = []
        for tch in tch_values_sec:
            caught = sum(
                1 for r in self.rollback_records
                if r.detected and r.detection_latency_sec <= tch
            )
            results.append({
                "tch_sec": tch,
                "tch_hours": tch / 3600,
                "detection_rate": caught / total,
                "caught": caught,
                "total": total,
            })
        return results

    def generate_report(self, total_contact_sec: float = 0.0) -> dict:
        return {
            "jain_fairness_index": self.compute_jain_fairness_index(),
            "utility_jain": self.compute_utility_jain(),
            "payments_received_by": dict(self.payments_received_by),
            "free_rider_prevention_rate": self.compute_free_rider_prevention_rate(),
            "settlement_latency": self.compute_settlement_latency_stats(),
            "oos_fraction": self.compute_oos_fraction(total_contact_sec),
            "penalty_events": len(self.penalty_events),
            "settlement_events": len(self.settlement_events),
            "total_forwarding_events": len(self.forwarding_timeline),
            "bytes_forwarded_by": self.bytes_forwarded_by,
            "bytes_received_by": self.bytes_received_by,
            # Security-specific fields (non-zero only in adversarial runs)
            "detection_rate": self.compute_detection_rate(),
            "detection_latency": self.compute_detection_latency_stats(),
            "economic_deterrence": self.compute_economic_deterrence(),
            "false_claim": self.compute_false_claim_rate(),
            "rollback_attempts": len(self.rollback_records),
            "selective_drops": len(self.selective_drop_records),
        }

    # ── plotting ───────────────────────────────────────────────────────────────

    def plot_fairness_over_time(self, output_path: str, window_sec: float = 600.0) -> None:
        """Jain fairness index computed in a sliding window over simulation time."""
        if not self.forwarding_timeline:
            return
        times = sorted(set(r.sim_time for r in self.forwarding_timeline))
        if len(times) < 2:
            return
        t_start, t_end = times[0], times[-1]
        ts = np.arange(t_start + window_sec, t_end, window_sec / 2)
        jain_vals: List[float] = []
        t_vals: List[float] = []

        for t in ts:
            window_records = [r for r in self.forwarding_timeline if t - window_sec <= r.sim_time <= t]
            fwd: Dict[str, float] = {op: 0.0 for op in self.operators}
            rcv: Dict[str, float] = {op: 0.0 for op in self.operators}
            for r in window_records:
                if r.from_op in fwd:
                    fwd[r.from_op] += r.bytes_kb
                if r.to_op in rcv:
                    rcv[r.to_op] += r.bytes_kb
            ratios = [fwd[op] / rcv[op] for op in self.operators if rcv[op] > 0]
            if ratios:
                n = len(ratios)
                j = (sum(ratios) ** 2) / (n * sum(x**2 for x in ratios))
                jain_vals.append(j)
                t_vals.append(t / 3600)

        if not t_vals:
            return
        fig, ax = plt.subplots(figsize=(10, 4))
        ax.plot(t_vals, jain_vals, linewidth=1.5, color="steelblue")
        ax.axhline(0.95, linestyle="--", color="red", linewidth=0.8, label="Target (0.95)")
        ax.set_xlabel("Simulation time (hours)")
        ax.set_ylabel("Jain Fairness Index")
        ax.set_title("LIOS Protocol — Fairness over Time")
        ax.set_ylim(0, 1.05)
        ax.legend()
        ax.grid(True, alpha=0.3)
        fig.tight_layout()
        Path(output_path).parent.mkdir(parents=True, exist_ok=True)
        fig.savefig(output_path, dpi=150)
        plt.close(fig)

    def plot_operator_balance_evolution(self, output_path: str) -> None:
        """Line chart of balance per operator pair over time."""
        if not self._balance_snapshots:
            return
        fig, ax = plt.subplots(figsize=(12, 5))
        for pair, snapshots in self._balance_snapshots.items():
            ts = [s[0] / 3600 for s in snapshots]
            bal_a = [s[1] / 1024 for s in snapshots]  # KB → MB
            ax.plot(ts, bal_a, label=f"{pair}-A")
        ax.set_xlabel("Simulation time (hours)")
        ax.set_ylabel("Balance (MB)")
        ax.set_title("Operator Channel Balance Evolution")
        ax.legend(fontsize=8)
        ax.grid(True, alpha=0.3)
        fig.tight_layout()
        Path(output_path).parent.mkdir(parents=True, exist_ok=True)
        fig.savefig(output_path, dpi=150)
        plt.close(fig)

    def plot_settlement_timeline(self, output_path: str) -> None:
        """Gantt-style chart of OOS periods per channel."""
        if not self.oos_records:
            return
        channels = sorted(set(r.sat_channel_id for r in self.oos_records))
        fig, ax = plt.subplots(figsize=(14, max(4, len(channels) * 0.6)))
        colors = plt.cm.tab10.colors
        for ci, ch in enumerate(channels):
            recs = [r for r in self.oos_records if r.sat_channel_id == ch]
            for r in recs:
                ax.barh(
                    ci,
                    width=(r.resume_time - r.pause_time) / 3600,
                    left=r.pause_time / 3600,
                    height=0.6,
                    color=colors[ci % len(colors)],
                    alpha=0.7,
                )
        ax.set_yticks(range(len(channels)))
        ax.set_yticklabels(channels, fontsize=7)
        ax.set_xlabel("Simulation time (hours)")
        ax.set_title("ISL Out-of-Service Periods by Channel")
        ax.grid(True, axis="x", alpha=0.3)
        fig.tight_layout()
        Path(output_path).parent.mkdir(parents=True, exist_ok=True)
        fig.savefig(output_path, dpi=150)
        plt.close(fig)

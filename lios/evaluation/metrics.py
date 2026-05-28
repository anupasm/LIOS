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


# ── MetricsCollector ───────────────────────────────────────────────────────────

class MetricsCollector:
    """Collects and computes all evaluation metrics (§16.2)."""

    def __init__(self, operators: List[str]) -> None:
        self.operators = operators
        self.bytes_forwarded_by: Dict[str, float] = {op: 0.0 for op in operators}
        self.bytes_received_by: Dict[str, float] = {op: 0.0 for op in operators}
        self.settlement_events: List[SettlementRecord] = []
        self.penalty_events: List[PenaltyRecord] = []
        self.oos_records: List[OOSRecord] = []
        self.forwarding_timeline: List[ForwardingRecord] = []
        self.rollback_attempts: int = 0
        self._balance_snapshots: Dict[str, List[Tuple[float, float, float]]] = {}  # op_pair → [(t, balA, balB)]

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

    def compute_hash_chain_storage_overhead_bytes(self) -> dict:
        """Estimate storage overhead per forwarding event (from forwarding timeline)."""
        entry_size_bytes = 256  # conservative estimate from spec
        total_entries = len(self.forwarding_timeline)
        return {
            "entries": total_entries,
            "bytes_per_entry": entry_size_bytes,
            "total_bytes": total_entries * entry_size_bytes,
        }

    def generate_report(self, total_contact_sec: float = 0.0) -> dict:
        return {
            "jain_fairness_index": self.compute_jain_fairness_index(),
            "free_rider_prevention_rate": self.compute_free_rider_prevention_rate(),
            "settlement_latency": self.compute_settlement_latency_stats(),
            "oos_fraction": self.compute_oos_fraction(total_contact_sec),
            "hash_chain_overhead": self.compute_hash_chain_storage_overhead_bytes(),
            "penalty_events": len(self.penalty_events),
            "settlement_events": len(self.settlement_events),
            "total_forwarding_events": len(self.forwarding_timeline),
            "bytes_forwarded_by": self.bytes_forwarded_by,
            "bytes_received_by": self.bytes_received_by,
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

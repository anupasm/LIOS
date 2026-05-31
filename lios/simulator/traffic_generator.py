"""Synthetic traffic generator for the LIOS protocol emulator.

Traffic model:
  - Poisson arrival process per ground node (configurable λ in flows/sec).
  - Flow size: log-normal distribution (mean ≈ 1 MB, σ = 2 MB), clamped [10 KB, 100 MB].
  - Source: random ground node.
  - Destination satellite: different operator preferred (configurable bias).
  - Path: computed via CGR.
"""
from __future__ import annotations

import math
import random
from dataclasses import dataclass, field
from typing import Dict, List, Optional
from uuid import uuid4

from config import cfg
from contact_plan.window_calculator import ContactPlan
from routing.cgr import CGR, Path


@dataclass
class TrafficFlow:
    flow_id: str
    source_ground_node: str
    src_satellite: str
    dst_satellite: str
    path: Optional[Path]
    size_kb: float
    generated_at: float
    priority: int = 2  # 1 (high) .. 3 (low)
    delivered_at: Optional[float] = None
    dropped: bool = False

    def is_inter_operator(self) -> bool:
        if self.path is None:
            return False
        ops = set(self.path.operator_sequence)
        return len(ops) > 1


class TrafficStats:
    def __init__(self) -> None:
        self.flows_generated: int = 0
        self.flows_no_path: int = 0
        self.total_kb_generated: float = 0.0
        self.hop_counts: List[int] = []
        self.operator_pairs: Dict[str, int] = {}

    def record(self, flow: TrafficFlow) -> None:
        self.flows_generated += 1
        self.total_kb_generated += flow.size_kb
        if flow.path is None:
            self.flows_no_path += 1
        else:
            self.hop_counts.append(len(flow.path.hops))
            ops = flow.path.operator_sequence
            if len(ops) >= 2:
                a, b = sorted([ops[0], ops[-1]])
                pair = f"{a}-{b}"
                self.operator_pairs[pair] = self.operator_pairs.get(pair, 0) + 1

    def summary(self) -> Dict:
        found = self.flows_generated - self.flows_no_path
        return {
            "flows_generated": self.flows_generated,
            "flows_with_path": found,
            "flows_no_path": self.flows_no_path,
            "path_find_rate": found / max(1, self.flows_generated),
            "mean_hops": sum(self.hop_counts) / max(1, len(self.hop_counts)),
            "total_kb_generated": self.total_kb_generated,
            "operator_pair_distribution": self.operator_pairs,
        }


def _lognormal_size_kb(rng: random.Random) -> float:
    """Log-normal size: median ~1 MB, σ=1; clamped by config flow_size_min/max_kb."""
    mu = math.log(1024)   # ln(1 MB in KB)
    sigma = 1.0
    val = rng.lognormvariate(mu, sigma)
    return max(float(cfg.simulation.flow_size_min_kb), min(float(cfg.simulation.flow_size_max_kb), val))


class TrafficGenerator:
    """Generates synthetic traffic flows driven by Poisson arrivals."""

    def __init__(
        self,
        contact_plan: ContactPlan,
        cgr: CGR,
        operator_map: Dict[str, str],
        arrival_rate: float = cfg.simulation.arrival_rate,
        lookahead_sec: float = cfg.simulation.lookahead_sec,
        seed: int = cfg.simulation.random_seed,
    ):
        self._cp = contact_plan
        self._cgr = cgr
        self._op_map = operator_map
        self._rate = arrival_rate
        self._lookahead = lookahead_sec
        self._rng = random.Random(seed)
        self.stats = TrafficStats()
        self._ground_nodes = contact_plan.get_ground_nodes()
        self._all_sats = contact_plan.get_all_satellites()
        # Route cache: (src_sat, dst_sat, time_bucket) → List[Path]
        # 5-min buckets keep routes fresh across contact transitions while
        # amortising Dijkstra cost over the many flows in each window.
        self._route_cache: dict = {}
        self._route_bucket_sec = 300.0
        # Per-GS contact groups: gs_id → {sat_id → [contacts]} (built lazily)
        self._gs_sat_contacts: Dict[str, Dict[str, list]] = {}

    def next_arrival_time(self, t_now: float) -> float:
        """Poisson inter-arrival: exponential distribution."""
        n = max(1, len(self._ground_nodes))
        effective_rate = self._rate * n
        return t_now + self._rng.expovariate(effective_rate)

    def generate_flow(self, t_now: float, priority: int = 2) -> Optional[TrafficFlow]:
        """Generate one traffic flow at simulation time t_now."""
        if not self._ground_nodes or not self._all_sats:
            return None

        # 1. Pick source ground node
        src_gn = self._rng.choice(self._ground_nodes)
        src_op = src_gn.split("-")[0] if "-" in src_gn else "unknown"

        # 2. Reachable satellites from src in lookahead window
        reachable = self._cp.get_reachable_satellites(src_gn, t_now, t_now + self._lookahead)
        if not reachable:
            flow = TrafficFlow(
                flow_id=str(uuid4()),
                source_ground_node=src_gn,
                src_satellite="",
                dst_satellite="",
                path=None,
                size_kb=_lognormal_size_kb(self._rng),
                generated_at=t_now,
                priority=priority,
            )
            self.stats.record(flow)
            return flow

        # Weight selection by upcoming contact duration.
        # Build per-GS sat-contact map lazily and reuse across flows.
        if src_gn not in self._gs_sat_contacts:
            gsc: Dict[str, list] = {}
            for c in self._cp.get_contacts_for_node(src_gn):
                peer = c.to_node if c.from_node == src_gn else c.from_node
                gsc.setdefault(peer, []).append(c)
            self._gs_sat_contacts[src_gn] = gsc
        sat_contacts = self._gs_sat_contacts[src_gn]

        weights = []
        t_end_look = t_now + self._lookahead
        for sat in reachable:
            duration = sum(
                c.duration_sec
                for c in sat_contacts.get(sat, [])
                if c.start_time_sec <= t_end_look and c.end_time_sec >= t_now
            )
            weights.append(max(duration, 1.0))
        total = sum(weights)
        r = self._rng.random() * total
        cumulative = 0.0
        src_sat = reachable[-1]
        for sat, w in zip(reachable, weights):
            cumulative += w
            if r <= cumulative:
                src_sat = sat
                break

        # 3. Pick destination satellite — always a different operator.
        # Same-operator flows produce no LIOS settlement events.
        src_sat_op = self._op_map.get(src_sat, src_op)
        other_op_sats = [s for s in self._all_sats if self._op_map.get(s, "") != src_sat_op and s != src_sat]
        if not other_op_sats:
            return None
        dst_sat = self._rng.choice(other_op_sats)

        # 4. Compute path via CGR — cached by 5-min bucket so Dijkstra runs
        #    once per (src, dst, bucket) instead of once per flow.
        bucket = int(t_now / self._route_bucket_sec) * self._route_bucket_sec
        cache_key = (src_sat, dst_sat, bucket)
        if cache_key not in self._route_cache:
            self._route_cache[cache_key] = self._cgr.route(src_sat, dst_sat, bucket, k=1, max_hops=5)
        paths = self._route_cache[cache_key]
        path = paths[0] if paths else None

        flow = TrafficFlow(
            flow_id=str(uuid4()),
            source_ground_node=src_gn,
            src_satellite=src_sat,
            dst_satellite=dst_sat,
            path=path,
            size_kb=_lognormal_size_kb(self._rng),
            generated_at=t_now,
            priority=priority,
        )
        self.stats.record(flow)
        return flow

    def generate_poisson_schedule(
        self, t_start: float, t_end: float
    ) -> List[tuple[float, TrafficFlow]]:
        """Pre-generate all flows for a simulation window. Returns (arrival_time, flow) pairs.

        Flows are only generated during active ISL contact windows — arrivals outside
        any ISL contact would be dropped by the satellite node anyway.
        """
        import sys

        # Collect ISL windows that overlap [t_start, t_end]
        raw: List[tuple[float, float]] = sorted(
            (max(c.start_time_sec, t_start), min(c.end_time_sec, t_end))
            for c in self._cp.get_isl_contacts()
            if c.start_time_sec < t_end and c.end_time_sec > t_start
        )
        # Merge overlapping windows into disjoint intervals
        merged: List[tuple[float, float]] = []
        for ws, we in raw:
            if merged and ws <= merged[-1][1]:
                merged[-1] = (merged[-1][0], max(merged[-1][1], we))
            else:
                merged.append((ws, we))

        total_active = sum(we - ws for ws, we in merged)
        n = max(1, len(self._ground_nodes))
        effective_rate = self._rate * n
        schedule: List[tuple[float, TrafficFlow]] = []

        elapsed_active = 0.0
        _report_every = max(1.0, total_active / 20)
        _next_report = _report_every

        for win_start, win_end in merged:
            t = win_start + self._rng.expovariate(effective_rate)
            while t < win_end:
                flow = self.generate_flow(t)
                if flow is not None:
                    schedule.append((t, flow))
                t += self._rng.expovariate(effective_rate)

            elapsed_active += win_end - win_start
            if elapsed_active >= _next_report:
                pct = int(elapsed_active * 100 / max(1.0, total_active))
                print(
                    f"  Traffic gen: {pct:3d}%  {len(schedule)} flows so far   ",
                    end="\r", flush=True,
                )
                _next_report += _report_every

        print(f"  Traffic gen: done  {len(schedule)} flows scheduled                ")
        schedule.sort(key=lambda x: x[0])
        return schedule

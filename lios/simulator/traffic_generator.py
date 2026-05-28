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
                pair = f"{ops[0]}-{ops[-1]}"
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
    """Log-normal size: median ~1 MB, σ=2 MB; clamped [10 KB, 100_000 KB]."""
    mu = math.log(1024)   # ln(1 MB in KB)
    sigma = 1.0
    val = rng.lognormvariate(mu, sigma)
    return max(10.0, min(100_000.0, val))


class TrafficGenerator:
    """Generates synthetic traffic flows driven by Poisson arrivals."""

    def __init__(
        self,
        contact_plan: ContactPlan,
        cgr: CGR,
        operator_map: Dict[str, str],
        arrival_rate: float = cfg.simulation.arrival_rate,
        lookahead_sec: float = cfg.simulation.lookahead_sec,
        cross_operator_bias: float = cfg.simulation.cross_operator_bias,
        seed: int = cfg.simulation.random_seed,
    ):
        self._cp = contact_plan
        self._cgr = cgr
        self._op_map = operator_map
        self._rate = arrival_rate
        self._lookahead = lookahead_sec
        self._bias = cross_operator_bias
        self._rng = random.Random(seed)
        self.stats = TrafficStats()
        self._ground_nodes = contact_plan.get_ground_nodes()
        self._all_sats = contact_plan.get_all_satellites()

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

        # Weight selection by upcoming contact duration (longer contact = more capacity available)
        weights = []
        for sat in reachable:
            duration = sum(
                c.duration_sec
                for c in self._cp.get_contacts_for_node(src_gn)
                if (c.from_node == sat or c.to_node == sat)
                and c.start_time_sec <= t_now + self._lookahead
                and c.end_time_sec >= t_now
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

        # 3. Pick destination satellite (prefer different operator)
        src_sat_op = self._op_map.get(src_sat, src_op)
        other_op_sats = [s for s in self._all_sats if self._op_map.get(s, "") != src_sat_op and s != src_sat]
        same_op_sats = [s for s in self._all_sats if self._op_map.get(s, "") == src_sat_op and s != src_sat]

        if other_op_sats and (not same_op_sats or self._rng.random() < self._bias):
            dst_sat = self._rng.choice(other_op_sats)
        elif same_op_sats:
            dst_sat = self._rng.choice(same_op_sats)
        else:
            dst_sat = self._rng.choice([s for s in self._all_sats if s != src_sat])

        # 4. Compute path via CGR
        paths = self._cgr.route(src_sat, dst_sat, t_now, k=1)
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
        """Pre-generate all flows for a simulation window. Returns (arrival_time, flow) pairs."""
        schedule: List[tuple[float, TrafficFlow]] = []
        t = self.next_arrival_time(t_start)
        while t < t_end:
            flow = self.generate_flow(t)
            if flow is not None:
                schedule.append((t, flow))
            t = self.next_arrival_time(t)
        return schedule

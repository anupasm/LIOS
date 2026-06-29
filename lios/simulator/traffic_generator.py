"""Synthetic direct-pair traffic generator for the LIOS emulator.

Traffic model:
  - Global Poisson arrival process (configurable λ in flows/second).
  - Each arrival is allocated to a cross-operator ISL pair active at that time.
  - Optional source-operator weights skew pair and direction selection.
  - ``traffic_direction_bias`` remains the canonical A→B/B→A prior.
  - Flow size is log-normal and clamped by the configured channel limits.

This workload intentionally does not perform end-to-end route calculation.
It isolates LIOS bilateral forwarding and settlement behavior.
"""
from __future__ import annotations

import math
import random
from dataclasses import dataclass
from typing import Dict, List, Mapping, Optional
from uuid import UUID

from config import cfg
from contact_plan.window_calculator import Contact, ContactPlan


@dataclass
class TrafficFlow:
    flow_id: str
    src_satellite: str
    dst_satellite: str
    contact_id: str
    pair_id: str
    size_kb: float
    generated_at: float
    priority: int = 2
    delivered_at: Optional[float] = None
    dropped: bool = False

    def is_inter_operator(self) -> bool:
        return self.src_satellite.split("-", 1)[0] != self.dst_satellite.split("-", 1)[0]


class TrafficStats:
    def __init__(self) -> None:
        self.flows_generated: int = 0
        self.flows_no_active_pair: int = 0
        self.total_kb_generated: float = 0.0
        self.operator_pairs: Dict[str, int] = {}
        self.source_operators: Dict[str, int] = {}
        self.satellite_pairs: Dict[str, int] = {}

    def record(self, flow: TrafficFlow) -> None:
        self.flows_generated += 1
        self.total_kb_generated += flow.size_kb
        src_op = flow.src_satellite.split("-", 1)[0]
        dst_op = flow.dst_satellite.split("-", 1)[0]
        op_pair = "-".join(sorted([src_op, dst_op]))
        self.operator_pairs[op_pair] = self.operator_pairs.get(op_pair, 0) + 1
        self.source_operators[src_op] = self.source_operators.get(src_op, 0) + 1
        self.satellite_pairs[flow.pair_id] = self.satellite_pairs.get(flow.pair_id, 0) + 1

    def summary(self) -> Dict:
        return {
            "flows_generated": self.flows_generated,
            "flows_allocated": self.flows_generated,
            "flows_no_active_pair": self.flows_no_active_pair,
            "allocation_rate": (
                self.flows_generated
                / max(1, self.flows_generated + self.flows_no_active_pair)
            ),
            "mean_hops": 1.0 if self.flows_generated else 0.0,
            "total_kb_generated": self.total_kb_generated,
            "operator_pair_distribution": self.operator_pairs,
            "source_operator_distribution": self.source_operators,
            "satellite_pair_distribution": self.satellite_pairs,
        }


def _lognormal_size_kb(rng: random.Random) -> float:
    """Log-normal size with 1 MB median, clamped to configured bounds."""
    val = rng.lognormvariate(math.log(1024), 1.0)
    return max(
        float(cfg.simulation.flow_size_min_kb),
        min(float(cfg.simulation.flow_size_max_kb), val),
    )


class TrafficGenerator:
    """Allocate Poisson traffic directly to active cross-operator ISL pairs."""

    def __init__(
        self,
        contact_plan: ContactPlan,
        operator_map: Dict[str, str],
        arrival_rate: float = cfg.simulation.arrival_rate,
        seed: int = cfg.simulation.random_seed,
        allocation: str = cfg.simulation.traffic_allocation,
        direction_bias: float = cfg.simulation.traffic_direction_bias,
        operator_load_weights: Optional[Mapping[str, float]] = None,
    ) -> None:
        if arrival_rate <= 0:
            raise ValueError("arrival_rate must be greater than zero")
        if allocation != "uniform_active_pair":
            raise ValueError(f"unsupported traffic allocation policy: {allocation}")
        if not 0.0 <= direction_bias <= 1.0:
            raise ValueError("direction_bias must be between 0.0 and 1.0")
        load_weights = dict(
            cfg.simulation.operator_load_weights
            if operator_load_weights is None
            else operator_load_weights
        )
        if any(
            not isinstance(weight, (int, float))
            or not math.isfinite(weight)
            or weight < 0
            for weight in load_weights.values()
        ):
            raise ValueError(
                "operator_load_weights values must be finite and non-negative"
            )
        if load_weights and not any(weight > 0 for weight in load_weights.values()):
            raise ValueError(
                "operator_load_weights must contain at least one positive value"
            )

        self._cp = contact_plan
        self._op_map = operator_map
        self._rate = arrival_rate
        self._direction_bias = direction_bias
        self._operator_load_weights = load_weights
        self._rng = random.Random(seed)
        self.stats = TrafficStats()

        self._cross_operator_contacts = sorted(
            (
                c
                for c in contact_plan.get_isl_contacts()
                if self._operator(c.from_node) != self._operator(c.to_node)
            ),
            key=lambda c: (c.start_time_sec, c.end_time_sec, c.contact_id),
        )
        effective_weights = {
            self._load_weight(satellite)
            for contact in self._cross_operator_contacts
            for satellite in (contact.from_node, contact.to_node)
        }
        self._uniform_load_weights = len(effective_weights) <= 1

        # Time-bucket candidate index. Exact contact bounds are checked at lookup.
        self._bucket_sec = 300.0
        self._contacts_by_bucket: Dict[int, List[Contact]] = {}
        for contact in self._cross_operator_contacts:
            first = int(contact.start_time_sec // self._bucket_sec)
            last = int(max(contact.start_time_sec, contact.end_time_sec - 1e-9) // self._bucket_sec)
            for bucket in range(first, last + 1):
                self._contacts_by_bucket.setdefault(bucket, []).append(contact)

        print(
            "  [TrafficGenerator] "
            f"allocation={allocation}, global_rate={arrival_rate}, "
            f"direction_bias={direction_bias}, "
            f"operator_load_weights={self._operator_load_weights or 'uniform'}, "
            f"cross_operator_contacts={len(self._cross_operator_contacts)}"
        )

    def _operator(self, satellite_id: str) -> str:
        return self._op_map.get(
            satellite_id,
            satellite_id.split("-", 1)[0] if "-" in satellite_id else "unknown",
        )

    def next_arrival_time(self, t_now: float) -> float:
        return t_now + self._rng.expovariate(self._rate)

    def _load_weight(self, satellite_id: str) -> float:
        return float(self._operator_load_weights.get(self._operator(satellite_id), 1.0))

    def _active_pair_contacts(self, t_now: float) -> List[Contact]:
        candidates = self._contacts_by_bucket.get(int(t_now // self._bucket_sec), [])
        # Deduplicate a pair if overlapping contact records exist. Stable contact-ID
        # selection keeps seeded runs deterministic.
        by_pair: Dict[str, Contact] = {}
        for contact in candidates:
            if not (contact.start_time_sec <= t_now < contact.end_time_sec):
                continue
            pair_id = "__".join(sorted([contact.from_node, contact.to_node]))
            previous = by_pair.get(pair_id)
            if previous is None or contact.contact_id < previous.contact_id:
                by_pair[pair_id] = contact
        return [by_pair[pair] for pair in sorted(by_pair)]

    def generate_flow(self, t_now: float, priority: int = 2) -> Optional[TrafficFlow]:
        active = self._active_pair_contacts(t_now)
        if not active:
            self.stats.flows_no_active_pair += 1
            return None

        # Retain the original O(1) pair selection and seeded sequence when all
        # effective operator weights are equal.
        if self._uniform_load_weights:
            contact = self._rng.choice(active)
            sat_a, sat_b = sorted([contact.from_node, contact.to_node])
            if self._rng.random() < self._direction_bias:
                src_sat, dst_sat = sat_a, sat_b
            else:
                src_sat, dst_sat = sat_b, sat_a
        else:
            contact, src_sat, dst_sat = self._choose_weighted_direction(active)
            if contact is None:
                self.stats.flows_no_active_pair += 1
                return None
            sat_a, sat_b = sorted([contact.from_node, contact.to_node])

        flow = TrafficFlow(
            flow_id=str(UUID(int=self._rng.getrandbits(128))),
            src_satellite=src_sat,
            dst_satellite=dst_sat,
            contact_id=contact.contact_id,
            pair_id=f"{sat_a}__{sat_b}",
            size_kb=_lognormal_size_kb(self._rng),
            generated_at=t_now,
            priority=priority,
        )
        self.stats.record(flow)
        return flow

    def _choose_weighted_direction(
        self, active: List[Contact]
    ) -> tuple[Optional[Contact], str, str]:
        """Sample an active pair and source direction using operator weights."""
        # A directed candidate combines the configured operator source weight
        # with the existing canonical direction prior. With equal operator
        # weights every active pair has equal total weight and the old
        # uniform-pair/direction behavior is preserved.
        directed: List[tuple[Contact, str, str]] = []
        weights: List[float] = []
        for candidate in active:
            sat_a, sat_b = sorted([candidate.from_node, candidate.to_node])
            directed.extend(
                [(candidate, sat_a, sat_b), (candidate, sat_b, sat_a)]
            )
            weights.extend(
                [
                    self._load_weight(sat_a) * self._direction_bias,
                    self._load_weight(sat_b) * (1.0 - self._direction_bias),
                ]
            )

        if not any(weight > 0 for weight in weights):
            return None, "", ""

        contact, src_sat, dst_sat = self._rng.choices(
            directed, weights=weights, k=1
        )[0]
        return contact, src_sat, dst_sat

    def generate_poisson_schedule(
        self, t_start: float, t_end: float
    ) -> List[tuple[float, TrafficFlow]]:
        """Generate direct-pair flows during periods with any eligible ISL."""
        import time as _time

        started = _time.perf_counter()
        raw = sorted(
            (max(c.start_time_sec, t_start), min(c.end_time_sec, t_end))
            for c in self._cross_operator_contacts
            if c.start_time_sec < t_end and c.end_time_sec > t_start
        )
        merged: List[tuple[float, float]] = []
        for window_start, window_end in raw:
            if merged and window_start <= merged[-1][1]:
                merged[-1] = (merged[-1][0], max(merged[-1][1], window_end))
            else:
                merged.append((window_start, window_end))

        total_active = sum(end - start for start, end in merged)
        expected = int(self._rate * total_active)
        print(
            f"  [tgen] {len(merged)} active cross-operator windows, "
            f"{total_active:.0f}s active, global_rate={self._rate:.3f} flows/s, "
            f"~{expected:,} expected flows",
            flush=True,
        )

        schedule: List[tuple[float, TrafficFlow]] = []
        for window_start, window_end in merged:
            t = self.next_arrival_time(window_start)
            while t < window_end:
                flow = self.generate_flow(t)
                if flow is not None:
                    schedule.append((t, flow))
                t = self.next_arrival_time(t)

        schedule.sort(key=lambda item: item[0])
        print(
            f"  [tgen] done: {len(schedule):,} allocated flows in "
            f"{_time.perf_counter() - started:.1f}s",
            flush=True,
        )
        return schedule

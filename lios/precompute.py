"""Local cache helpers for run_experiments.py.

Provides load/save for contact plans and traffic schedules so that
repeated experiment runs skip expensive recomputation.

File layout under cache_dir:
  cp_{dur}s_step{step}_range{rng:.0f}.csv
  tf_pair_v1_d{bias}_{dur}s_s{seed}_r{rate:.6f}_step{step}_range{rng:.0f}.json
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import List, Optional, Tuple

from config import cfg


# ── Path helpers ──────────────────────────────────────────────────────────────

def _cp_path(cache_dir: Path, duration_sec: int, step: int, rng: float) -> Path:
    return cache_dir / f"cp_{duration_sec}s_step{step}_range{rng:.0f}.csv"


def _tf_path(cache_dir: Path, duration_sec: int, seed: int,
             rate: float, step: int, rng: float) -> Path:
    bias = cfg.simulation.traffic_direction_bias
    return cache_dir / (
        f"tf_pair_v1_d{bias:.3f}_{duration_sec}s_s{seed}_r{rate:.6f}_"
        f"step{step}_range{rng:.0f}.json"
    )


def _flow_to_dict(t: float, flow) -> dict:
    return {
        "t": t,
        "flow_id": flow.flow_id,
        "src_satellite": flow.src_satellite,
        "dst_satellite": flow.dst_satellite,
        "contact_id": flow.contact_id,
        "pair_id": flow.pair_id,
        "size_kb": flow.size_kb,
        "generated_at": flow.generated_at,
        "priority": flow.priority,
    }


def _dict_to_flow(rec: dict):
    from simulator.traffic_generator import TrafficFlow
    flow = TrafficFlow(
        flow_id=rec["flow_id"],
        src_satellite=rec["src_satellite"],
        dst_satellite=rec["dst_satellite"],
        contact_id=rec["contact_id"],
        pair_id=rec["pair_id"],
        size_kb=rec["size_kb"],
        generated_at=rec["generated_at"],
        priority=rec["priority"],
    )
    return rec["t"], flow


# ── Contact plan cache ────────────────────────────────────────────────────────

def load_contact_plan(cache_dir: Path, duration_sec: int,
                      time_step_sec: int, isl_range_km: float):
    """Return cached ContactPlan or None on cache miss.

    Checks two filename conventions:
      1. Local format:  cp_{dur}s_step{step}_range{rng:.0f}.csv
      2. HPC format:    cp_24h_step{step}_range{rng:.0f}.csv  (superset, covers any duration)
    """
    from contact_plan.window_calculator import ContactPlan
    from datetime import datetime, timezone

    candidates = [
        _cp_path(cache_dir, duration_sec, time_step_sec, isl_range_km),
        cache_dir / f"cp_24h_step{time_step_sec}_range{isl_range_km:.0f}.csv",
    ]
    p = next((c for c in candidates if c.exists()), None)
    if p is None:
        return None
    epoch = datetime(2025, 11, 19, 0, 15, 0, tzinfo=timezone.utc)
    cp = ContactPlan.from_csv(p, epoch)
    print(f"  [cache] contact plan loaded: {p.name} ({len(cp.contacts)} contacts)")
    return cp


def save_contact_plan(cache_dir: Path, cp,
                      duration_sec: int, time_step_sec: int, isl_range_km: float) -> None:
    cache_dir.mkdir(parents=True, exist_ok=True)
    p = _cp_path(cache_dir, duration_sec, time_step_sec, isl_range_km)
    cp.to_csv(p)
    print(f"  [cache] contact plan saved: {p.name}")


# ── Traffic schedule cache ────────────────────────────────────────────────────

def load_traffic_schedule(cache_dir: Path, seed: int, rate: float,
                          time_step_sec: int, isl_range_km: float,
                          duration_sec: int) -> Optional[List[Tuple]]:
    """Return cached schedule as list of (t, TrafficFlow) or None on cache miss.

    Checks two filename conventions:
      1. Local pair-allocation format for the requested duration.
      2. HPC pair-allocation format covering the full precomputed duration.
         (superset — flows are filtered to [0, duration_sec])
    """
    candidates = [
        _tf_path(cache_dir, duration_sec, seed, rate, time_step_sec, isl_range_km),
        cache_dir / (
            f"tf_pair_v1_d{cfg.simulation.traffic_direction_bias:.3f}_24h_"
            f"s{seed}_r{rate:.6f}_step{time_step_sec}_range{isl_range_km:.0f}.json"
        ),
    ]
    p = next((c for c in candidates if c.exists()), None)
    if p is None:
        return None
    with p.open() as f:
        records = json.load(f)
    schedule = [_dict_to_flow(r) for r in records if r["t"] <= duration_sec]
    print(f"  [cache] traffic schedule loaded: {p.name} ({len(schedule)} flows ≤ {duration_sec}s)")
    return schedule


def save_traffic_schedule(cache_dir: Path, schedule: List[Tuple],
                          seed: int, rate: float, time_step_sec: int,
                          isl_range_km: float, duration_sec: int) -> None:
    cache_dir.mkdir(parents=True, exist_ok=True)
    p = _tf_path(cache_dir, duration_sec, seed, rate, time_step_sec, isl_range_km)
    records = [_flow_to_dict(t, flow) for t, flow in schedule]
    with p.open("w") as f:
        json.dump(records, f, separators=(",", ":"))
    print(f"  [cache] traffic schedule saved: {p.name} ({len(records)} flows)")

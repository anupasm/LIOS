"""Local cache helpers for run_experiments.py.

Provides load/save for contact plans and traffic schedules so that
repeated experiment runs skip expensive recomputation.

File layout under cache_dir:
  cp_{dur}s_step{step}_range{rng:.0f}.csv
  tf_{dur}s_s{seed}_r{rate:.6f}_step{step}_range{rng:.0f}.json
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import List, Optional, Tuple

MIN_CONTACT_DURATION_SEC = 600


# ── Path helpers ──────────────────────────────────────────────────────────────

def _cp_path(cache_dir: Path, duration_sec: int, step: int, rng: float) -> Path:
    return cache_dir / f"cp_{duration_sec}s_step{step}_range{rng:.0f}.csv"


def _tf_path(cache_dir: Path, duration_sec: int, seed: int,
             rate: float, step: int, rng: float) -> Path:
    return cache_dir / f"tf_{duration_sec}s_s{seed}_r{rate:.6f}_step{step}_range{rng:.0f}.json"


# ── Serialisation helpers ─────────────────────────────────────────────────────

def _path_to_dict(p) -> dict:
    return {
        "hops": p.hops,
        "contacts": p.contacts,
        "latency_sec": p.latency_sec,
        "arrival_time_sec": p.arrival_time_sec,
        "operator_sequence": p.operator_sequence,
        "inter_operator_hops": p.inter_operator_hops,
    }


def _dict_to_path(d: dict):
    from routing.cgr import Path as CGRPath
    return CGRPath(
        hops=d["hops"],
        contacts=d["contacts"],
        latency_sec=d["latency_sec"],
        arrival_time_sec=d["arrival_time_sec"],
        operator_sequence=d["operator_sequence"],
        inter_operator_hops=d["inter_operator_hops"],
    )


def _flow_to_dict(t: float, flow) -> dict:
    return {
        "t": t,
        "flow_id": flow.flow_id,
        "source_ground_node": flow.source_ground_node,
        "src_satellite": flow.src_satellite,
        "dst_satellite": flow.dst_satellite,
        "size_kb": flow.size_kb,
        "generated_at": flow.generated_at,
        "priority": flow.priority,
        "path": _path_to_dict(flow.path) if flow.path else None,
    }


def _dict_to_flow(rec: dict):
    from simulator.traffic_generator import TrafficFlow
    path = _dict_to_path(rec["path"]) if rec.get("path") else None
    flow = TrafficFlow(
        flow_id=rec["flow_id"],
        source_ground_node=rec["source_ground_node"],
        src_satellite=rec["src_satellite"],
        dst_satellite=rec["dst_satellite"],
        path=path,
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
      1. Local format: tf_{dur}s_s{seed}_r{rate:.6f}_step{step}_range{rng:.0f}.json
      2. HPC format:   tf_24h_s{seed}_r{rate:.6f}_step{step}_range{rng:.0f}.json
         (superset — flows are filtered to [0, duration_sec])
    """
    candidates = [
        _tf_path(cache_dir, duration_sec, seed, rate, time_step_sec, isl_range_km),
        cache_dir / f"tf_24h_s{seed}_r{rate:.6f}_step{time_step_sec}_range{isl_range_km:.0f}.json",
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


# ── SQLite route table CGR ────────────────────────────────────────────────────

ROUTE_BUCKET_SEC = 600


class SqliteRouteTableCGR:
    """Drop-in CGR backed by the pre-computed SQLite route table.

    Opens a read-only connection per instance so multiple experiments
    can run concurrently without locking.
    """

    def __init__(self, db_path: str):
        import sqlite3
        self._conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        self._conn.execute("PRAGMA query_only=ON")

    def route(self, src: str, dst: str, t_start: float,
              k: int = 3, max_hops: int = 5):
        import sqlite3, json as _json
        from routing.cgr import Path as CGRPath
        bucket = int(t_start / ROUTE_BUCKET_SEC) * ROUTE_BUCKET_SEC
        key = f"{src}|{dst}|{bucket:.0f}"
        row = self._conn.execute(
            "SELECT path FROM routes WHERE key=?", (key,)
        ).fetchone()
        if row is None:
            return []
        d = _json.loads(row[0])
        return [CGRPath(
            hops=d["hops"], contacts=d["contacts"],
            latency_sec=d["latency_sec"], arrival_time_sec=d["arrival_time_sec"],
            operator_sequence=d["operator_sequence"],
            inter_operator_hops=d["inter_operator_hops"],
        )]

    def route_with_gs(self, *args, **kwargs):
        return []


def load_sqlite_cgr(cache_dir: Path, time_step_sec: int,
                    isl_range_km: float) -> Optional[SqliteRouteTableCGR]:
    """Return a SqliteRouteTableCGR if the pre-computed DB exists, else None."""
    p = cache_dir / f"rt_24h_step{time_step_sec}_range{isl_range_km:.0f}.sqlite"
    if not p.exists():
        return None
    size_mb = p.stat().st_size // (1024 * 1024)
    print(f"  [cache] SQLite route table: {p.name} ({size_mb} MB)")
    return SqliteRouteTableCGR(str(p))


# ── Contact filter ────────────────────────────────────────────────────────────

def filter_routing_contacts(cp):
    """Return a new ContactPlan keeping only contacts long enough for routing."""
    from contact_plan.window_calculator import ContactPlan
    routing_cp = ContactPlan(epoch=cp.epoch)
    routing_cp.contacts = [
        c for c in cp.contacts if c.duration_sec >= MIN_CONTACT_DURATION_SEC
    ]
    return routing_cp

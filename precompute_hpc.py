#!/usr/bin/env python3
"""High-performance LIOS precomputation for multi-core machines.

Produces three artifacts per (time_step_sec, isl_range_km) variant:
  1. Contact plan  →  {cache}/cp_24h_step{N}_range{R}.csv
  2. Route table   →  {cache}/rt_24h_step{N}_range{R}.json.gz
  3. Traffic sched →  {cache}/tf_24h_s{S}_r{R:.6f}_step{N}_range{R}.json

Key improvements over lios/precompute.py:
  - --workers saturates all cores (default: all available CPUs).
  - Route table pre-computes every (src, dst, 5-min bucket) CGR path in parallel,
    so traffic workers do a single dict lookup instead of running Dijkstra per flow.
  - ProcessPoolExecutor with fine-grained chunks enables work-stealing across cores.
  - Multiple traffic variants (different seeds/rates) run in parallel.
  - All phases are independently skippable and cache-aware.

Usage (from repo root):
  python precompute_hpc.py
  python precompute_hpc.py --workers 64
  python precompute_hpc.py --step 30 --range 2000 --seeds 42,43,44 --rates 0.005,0.01
  python precompute_hpc.py --force-routes --force-traffic
  python precompute_hpc.py --skip-routes --skip-traffic   # contact plan only
"""
from __future__ import annotations

import argparse
import json
import multiprocessing as mp
import os
import sqlite3
import sys
import time
from concurrent.futures import FIRST_COMPLETED, ProcessPoolExecutor, as_completed, wait

# Prevent BLAS/OpenBLAS from spawning extra threads inside each worker
# process — parallelism is handled at the process level already.
for _blas_var in (
    "OMP_NUM_THREADS",
    "OPENBLAS_NUM_THREADS",
    "MKL_NUM_THREADS",
    "BLAS_NUM_THREADS",
    "VECLIB_MAXIMUM_THREADS",
    "NUMEXPR_NUM_THREADS",
):
    os.environ.setdefault(_blas_var, "1")
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

# Make lios/ importable regardless of working directory
_LIOS_DIR = Path(__file__).parent / "lios"
sys.path.insert(0, str(_LIOS_DIR))

from contact_plan.gs_loader import GSLoader
from contact_plan.tle_loader import TLELoader
from contact_plan.window_calculator import Contact, ContactPlan, WindowCalculator
from config import cfg
from routing.cgr import CGR, Path as CGRPath
from simulator.traffic_generator import TrafficFlow, TrafficGenerator

EPOCH = datetime(2025, 11, 19, 0, 15, 0, tzinfo=timezone.utc)
DURATION_SEC = 21600
MIN_CONTACT_DURATION_SEC = 600
ROUTE_BUCKET_SEC = 600  # 10-min buckets — halves route count vs 5-min with negligible accuracy loss


def _ts() -> str:
    return datetime.now(timezone.utc).strftime("%H:%M:%S")


# ── Cache paths ───────────────────────────────────────────────────────────────

def _cp_path(cache: Path, step: int, rng: float) -> Path:
    return cache / f"cp_24h_step{step}_range{rng:.0f}.csv"

def _cp_meta_path(cache: Path, step: int, rng: float) -> Path:
    return cache / f"cp_24h_step{step}_range{rng:.0f}_meta.json"

def _rt_path(cache: Path, step: int, rng: float) -> Path:
    return cache / f"rt_24h_step{step}_range{rng:.0f}.sqlite"

def _tf_path(cache: Path, seed: int, rate: float, step: int, rng: float) -> Path:
    return cache / f"tf_24h_s{seed}_r{rate:.6f}_step{step}_range{rng:.0f}.json"


# ── Serialisation helpers ─────────────────────────────────────────────────────

def _path_to_dict(p: CGRPath) -> dict:
    return {
        "hops": p.hops,
        "contacts": p.contacts,
        "latency_sec": p.latency_sec,
        "arrival_time_sec": p.arrival_time_sec,
        "operator_sequence": p.operator_sequence,
        "inter_operator_hops": p.inter_operator_hops,
    }

def _dict_to_path(d: dict) -> CGRPath:
    return CGRPath(
        hops=d["hops"],
        contacts=d["contacts"],
        latency_sec=d["latency_sec"],
        arrival_time_sec=d["arrival_time_sec"],
        operator_sequence=d["operator_sequence"],
        inter_operator_hops=d["inter_operator_hops"],
    )

def _flow_to_dict(t: float, flow: TrafficFlow) -> dict:
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

def _dict_to_flow(rec: dict) -> Tuple[float, TrafficFlow]:
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


# ── Phase 1: Contact plan ─────────────────────────────────────────────────────

def compute_contact_plan(
    cache: Path,
    operators_tles: dict,
    ground_stations: dict,
    step: int,
    isl_range: float,
    workers: int,
    force: bool = False,
) -> ContactPlan:
    cp_file   = _cp_path(cache, step, isl_range)
    meta_file = _cp_meta_path(cache, step, isl_range)

    if not force and cp_file.exists() and meta_file.exists():
        print(f"  [{_ts()}] [cached] contact plan: {cp_file.name}")
        cp = ContactPlan.from_csv(cp_file, EPOCH)
        n_isl = sum(1 for c in cp.contacts if c.node_type_from == "SAT")
        n_gs  = len(cp.contacts) - n_isl
        print(f"           {len(cp.contacts)} contacts ({n_isl} ISL, {n_gs} GS-sat)")
        return cp

    n_sats = sum(len(v) for v in operators_tles.values())
    n_steps = DURATION_SEC // step + 1
    print(f"  [{_ts()}] Computing contact plan: {n_sats} sats × {n_steps} steps "
          f"(step={step}s, range={isl_range:.0f}km, workers={workers})")

    t_end = EPOCH + timedelta(seconds=DURATION_SEC)
    calc  = WindowCalculator(EPOCH, t_end, step, isl_range, checkpoint_dir=cache)
    t0    = time.perf_counter()
    cp    = calc.compute(operators_tles, ground_stations, n_workers=workers)
    elapsed = time.perf_counter() - t0

    cp.to_csv(cp_file)
    meta_file.write_text(json.dumps({
        "epoch": EPOCH.isoformat(),
        "duration_sec": DURATION_SEC,
        "time_step_sec": step,
        "isl_max_range_km": isl_range,
        "n_contacts": len(cp.contacts),
    }, indent=2))
    n_isl = sum(1 for c in cp.contacts if c.node_type_from == "SAT")
    n_gs_c = len(cp.contacts) - n_isl
    print(f"  [{_ts()}] Contact plan done: {len(cp.contacts)} contacts "
          f"({n_isl} ISL, {n_gs_c} GS-sat) in {elapsed:.1f}s → {cp_file.name}")
    return cp


# ── Phase 2: Route table ──────────────────────────────────────────────────────

def _route_chunk_worker(args: tuple) -> List[Tuple[str, Optional[dict]]]:
    """Compute CGR paths for a chunk of (src, dst, bucket) triples.

    Reconstructs its own ContactPlan and CGR to avoid pickling issues.
    Returns list of (key, path_dict|None) for each triple in the chunk.
    """
    cp_csv, epoch_iso, sat_op_map, chunk = args

    for _v in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS",
               "BLAS_NUM_THREADS", "VECLIB_MAXIMUM_THREADS", "NUMEXPR_NUM_THREADS"):
        os.environ.setdefault(_v, "1")

    lios_dir = str(Path(__file__).parent / "lios")
    if lios_dir not in sys.path:
        sys.path.insert(0, lios_dir)

    from contact_plan.window_calculator import ContactPlan as _CP
    from routing.cgr import CGR as _CGR

    cp = _CP.from_csv(Path(cp_csv), datetime.fromisoformat(epoch_iso))
    cp.contacts = [c for c in cp.contacts if c.duration_sec >= MIN_CONTACT_DURATION_SEC]
    cgr = _CGR(cp, operator_map=sat_op_map)

    results: List[Tuple[str, Optional[dict]]] = []
    for src, dst, bucket in chunk:
        paths = cgr.route(src, dst, bucket, k=1, max_hops=5)
        key = f"{src}|{dst}|{bucket:.0f}"
        results.append((key, _path_to_dict(paths[0]) if paths else None))
    return results


def compute_route_table(
    cache: Path,
    cp: ContactPlan,
    sat_op_map: Dict[str, str],
    step: int,
    isl_range: float,
    workers: int,
    force: bool = False,
) -> Path:
    rt_file = _rt_path(cache, step, isl_range)
    if not force and rt_file.exists():
        size_kb = rt_file.stat().st_size // 1024
        print(f"  [{_ts()}] [cached] route table: {rt_file.name} ({size_kb} KB)")
        return rt_file

    # Build inter-operator sat pairs (both directions)
    operators: Dict[str, List[str]] = {}
    for sat_id, op in sat_op_map.items():
        operators.setdefault(op, []).append(sat_id)

    op_names = sorted(operators)
    pairs: List[Tuple[str, str]] = []
    for i, op_a in enumerate(op_names):
        for op_b in op_names[i + 1:]:
            for sa in operators[op_a]:
                for sb in operators[op_b]:
                    pairs.append((sa, sb))
                    pairs.append((sb, sa))

    n_inter_pairs = len(pairs)

    # Add same-operator pairs (needed for intra-op routing)
    for op, sats in operators.items():
        for i, sa in enumerate(sats):
            for sb in sats[i + 1:]:
                pairs.append((sa, sb))
                pairs.append((sb, sa))

    n_intra_pairs = len(pairs) - n_inter_pairs
    op_summary = ", ".join(f"{op}:{len(sats)}" for op, sats in sorted(operators.items()))
    print(f"  [{_ts()}] Operators: {op_summary}")
    print(f"  [{_ts()}] Pairs: {n_inter_pairs:,} inter-op + {n_intra_pairs:,} intra-op = {len(pairs):,} total")

    # ── Reachability pre-filter ────────────────────────────────────────────────
    # Build undirected ISL contact graph, then BFS ≤ MAX_HOPS from each node.
    # Only keep (src, dst) pairs where dst is reachable — eliminates the ~99% of
    # pairs that would return no route and waste a full Dijkstra call each.
    MAX_HOPS = 5
    from collections import defaultdict
    isl_adj: Dict[str, set] = defaultdict(set)
    for c in cp.contacts:
        if c.node_type_from == "SAT" and c.node_type_to == "SAT":
            isl_adj[c.from_node].add(c.to_node)
            isl_adj[c.to_node].add(c.from_node)

    def _reachable(src: str) -> set:
        visited: set = {src}
        frontier = [src]
        for _ in range(MAX_HOPS):
            nxt = []
            for node in frontier:
                for nb in isl_adj[node]:
                    if nb not in visited:
                        visited.add(nb)
                        nxt.append(nb)
            frontier = nxt
            if not frontier:
                break
        visited.discard(src)
        return visited

    reachable_pairs: set = set()
    for sat in list(isl_adj):          # only satellites that appear in any contact
        for dst in _reachable(sat):
            reachable_pairs.add((sat, dst))

    pairs_before = len(pairs)
    pairs = [(s, d) for s, d in pairs if (s, d) in reachable_pairs]
    pct_kept = len(pairs) * 100 // max(1, pairs_before)
    print(f"  [{_ts()}] Reachability filter: {len(pairs):,} / {pairs_before:,} pairs kept "
          f"({pct_kept}%, {MAX_HOPS}-hop BFS over {len(isl_adj)} ISL-connected sats)")

    buckets = [b * ROUTE_BUCKET_SEC for b in range(DURATION_SEC // ROUTE_BUCKET_SEC + 1)]
    total = len(pairs) * len(buckets)
    # ~5 000 routes per chunk: small enough to stay RAM-light, large enough to
    # amortise process-pool overhead per Dijkstra call.
    chunk_size = max(500, min(5_000, total // max(1, workers * 8)))

    cp_csv    = str(_cp_path(cache, step, isl_range))
    epoch_iso = EPOCH.isoformat()

    def _chunk_gen():
        """Yield worker-arg tuples one chunk at a time; never builds all combos."""
        chunk: List[Tuple[str, str, float]] = []
        for src, dst in pairs:
            for b in buckets:
                chunk.append((src, dst, b))
                if len(chunk) == chunk_size:
                    yield (cp_csv, epoch_iso, sat_op_map, chunk)
                    chunk = []
        if chunk:
            yield (cp_csv, epoch_iso, sat_op_map, chunk)

    n_chunks = (total + chunk_size - 1) // chunk_size
    print(f"  [{_ts()}] Computing route table: {len(pairs):,} pairs × {len(buckets)} buckets "
          f"= {total:,} routes, ~{n_chunks:,} chunks, {workers} workers")

    # Open SQLite for incremental writes; never accumulate results in RAM.
    conn = sqlite3.connect(str(rt_file))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=OFF")
    conn.execute("PRAGMA cache_size=-65536")   # 64 MB page cache
    conn.execute("""
        CREATE TABLE IF NOT EXISTS routes (
            key  TEXT PRIMARY KEY,
            path TEXT NOT NULL
        )
    """)
    conn.commit()

    _DB_BATCH = 20_000        # rows per SQLite executemany
    pending: List[Tuple[str, str]] = []

    def _flush_db() -> None:
        conn.executemany("INSERT OR REPLACE INTO routes VALUES (?,?)", pending)
        conn.commit()
        pending.clear()

    t0        = time.perf_counter()
    completed = 0
    n_found   = 0
    # Keep at most workers×4 futures in flight so RAM stays bounded.
    MAX_FLIGHT = workers * 4
    gen         = _chunk_gen()
    in_flight: set = set()
    gen_done    = False

    def _refill() -> None:
        nonlocal gen_done
        while len(in_flight) < MAX_FLIGHT and not gen_done:
            try:
                in_flight.add(executor.submit(_route_chunk_worker, next(gen)))
            except StopIteration:
                gen_done = True

    with ProcessPoolExecutor(max_workers=workers) as executor:
        _refill()
        while in_flight:
            done, _ = wait(in_flight, return_when=FIRST_COMPLETED)
            for fut in done:
                in_flight.discard(fut)
                for key, path_dict in fut.result():
                    if path_dict is not None:
                        pending.append((key, json.dumps(path_dict, separators=(",", ":"))))
                        n_found += 1
                completed += chunk_size   # approximate; last chunk may be smaller
            if len(pending) >= _DB_BATCH:
                _flush_db()
            _refill()
            pct = min(99, completed * 100 // total)
            elapsed_so_far = time.perf_counter() - t0
            print(f"  Route table: {pct:3d}%  ({completed:,}/{total:,} routes, "
                  f"{n_found:,} with paths, {elapsed_so_far:.0f}s)   ",
                  end="\r", flush=True)

    if pending:
        _flush_db()
    conn.execute("ANALYZE routes")
    conn.close()

    elapsed = time.perf_counter() - t0
    hit_rate = n_found * 100 // max(1, total)
    size_mb  = rt_file.stat().st_size // (1024 * 1024)
    print(f"\n  [{_ts()}] Route table done: {n_found:,}/{total:,} routes found "
          f"({hit_rate}%) in {elapsed:.1f}s ({elapsed/60:.1f} min)")
    print(f"  [{_ts()}] Route table → {rt_file.name} ({size_mb} MB)")
    return rt_file


# ── Phase 3: Traffic schedule ─────────────────────────────────────────────────

class _RouteTableCGR:
    """Drop-in CGR replacement backed by a pre-computed SQLite route table.

    TrafficGenerator calls cgr.route(src, dst, t_start, k, max_hops).
    We bucket t_start to ROUTE_BUCKET_SEC and do a single key lookup — O(log N)
    via SQLite index versus O(E log V) Dijkstra in real CGR. Workers open their
    own read-only connection so no locking is needed.
    """

    def __init__(self, rt_path: str):
        self._conn = sqlite3.connect(f"file:{rt_path}?mode=ro", uri=True)
        self._conn.execute("PRAGMA query_only=ON")

    def route(
        self,
        src: str,
        dst: str,
        t_start: float,
        k: int = 3,
        max_hops: int = 5,
    ) -> List[CGRPath]:
        bucket = int(t_start / ROUTE_BUCKET_SEC) * ROUTE_BUCKET_SEC
        key = f"{src}|{dst}|{bucket:.0f}"
        row = self._conn.execute(
            "SELECT path FROM routes WHERE key=?", (key,)
        ).fetchone()
        if row is None:
            return []
        return [_dict_to_path(json.loads(row[0]))]

    # ContactPlan-based methods needed by TrafficGenerator are not called
    # when routing is fully pre-computed, but provide stubs just in case.
    def route_with_gs(self, *args, **kwargs) -> List[CGRPath]:  # noqa: ARG002
        return []


def _traffic_chunk_worker(args: tuple) -> List[dict]:
    """Generate traffic flows for a subset of ISL windows using the route table.

    Reconstructs ContactPlan + TrafficGenerator with a _RouteTableCGR so that
    path lookup is O(1). Each chunk gets a deterministic derived seed.
    """
    cp_csv, epoch_iso, sat_op_map, rt_path_str, arrival_rate, seed, windows = args

    for _v in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS",
               "BLAS_NUM_THREADS", "VECLIB_MAXIMUM_THREADS", "NUMEXPR_NUM_THREADS"):
        os.environ.setdefault(_v, "1")

    lios_dir = str(Path(__file__).parent / "lios")
    if lios_dir not in sys.path:
        sys.path.insert(0, lios_dir)

    from contact_plan.window_calculator import ContactPlan as _CP
    from simulator.traffic_generator import TrafficGenerator as _TG

    cp = _CP.from_csv(Path(cp_csv), datetime.fromisoformat(epoch_iso))
    cp.contacts = [c for c in cp.contacts if c.duration_sec >= MIN_CONTACT_DURATION_SEC]

    cgr_rt = _RouteTableCGR(rt_path_str)
    tgen   = _TG(cp, cgr_rt, sat_op_map, arrival_rate=arrival_rate, seed=seed)

    n_gn = max(1, len(tgen._ground_nodes))
    effective_rate = arrival_rate * n_gn

    records: List[dict] = []
    for win_start, win_end in windows:
        t = win_start + tgen._rng.expovariate(effective_rate)
        while t < win_end:
            flow = tgen.generate_flow(t)
            if flow is not None:
                records.append(_flow_to_dict(t, flow))
            t += tgen._rng.expovariate(effective_rate)
    return records


def _merge_isl_windows(cp: ContactPlan) -> List[Tuple[float, float]]:
    """Merge overlapping filtered ISL windows into disjoint intervals."""
    raw = sorted(
        (c.start_time_sec, c.end_time_sec)
        for c in cp.get_isl_contacts()
        if c.duration_sec >= MIN_CONTACT_DURATION_SEC
    )
    merged: List[Tuple[float, float]] = []
    for ws, we in raw:
        if merged and ws <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], we))  # type: ignore[index]
        else:
            merged.append([ws, we])  # type: ignore[arg-type]
    return [(ws, we) for ws, we in merged if we - ws >= MIN_CONTACT_DURATION_SEC]


def compute_traffic_schedule(
    cache: Path,
    cp: ContactPlan,
    sat_op_map: Dict[str, str],
    rt_file: Path,
    seed: int,
    rate: float,
    step: int,
    isl_range: float,
    workers: int,
    force: bool = False,
) -> None:
    tf_file = _tf_path(cache, seed, rate, step, isl_range)
    if not force and tf_file.exists():
        size_kb = tf_file.stat().st_size // 1024
        print(f"  [cached] traffic schedule: {tf_file.name} ({size_kb} KB)")
        return

    # Build routing contact plan (filter short contacts)
    routing_cp = ContactPlan(epoch=cp.epoch)
    routing_cp.contacts = [c for c in cp.contacts if c.duration_sec >= MIN_CONTACT_DURATION_SEC]

    windows = _merge_isl_windows(routing_cp)
    total_active_h = sum(we - ws for ws, we in windows) / 3600
    print(f"  Traffic schedule: seed={seed}, rate={rate:.6f}, "
          f"{len(windows)} ISL windows, {total_active_h:.1f}h active time")

    # Distribute windows across workers round-robin for load balance
    n_workers = min(32, workers, max(1, len(windows)))
    if n_workers < workers:
        print(f"  [{_ts()}] Traffic workers capped at {n_workers} "
              f"(requested {workers}, windows={len(windows)})")
    chunks: List[List[Tuple[float, float]]] = [[] for _ in range(n_workers)]
    for i, w in enumerate(windows):
        chunks[i % n_workers].append(w)

    cp_csv    = str(_cp_path(cache, step, isl_range))
    epoch_iso = EPOCH.isoformat()
    rt_str    = str(rt_file)
    args_list = [
        (cp_csv, epoch_iso, sat_op_map, rt_str, rate, seed + i, chunks[i])
        for i in range(n_workers)
        if chunks[i]
    ]

    t0 = time.perf_counter()
    all_records: List[dict] = []
    completed_chunks = 0

    print(f"  [{_ts()}] Traffic: submitting {len(args_list)} chunks to {n_workers} workers ...")
    with ProcessPoolExecutor(max_workers=n_workers) as executor:
        futs = {executor.submit(_traffic_chunk_worker, a): i for i, a in enumerate(args_list)}
        for future in as_completed(futs):
            chunk_recs = future.result()
            all_records.extend(chunk_recs)
            completed_chunks += 1
            pct = completed_chunks * 100 // len(args_list)
            elapsed_so_far = time.perf_counter() - t0
            print(f"  [{_ts()}] Traffic: {pct:3d}%  "
                  f"({completed_chunks}/{len(args_list)} chunks, "
                  f"{len(all_records)} flows, {elapsed_so_far:.1f}s)   ",
                  end="\r", flush=True)
    print()

    print(f"  [{_ts()}] Sorting {len(all_records)} flows by time ...")
    all_records.sort(key=lambda r: r["t"])
    elapsed = time.perf_counter() - t0

    with tf_file.open("w") as f:
        json.dump(all_records, f, separators=(",", ":"))
    size_kb = tf_file.stat().st_size // 1024
    print(f"  [{_ts()}] Traffic done: {len(all_records)} flows in {elapsed:.1f}s "
          f"({elapsed/60:.1f} min) → {tf_file.name} ({size_kb} KB)")


# ── Main ──────────────────────────────────────────────────────────────────────

def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="High-performance LIOS precomputation (contact plan + routes + traffic)"
    )
    p.add_argument("--data",   default=str(_LIOS_DIR / "data"),
                   help="Data directory containing tles/ and gss/ subdirs")
    p.add_argument("--cache",  default=str(_LIOS_DIR / "cache"),
                   help="Output cache directory")
    p.add_argument("--workers", type=int, default=32,
                   help="Number of parallel worker processes (default: 32)")
    p.add_argument("--step",    type=int,   default=cfg.simulation.time_step_sec,
                   help="Contact plan time step in seconds")
    p.add_argument("--range",   type=float, default=cfg.link.isl_max_range_km,
                   help="ISL max range in km")
    p.add_argument("--seeds",   default=str(cfg.simulation.random_seed),
                   help="Comma-separated random seeds for traffic variants")
    p.add_argument("--rates",   default="0.01",
                   help="Comma-separated arrival rates (flows/sec per GN) for traffic variants")
    p.add_argument("--force-cp",      action="store_true", help="Force recompute contact plan")
    p.add_argument("--force-routes",  action="store_true", help="Force recompute route table")
    p.add_argument("--force-traffic", action="store_true", help="Force recompute traffic schedule")
    p.add_argument("--force",         action="store_true", help="Force recompute everything")
    p.add_argument("--skip-routes",   action="store_true", help="Skip route table computation")
    p.add_argument("--skip-traffic",  action="store_true", help="Skip traffic schedule computation")
    return p.parse_args()


def main() -> None:
    args = _parse_args()

    data_dir  = Path(args.data)
    cache_dir = Path(args.cache)
    cache_dir.mkdir(parents=True, exist_ok=True)

    seeds = [int(s.strip()) for s in args.seeds.split(",")]
    rates = [float(r.strip()) for r in args.rates.split(",")]
    step      = args.step
    isl_range = args.range
    workers   = args.workers

    force_cp      = args.force or args.force_cp
    force_routes  = args.force or args.force_routes
    force_traffic = args.force or args.force_traffic

    print("=" * 60)
    print("LIOS HPC Precompute")
    print("=" * 60)
    print(f"  data     : {data_dir}")
    print(f"  cache    : {cache_dir}")
    print(f"  workers  : {workers}")
    print(f"  step     : {step}s")
    print(f"  range    : {isl_range:.0f}km")
    print(f"  seeds    : {seeds}")
    print(f"  rates    : {rates}")
    print(f"  min contact dur: {MIN_CONTACT_DURATION_SEC}s ({MIN_CONTACT_DURATION_SEC // 60} min)")
    print()

    t_total_start = time.perf_counter()

    # Load TLEs and ground stations
    print(f"[{_ts()}][1/3] Loading TLEs and ground stations...")
    operators_tles  = TLELoader.load_all(data_dir)
    ground_stations = GSLoader.load_all(data_dir)
    n_sats = sum(len(v) for v in operators_tles.values())
    n_gs   = sum(len(v) for v in ground_stations.values())
    print(f"      {len(operators_tles)} operators, {n_sats} satellites, {n_gs} ground stations")
    for op, sats in sorted(operators_tles.items()):
        print(f"        {op}: {len(sats)} satellites")

    sat_op_map: Dict[str, str] = {
        sat.sat_id: op
        for op, sats in operators_tles.items()
        for sat in sats
    }

    # Phase 1: Contact plan
    print(f"\n[{_ts()}][2/3] Contact plan (step={step}s, range={isl_range:.0f}km)")
    cp = compute_contact_plan(
        cache_dir, operators_tles, ground_stations, step, isl_range, workers, force=force_cp
    )

    # Phase 2: Route table
    if not args.skip_routes:
        print(f"\n[{_ts()}][3/3a] Route table (step={step}s, range={isl_range:.0f}km)")
        rt_file = compute_route_table(
            cache_dir, cp, sat_op_map, step, isl_range, workers, force=force_routes
        )
    else:
        rt_file = _rt_path(cache_dir, step, isl_range)
        print(f"\n[{_ts()}][3/3a] Route table: skipped (--skip-routes)")

    # Phase 3: Traffic schedules — run variants in parallel if route table exists
    if not args.skip_traffic:
        if not rt_file.exists():
            print("\n[3/3b] Traffic: route table not found — cannot generate traffic.")
            print("       Run without --skip-routes first.")
        else:
            variants = [(seed, rate) for seed in seeds for rate in rates]
            print(f"\n[{_ts()}][3/3b] Traffic schedules: {len(variants)} variant(s)")
            for i, (seed, rate) in enumerate(variants, 1):
                print(f"\n  [{_ts()}] Variant {i}/{len(variants)}: seed={seed}, rate={rate:.6f}")
                compute_traffic_schedule(
                    cache_dir, cp, sat_op_map, rt_file,
                    seed, rate, step, isl_range, workers,
                    force=force_traffic,
                )
    else:
        print(f"\n[{_ts()}][3/3b] Traffic schedules: skipped (--skip-traffic)")

    elapsed_total = time.perf_counter() - t_total_start
    print("\n" + "=" * 60)
    print(f"Precompute complete.  Total: {elapsed_total:.1f}s ({elapsed_total/60:.1f} min)")
    print("=" * 60)


if __name__ == "__main__":
    mp.set_start_method("spawn", force=True)
    main()
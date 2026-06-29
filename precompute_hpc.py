#!/usr/bin/env python3
"""Precompute LIOS contact plans and direct active-pair traffic schedules.

Artifacts:
  cp_24h_step{step}_range{range}.csv
  tf_pair_v2_e{epoch}_d{bias}_w{weights}_24h_s{seed}_r{rate}_step{step}_range{range}.json

Traffic generation does not calculate routes. Each global Poisson arrival is
allocated to a cross-operator ISL pair using constellation-size source weights.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import multiprocessing as mp
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, Mapping

_LIOS_DIR = Path(__file__).parent / "lios"
sys.path.insert(0, str(_LIOS_DIR))

from config import cfg
from contact_plan.gs_loader import GSLoader
from contact_plan.tle_loader import TLELoader
from contact_plan.window_calculator import ContactPlan, WindowCalculator
from precompute import CONTACT_PLAN_EPOCH, contact_plan_epoch_key
from simulator.traffic_generator import (
    TrafficFlow,
    TrafficGenerator,
    constellation_size_weights,
)

EPOCH = CONTACT_PLAN_EPOCH
DURATION_SEC = 86_400
CONTACT_MODEL_VERSION = "xop_operator_visibility_gates_gsany_v1"


def _ts() -> str:
    return datetime.now(timezone.utc).strftime("%H:%M:%S")


def _cp_path(cache: Path, step: int, isl_range: float) -> Path:
    return cache / f"cp_24h_step{step}_range{isl_range:.0f}.csv"


def _cp_meta_path(cache: Path, step: int, isl_range: float) -> Path:
    return cache / f"cp_24h_step{step}_range{isl_range:.0f}_meta.json"


def _tf_path(
    cache: Path,
    seed: int,
    rate: float,
    step: int,
    isl_range: float,
    operator_load_weights: Mapping[str, float],
) -> Path:
    weights_json = json.dumps(
        operator_load_weights, sort_keys=True, separators=(",", ":")
    )
    weights_key = hashlib.sha256(weights_json.encode()).hexdigest()[:10]
    return cache / (
        f"tf_pair_v2_e{contact_plan_epoch_key()}_"
        f"d{cfg.simulation.traffic_direction_bias:.3f}_"
        f"w{weights_key}_24h_"
        f"s{seed}_r{rate:.6f}_"
        f"step{step}_range{isl_range:.0f}.json"
    )


def _data_signature(data_dir: Path) -> str:
    digest = hashlib.sha256()
    for subdir, patterns in (("tles", ("*.txt", "*.tle")), ("gss", ("*.txt",))):
        root = data_dir / subdir
        for pattern in patterns:
            for path in sorted(root.glob(pattern)):
                rel = path.relative_to(data_dir).as_posix()
                digest.update(rel.encode())
                digest.update(b"\0")
                digest.update(path.read_bytes())
                digest.update(b"\0")
    return digest.hexdigest()[:16]


def _flow_to_dict(t: float, flow: TrafficFlow) -> dict:
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


def compute_contact_plan(
    cache: Path,
    operators_tles: dict,
    ground_stations: dict,
    step: int,
    isl_range: float,
    workers: int,
    data_signature: str,
    force: bool = False,
) -> ContactPlan:
    cp_file = _cp_path(cache, step, isl_range)
    meta_file = _cp_meta_path(cache, step, isl_range)
    if not force and cp_file.exists():
        try:
            meta = json.loads(meta_file.read_text()) if meta_file.exists() else {}
        except json.JSONDecodeError:
            meta = {}
        cache_is_current = (
            meta.get("contact_model_version") == CONTACT_MODEL_VERSION
            and meta.get("epoch") == EPOCH.isoformat()
            and meta.get("cross_operator_only") is True
            and meta.get("isl_visibility_gates") == "operator_range_clearance_fov_pointing_doppler_margin"
            and meta.get("gs_access_policy") == "any_operator_secure_access"
            and meta.get("data_signature") == data_signature
        )
        if cache_is_current:
            cp = ContactPlan.from_csv(cp_file, EPOCH)
            print(f"  [{_ts()}] [cached] {cp_file.name}: {len(cp.contacts):,} contacts")
            return cp
        print(
            f"  [{_ts()}] [stale] {cp_file.name}: rebuilding for "
            f"{CONTACT_MODEL_VERSION}"
        )

    end = EPOCH + timedelta(seconds=DURATION_SEC)
    calculator = WindowCalculator(
        EPOCH, end, step, isl_range, checkpoint_dir=cache
    )
    started = time.perf_counter()
    cp = calculator.compute(operators_tles, ground_stations, n_workers=workers)
    cp.to_csv(cp_file)
    meta_file.write_text(
        json.dumps(
            {
                "epoch": EPOCH.isoformat(),
                "duration_sec": DURATION_SEC,
                "time_step_sec": step,
                "isl_max_range_km": isl_range,
                "contact_model_version": CONTACT_MODEL_VERSION,
                "cross_operator_only": True,
                "isl_earth_line_of_sight": "wgs84_ellipsoid",
                "isl_visibility_gates": "operator_range_clearance_fov_pointing_doppler_margin",
                "gs_visibility": "wgs84_station_elevation_mask",
                "gs_access_policy": "any_operator_secure_access",
                "data_signature": data_signature,
                "n_contacts": len(cp.contacts),
            },
            indent=2,
        )
    )
    print(
        f"  [{_ts()}] Contact plan: {len(cp.contacts):,} contacts in "
        f"{time.perf_counter() - started:.1f}s → {cp_file.name}"
    )
    return cp


def compute_traffic_schedule(
    cache: Path,
    cp: ContactPlan,
    operator_map: Dict[str, str],
    seed: int,
    rate: float,
    step: int,
    isl_range: float,
    operator_load_weights: Mapping[str, float],
    force: bool = False,
) -> None:
    tf_file = _tf_path(
        cache, seed, rate, step, isl_range, operator_load_weights
    )
    if not force and tf_file.exists():
        print(f"  [{_ts()}] [cached] {tf_file.name}")
        return

    started = time.perf_counter()
    generator = TrafficGenerator(
        cp,
        operator_map,
        arrival_rate=rate,
        seed=seed,
        operator_load_weights=operator_load_weights,
    )
    schedule = generator.generate_poisson_schedule(0.0, DURATION_SEC)
    records = [_flow_to_dict(t, flow) for t, flow in schedule]
    with tf_file.open("w") as handle:
        json.dump(records, handle, separators=(",", ":"))
    print(
        f"  [{_ts()}] Traffic: {len(records):,} flows in "
        f"{time.perf_counter() - started:.1f}s → {tf_file.name}"
    )


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Precompute LIOS contact plans and active-pair traffic"
    )
    parser.add_argument("--data", default=str(_LIOS_DIR / "data"))
    parser.add_argument("--cache", default=str(_LIOS_DIR / "cache"))
    parser.add_argument("--workers", type=int, default=32)
    parser.add_argument("--step", type=int, default=cfg.simulation.time_step_sec)
    parser.add_argument("--range", type=float, default=cfg.link.isl_max_range_km)
    parser.add_argument("--seeds", default=str(cfg.simulation.random_seed))
    parser.add_argument(
        "--rates",
        default=str(cfg.simulation.arrival_rate),
        help="Comma-separated global arrival rates in flows/second",
    )
    parser.add_argument("--force-cp", action="store_true")
    parser.add_argument("--force-traffic", action="store_true")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--skip-traffic", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    data_dir = Path(args.data)
    cache_dir = Path(args.cache)
    cache_dir.mkdir(parents=True, exist_ok=True)

    seeds = [int(value.strip()) for value in args.seeds.split(",")]
    rates = [float(value.strip()) for value in args.rates.split(",")]
    force_cp = args.force or args.force_cp
    force_traffic = args.force or args.force_traffic

    operators_tles = TLELoader.load_all(data_dir)
    ground_stations = GSLoader.load_all(data_dir)
    tle_files = sorted((data_dir / "tles").glob("*.txt")) + sorted((data_dir / "tles").glob("*.tle"))
    gs_files = sorted((data_dir / "gss").glob("*.txt"))
    data_signature = _data_signature(data_dir)
    operator_map = {
        satellite.sat_id: operator
        for operator, satellites in operators_tles.items()
        for satellite in satellites
    }
    if not operator_map:
        raise SystemExit(
            f"No satellites loaded from {data_dir / 'tles'}; expected .txt or .tle files"
        )
    operator_load_weights = constellation_size_weights(operators_tles)

    print(
        f"[{_ts()}] LIOS precompute: {len(operator_map)} satellites, "
        f"{len(operators_tles)} operators, {len(tle_files)} TLE files, "
        f"{len(gs_files)} GS files, step={args.step}s, range={args.range:.0f}km"
    )
    print(f"[{_ts()}] Operator load weights: {operator_load_weights}")
    cp = compute_contact_plan(
        cache_dir,
        operators_tles,
        ground_stations,
        args.step,
        args.range,
        args.workers,
        data_signature,
        force=force_cp,
    )

    if not args.skip_traffic:
        for seed in seeds:
            for rate in rates:
                compute_traffic_schedule(
                    cache_dir,
                    cp,
                    operator_map,
                    seed,
                    rate,
                    args.step,
                    args.range,
                    operator_load_weights,
                    force=force_traffic,
                )


if __name__ == "__main__":
    mp.set_start_method("spawn", force=True)
    main()

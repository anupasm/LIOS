#!/usr/bin/env python3
"""CLI entry point — compute contact windows from TLEs and GS configs.

Usage:
  python compute_windows.py \\
      --start 2025-01-01T00:00:00Z \\
      --end   2025-01-01T01:30:00Z \\
      --step  10 \\
      --data  ../data \\
      --out   contact_plan.csv
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

# Allow running from within lios/ or project root
sys.path.insert(0, str(Path(__file__).parent.parent))

from contact_plan.gs_loader import GSLoader
from contact_plan.tle_loader import TLELoader
from contact_plan.window_calculator import WindowCalculator


def parse_dt(s: str) -> datetime:
    s = s.rstrip("Z")
    dt = datetime.fromisoformat(s)
    return dt.replace(tzinfo=timezone.utc)


def main() -> None:
    parser = argparse.ArgumentParser(description="Compute LIOS contact windows")
    parser.add_argument("--start", required=True, help="Simulation start (ISO-8601 UTC)")
    parser.add_argument("--end", required=True, help="Simulation end (ISO-8601 UTC)")
    parser.add_argument("--step", type=int, default=10, help="Propagation step in seconds")
    parser.add_argument("--isl-range", type=float, default=2500.0, help="Max ISL range (km)")
    parser.add_argument(
        "--data",
        default=str(Path(__file__).parent.parent / "data"),
        help="Path to data/ directory",
    )
    parser.add_argument(
        "--out",
        default=str(Path(__file__).parent / "contact_plan.csv"),
        help="Output CSV path",
    )
    parser.add_argument(
        "--propagation-log",
        default=None,
        metavar="PATH",
        help="Write satellite mobility + contact log JSON to this path (for visualization)",
    )
    args = parser.parse_args()

    t_start = parse_dt(args.start)
    t_end = parse_dt(args.end)
    data_dir = Path(args.data)
    out_path = Path(args.out)
    prop_log_path = Path(args.propagation_log) if args.propagation_log else None

    print(f"Loading TLEs from {data_dir}/tles/")
    operators = TLELoader.load_all(data_dir)
    total_sats = sum(len(v) for v in operators.values())
    print(f"  Loaded {total_sats} satellites across {len(operators)} operators: {list(operators)}")

    print(f"Loading ground stations from {data_dir}/gss/")
    ground_stations = GSLoader.load_all(data_dir)
    total_gs = sum(len(v) for v in ground_stations.values())
    print(f"  Loaded {total_gs} ground stations")

    print(f"Computing windows: {t_start.isoformat()} → {t_end.isoformat()}, step={args.step}s")
    calc = WindowCalculator(t_start, t_end, args.step, args.isl_range,
                            propagation_log_path=prop_log_path)
    plan = calc.compute(operators, ground_stations)

    plan.to_csv(out_path)
    isl = len(plan.get_isl_contacts())
    gs = len(plan.get_gs_contacts())
    print(f"Done: {len(plan.contacts)} contacts ({isl} ISL, {gs} GS) → {out_path}")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Check ISL channel balance percentages from a LIOS experiment run.

Reads OFFCHAIN_PROOF_UPDATE events saved by run_experiments.py and reports:
  - Per-channel min/mean balance and fraction of time below T_LOW
  - Number of T1 (low-balance) triggers per channel
  - Aggregate summary + optional histogram

Usage
-----
  # Run experiments first (saves balance_events.json to results/logs/)
  python evaluation/run_experiments.py --out results/

  # Then check balance stats
  python evaluation/check_balance.py --out results/
  python evaluation/check_balance.py --out results/ --config lios --no-plot
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path
from typing import Dict, List

sys.path.insert(0, str(Path(__file__).parent.parent))
from config import cfg

T_LOW_FRACTION  = cfg.protocol.t_low_fraction
INITIAL_CAP_KB  = cfg.protocol.channel_balance_kb * 2   # total pool (both sides)


# ── Analysis ──────────────────────────────────────────────────────────────────

def analyse(events: List[dict]) -> dict:
    """Return per-channel and aggregate balance statistics."""
    by_channel: Dict[str, List[dict]] = defaultdict(list)
    for e in events:
        by_channel[e.get("channel_id", "unknown")].append(e)

    t_low_pct = T_LOW_FRACTION * 100
    rows = []

    for ch_id, ch_events in sorted(by_channel.items()):
        ch_events.sort(key=lambda e: e["t"])
        initial = ch_events[0].get("initial_capacity_kb", INITIAL_CAP_KB)

        # Weaker side % at each snapshot
        pcts = [
            min(e["balance_a_kb"], e["balance_b_kb"]) / initial * 100
            for e in ch_events
        ]

        below   = sum(1 for p in pcts if p < t_low_pct)
        t1_hits = sum(
            1 for i in range(1, len(pcts))
            if pcts[i - 1] >= t_low_pct and pcts[i] < t_low_pct
        )

        rows.append({
            "channel_id":  ch_id,
            "snapshots":   len(pcts),
            "min_pct":     round(min(pcts), 2),
            "mean_pct":    round(sum(pcts) / len(pcts), 2),
            "frac_below":  round(below / len(pcts) * 100, 2),
            "t1_triggers": t1_hits,
            "pcts":        pcts,
        })

    all_pcts = [p for r in rows for p in r["pcts"]]
    summary = {
        "n_channels":         len(rows),
        "total_snapshots":    len(all_pcts),
        "global_min_pct":     round(min(all_pcts), 2) if all_pcts else 0,
        "global_mean_pct":    round(sum(all_pcts) / max(1, len(all_pcts)), 2),
        "channels_hit_t_low": sum(1 for r in rows if r["min_pct"] < t_low_pct),
        "total_t1_triggers":  sum(r["t1_triggers"] for r in rows),
        "t_low_pct":          t_low_pct,
    }
    return {"channels": rows, "summary": summary}


# ── Report ────────────────────────────────────────────────────────────────────

def print_report(name: str, result: dict) -> None:
    s    = result["summary"]
    rows = sorted(result["channels"], key=lambda r: r["min_pct"])   # worst first

    print(f"\n{'='*72}")
    print(f"  Balance Report  —  {name}")
    print(f"  T_LOW = {s['t_low_pct']:.0f}% of initial capacity  "
          f"(config: t_low_fraction = {T_LOW_FRACTION})")
    print(f"{'='*72}")

    if not rows:
        print("  No OFFCHAIN_PROOF_UPDATE events found.")
        return

    print(f"  {'Channel':<36} {'Min%':>6} {'Mean%':>7} {'<T_LOW':>7} {'T1':>5}")
    print(f"  {'-'*36} {'-'*6} {'-'*7} {'-'*7} {'-'*5}")
    for r in rows[:25]:
        flag = "  ⚠" if r["min_pct"] < s["t_low_pct"] else ""
        print(f"  {r['channel_id']:<36} {r['min_pct']:>6.1f} "
              f"{r['mean_pct']:>7.1f} {r['frac_below']:>6.1f}% "
              f"{r['t1_triggers']:>5}{flag}")
    if len(rows) > 25:
        print(f"  ... ({len(rows)-25} more channels)")

    pct_hit = s["channels_hit_t_low"] * 100 // max(1, s["n_channels"])
    print(f"\n  Channels tracked        : {s['n_channels']}")
    print(f"  Total snapshots         : {s['total_snapshots']:,}")
    print(f"  Global min balance      : {s['global_min_pct']:.2f}%")
    print(f"  Global mean balance     : {s['global_mean_pct']:.2f}%")
    print(f"  Channels that hit T_LOW : {s['channels_hit_t_low']}/{s['n_channels']}  ({pct_hit}%)")
    print(f"  Total T1 triggers       : {s['total_t1_triggers']}")
    print()


def plot_histogram(all_results: Dict[str, dict], out_path: Path) -> None:
    try:
        import matplotlib.pyplot as plt
    except ImportError:
        print("  matplotlib not available — skipping plot")
        return

    fig, ax = plt.subplots(figsize=(9, 4))
    for name, result in all_results.items():
        pcts = [p for r in result["channels"] for p in r["pcts"]]
        if pcts:
            ax.hist(pcts, bins=50, alpha=0.6, label=name, density=True)

    t_low = next(iter(all_results.values()))["summary"]["t_low_pct"]
    ax.axvline(t_low, color="red", linestyle="--", linewidth=1.2,
               label=f"T_LOW ({t_low:.0f}%)")
    ax.set_xlabel("Channel balance — weaker side (% of initial capacity)")
    ax.set_ylabel("Density")
    ax.set_title("LIOS ISL Channel Balance Distribution")
    ax.legend()
    fig.tight_layout()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out_path)
    print(f"  Histogram → {out_path}")


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    p = argparse.ArgumentParser(description="LIOS balance percentage checker")
    p.add_argument("--out",     default="results",
                   help="Directory written by run_experiments.py (default: results/)")
    p.add_argument("--config",  default=None,
                   help="Filter to one experiment name (default: all found)")
    p.add_argument("--no-plot", action="store_true")
    args = p.parse_args()

    out_dir  = Path(args.out)
    log_dir  = out_dir / "logs"

    if not log_dir.exists():
        print(f"No logs directory found at {log_dir}")
        print("Run:  python evaluation/run_experiments.py --out results/")
        sys.exit(1)

    bal_files = sorted(log_dir.glob("*_balance_events.json"))
    if args.config:
        bal_files = [f for f in bal_files if f.name.startswith(args.config + "_")]

    if not bal_files:
        print(f"No balance event files found in {log_dir}")
        print("Make sure run_experiments.py was run with --out to capture logs.")
        sys.exit(1)

    all_results: Dict[str, dict] = {}
    for bf in bal_files:
        name = bf.name.replace("_balance_events.json", "")
        with bf.open() as f:
            events = json.load(f)
        result = analyse(events)
        print_report(name, result)
        all_results[name] = result

    if all_results and not args.no_plot:
        plot_histogram(all_results, out_dir / "balance_histogram.pdf")


if __name__ == "__main__":
    main()

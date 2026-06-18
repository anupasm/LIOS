#!/usr/bin/env python3
"""Check traffic distribution and asymmetry within LIOS payment channels.

For each bilateral channel (satA__satB) the script computes:
  - Volume forwarded in each direction (A→B and B→A)
  - Traffic Asymmetry Index (TAI): |fwd - rev| / (fwd + rev)
      0 = perfectly symmetric, 1 = fully one-directional
  - Dominant direction and ratio

Results are aggregated at three levels:
  1. Per satellite channel
  2. Per operator-pair (e.g. op1↔op2)
  3. Overall summary

Input:  lios_balance_events.json  (OFFCHAIN_PROOF_UPDATE events)
Output: printed tables + optional matplotlib plots

Usage:
  python scripts/check_traffic_asymmetry.py
  python scripts/check_traffic_asymmetry.py --log results/logs/lios_balance_events.json
  python scripts/check_traffic_asymmetry.py --plot --out results/figures/
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path
from typing import Dict, Tuple


# ── Helpers ────────────────────────────────────────────────────────────────────

def _operators_from_channel(channel_id: str) -> Tuple[str, str]:
    """Extract (op_a, op_b) from 'opX-starlink-NNN__opY-starlink-MMM'."""
    sat_a, sat_b = channel_id.split("__", 1)
    op_a = sat_a.split("-")[0]
    op_b = sat_b.split("-")[0]
    return op_a, op_b


def tai(fwd: float, rev: float) -> float:
    """Traffic Asymmetry Index: 0 = symmetric, 1 = fully one-directional."""
    total = fwd + rev
    return abs(fwd - rev) / total if total > 0 else 0.0


def ratio(fwd: float, rev: float) -> float:
    """dominant / subordinate volume ratio (≥1)."""
    hi, lo = max(fwd, rev), min(fwd, rev)
    return hi / lo if lo > 0 else float("inf")


# ── Loading ────────────────────────────────────────────────────────────────────

def load_channel_volumes(log_path: Path) -> Dict[str, Dict[str, float]]:
    """Parse OFFCHAIN_PROOF_UPDATE events and return per-channel directional volumes.

    Returns:
        {channel_id: {"a_to_b": float, "b_to_a": float}}
    """
    volumes: Dict[str, Dict[str, float]] = defaultdict(lambda: {"a_to_b": 0.0, "b_to_a": 0.0})

    with log_path.open() as fh:
        events = json.load(fh)

    for ev in events:
        if ev.get("event") != "OFFCHAIN_PROOF_UPDATE":
            continue
        ch_id: str = ev["channel_id"]
        satellite: str = ev["satellite"]
        bytes_kb: float = float(ev["bytes_kb"])

        sat_a = ch_id.split("__")[0]
        if satellite == sat_a:
            volumes[ch_id]["a_to_b"] += bytes_kb
        else:
            volumes[ch_id]["b_to_a"] += bytes_kb

    return dict(volumes)


# ── Aggregation ────────────────────────────────────────────────────────────────

def aggregate_by_operator_pair(
    volumes: Dict[str, Dict[str, float]]
) -> Dict[str, Dict[str, float]]:
    """Sum directional volumes across all channels for each operator pair.

    The key is 'opA-opB' (sorted alphabetically so op1-op2 is canonical).
    Note: A→B and B→A within a satellite channel both map to the operator pair,
    but the *direction* is from the perspective of the sorted satellite IDs, not
    the operators.  We therefore also track op-level fwd/rev by mapping sat-level
    directions to operator directions.
    """
    op_pair: Dict[str, Dict[str, float]] = defaultdict(
        lambda: {"a_to_b": 0.0, "b_to_a": 0.0, "channels": 0}
    )
    for ch_id, v in volumes.items():
        op_a, op_b = _operators_from_channel(ch_id)
        # Ensure canonical ordering matches the channel sat ordering
        key = f"{op_a}-{op_b}"
        op_pair[key]["a_to_b"] += v["a_to_b"]
        op_pair[key]["b_to_a"] += v["b_to_a"]
        op_pair[key]["channels"] += 1

    return dict(op_pair)


# ── Printing ───────────────────────────────────────────────────────────────────

def _fmt_kb(kb: float) -> str:
    if kb >= 1_048_576:
        return f"{kb/1_048_576:.2f} GB"
    if kb >= 1_024:
        return f"{kb/1_024:.2f} MB"
    return f"{kb:.1f} KB"


def print_operator_pair_table(op_pair: Dict[str, Dict[str, float]]) -> None:
    pairs = sorted(op_pair.items())
    hdr = f"{'Pair':<12} {'Channels':>8} {'A→B':>12} {'B→A':>12} {'Total':>12} {'TAI':>6} {'Ratio':>7} {'Dominant':>10}"
    print("\n── Operator-pair summary ─────────────────────────────────────────────────")
    print(hdr)
    print("─" * len(hdr))
    for key, v in pairs:
        a2b = v["a_to_b"]
        b2a = v["b_to_a"]
        t = tai(a2b, b2a)
        r = ratio(a2b, b2a)
        dom_dir = f"{key.split('-')[0]}→{key.split('-')[1]}" if a2b >= b2a else f"{key.split('-')[1]}→{key.split('-')[0]}"
        ratio_str = f"{r:.2f}x" if r != float("inf") else "∞"
        print(
            f"{key:<12} {int(v['channels']):>8} {_fmt_kb(a2b):>12} {_fmt_kb(b2a):>12} "
            f"{_fmt_kb(a2b+b2a):>12} {t:>6.3f} {ratio_str:>7} {dom_dir:>10}"
        )


def print_channel_table(volumes: Dict[str, Dict[str, float]], top_n: int = 20) -> None:
    rows = []
    for ch_id, v in volumes.items():
        sat_a, sat_b = ch_id.split("__", 1)
        a2b, b2a = v["a_to_b"], v["b_to_a"]
        rows.append((ch_id, sat_a, sat_b, a2b, b2a, tai(a2b, b2a), a2b + b2a))

    # Sort by TAI descending (most asymmetric first)
    rows_by_tai  = sorted(rows, key=lambda r: r[5], reverse=True)
    # Sort by total volume descending for the busiest channels
    rows_by_vol  = sorted(rows, key=lambda r: r[6], reverse=True)

    def _print_rows(header: str, data: list) -> None:
        print(f"\n── {header} ────────────────────────────────────────────────")
        hdr = f"{'Channel':<48} {'A→B':>12} {'B→A':>12} {'TAI':>6} {'Ratio':>7}"
        print(hdr)
        print("─" * len(hdr))
        for ch_id, sat_a, sat_b, a2b, b2a, t, _ in data:
            r = ratio(a2b, b2a)
            ratio_str = f"{r:.2f}x" if r != float("inf") else "∞"
            print(f"{ch_id:<48} {_fmt_kb(a2b):>12} {_fmt_kb(b2a):>12} {t:>6.3f} {ratio_str:>7}")

    _print_rows(f"Top {top_n} channels by asymmetry (TAI)", rows_by_tai[:top_n])
    _print_rows(f"Top {top_n} channels by total volume",   rows_by_vol[:top_n])


def print_overall_summary(volumes: Dict[str, Dict[str, float]]) -> None:
    all_tais = [tai(v["a_to_b"], v["b_to_a"]) for v in volumes.values()]
    active = [v for v in volumes.values() if (v["a_to_b"] + v["b_to_a"]) > 0]
    active_tais = [tai(v["a_to_b"], v["b_to_a"]) for v in active]

    total_fwd = sum(v["a_to_b"] for v in volumes.values())
    total_rev = sum(v["b_to_a"] for v in volumes.values())

    sym_thresh = 0.1
    sym_count = sum(1 for t in active_tais if t <= sym_thresh)

    print("\n── Overall summary ────────────────────────────────────────────────────────")
    print(f"  Total channels tracked      : {len(volumes)}")
    print(f"  Active channels (any traffic): {len(active)}")
    print(f"  Symmetric (TAI ≤ {sym_thresh:.1f})        : {sym_count} / {len(active)} "
          f"({100*sym_count/max(1, len(active)):.1f}%)")
    if active_tais:
        avg_tai = sum(active_tais) / len(active_tais)
        print(f"  Mean TAI (active channels)  : {avg_tai:.4f}")
        print(f"  Max  TAI                    : {max(active_tais):.4f}")
        print(f"  Min  TAI                    : {min(active_tais):.4f}")
    print(f"  Total A→B volume            : {_fmt_kb(total_fwd)}")
    print(f"  Total B→A volume            : {_fmt_kb(total_rev)}")
    print(f"  Global TAI (aggregate)      : {tai(total_fwd, total_rev):.4f}")
    print(f"  Global ratio                : {ratio(total_fwd, total_rev):.3f}x")


# ── Plotting ───────────────────────────────────────────────────────────────────

def plot_asymmetry(
    volumes: Dict[str, Dict[str, float]],
    op_pair: Dict[str, Dict[str, float]],
    out_dir: Path,
) -> None:
    try:
        import matplotlib.pyplot as plt
        import numpy as np
    except ImportError:
        print("[warn] matplotlib not found — skipping plots", file=sys.stderr)
        return

    out_dir.mkdir(parents=True, exist_ok=True)

    active = {ch: v for ch, v in volumes.items() if (v["a_to_b"] + v["b_to_a"]) > 0}
    tais = [tai(v["a_to_b"], v["b_to_a"]) for v in active.values()]

    # ── 1. TAI histogram ──────────────────────────────────────────────────────
    fig, ax = plt.subplots(figsize=(8, 4))
    ax.hist(tais, bins=20, range=(0, 1), edgecolor="white", color="#4C78A8")
    ax.axvline(sum(tais) / max(1, len(tais)), color="#E45756", linestyle="--",
               label=f"mean TAI = {sum(tais)/max(1, len(tais)):.3f}")
    ax.set_xlabel("Traffic Asymmetry Index (TAI)")
    ax.set_ylabel("Number of channels")
    ax.set_title("Per-channel TAI distribution")
    ax.legend()
    fig.tight_layout()
    fig.savefig(out_dir / "tai_histogram.png", dpi=150)
    plt.close(fig)
    print(f"  saved → {out_dir / 'tai_histogram.png'}")

    # ── 2. Operator-pair directional bar chart ─────────────────────────────
    pairs = sorted(op_pair.items())
    labels = [k for k, _ in pairs]
    a2b_vals = np.array([v["a_to_b"] / 1_024 for _, v in pairs])  # MB
    b2a_vals = np.array([v["b_to_a"] / 1_024 for _, v in pairs])

    x = np.arange(len(labels))
    width = 0.35
    fig, ax = plt.subplots(figsize=(10, 5))
    ax.bar(x - width / 2, a2b_vals, width, label="A→B", color="#4C78A8")
    ax.bar(x + width / 2, b2a_vals, width, label="B→A", color="#F58518")
    ax.set_xticks(x)
    ax.set_xticklabels(labels, rotation=20, ha="right")
    ax.set_ylabel("Volume (MB)")
    ax.set_title("Per operator-pair directional traffic volume")
    ax.legend()
    fig.tight_layout()
    fig.savefig(out_dir / "op_pair_volume.png", dpi=150)
    plt.close(fig)
    print(f"  saved → {out_dir / 'op_pair_volume.png'}")

    # ── 3. Scatter: total volume vs TAI per channel ────────────────────────
    totals = [(v["a_to_b"] + v["b_to_a"]) / 1_024 for v in active.values()]
    fig, ax = plt.subplots(figsize=(8, 5))
    ax.scatter(totals, tais, alpha=0.5, s=18, color="#4C78A8")
    ax.set_xlabel("Total channel volume (MB)")
    ax.set_ylabel("TAI")
    ax.set_title("Channel volume vs asymmetry")
    fig.tight_layout()
    fig.savefig(out_dir / "volume_vs_tai.png", dpi=150)
    plt.close(fig)
    print(f"  saved → {out_dir / 'volume_vs_tai.png'}")


# ── CLI ────────────────────────────────────────────────────────────────────────

def parse_args() -> argparse.Namespace:
    here = Path(__file__).resolve().parent.parent.parent  # repo root
    default_log = here / "results" / "logs" / "lios_balance_events.json"
    default_out  = here / "results" / "figures"

    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--log",  type=Path, default=default_log,
                   help="Path to lios_balance_events.json (default: %(default)s)")
    p.add_argument("--top",  type=int, default=20,
                   help="Number of channels to show in per-channel tables (default: %(default)s)")
    p.add_argument("--plot", action="store_true",
                   help="Generate matplotlib plots")
    p.add_argument("--out",  type=Path, default=default_out,
                   help="Output directory for plots (default: %(default)s)")
    return p.parse_args()


def main() -> None:
    args = parse_args()

    if not args.log.exists():
        sys.exit(f"[error] log file not found: {args.log}")

    print(f"Loading {args.log} …", flush=True)
    volumes = load_channel_volumes(args.log)
    op_pair = aggregate_by_operator_pair(volumes)

    print_overall_summary(volumes)
    print_operator_pair_table(op_pair)
    print_channel_table(volumes, top_n=args.top)

    if args.plot:
        print("\nGenerating plots …")
        plot_asymmetry(volumes, op_pair, args.out)

    print()


if __name__ == "__main__":
    main()

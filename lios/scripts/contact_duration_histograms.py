#!/usr/bin/env python3
"""Generate separate duration histograms for SAT-SAT and SAT-GS contacts.

Default input:
  lios/cache/ckpt_step30_range1500_deg4_all_contacts.csv

Usage:
  python3 lios/scripts/contact_duration_histograms.py
  python3 lios/scripts/contact_duration_histograms.py --bins 20 --out-dir lios/cache
"""
from __future__ import annotations

import argparse
import csv
import os
import statistics
from pathlib import Path
from typing import Any

os.environ.setdefault("MPLCONFIGDIR", "/tmp/matplotlib")

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt


REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CSV = REPO_ROOT / "lios/cache/ckpt_step30_range1500_deg4_xop_los_isl_contacts.csv"
DEFAULT_OUT_DIR = REPO_ROOT / "lios/cache"


def load_contacts(path: Path) -> list[dict[str, Any]]:
    try:
        with path.open(newline="", encoding="utf-8") as handle:
            rows = list(csv.DictReader(handle))
    except FileNotFoundError as exc:
        raise SystemExit(f"Contact CSV not found: {path}") from exc

    required = {
        "start_time_sec",
        "end_time_sec",
        "node_type_from",
        "node_type_to",
        "operator_from",
        "operator_to",
    }
    missing = required.difference(rows[0] if rows else {})
    if missing:
        raise SystemExit(
            f"{path} is missing required columns: {', '.join(sorted(missing))}"
        )

    contacts: list[dict[str, Any]] = []
    for row_number, row in enumerate(rows, start=2):
        if row['operator_from'] == row['operator_to']: continue
        try:
            start = float(row["start_time_sec"])
            end = float(row["end_time_sec"])
        except ValueError as exc:
            raise SystemExit(
                f"Invalid start/end time in {path}:{row_number}: {row}"
            ) from exc

        if end < start:
            raise SystemExit(f"Negative duration in {path}:{row_number}: {row}")

        
        contact = dict(row)
        contact["duration_sec"] = end - start
        if contact['duration_sec'] < 3600:continue
        contacts.append(contact)

    return contacts


def split_contacts(
    contacts: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    sat_sat: list[dict[str, Any]] = []
    sat_gs: list[dict[str, Any]] = []

    for contact in contacts:
        type_from = str(contact["node_type_from"]).upper()
        type_to = str(contact["node_type_to"]).upper()
        types = {type_from, type_to}
        if types == {"SAT"}:
            sat_sat.append(contact)
        elif types == {"SAT", "GS"}:
            sat_gs.append(contact)

    return sat_sat, sat_gs


def format_minutes(minutes: float) -> str:
    return f"{minutes:.2f} min"


def plot_histogram(
    contacts: list[dict[str, Any]],
    title: str,
    out_path: Path,
    bins: int,
    color: str,
) -> None:
    if not contacts:
        print(f"[warn] No contacts for {title}; skipping {out_path}")
        return

    durations_min = [contact["duration_sec"] / 60.0 for contact in contacts]
    mean_min = statistics.fmean(durations_min)
    median_min = statistics.median(durations_min)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    fig, ax = plt.subplots(figsize=(8, 4.5))
    ax.hist(
        durations_min,
        bins=max(1, bins),
        color=color,
        edgecolor="white",
        linewidth=0.8,
    )
    ax.axvline(
        mean_min,
        color="#E45756",
        linestyle="--",
        linewidth=1.5,
        label=f"mean = {format_minutes(mean_min)}",
    )
    ax.axvline(
        median_min,
        color="#54A24B",
        linestyle=":",
        linewidth=1.8,
        label=f"median = {format_minutes(median_min)}",
    )
    ax.set_title(title)
    ax.set_xlabel("Contact duration (minutes)")
    ax.set_ylabel("Number of contacts")
    ax.grid(axis="y", alpha=0.25)
    ax.legend()
    fig.tight_layout()
    fig.savefig(out_path, dpi=200)
    plt.close(fig)

    print(
        f"Wrote {out_path} "
        f"({len(contacts)} contacts, mean {format_minutes(mean_min)}, "
        f"median {format_minutes(median_min)})"
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate separate SAT-SAT and SAT-GS contact-duration histograms."
    )
    parser.add_argument(
        "--csv",
        type=Path,
        default=DEFAULT_CSV,
        help=f"Combined contact CSV path (default: {DEFAULT_CSV})",
    )
    parser.add_argument(
        "--out-dir",
        type=Path,
        default=DEFAULT_OUT_DIR,
        help=f"Directory for histogram PNGs (default: {DEFAULT_OUT_DIR})",
    )
    parser.add_argument(
        "--bins",
        type=int,
        default=20,
        help="Number of histogram bins.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    contacts = load_contacts(args.csv)
    sat_sat, sat_gs = split_contacts(contacts)

    print(f"Loaded {len(contacts)} contacts from {args.csv}")
    print(f"  SAT-SAT: {len(sat_sat)}")
    print(f"  SAT-GS : {len(sat_gs)}")

    plot_histogram(
        sat_sat,
        "SAT-SAT Contact Duration Distribution",
        args.out_dir / "sat_sat_contact_duration_histogram.png",
        args.bins,
        "#4C78A8",
    )
    plot_histogram(
        sat_gs,
        "SAT-GS Contact Duration Distribution",
        args.out_dir / "sat_gs_contact_duration_histogram.png",
        args.bins,
        "#F58518",
    )


if __name__ == "__main__":
    main()

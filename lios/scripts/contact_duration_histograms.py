#!/usr/bin/env python3
"""Compare SAT-SAT (ISL) and SAT-GS (ground) contact windows.

Produces separate and overlaid duration histograms, prints a statistics table,
and writes the same statistics to CSV.
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
DEFAULT_CSV = REPO_ROOT / "lios/cache/cp_86400s_step30_range4000_gsany.csv"
DEFAULT_OUT_DIR = REPO_ROOT / "lios/cache"


def load_contacts(
    path: Path, min_duration_sec: float = 0.0, cross_operator_only: bool = False
) -> list[dict[str, Any]]:
    try:
        with path.open(newline="", encoding="utf-8") as handle:
            reader = csv.DictReader(handle)
            required = {
                "start_time_sec", "end_time_sec", "node_type_from",
                "node_type_to", "operator_from", "operator_to",
            }
            missing = required.difference(reader.fieldnames or [])
            if missing:
                raise SystemExit(
                    f"{path} is missing required columns: {', '.join(sorted(missing))}"
                )
            rows = list(reader)
    except FileNotFoundError as exc:
        raise SystemExit(f"Contact CSV not found: {path}") from exc

    contacts: list[dict[str, Any]] = []
    for row_number, row in enumerate(rows, start=2):
        try:
            start = float(row["start_time_sec"])
            end = float(row["end_time_sec"])
        except (TypeError, ValueError) as exc:
            raise SystemExit(
                f"Invalid start/end time in {path}:{row_number}: {row}"
            ) from exc
        if end < start:
            raise SystemExit(f"Negative duration in {path}:{row_number}: {row}")
        duration = end - start
        if duration < min_duration_sec:
            continue
        if cross_operator_only and row["operator_from"] == row["operator_to"]:
            continue
        contact: dict[str, Any] = dict(row)
        contact.update(start_time_sec=start, end_time_sec=end, duration_sec=duration)
        contacts.append(contact)
    return contacts


def contact_kind(contact: dict[str, Any]) -> str | None:
    types = {
        str(contact["node_type_from"]).strip().upper(),
        str(contact["node_type_to"]).strip().upper(),
    }
    if types == {"SAT"}:
        return "SAT-SAT"
    if types == {"SAT", "GS"}:
        return "SAT-GS"
    return None


def split_contacts(contacts: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    return (
        [c for c in contacts if contact_kind(c) == "SAT-SAT"],
        [c for c in contacts if contact_kind(c) == "SAT-GS"],
    )


def percentile(values: list[float], fraction: float) -> float:
    """Linearly interpolated percentile, matching the usual inclusive definition."""
    ordered = sorted(values)
    position = (len(ordered) - 1) * fraction
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def summarize(label: str, contacts: list[dict[str, Any]], observation_sec: float) -> dict[str, Any]:
    durations = [float(c["duration_sec"]) for c in contacts]
    pairs = {
        tuple(sorted((str(c.get("from_node", "")), str(c.get("to_node", "")))))
        for c in contacts
    }
    result: dict[str, Any] = {
        "contact_type": label,
        "contact_count": len(contacts),
        "unique_node_pairs": len(pairs),
        "observation_hours": observation_sec / 3600.0,
        "windows_per_hour": len(contacts) * 3600.0 / observation_sec if observation_sec else 0.0,
        "windows_per_day": len(contacts) * 86400.0 / observation_sec if observation_sec else 0.0,
        "total_contact_time_sec": sum(durations),
    }
    for name in ("min", "mean", "median", "p25", "p75", "p95", "max", "stdev"):
        result[f"duration_{name}_sec"] = 0.0
    if durations:
        result.update({
            "duration_min_sec": min(durations),
            "duration_mean_sec": statistics.fmean(durations),
            "duration_median_sec": statistics.median(durations),
            "duration_p25_sec": percentile(durations, 0.25),
            "duration_p75_sec": percentile(durations, 0.75),
            "duration_p95_sec": percentile(durations, 0.95),
            "duration_max_sec": max(durations),
            "duration_stdev_sec": statistics.stdev(durations) if len(durations) > 1 else 0.0,
        })
    return result


def plot_histogram(contacts: list[dict[str, Any]], title: str, out_path: Path, bins: int, color: str) -> None:
    if not contacts:
        print(f"[warn] No contacts for {title}; skipping {out_path}")
        return
    durations_min = [float(c["duration_sec"]) / 60.0 for c in contacts]
    mean_min = statistics.fmean(durations_min)
    median_min = statistics.median(durations_min)
    fig, ax = plt.subplots(figsize=(8, 4.5))
    ax.hist(durations_min, bins=bins, color=color, edgecolor="white", linewidth=0.8)
    ax.axvline(mean_min, color="#E45756", linestyle="--", label=f"mean = {mean_min:.2f} min")
    ax.axvline(median_min, color="#54A24B", linestyle=":", label=f"median = {median_min:.2f} min")
    ax.set(title=title, xlabel="Contact duration (minutes)", ylabel="Number of windows")
    ax.grid(axis="y", alpha=0.25)
    ax.legend()
    fig.tight_layout()
    fig.savefig(out_path, dpi=200)
    plt.close(fig)


def plot_comparison(sat_sat: list[dict[str, Any]], sat_gs: list[dict[str, Any]], out_path: Path, bins: int) -> None:
    datasets = [
        ([float(c["duration_sec"]) / 60.0 for c in sat_sat], "SAT-SAT (ISL)", "#4C78A8"),
        ([float(c["duration_sec"]) / 60.0 for c in sat_gs], "SAT-GS (ground)", "#F58518"),
    ]
    datasets = [item for item in datasets if item[0]]
    if not datasets:
        return
    fig, ax = plt.subplots(figsize=(8.5, 4.8))
    for values, label, color in datasets:
        ax.hist(values, bins=bins, density=True, histtype="step", linewidth=2, label=f"{label} (n={len(values)})", color=color)
    ax.set(title="ISL vs Ground Contact Duration", xlabel="Contact duration (minutes)", ylabel="Probability density")
    ax.grid(axis="y", alpha=0.25)
    ax.legend()
    fig.tight_layout()
    fig.savefig(out_path, dpi=200)
    plt.close(fig)


def write_statistics(rows: list[dict[str, Any]], path: Path) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def print_statistics(rows: list[dict[str, Any]]) -> None:
    print("\nContact-window comparison")
    print(f"{'Type':<16}{'Windows':>10}{'Pairs':>9}{'/ hour':>10}{'Total h':>11}{'Mean min':>11}{'Median':>10}{'P95':>10}")
    for row in rows:
        print(
            f"{row['contact_type']:<16}{row['contact_count']:>10,}{row['unique_node_pairs']:>9,}"
            f"{row['windows_per_hour']:>10.2f}{row['total_contact_time_sec']/3600:>11.2f}"
            f"{row['duration_mean_sec']/60:>11.2f}{row['duration_median_sec']/60:>10.2f}"
            f"{row['duration_p95_sec']/60:>10.2f}"
        )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Compare SAT-SAT and SAT-GS contact windows.")
    parser.add_argument("--csv", type=Path, default=DEFAULT_CSV)
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR)
    parser.add_argument("--bins", type=int, default=20)
    parser.add_argument("--min-duration-sec", type=float, default=0.0, help="Exclude shorter windows (default: 0).")
    parser.add_argument("--cross-operator-only", action="store_true", help="Only include contacts whose endpoint operators differ.")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if args.bins < 1 or args.min_duration_sec < 0:
        raise SystemExit("--bins must be >= 1 and --min-duration-sec must be >= 0")
    contacts = load_contacts(args.csv, args.min_duration_sec, args.cross_operator_only)
    sat_sat, sat_gs = split_contacts(contacts)
    classified = sat_sat + sat_gs
    if not classified:
        raise SystemExit("No SAT-SAT or SAT-GS contacts matched the selected filters")
    observation_sec = max(float(c["end_time_sec"]) for c in classified) - min(float(c["start_time_sec"]) for c in classified)
    rows = [summarize("SAT-SAT (ISL)", sat_sat, observation_sec), summarize("SAT-GS (ground)", sat_gs, observation_sec)]

    args.out_dir.mkdir(parents=True, exist_ok=True)
    print(f"Loaded {len(classified):,} classified contacts from {args.csv} over {observation_sec/3600:.2f} hours")
    print_statistics(rows)
    write_statistics(rows, args.out_dir / "contact_window_statistics.csv")
    plot_histogram(sat_sat, "SAT-SAT (ISL) Contact Durations", args.out_dir / "sat_sat_contact_duration_histogram.png", args.bins, "#4C78A8")
    plot_histogram(sat_gs, "SAT-GS (Ground) Contact Durations", args.out_dir / "sat_gs_contact_duration_histogram.png", args.bins, "#F58518")
    plot_comparison(sat_sat, sat_gs, args.out_dir / "sat_sat_vs_sat_gs_duration_histogram.png", args.bins)
    print(f"\nWrote plots and statistics to {args.out_dir}")


if __name__ == "__main__":
    main()

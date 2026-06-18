#!/usr/bin/env python3
"""Visualize one LIOS satellite-channel lifecycle from a settlement log.

The output contains balance trajectories, forwarded traffic, and settlement
state transitions for the canonical channel formed by two satellite IDs.

Usage:
  python3 scripts/visualize_channel_lifecycle.py \
      results/logs/lios_settlement_log.json \
      op1-starlink-2742 op2-starlink-5590

  python3 scripts/visualize_channel_lifecycle.py \
      results/logs/lios_settlement_log.json \
      op1-starlink-2742 op2-starlink-5590 \
      --out results/figures/channel_lifecycle.pdf
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt


BALANCE_COLOURS = {"A": "#4C78A8", "B": "#F58518"}
EVENT_COLOURS = {
    "SETTLEMENT_QUEUED": "#E45756",
    "SETTLEMENT_RECEIVED": "#B279A2",
    "SETTLEMENT_FINALIZED": "#54A24B",
    "PEER_SETTLEMENT_NOTIFIED": "#72B7B2",
    "ISL_RESUMED": "#2CA02C",
}
EVENT_MARKERS = {
    "SETTLEMENT_QUEUED": "D",
    "SETTLEMENT_RECEIVED": "^",
    "SETTLEMENT_FINALIZED": "*",
    "PEER_SETTLEMENT_NOTIFIED": ">",
    "ISL_RESUMED": "o",
}
EVENT_ORDER = [
    "SETTLEMENT_QUEUED",
    "SETTLEMENT_RECEIVED",
    "SETTLEMENT_FINALIZED",
    "PEER_SETTLEMENT_NOTIFIED",
    "ISL_RESUMED",
]


def canonical_channel_id(satellite_1: str, satellite_2: str) -> str:
    """Return the channel ID using the simulator's lexical ordering."""
    if satellite_1 == satellite_2:
        raise ValueError("The two satellite IDs must be different.")
    return "__".join(sorted((satellite_1, satellite_2)))


def load_log(path: Path) -> dict[str, Any]:
    try:
        with path.open(encoding="utf-8") as handle:
            data = json.load(handle)
    except FileNotFoundError as exc:
        raise ValueError(f"Log file does not exist: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid JSON in {path}: {exc}") from exc

    required = {"offchain", "ground_settlement", "latency_summary"}
    missing = required.difference(data)
    if missing:
        raise ValueError(
            f"{path} is not a LIOS settlement log; missing: "
            f"{', '.join(sorted(missing))}"
        )
    return data


def channel_records(
    data: dict[str, Any], channel_id: str
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    offchain = [
        event for event in data["offchain"] if event.get("channel_id") == channel_id
    ]
    ground = [
        event
        for event in data["ground_settlement"]
        if event.get("channel_id") == channel_id
    ]
    latency = [
        event
        for event in data["latency_summary"]
        if event.get("channel_id") == channel_id
    ]
    return offchain, ground, latency


def _hours(event: dict[str, Any], field: str = "t") -> float:
    return float(event[field]) / 3600.0


def _event_label(event: dict[str, Any]) -> str:
    event_type = event["event"].replace("_", " ").title()
    details: list[str] = []
    if event.get("triggers"):
        details.append("+".join(event["triggers"]))
    if event.get("via"):
        details.append(str(event["via"]))
    if event.get("satellite"):
        details.append(str(event["satellite"]))
    return event_type if not details else f"{event_type}\n({', '.join(details)})"


def _add_lifecycle_lines(
    axes: list[plt.Axes], events: list[dict[str, Any]]
) -> None:
    for event in events:
        event_type = event.get("event")
        if event_type not in {
            "SETTLEMENT_QUEUED",
            "SETTLEMENT_RECEIVED",
            "SETTLEMENT_FINALIZED",
        }:
            continue
        for axis in axes:
            axis.axvline(
                _hours(event),
                color=EVENT_COLOURS[event_type],
                linewidth=1.0,
                linestyle=":",
                alpha=0.8,
                zorder=0,
            )


def build_figure(
    channel_id: str,
    satellite_a: str,
    satellite_b: str,
    offchain: list[dict[str, Any]],
    ground: list[dict[str, Any]],
    latency: list[dict[str, Any]],
    t_low_fraction: float,
) -> plt.Figure:
    proofs = sorted(
        (
            event
            for event in offchain
            if event.get("event") == "OFFCHAIN_PROOF_UPDATE"
        ),
        key=lambda event: float(event["t"]),
    )
    lifecycle = sorted(
        (
            event
            for event in offchain + ground
            if event.get("event") in EVENT_COLOURS and "t" in event
        ),
        key=lambda event: float(event["t"]),
    )

    figure, axes_array = plt.subplots(
        3,
        1,
        figsize=(16, 10),
        sharex=True,
        gridspec_kw={"height_ratios": [3.3, 1.5, 1.5], "hspace": 0.18},
    )
    balance_axis, traffic_axis, lifecycle_axis = axes_array
    axes = [balance_axis, traffic_axis, lifecycle_axis]

    reporters = sorted({str(event.get("satellite", "unknown")) for event in proofs})
    line_styles = ["-", "--", "-.", ":"]

    # Each satellite has a local sequence and state view. Plotting reporters
    # independently avoids falsely connecting unsynchronised local proofs.
    for reporter_index, reporter in enumerate(reporters):
        reporter_proofs = [
            event for event in proofs if event.get("satellite") == reporter
        ]
        times = [_hours(event) for event in reporter_proofs]
        linestyle = line_styles[reporter_index % len(line_styles)]

        balance_axis.plot(
            times,
            [float(event["balance_a_kb"]) / 1024.0 for event in reporter_proofs],
            color=BALANCE_COLOURS["A"],
            linestyle=linestyle,
            marker="o",
            markersize=4,
            linewidth=1.8,
            label=f"{satellite_a} · reported by {reporter}",
        )
        balance_axis.plot(
            times,
            [float(event["balance_b_kb"]) / 1024.0 for event in reporter_proofs],
            color=BALANCE_COLOURS["B"],
            linestyle=linestyle,
            marker="o",
            markersize=4,
            linewidth=1.8,
            label=f"{satellite_b} · reported by {reporter}",
        )

        bar_width = 0.07 if len(proofs) < 100 else 0.03
        offset = (reporter_index - (len(reporters) - 1) / 2.0) * bar_width
        traffic_axis.bar(
            [time + offset for time in times],
            [float(event.get("bytes_kb", 0.0)) / 1024.0 for event in reporter_proofs],
            width=bar_width,
            color=(
                BALANCE_COLOURS["A"]
                if reporter == satellite_a
                else BALANCE_COLOURS["B"]
            ),
            alpha=0.75,
            label=f"Forwarded by {reporter}",
        )

    if proofs:
        capacity_kb = float(proofs[0]["balance_a_kb"]) + float(
            proofs[0]["balance_b_kb"]
        )
        threshold_mb = capacity_kb * t_low_fraction / 1024.0
        balance_axis.axhline(
            threshold_mb,
            color="#E45756",
            linestyle=":",
            linewidth=1.3,
            label=f"T1 threshold ({threshold_mb:.2f} MB)",
        )

    present_types = [
        event_type
        for event_type in EVENT_ORDER
        if any(event.get("event") == event_type for event in lifecycle)
    ]
    event_positions = {
        event_type: len(present_types) - index
        for index, event_type in enumerate(present_types)
    }

    for event_type in present_types:
        events = [
            event for event in lifecycle if event.get("event") == event_type
        ]
        y = event_positions[event_type]
        lifecycle_axis.scatter(
            [_hours(event) for event in events],
            [y] * len(events),
            s=95 if event_type != "SETTLEMENT_FINALIZED" else 150,
            marker=EVENT_MARKERS[event_type],
            color=EVENT_COLOURS[event_type],
            edgecolor="white",
            linewidth=0.8,
            zorder=3,
            label=event_type.replace("_", " ").title(),
        )
        for index, event in enumerate(events):
            lifecycle_axis.annotate(
                _event_label(event),
                (_hours(event), y),
                xytext=(5, 9 if index % 2 == 0 else -24),
                textcoords="offset points",
                fontsize=7,
                ha="left",
                va="center",
            )

    if present_types:
        lifecycle_axis.set_yticks(
            [event_positions[event_type] for event_type in present_types],
            [event_type.replace("_", " ").title() for event_type in present_types],
        )
        lifecycle_axis.set_ylim(0.5, len(present_types) + 0.5)
    else:
        lifecycle_axis.set_yticks([])
        lifecycle_axis.text(
            0.5,
            0.5,
            "No settlement or resume events recorded for this channel",
            transform=lifecycle_axis.transAxes,
            ha="center",
            va="center",
            color="#E45756",
            fontsize=11,
        )

    _add_lifecycle_lines(axes, lifecycle)

    total_mb = sum(float(event.get("bytes_kb", 0.0)) for event in proofs) / 1024.0
    summary = [
        f"{len(proofs)} proof updates",
        f"{total_mb:,.2f} MB forwarded",
        f"{len(ground)} ground events",
    ]
    if latency:
        record = latency[-1]
        summary.append(
            f"{float(record.get('protocol_latency_sec', 0.0)):.3f} s protocol latency"
        )
        gs_wait = record.get("ground", {}).get("wait_for_gs_sec")
        if gs_wait is not None:
            summary.append(f"{float(gs_wait):.1f} s GS wait")

    figure.suptitle(
        f"LIOS Channel Lifecycle\n{satellite_a} ↔ {satellite_b}"
        f"\n{' · '.join(summary)}",
        fontsize=15,
        y=0.985,
    )
    balance_axis.set_title("Local balance-proof trajectories", loc="left", fontsize=11)
    traffic_axis.set_title("Traffic forwarded per proof update", loc="left", fontsize=11)
    lifecycle_axis.set_title("Settlement lifecycle", loc="left", fontsize=11)
    balance_axis.set_ylabel("Balance (MB)")
    traffic_axis.set_ylabel("Forwarded (MB)")
    lifecycle_axis.set_xlabel("Simulation time (hours)")

    for axis in axes:
        axis.grid(True, axis="both", linestyle="--", linewidth=0.5, alpha=0.35)
        axis.spines["top"].set_visible(False)
        axis.spines["right"].set_visible(False)

    balance_axis.set_ylim(bottom=0)
    traffic_axis.set_ylim(bottom=0)
    balance_axis.legend(loc="upper left", fontsize=7, ncol=2, frameon=False)
    traffic_axis.legend(loc="upper left", fontsize=8, ncol=2, frameon=False)
    if present_types:
        lifecycle_axis.legend(loc="upper left", fontsize=7, ncol=3, frameon=False)

    figure.subplots_adjust(left=0.16, right=0.98, top=0.86, bottom=0.08)
    return figure


def default_output_path(log_path: Path, channel_id: str) -> Path:
    return log_path.parent.parent / "figures" / f"{channel_id}_lifecycle.png"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Visualize one channel lifecycle from a LIOS settlement log."
    )
    parser.add_argument("log", type=Path, help="Path to lios_settlement_log.json")
    parser.add_argument("satellite_1", help="First satellite ID")
    parser.add_argument("satellite_2", help="Second satellite ID")
    parser.add_argument(
        "--out",
        type=Path,
        help="Output PNG/PDF/SVG path (default: results/figures/<channel>_lifecycle.png)",
    )
    parser.add_argument(
        "--t-low-fraction",
        type=float,
        default=0.2,
        help="T1 low-balance fraction used for the reference line (default: 0.2)",
    )
    parser.add_argument(
        "--dpi",
        type=int,
        default=180,
        help="Raster output resolution (default: 180)",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if not 0.0 <= args.t_low_fraction <= 1.0:
        print("error: --t-low-fraction must be between 0 and 1", file=sys.stderr)
        return 2
    if args.dpi <= 0:
        print("error: --dpi must be positive", file=sys.stderr)
        return 2

    try:
        channel_id = canonical_channel_id(args.satellite_1, args.satellite_2)
        data = load_log(args.log)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    offchain, ground, latency = channel_records(data, channel_id)
    if not offchain and not ground and not latency:
        related = sorted(
            {
                event["channel_id"]
                for section in ("offchain", "ground_settlement")
                for event in data[section]
                if "channel_id" in event
                and (
                    args.satellite_1 in event["channel_id"]
                    or args.satellite_2 in event["channel_id"]
                )
            }
        )
        hint = f"\nRelated channels:\n  " + "\n  ".join(related[:10]) if related else ""
        print(
            f"error: channel {channel_id!r} was not found in {args.log}{hint}",
            file=sys.stderr,
        )
        return 1

    satellite_a, satellite_b = channel_id.split("__", 1)
    figure = build_figure(
        channel_id,
        satellite_a,
        satellite_b,
        offchain,
        ground,
        latency,
        args.t_low_fraction,
    )
    output = args.out or default_output_path(args.log, channel_id)
    output.parent.mkdir(parents=True, exist_ok=True)
    figure.savefig(output, dpi=args.dpi)
    plt.close(figure)

    proof_count = sum(
        event.get("event") == "OFFCHAIN_PROOF_UPDATE" for event in offchain
    )
    print(f"Channel: {channel_id}")
    print(f"Proof updates: {proof_count}")
    print(f"Ground settlement events: {len(ground)}")
    print(f"Latency records: {len(latency)}")
    print(f"Wrote: {output.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

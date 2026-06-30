#!/usr/bin/env python3
"""Visualize one LIOS satellite-channel lifecycle from a settlement log.

The output contains balance trajectories, forwarded traffic, settlement state
transitions, and SAT-SAT/SAT-GS contact windows for two satellite IDs.

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
import csv
import json
import sys
from pathlib import Path
from typing import Any

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.cm import ScalarMappable
from matplotlib.colors import Normalize
from matplotlib.patches import Patch
import plotly.graph_objects as go
from plotly.colors import sample_colorscale
from plotly.subplots import make_subplots


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


def default_contact_plan_path(log_path: Path, data: dict[str, Any]) -> Path:
    """Infer the experiment contact-plan CSV next to the settlement log."""
    experiment = data.get("experiment")
    if experiment:
        return log_path.with_name(f"{experiment}_contact_plan.csv")
    stem = log_path.stem.removesuffix("_settlement_log")
    return log_path.with_name(f"{stem}_contact_plan.csv")


def load_contact_plan(path: Path) -> list[dict[str, Any]]:
    """Load contact windows from the experiment contact-plan CSV."""
    try:
        with path.open(newline="", encoding="utf-8") as handle:
            rows = list(csv.DictReader(handle))
    except FileNotFoundError as exc:
        raise ValueError(f"Contact-plan CSV does not exist: {path}") from exc

    required = {
        "contact_id", "from_node", "to_node", "start_time_sec", "end_time_sec",
        "range_km", "node_type_from", "node_type_to",
    }
    if not rows:
        return []
    missing = required.difference(rows[0])
    if missing:
        raise ValueError(
            f"Invalid contact-plan CSV {path}; missing: {', '.join(sorted(missing))}"
        )
    return rows


def select_contacts(
    contacts: list[dict[str, Any]], satellite_a: str, satellite_b: str
) -> tuple[list[dict[str, Any]], dict[str, list[dict[str, Any]]]]:
    """Return direct A-B ISLs and every ground contact involving A or B."""
    satellites = {satellite_a, satellite_b}
    isl_contacts: list[dict[str, Any]] = []
    gs_contacts = {satellite_a: [], satellite_b: []}
    for contact in contacts:
        endpoints = {contact["from_node"], contact["to_node"]}
        node_types = {contact["node_type_from"], contact["node_type_to"]}
        if endpoints == satellites and node_types == {"SAT"}:
            isl_contacts.append(contact)
            continue
        if node_types != {"GS", "SAT"}:
            continue
        for satellite in satellites.intersection(endpoints):
            gs_contacts[satellite].append(contact)

    key = lambda contact: float(contact["start_time_sec"])
    return sorted(isl_contacts, key=key), {
        satellite: sorted(windows, key=key)
        for satellite, windows in gs_contacts.items()
    }


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


def _contact_span(contact: dict[str, Any]) -> tuple[float, float]:
    start = float(contact["start_time_sec"]) / 3600.0
    duration = (
        float(contact["end_time_sec"]) - float(contact["start_time_sec"])
    ) / 3600.0
    return start, duration


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
    isl_contacts: list[dict[str, Any]],
    gs_contacts: dict[str, list[dict[str, Any]]],
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
        4,
        1,
        figsize=(16, 12),
        sharex=True,
        gridspec_kw={"height_ratios": [3.3, 1.5, 1.5, 1.35], "hspace": 0.20},
    )
    balance_axis, traffic_axis, lifecycle_axis, contact_axis = axes_array
    axes = [balance_axis, traffic_axis, lifecycle_axis, contact_axis]

    # Direct ISL availability is useful context for every lifecycle panel.
    for contact in isl_contacts:
        start, duration = _contact_span(contact)
        for axis in (balance_axis, traffic_axis, lifecycle_axis):
            axis.axvspan(start, start + duration, color="#7A5195", alpha=0.16,
                         linewidth=0, zorder=-2)
            axis.axvline(start, color="#4A235A", linewidth=0.9,
                         linestyle="--", alpha=0.65, zorder=-1)
            axis.axvline(start + duration, color="#4A235A", linewidth=0.9,
                         linestyle="--", alpha=0.65, zorder=-1)

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

    contact_lanes = [
        (f"ISL: {satellite_a} ↔ {satellite_b}", isl_contacts, 2.1),
        (f"GS: {satellite_a}", gs_contacts.get(satellite_a, []), 1.1),
        (f"GS: {satellite_b}", gs_contacts.get(satellite_b, []), 0.1),
    ]

    # Encode the recorded SAT-SAT range directly on each ISL contact bar.
    if isl_contacts:
        ranges_km = [float(contact["range_km"]) for contact in isl_contacts]
        range_min, range_max = min(ranges_km), max(ranges_km)
        if range_min == range_max:
            range_min -= 1.0
            range_max += 1.0
        range_norm = Normalize(vmin=range_min, vmax=range_max)
        range_map = plt.get_cmap("viridis")
        for contact, range_km in zip(isl_contacts, ranges_km):
            start, duration = _contact_span(contact)
            colour = range_map(range_norm(range_km))
            contact_axis.broken_barh(
                [(start, duration)], (2.1, 0.65),
                facecolors=colour, edgecolors="#111111", linewidth=1.2,
                hatch="////", alpha=1.0, zorder=5,
            )
            contact_axis.axvline(
                start, color="#111111", linewidth=0.8, linestyle="--", alpha=0.7
            )
            contact_axis.axvline(
                start + duration, color="#111111", linewidth=0.8,
                linestyle="--", alpha=0.7
            )
            contact_axis.annotate(
                f"{range_km:,.0f} km",
                (start + duration / 2.0, 2.75),
                xytext=(0, 3),
                textcoords="offset points",
                rotation=35,
                ha="center",
                va="bottom",
                fontsize=7,
                color="#333333",
                bbox={"boxstyle": "round,pad=0.15", "facecolor": "white",
                      "edgecolor": "#555555", "alpha": 0.9},
            )
        range_scale = ScalarMappable(norm=range_norm, cmap=range_map)
        range_scale.set_array([])
        colour_bar = figure.colorbar(
            range_scale, ax=contact_axis, pad=0.012, aspect=25, fraction=0.025
        )
        colour_bar.set_label("Recorded SAT-SAT range (km)", fontsize=8)
        colour_bar.ax.tick_params(labelsize=7)

    gs_lanes = [
        (gs_contacts.get(satellite_a, []), 1.1, BALANCE_COLOURS["A"]),
        (gs_contacts.get(satellite_b, []), 0.1, BALANCE_COLOURS["B"]),
    ]
    for contacts, y, colour in gs_lanes:
        if contacts:
            contact_axis.broken_barh(
                [_contact_span(contact) for contact in contacts],
                (y, 0.65),
                facecolors=colour,
                edgecolors="white",
                linewidth=0.4,
                alpha=0.8,
            )
    contact_axis.set_yticks(
        [y + 0.325 for _, _, y in contact_lanes],
        [label for label, _, _ in contact_lanes],
    )
    contact_axis.set_ylim(0, 3.25)
    contact_axis.legend(
        handles=[
            Patch(facecolor="white", edgecolor="#111111", hatch="////",
                  label="SAT-SAT contact"),
            Patch(facecolor=BALANCE_COLOURS["A"], label=f"{satellite_a} SAT-GS"),
            Patch(facecolor=BALANCE_COLOURS["B"], label=f"{satellite_b} SAT-GS"),
        ],
        loc="upper left", fontsize=7, ncol=3, frameon=False,
    )

    _add_lifecycle_lines(axes, lifecycle)

    total_mb = sum(float(event.get("bytes_kb", 0.0)) for event in proofs) / 1024.0
    summary = [
        f"{len(proofs)} proof updates",
        f"{total_mb:,.2f} MB forwarded",
        f"{len(ground)} ground events",
        f"{len(isl_contacts)} SAT-SAT contacts",
        f"{sum(len(v) for v in gs_contacts.values())} SAT-GS contacts",
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
    contact_axis.set_title("Physical contact windows", loc="left", fontsize=11)
    balance_axis.set_ylabel("Balance (MB)")
    traffic_axis.set_ylabel("Forwarded (MB)")
    contact_axis.set_xlabel("Simulation time (hours)")

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

    figure.subplots_adjust(left=0.22, right=0.97, top=0.86, bottom=0.08)
    return figure


def build_html_figure(
    channel_id: str,
    satellite_a: str,
    satellite_b: str,
    offchain: list[dict[str, Any]],
    ground: list[dict[str, Any]],
    latency: list[dict[str, Any]],
    isl_contacts: list[dict[str, Any]],
    gs_contacts: dict[str, list[dict[str, Any]]],
    t_low_fraction: float,
) -> go.Figure:
    """Build an interactive Plotly equivalent of the lifecycle visualization."""
    proofs = sorted(
        (event for event in offchain if event.get("event") == "OFFCHAIN_PROOF_UPDATE"),
        key=lambda event: float(event["t"]),
    )
    lifecycle = sorted(
        (event for event in offchain + ground
         if event.get("event") in EVENT_COLOURS and "t" in event),
        key=lambda event: float(event["t"]),
    )
    figure = make_subplots(
        rows=4,
        cols=1,
        shared_xaxes=True,
        vertical_spacing=0.055,
        row_heights=[0.42, 0.20, 0.17, 0.21],
        subplot_titles=(
            "Local balance-proof trajectories",
            "Traffic forwarded per proof update",
            "Settlement lifecycle",
            "Physical contact windows",
        ),
    )

    reporters = sorted({str(event.get("satellite", "unknown")) for event in proofs})
    dashes = ["solid", "dash", "dot", "dashdot"]
    for reporter_index, reporter in enumerate(reporters):
        reporter_proofs = [event for event in proofs if event.get("satellite") == reporter]
        times = [_hours(event) for event in reporter_proofs]
        custom = [
            [event.get("seq_num"), event.get("contact_id"), event.get("bytes_kb", 0.0)]
            for event in reporter_proofs
        ]
        for role, satellite, field in (
            ("A", satellite_a, "balance_a_kb"),
            ("B", satellite_b, "balance_b_kb"),
        ):
            figure.add_trace(
                go.Scatter(
                    x=times,
                    y=[float(event[field]) / 1024.0 for event in reporter_proofs],
                    mode="lines+markers",
                    name=f"{satellite} · reported by {reporter}",
                    legendgroup=f"balance-{reporter}-{role}",
                    line={"color": BALANCE_COLOURS[role],
                          "dash": dashes[reporter_index % len(dashes)]},
                    marker={"size": 5},
                    customdata=custom,
                    hovertemplate=(
                        "%{x:.4f} h<br>Balance: %{y:,.2f} MB"
                        "<br>Sequence: %{customdata[0]}"
                        "<br>Contact: %{customdata[1]}<extra>%{fullData.name}</extra>"
                    ),
                ),
                row=1,
                col=1,
            )
        figure.add_trace(
            go.Bar(
                x=times,
                y=[float(event.get("bytes_kb", 0.0)) / 1024.0
                   for event in reporter_proofs],
                name=f"Forwarded by {reporter}",
                marker_color=(BALANCE_COLOURS["A"] if reporter == satellite_a
                              else BALANCE_COLOURS["B"]),
                opacity=0.78,
                customdata=custom,
                hovertemplate=(
                    "%{x:.4f} h<br>Forwarded: %{y:,.2f} MB"
                    "<br>Sequence: %{customdata[0]}"
                    "<br>Contact: %{customdata[1]}<extra>%{fullData.name}</extra>"
                ),
            ),
            row=2,
            col=1,
        )

    if proofs:
        capacity_kb = float(proofs[0]["balance_a_kb"]) + float(proofs[0]["balance_b_kb"])
        threshold_mb = capacity_kb * t_low_fraction / 1024.0
        figure.add_hline(
            y=threshold_mb, row=1, col=1, line_color="#E45756",
            line_dash="dot", annotation_text=f"T1 threshold ({threshold_mb:,.2f} MB)",
        )

    present_types = [
        event_type for event_type in EVENT_ORDER
        if any(event.get("event") == event_type for event in lifecycle)
    ]
    lifecycle_positions = {
        event_type: len(present_types) - index
        for index, event_type in enumerate(present_types)
    }
    for event_type in present_types:
        events = [event for event in lifecycle if event.get("event") == event_type]
        figure.add_trace(
            go.Scatter(
                x=[_hours(event) for event in events],
                y=[lifecycle_positions[event_type]] * len(events),
                mode="markers",
                name=event_type.replace("_", " ").title(),
                marker={"size": 11, "color": EVENT_COLOURS[event_type],
                        "symbol": "diamond"},
                text=[_event_label(event) for event in events],
                hovertemplate="%{x:.4f} h<br>%{text}<extra></extra>",
            ),
            row=3,
            col=1,
        )
    if present_types:
        figure.update_yaxes(
            tickmode="array",
            tickvals=list(lifecycle_positions.values()),
            ticktext=[event_type.replace("_", " ").title()
                      for event_type in present_types],
            row=3,
            col=1,
        )
    else:
        figure.add_annotation(
            x=0.5, y=0.5, xref="x3 domain", yref="y3 domain",
            text="No settlement or resume events recorded for this channel",
            showarrow=False, font={"color": "#E45756"},
        )

    for contact in isl_contacts:
        start, duration = _contact_span(contact)
        figure.add_vrect(
            x0=start, x1=start + duration, fillcolor="#7A5195", opacity=0.10,
            line={"color": "#4A235A", "dash": "dash", "width": 1},
            layer="below", row="all", col=1,
        )

    ranges_km = [float(contact["range_km"]) for contact in isl_contacts]
    range_min = min(ranges_km) if ranges_km else 0.0
    range_max = max(ranges_km) if ranges_km else 1.0
    range_span = range_max - range_min or 1.0
    for index, (contact, range_km) in enumerate(zip(isl_contacts, ranges_km)):
        start, duration = _contact_span(contact)
        colour = sample_colorscale("Viridis", (range_km - range_min) / range_span)[0]
        figure.add_trace(
            go.Scatter(
                x=[start, start + duration],
                y=["SAT-SAT", "SAT-SAT"],
                mode="lines",
                name="SAT-SAT contact",
                legendgroup="sat-sat",
                showlegend=index == 0,
                line={"color": colour, "width": 16},
                customdata=[[contact["contact_id"], range_km, duration * 60.0]] * 2,
                hovertemplate=(
                    "Contact: %{customdata[0]}<br>Time: %{x:.4f} h"
                    "<br>Range: %{customdata[1]:,.2f} km"
                    "<br>Duration: %{customdata[2]:.2f} min<extra></extra>"
                ),
            ),
            row=4,
            col=1,
        )
    if isl_contacts:
        figure.add_trace(
            go.Scatter(
                x=[sum(_contact_span(contact)) - _contact_span(contact)[1] / 2.0
                   for contact in isl_contacts],
                y=["SAT-SAT"] * len(isl_contacts),
                mode="markers",
                marker={
                    "size": 1,
                    "color": ranges_km,
                    "colorscale": "Viridis",
                    "cmin": range_min,
                    "cmax": range_max if range_max != range_min else range_min + 1.0,
                    "showscale": True,
                    "colorbar": {"title": "SAT-SAT range (km)", "thickness": 12},
                },
                showlegend=False,
                hoverinfo="skip",
            ),
            row=4,
            col=1,
        )

    gs_lane_data = [
        (satellite_a, gs_contacts.get(satellite_a, []), BALANCE_COLOURS["A"]),
        (satellite_b, gs_contacts.get(satellite_b, []), BALANCE_COLOURS["B"]),
    ]
    for satellite, contacts, colour in gs_lane_data:
        for index, contact in enumerate(contacts):
            start, duration = _contact_span(contact)
            gs_id = (contact["from_node"] if contact["node_type_from"] == "GS"
                     else contact["to_node"])
            figure.add_trace(
                go.Scatter(
                    x=[start, start + duration],
                    y=[f"GS: {satellite}"] * 2,
                    mode="lines",
                    name=f"{satellite} SAT-GS",
                    legendgroup=f"gs-{satellite}",
                    showlegend=index == 0,
                    line={"color": colour, "width": 10},
                    customdata=[[contact["contact_id"], gs_id,
                                 float(contact["range_km"]), duration * 60.0]] * 2,
                    hovertemplate=(
                        "Contact: %{customdata[0]}<br>Ground station: %{customdata[1]}"
                        "<br>Time: %{x:.4f} h<br>Range: %{customdata[2]:,.2f} km"
                        "<br>Duration: %{customdata[3]:.2f} min<extra></extra>"
                    ),
                ),
                row=4,
                col=1,
            )

    total_mb = sum(float(event.get("bytes_kb", 0.0)) for event in proofs) / 1024.0
    subtitle = (
        f"{len(proofs)} proof updates · {total_mb:,.2f} MB forwarded · "
        f"{len(ground)} ground events · {len(isl_contacts)} SAT-SAT contacts · "
        f"{sum(len(v) for v in gs_contacts.values())} SAT-GS contacts"
    )
    figure.update_layout(
        title={"text": f"LIOS Channel Lifecycle<br><sup>{satellite_a} ↔ {satellite_b}"
                       f" · {subtitle}</sup>", "x": 0.5},
        height=1050,
        barmode="overlay",
        hovermode="closest",
        template="plotly_white",
        legend={"orientation": "h", "yanchor": "bottom", "y": 1.01,
                "xanchor": "left", "x": 0},
        margin={"l": 180, "r": 90, "t": 125, "b": 70},
    )
    figure.update_yaxes(title_text="Balance (MB)", rangemode="tozero", row=1, col=1)
    figure.update_yaxes(title_text="Forwarded (MB)", rangemode="tozero", row=2, col=1)
    figure.update_yaxes(title_text="Lifecycle", row=3, col=1)
    figure.update_yaxes(title_text="Contacts", row=4, col=1)
    figure.update_xaxes(title_text="Simulation time (hours)", row=4, col=1)
    return figure


def default_output_path(log_path: Path, channel_id: str) -> Path:
    return log_path.parent.parent / "figures" / f"{channel_id}_lifecycle.html"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Visualize one channel lifecycle from a LIOS settlement log."
    )
    parser.add_argument("log", type=Path, help="Path to lios_settlement_log.json")
    parser.add_argument("satellite_1", help="First satellite ID")
    parser.add_argument("satellite_2", help="Second satellite ID")
    parser.add_argument(
        "--contact-plan",
        type=Path,
        help="Contact-plan CSV (default: inferred beside the settlement log)",
    )
    parser.add_argument(
        "--out",
        type=Path,
        help=("Output HTML/PNG/PDF/SVG path "
              "(default: results/figures/<channel>_lifecycle.html)"),
    )
    parser.add_argument(
        "--t-low-fraction",
        type=float,
        default=0.05,
        help="T1 low-balance fraction used for the reference line (default: 0.05)",
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
        contact_plan_path = args.contact_plan or default_contact_plan_path(args.log, data)
        contacts = load_contact_plan(contact_plan_path)
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
    isl_contacts, gs_contacts = select_contacts(contacts, satellite_a, satellite_b)
    output = args.out or default_output_path(args.log, channel_id)
    output.parent.mkdir(parents=True, exist_ok=True)
    if output.suffix.lower() == ".html":
        html_figure = build_html_figure(
            channel_id, satellite_a, satellite_b, offchain, ground, latency,
            isl_contacts, gs_contacts, args.t_low_fraction,
        )
        html_figure.write_html(
            output, include_plotlyjs=True, full_html=True,
            config={"displaylogo": False, "responsive": True},
        )
    else:
        figure = build_figure(
            channel_id, satellite_a, satellite_b, offchain, ground, latency,
            isl_contacts, gs_contacts, args.t_low_fraction,
        )
        figure.savefig(output, dpi=args.dpi)
        plt.close(figure)

    proof_count = sum(
        event.get("event") == "OFFCHAIN_PROOF_UPDATE" for event in offchain
    )
    print(f"Channel: {channel_id}")
    print(f"Proof updates: {proof_count}")
    print(f"Ground settlement events: {len(ground)}")
    print(f"Latency records: {len(latency)}")
    print(f"SAT-SAT contacts: {len(isl_contacts)}")
    print(f"SAT-GS contacts: {sum(len(v) for v in gs_contacts.values())}")
    print(f"Contact plan: {contact_plan_path.resolve()}")
    print(f"Wrote: {output.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

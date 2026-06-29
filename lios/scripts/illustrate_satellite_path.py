#!/usr/bin/env python3
"""Visualize satellite mobility, ground stations, and contact geometry.

The preferred input is a propagation log produced by WindowCalculator via
``propagation_log_path``.  That log already contains satellite ground tracks,
ground-station locations, and contact windows, so this script can mark both
SAT-SAT and SAT-GS contacts without recomputing orbital visibility.
"""
from __future__ import annotations

import argparse
import json
import os
from collections import Counter
from pathlib import Path
from typing import Any

os.environ.setdefault("MPLCONFIGDIR", "/tmp/matplotlib")

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np


REPO_ROOT = Path(__file__).resolve().parents[2]
LIOS_DIR = REPO_ROOT / "lios"
DEFAULT_LOG = LIOS_DIR / "results/logs/lios_constellation_weighted_propagation_log.json"
DEFAULT_OUT = LIOS_DIR / "cache/satellite_gs_mobility_contacts.png"

OP_COLORS = [
    "#4C78A8",
    "#F58518",
    "#54A24B",
    "#E45756",
    "#72B7B2",
    "#B279A2",
    "#FF9DA6",
    "#9D755D",
    "#BAB0AC",
    "#2F4B7C",
]


Track = list[tuple[float, float, float, float]]


def _parse_csv_arg(value: str | None) -> set[str]:
    if not value:
        return set()
    return {item.strip() for item in value.split(",") if item.strip()}


def _load_log(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise SystemExit(f"Propagation log not found: {path}")
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)


def _satellite_tracks(log: dict[str, Any]) -> dict[str, dict[str, Any]]:
    satellites: dict[str, dict[str, Any]] = {}
    for sat in log.get("satellites", []):
        sat_id = sat["sat_id"]
        track: Track = [
            (float(t), float(lat), float(lon), float(alt))
            for t, lat, lon, alt in sat.get("track", [])
        ]
        if track:
            satellites[sat_id] = {
                "sat_id": sat_id,
                "operator_id": sat.get("operator_id", ""),
                "track": track,
            }
    return satellites


def _ground_stations(log: dict[str, Any]) -> dict[str, dict[str, Any]]:
    stations: dict[str, dict[str, Any]] = {}
    for gs in log.get("ground_stations", []):
        stations[gs["gs_id"]] = {
            "gs_id": gs["gs_id"],
            "operator_id": gs.get("operator_id", ""),
            "lat": float(gs["lat"]),
            "lon": float(gs["lon"]),
            "alt_m": float(gs.get("alt_m", 0.0)),
        }
    return stations


def _operator_colors(satellites: dict[str, dict[str, Any]], ground_stations: dict[str, dict[str, Any]]) -> dict[str, str]:
    operators = sorted(
        {
            item.get("operator_id", "")
            for item in [*satellites.values(), *ground_stations.values()]
            if item.get("operator_id", "")
        }
    )
    return {operator: OP_COLORS[idx % len(OP_COLORS)] for idx, operator in enumerate(operators)}


def _split_dateline(points: Track) -> list[Track]:
    if not points:
        return []
    segments: list[Track] = [[points[0]]]
    for previous, current in zip(points, points[1:]):
        if abs(current[2] - previous[2]) > 180.0:
            segments.append([current])
        else:
            segments[-1].append(current)
    return segments


def _position_at(track: Track, t_sec: float) -> tuple[float, float]:
    times = np.array([p[0] for p in track])
    lats = np.array([p[1] for p in track])
    lons = np.array([p[2] for p in track])
    return float(np.interp(t_sec, times, lats)), float(np.interp(t_sec, times, lons))


def _contact_kind(contact: dict[str, Any]) -> str:
    from_type = contact.get("node_type_from", "")
    to_type = contact.get("node_type_to", "")
    if from_type == "SAT" and to_type == "SAT":
        return "sat-sat"
    if from_type == "GS" or to_type == "GS":
        return "sat-gs"
    return "other"


def _contact_nodes(contact: dict[str, Any]) -> tuple[str, str]:
    return str(contact["from_node"]), str(contact["to_node"])


def _select_satellites(
    satellites: dict[str, dict[str, Any]],
    contacts: list[dict[str, Any]],
    requested: set[str],
    pair_nodes: set[str],
    max_sats: int,
) -> set[str]:
    selected = {node for node in requested | pair_nodes if node in satellites}
    if selected:
        return selected

    counts: Counter[str] = Counter()
    for contact in contacts:
        a, b = _contact_nodes(contact)
        if a in satellites:
            counts[a] += 1
        if b in satellites:
            counts[b] += 1
    if counts:
        return {sat_id for sat_id, _count in counts.most_common(max_sats)}
    return set(list(satellites)[:max_sats])


def _select_ground_stations(
    ground_stations: dict[str, dict[str, Any]],
    contacts: list[dict[str, Any]],
    requested: set[str],
    selected_sats: set[str],
    max_gs: int,
) -> set[str]:
    selected = {node for node in requested if node in ground_stations}
    if selected:
        return selected

    counts: Counter[str] = Counter()
    for contact in contacts:
        if _contact_kind(contact) != "sat-gs":
            continue
        a, b = _contact_nodes(contact)
        if a in ground_stations and b in selected_sats:
            counts[a] += 1
        elif b in ground_stations and a in selected_sats:
            counts[b] += 1
    if counts:
        return {gs_id for gs_id, _count in counts.most_common(max_gs)}
    return set(list(ground_stations)[:max_gs])


def _filter_contacts(
    contacts: list[dict[str, Any]],
    selected_sats: set[str],
    selected_gs: set[str],
    contact_type: str,
    src: str | None,
    dst: str | None,
    max_contacts: int,
) -> list[dict[str, Any]]:
    filtered: list[dict[str, Any]] = []
    requested_pair = {src, dst} if src and dst else set()
    for contact in contacts:
        kind = _contact_kind(contact)
        if contact_type != "all" and kind != contact_type:
            continue
        a, b = _contact_nodes(contact)
        if requested_pair and {a, b} != requested_pair:
            continue
        if kind == "sat-sat":
            if a not in selected_sats or b not in selected_sats:
                continue
        elif kind == "sat-gs":
            if not (
                (a in selected_gs and b in selected_sats)
                or (b in selected_gs and a in selected_sats)
            ):
                continue
        else:
            continue
        filtered.append(contact)

    filtered.sort(key=lambda c: (float(c.get("start_time_sec", 0.0)), str(c.get("contact_id", ""))))
    return filtered[:max_contacts]


def _draw_base_map(ax: plt.Axes) -> None:
    ax.set_xlim(-180, 180)
    ax.set_ylim(-90, 90)
    ax.set_xticks(range(-180, 181, 30))
    ax.set_yticks(range(-90, 91, 15))
    ax.grid(color="#d8d8d8", linewidth=0.7, alpha=0.8)
    ax.axhline(0, color="#888888", linewidth=0.8)
    ax.axvline(0, color="#888888", linewidth=0.8)
    ax.set_xlabel("Longitude (deg)")
    ax.set_ylabel("Latitude (deg)")


def _draw_satellite_track(
    ax: plt.Axes,
    sat: dict[str, Any],
    color: str,
    annotate: bool,
) -> None:
    track: Track = sat["track"]
    first = True
    for segment in _split_dateline(track):
        if len(segment) < 2:
            continue
        ax.plot(
            [p[2] for p in segment],
            [p[1] for p in segment],
            color=color,
            linewidth=1.15,
            alpha=0.82,
            label=f"{sat['operator_id']} satellite" if first else None,
        )
        first = False
    ax.scatter(track[0][2], track[0][1], color=color, marker="o", s=24, zorder=4)
    ax.scatter(track[-1][2], track[-1][1], color=color, marker="x", s=36, zorder=4)
    if annotate:
        ax.text(track[0][2], track[0][1], sat["sat_id"], fontsize=6, color=color)


def _draw_ground_stations(
    ax: plt.Axes,
    ground_stations: dict[str, dict[str, Any]],
    selected_gs: set[str],
    op_colors: dict[str, str],
    annotate: bool,
) -> None:
    for gs_id in sorted(selected_gs):
        gs = ground_stations[gs_id]
        color = op_colors.get(gs.get("operator_id", ""), "#555555")
        ax.scatter(gs["lon"], gs["lat"], color=color, marker="^", s=58, edgecolor="black", linewidth=0.4, zorder=5)
        if annotate:
            ax.text(gs["lon"], gs["lat"], gs_id, fontsize=6, color="#222222")


def _draw_contact(
    ax: plt.Axes,
    contact: dict[str, Any],
    satellites: dict[str, dict[str, Any]],
    ground_stations: dict[str, dict[str, Any]],
    annotate: bool,
) -> bool:
    kind = _contact_kind(contact)
    a, b = _contact_nodes(contact)
    midpoint = (float(contact["start_time_sec"]) + float(contact["end_time_sec"])) / 2.0
    color = "#2CA02C" if kind == "sat-sat" else "#7B3FB2"
    label = "SAT-SAT contact" if kind == "sat-sat" else "SAT-GS contact"

    if kind == "sat-sat":
        if a not in satellites or b not in satellites:
            return False
        lat_a, lon_a = _position_at(satellites[a]["track"], midpoint)
        lat_b, lon_b = _position_at(satellites[b]["track"], midpoint)
    elif kind == "sat-gs":
        sat_id = a if a in satellites else b
        gs_id = b if sat_id == a else a
        if sat_id not in satellites or gs_id not in ground_stations:
            return False
        lat_a, lon_a = _position_at(satellites[sat_id]["track"], midpoint)
        gs = ground_stations[gs_id]
        lat_b, lon_b = gs["lat"], gs["lon"]
    else:
        return False

    if abs(lon_a - lon_b) > 180.0:
        return False

    ax.plot([lon_a, lon_b], [lat_a, lat_b], color=color, linewidth=0.85, alpha=0.34, zorder=2)
    ax.scatter(
        [(lon_a + lon_b) / 2.0],
        [(lat_a + lat_b) / 2.0],
        color=color,
        marker=".",
        s=12,
        alpha=0.65,
        zorder=3,
        label=label,
    )
    if annotate:
        ax.text(
            (lon_a + lon_b) / 2.0,
            (lat_a + lat_b) / 2.0,
            str(contact.get("contact_id", "")),
            fontsize=5,
            color=color,
        )
    return True


def _dedupe_legend(ax: plt.Axes) -> None:
    handles, labels = ax.get_legend_handles_labels()
    seen: set[str] = set()
    deduped_handles = []
    deduped_labels = []
    for handle, label in zip(handles, labels):
        if not label or label in seen:
            continue
        seen.add(label)
        deduped_handles.append(handle)
        deduped_labels.append(label)
    if deduped_handles:
        ax.legend(deduped_handles, deduped_labels, loc="lower left", fontsize=7)


def plot_mobility(
    log: dict[str, Any],
    selected_sats: set[str],
    selected_gs: set[str],
    selected_contacts: list[dict[str, Any]],
    out_path: Path,
    annotate: bool,
) -> int:
    satellites = _satellite_tracks(log)
    ground_stations = _ground_stations(log)
    op_colors = _operator_colors(satellites, ground_stations)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    fig, ax = plt.subplots(figsize=(15, 7.8))
    _draw_base_map(ax)

    for sat_id in sorted(selected_sats):
        sat = satellites[sat_id]
        color = op_colors.get(sat.get("operator_id", ""), "#555555")
        _draw_satellite_track(ax, sat, color, annotate)

    _draw_ground_stations(ax, ground_stations, selected_gs, op_colors, annotate)

    drawn_contacts = 0
    for contact in selected_contacts:
        if _draw_contact(ax, contact, satellites, ground_stations, annotate):
            drawn_contacts += 1

    sat_sat_count = sum(1 for c in selected_contacts if _contact_kind(c) == "sat-sat")
    sat_gs_count = sum(1 for c in selected_contacts if _contact_kind(c) == "sat-gs")
    epoch = log.get("epoch", "unknown epoch")
    duration_h = float(log.get("duration_sec", 0.0)) / 3600.0
    ax.set_title(
        "Satellite mobility, ground stations, and contact geometry\n"
        f"epoch={epoch}, duration={duration_h:.2f} h, "
        f"sats={len(selected_sats)}, GS={len(selected_gs)}, "
        f"contacts={drawn_contacts} ({sat_sat_count} SAT-SAT, {sat_gs_count} SAT-GS)",
        fontsize=11,
    )
    _dedupe_legend(ax)
    fig.tight_layout()
    fig.savefig(out_path, dpi=220)
    plt.close(fig)
    return drawn_contacts


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Plot satellite ground tracks, ground stations, and SAT-SAT/SAT-GS contacts from a propagation log."
    )
    parser.add_argument("--log", type=Path, default=DEFAULT_LOG, help="WindowCalculator propagation_log JSON.")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT, help="Output image path.")
    parser.add_argument("--src", default="", help="Optional source node. With --dst, plots only this pair's contacts.")
    parser.add_argument("--dst", default="", help="Optional destination node. With --src, plots only this pair's contacts.")
    parser.add_argument("--sats", default="", help="Comma-separated satellite IDs to draw. Defaults to top contact participants.")
    parser.add_argument("--ground-stations", default="", help="Comma-separated GS IDs to draw. Defaults to GS contacting selected sats.")
    parser.add_argument("--contact-type", choices=["all", "sat-sat", "sat-gs"], default="all")
    parser.add_argument("--max-sats", type=int, default=20)
    parser.add_argument("--max-ground-stations", type=int, default=20)
    parser.add_argument("--max-contacts", type=int, default=120)
    parser.add_argument("--annotate", action="store_true", help="Annotate node/contact IDs. Best for small selections.")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    log = _load_log(args.log)
    satellites = _satellite_tracks(log)
    ground_stations = _ground_stations(log)
    contacts = list(log.get("contacts", []))

    requested_sats = _parse_csv_arg(args.sats)
    requested_gs = _parse_csv_arg(args.ground_stations)
    pair_nodes = {node for node in (args.src, args.dst) if node}
    contacts_for_selection = [
        contact for contact in contacts
        if args.contact_type == "all" or _contact_kind(contact) == args.contact_type
    ]

    selected_sats = _select_satellites(
        satellites,
        contacts_for_selection,
        requested_sats,
        pair_nodes,
        args.max_sats,
    )
    selected_gs = _select_ground_stations(
        ground_stations,
        contacts_for_selection,
        requested_gs | {node for node in pair_nodes if node in ground_stations},
        selected_sats,
        args.max_ground_stations,
    )
    selected_contacts = _filter_contacts(
        contacts,
        selected_sats,
        selected_gs,
        args.contact_type,
        args.src or None,
        args.dst or None,
        args.max_contacts,
    )

    drawn_contacts = plot_mobility(
        log,
        selected_sats,
        selected_gs,
        selected_contacts,
        args.out,
        args.annotate,
    )

    print(f"Wrote mobility/contact visualization to {args.out}")
    print(
        f"Selected {len(selected_sats)} satellites, {len(selected_gs)} ground stations, "
        f"{len(selected_contacts)} contacts ({drawn_contacts} drawn after dateline filtering)."
    )
    for contact in selected_contacts[:20]:
        print(
            f"{contact.get('contact_id', '')}: {_contact_kind(contact)} "
            f"{contact.get('from_node')} -> {contact.get('to_node')} "
            f"{float(contact.get('start_time_sec', 0.0)) / 3600.0:.2f}h-"
            f"{float(contact.get('end_time_sec', 0.0)) / 3600.0:.2f}h "
            f"range={float(contact.get('range_km', 0.0)):.1f} km"
        )


if __name__ == "__main__":
    main()

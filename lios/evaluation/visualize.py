#!/usr/bin/env python3
"""Interactive HTML dashboard for LIOS protocol experiment results.

Panels:
  Overview          — Jain fairness + OOS fraction per experiment config
  Operator Traffic  — Bytes forwarded vs received per operator × config
  Settlement        — Latency stats, settlement event counts
  Adversarial       — Penalty events and free-rider prevention rate
  Throughput        — Throughput vs fairness trade-off scatter
  Hash Chain        — Storage overhead per config
  Satellite Globe   — Animated world map: orbital tracks, satellite positions,
                      active ISL links and GS uplinks per time step
  Contact Timeline  — Gantt chart of all ISL and GS contact windows

Usage:
  python evaluation/visualize.py [--results results/] [--out results/dashboard.html]
  python evaluation/visualize.py --propagation-log results/propagation_log.json
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).parent.parent))

import plotly.graph_objects as go
from plotly.subplots import make_subplots


# ── Colour palette ─────────────────────────────────────────────────────────────

COLOURS = [
    "#4C78A8", "#F58518", "#E45756", "#72B7B2",
    "#54A24B", "#EECA3B", "#B279A2", "#FF9DA6",
]

# ── Data loading ───────────────────────────────────────────────────────────────

def load_results(results_dir: Path) -> List[Dict[str, Any]]:
    logs_dir = results_dir / "logs"
    if not logs_dir.exists():
        return []
    records = []
    for path in sorted(logs_dir.glob("*_metrics.json")):
        with path.open() as f:
            metrics = json.load(f)
        name = path.stem.replace("_metrics", "")
        records.append({"name": name, "metrics": metrics})
    return records


def load_propagation_log(results_dir: Path) -> Optional[Dict]:
    """Find the best available propagation log, in priority order:
    1. results/propagation_log.json  (manually generated via compute_windows.py)
    2. results/logs/baseline_propagation_log.json  (from run_experiments.py)
    3. Any results/logs/*_propagation_log.json, shortest-duration config first
    """
    direct = results_dir / "propagation_log.json"
    if direct.exists():
        with direct.open() as f:
            return json.load(f)

    logs_dir = results_dir / "logs"
    if not logs_dir.exists():
        return None

    # Prefer baseline; fall back to whichever file has the smallest duration (fastest config)
    candidates = sorted(logs_dir.glob("*_propagation_log.json"))
    if not candidates:
        return None

    preferred = next((p for p in candidates if p.name.startswith("baseline")), candidates[0])
    with preferred.open() as f:
        return json.load(f)


def load_contact_traffic(results_dir: Path) -> Optional[Dict]:
    """Load contact_traffic from logs/baseline_contact_traffic.json or any available file."""
    logs_dir = results_dir / "logs"
    if not logs_dir.exists():
        return None
    candidates = sorted(logs_dir.glob("*_contact_traffic.json"))
    if not candidates:
        return None
    preferred = next((p for p in candidates if p.name.startswith("baseline")), candidates[0])
    with preferred.open() as f:
        return json.load(f)


def _synthetic_results() -> List[Dict[str, Any]]:
    configs = [
        ("baseline",      0.973, 0.008, 12, 0, 3, 0.50, {"alpha": 420, "beta": 380, "gamma": 410}),
        ("depletion",     0.961, 0.021, 8,  0, 5, 0.95, {"alpha": 760, "beta": 590, "gamma": 730}),
        ("top_up",        0.968, 0.012, 60, 0, 22, 0.80, {"alpha": 1800, "beta": 1650, "gamma": 1720}),
        ("adversarial_1", 0.942, 0.018, 55, 4, 18, 0.70, {"alpha": 1420, "beta": 1100, "gamma": 1380}),
        ("adversarial_2", 0.938, 0.020, 52, 6, 17, 0.70, {"alpha": 1350, "beta": 980,  "gamma": 1300}),
        ("fairness_24h",  0.975, 0.009, 88, 0, 34, 0.60, {"alpha": 2100, "beta": 2050, "gamma": 2080}),
        ("high_density",  0.955, 0.032, 14, 0, 4,  0.99, {"alpha": 920,  "beta": 870,  "gamma": 905}),
    ]
    results = []
    for (name, jain, oos, settle, pen, fwd_ev, load, fwd_by) in configs:
        results.append({
            "name": name,
            "metrics": {
                "jain_fairness_index": jain,
                "oos_fraction": oos,
                "settlement_events": settle,
                "penalty_events": pen,
                "total_forwarding_events": fwd_ev * 100,
                "free_rider_prevention_rate": 1.0 if pen == 0 else 0.83,
                "settlement_latency": {
                    "mean": 172500, "p50": 171200, "p95": 172780,
                    "p99": 172799, "max": 172800, "count": settle,
                },
                "bytes_forwarded_by": fwd_by,
                "bytes_received_by": {op: v * 0.97 for op, v in fwd_by.items()},
                "_synthetic": True,
                "_traffic_load": load,
            },
        })
    return results


# ── Protocol metric figures ────────────────────────────────────────────────────

def _fig_overview(records: List[Dict]) -> go.Figure:
    names = [r["name"] for r in records]
    jain  = [r["metrics"].get("jain_fairness_index", 0) for r in records]
    oos   = [r["metrics"].get("oos_fraction", 0) * 100  for r in records]

    fig = make_subplots(
        rows=1, cols=2,
        subplot_titles=("Jain Fairness Index per Config", "OOS Fraction per Config (%)"),
        horizontal_spacing=0.12,
    )
    fig.add_trace(go.Bar(
        x=names, y=jain,
        marker_color=[COLOURS[i % len(COLOURS)] for i in range(len(names))],
        text=[f"{v:.3f}" for v in jain], textposition="outside", name="Jain Index",
    ), row=1, col=1)
    fig.add_shape(type="line", x0=-0.5, x1=len(names)-0.5, y0=0.95, y1=0.95,
                  line=dict(color="red", dash="dash", width=1.5), row=1, col=1)
    fig.add_annotation(x=len(names)-0.5, y=0.95, xref="x", yref="y",
                       text="Target 0.95", showarrow=False,
                       font=dict(color="red", size=10), xanchor="right", yanchor="bottom",
                       row=1, col=1)
    fig.add_trace(go.Bar(
        x=names, y=oos, marker_color="#E45756",
        text=[f"{v:.2f}%" for v in oos], textposition="outside", name="OOS %",
    ), row=1, col=2)
    fig.add_shape(type="line", x0=-0.5, x1=len(names)-0.5, y0=2.0, y1=2.0,
                  line=dict(color="green", dash="dash", width=1.5), row=1, col=2)
    fig.add_annotation(x=len(names)-0.5, y=2.0, xref="x2", yref="y2",
                       text="Target <2%", showarrow=False,
                       font=dict(color="green", size=10), xanchor="right", yanchor="bottom")
    fig.update_yaxes(range=[0, 1.08], title_text="Jain Index", row=1, col=1)
    fig.update_yaxes(title_text="OOS (%)", row=1, col=2)
    fig.update_layout(title="LIOS Protocol — Experiment Overview", showlegend=False,
                      height=420, margin=dict(t=80, b=60, l=60, r=30))
    return fig


def _fig_operator_traffic(records: List[Dict]) -> go.Figure:
    operators: List[str] = []
    for r in records:
        for op in r["metrics"].get("bytes_forwarded_by", {}):
            if op not in operators:
                operators.append(op)
    fig = go.Figure()
    for i, r in enumerate(records):
        fwd = r["metrics"].get("bytes_forwarded_by", {})
        rcv = r["metrics"].get("bytes_received_by", {})
        if not fwd:
            continue
        colour = COLOURS[i % len(COLOURS)]
        fig.add_trace(go.Bar(name=f"{r['name']} forwarded", x=operators,
                             y=[fwd.get(op, 0) for op in operators],
                             marker_color=colour, opacity=0.9, legendgroup=r["name"]))
        fig.add_trace(go.Bar(name=f"{r['name']} received", x=operators,
                             y=[rcv.get(op, 0) for op in operators],
                             marker_color=colour, opacity=0.45,
                             marker_pattern_shape="/", legendgroup=r["name"]))
    fig.update_layout(barmode="group", title="LIOS Protocol — Operator Traffic Volume (KB)",
                      xaxis_title="Operator", yaxis_title="Bytes (KB)", height=440,
                      legend=dict(groupclick="toggleitem", font=dict(size=10)),
                      margin=dict(t=80, b=60, l=60, r=30))
    return fig


def _fig_settlement(records: List[Dict]) -> go.Figure:
    names = [r["name"] for r in records]
    settle_counts = [r["metrics"].get("settlement_events", 0) for r in records]
    lat_mean = [r["metrics"].get("settlement_latency", {}).get("mean", 0) / 3600 for r in records]
    lat_p95  = [r["metrics"].get("settlement_latency", {}).get("p95",  0) / 3600 for r in records]
    fig = make_subplots(rows=1, cols=2,
                        subplot_titles=("Settlement Events per Config", "Settlement Latency (hours)"),
                        horizontal_spacing=0.14)
    fig.add_trace(go.Bar(x=names, y=settle_counts, marker_color="#72B7B2",
                         text=settle_counts, textposition="outside", name="Settlements"),
                  row=1, col=1)
    fig.add_trace(go.Bar(x=names, y=lat_mean, name="Mean latency (h)", marker_color="#4C78A8",
                         text=[f"{v:.1f}h" for v in lat_mean], textposition="outside"),
                  row=1, col=2)
    fig.add_trace(go.Scatter(x=names, y=lat_p95, mode="markers", name="p95 latency (h)",
                             marker=dict(color="#E45756", size=10, symbol="diamond")),
                  row=1, col=2)
    fig.update_yaxes(title_text="Count", row=1, col=1)
    fig.update_yaxes(title_text="Hours", row=1, col=2)
    fig.update_layout(title="LIOS Protocol — Settlement Analytics", height=420,
                      margin=dict(t=80, b=60, l=60, r=30), legend=dict(font=dict(size=10)))
    return fig


def _fig_adversarial(records: List[Dict]) -> go.Figure:
    names      = [r["name"] for r in records]
    penalties  = [r["metrics"].get("penalty_events", 0) for r in records]
    frrp_vals  = [r["metrics"].get("free_rider_prevention_rate", 1.0) for r in records]
    fig = make_subplots(rows=1, cols=2,
                        subplot_titles=("Penalty Events per Config",
                                        "Free-Rider Prevention Rate"),
                        horizontal_spacing=0.14)
    fig.add_trace(go.Bar(x=names, y=penalties,
                         marker_color=["#E45756" if v > 0 else "#72B7B2" for v in penalties],
                         text=penalties, textposition="outside", name="Penalties"),
                  row=1, col=1)
    fig.add_trace(go.Bar(x=names, y=frrp_vals,
                         marker_color=["#54A24B" if v >= 1.0 else
                                       ("#EECA3B" if v >= 0.75 else "#E45756")
                                       for v in frrp_vals],
                         text=[f"{v:.0%}" for v in frrp_vals], textposition="outside",
                         name="Prevention rate"),
                  row=1, col=2)
    fig.add_shape(type="line", x0=-0.5, x1=len(names)-0.5, y0=1.0, y1=1.0,
                  line=dict(color="green", dash="dash", width=1.2), row=1, col=2)
    fig.update_yaxes(title_text="Penalty events", row=1, col=1)
    fig.update_yaxes(title_text="Rate", range=[0, 1.08], row=1, col=2)
    fig.update_layout(title="LIOS Protocol — Adversarial Detection", height=420,
                      showlegend=False, margin=dict(t=80, b=60, l=60, r=30))
    return fig


def _fig_throughput_fairness(records: List[Dict]) -> go.Figure:
    fig = go.Figure()
    for i, r in enumerate(records):
        jain   = r["metrics"].get("jain_fairness_index", 0)
        events = r["metrics"].get("total_forwarding_events", 0)
        load   = r["metrics"].get("_traffic_load",
                 r["metrics"].get("simulation_stats", {}).get("traffic_load_fraction", 0.5))
        name   = r["name"]
        fig.add_trace(go.Scatter(
            x=[events], y=[jain], mode="markers+text", name=name,
            text=[name], textposition="top center",
            marker=dict(size=18, color=COLOURS[i % len(COLOURS)],
                        line=dict(width=1, color="white")),
            customdata=[[load, r["metrics"].get("settlement_events", 0)]],
            hovertemplate=(f"<b>{name}</b><br>Forwarding events: %{{x}}<br>"
                           "Jain Index: %{y:.4f}<br>Traffic load: %{customdata[0]:.0%}<br>"
                           "Settlements: %{customdata[1]}<extra></extra>"),
        ))
    fig.add_hline(y=0.95, line_dash="dash", line_color="red", line_width=1.5,
                  annotation_text="Fairness target (0.95)", annotation_position="right",
                  annotation_font_color="red")
    fig.update_layout(title="LIOS Protocol — Throughput vs Fairness Trade-off",
                      xaxis_title="Total forwarding events (throughput proxy)",
                      yaxis_title="Jain Fairness Index", yaxis_range=[0.88, 1.02],
                      height=480, showlegend=False, margin=dict(t=80, b=70, l=70, r=30))
    return fig


def _fig_forwarding_log_overhead(records: List[Dict]) -> go.Figure:
    names   = [r["name"] for r in records]
    entries = [r["metrics"].get("total_forwarding_events", 0) for r in records]
    fig = go.Figure(go.Bar(x=names, y=entries, name="Forwarding log entries",
                           marker_color="#B279A2", opacity=0.85))
    fig.update_yaxes(title_text="Log entries")
    fig.update_yaxes(title_text="Hash chain entries", secondary_y=True)
    fig.update_layout(title="LIOS Protocol — Hash Chain Storage Overhead",
                      height=400, margin=dict(t=80, b=60, l=70, r=70),
                      legend=dict(font=dict(size=10)))
    return fig


# ── Satellite mobility figures ─────────────────────────────────────────────────

def _fig_satellite_globe(prop_log: Dict) -> go.Figure:
    """Animated world map: ground tracks, satellite positions, ISL and GS links."""
    satellites    = prop_log["satellites"]
    ground_stations = prop_log["ground_stations"]
    contacts      = prop_log["contacts"]
    duration_sec  = prop_log.get("duration_sec", 5400)
    epoch         = prop_log.get("epoch", "")[:19]

    if not satellites or not satellites[0]["track"]:
        return go.Figure().update_layout(title="No propagation data available")

    # ── Build lookups ──────────────────────────────────────────────────────────

    operators = sorted(set(s["operator_id"] for s in satellites))
    op_colour = {op: COLOURS[i % len(COLOURS)] for i, op in enumerate(operators)}

    # sat_id → {t_sec → (lat, lon)}
    sat_pos: Dict[str, Dict[float, Tuple[float, float]]] = {}
    for s in satellites:
        sat_pos[s["sat_id"]] = {pt[0]: (pt[1], pt[2]) for pt in s["track"]}

    gs_map: Dict[str, Tuple[float, float]] = {
        gs["gs_id"]: (gs["lat"], gs["lon"]) for gs in ground_stations
    }

    # ── Choose animation frames (cap at 90 for HTML size) ─────────────────────
    all_times = sorted({pt[0] for s in satellites for pt in s["track"]})
    step = max(1, len(all_times) // 90)
    frame_times = all_times[::step]

    # ── Static traces ──────────────────────────────────────────────────────────
    data: List[go.BaseTraceType] = []

    # Operator legend dummies (invisible markers just to populate the legend)
    for op in operators:
        data.append(go.Scattergeo(
            lat=[None], lon=[None], mode="markers",
            marker=dict(size=10, color=op_colour[op], symbol="circle"),
            name=f"Operator {op}", showlegend=True,
        ))

    # Ground tracks (one line per satellite, very faint)
    for sat in satellites:
        lats = [pt[1] for pt in sat["track"]]
        lons = [pt[2] for pt in sat["track"]]
        data.append(go.Scattergeo(
            lat=lats, lon=lons, mode="lines",
            line=dict(color=op_colour[sat["operator_id"]], width=1),
            opacity=0.20, showlegend=False, hoverinfo="skip",
            name=f"{sat['sat_id']} track",
        ))

    # Ground station markers (fixed triangles)
    data.append(go.Scattergeo(
        lat=[gs["lat"] for gs in ground_stations],
        lon=[gs["lon"] for gs in ground_stations],
        mode="markers",
        marker=dict(symbol="triangle-up", size=9,
                    color=[op_colour.get(gs["operator_id"], "#888") for gs in ground_stations],
                    line=dict(color="white", width=1)),
        text=[f"{gs['gs_id']}<br>({gs['operator_id']})" for gs in ground_stations],
        hovertemplate="%{text}<extra>Ground Station</extra>",
        name="Ground Stations", showlegend=True,
    ))

    n_static = len(data)  # everything above is static

    # ── Animated traces (initial state at frame_times[0]) ─────────────────────
    t0 = frame_times[0]

    def _isl_lines(t: float) -> Tuple[List, List, List]:
        lats, lons, tips = [], [], []
        for c in contacts:
            if c["node_type_from"] != "SAT" or c["node_type_to"] != "SAT":
                continue
            if c["start_time_sec"] <= t <= c["end_time_sec"]:
                p1 = sat_pos.get(c["from_node"], {}).get(t)
                p2 = sat_pos.get(c["to_node"], {}).get(t)
                if p1 and p2:
                    lats += [p1[0], p2[0], None]
                    lons += [p1[1], p2[1], None]
                    tips.append(f"{c['from_node']} ↔ {c['to_node']}<br>"
                                f"{c['range_km']:.0f} km · {c['capacity_kbps']:.0f} kbps")
        return lats, lons, tips

    def _gs_lines(t: float) -> Tuple[List, List]:
        lats, lons = [], []
        for c in contacts:
            if c["node_type_from"] == "GS":
                gp = gs_map.get(c["from_node"])
                sp = sat_pos.get(c["to_node"], {}).get(t)
            elif c["node_type_to"] == "GS":
                gp = gs_map.get(c["to_node"])
                sp = sat_pos.get(c["from_node"], {}).get(t)
            else:
                continue
            if c["start_time_sec"] <= t <= c["end_time_sec"] and gp and sp:
                lats += [gp[0], sp[0], None]
                lons += [gp[1], sp[1], None]
        return lats, lons

    # Satellite current-position markers (one trace per satellite)
    for sat in satellites:
        pos = sat_pos[sat["sat_id"]].get(t0)
        short = sat["sat_id"].split("-", 1)[-1].upper()
        data.append(go.Scattergeo(
            lat=[pos[0]] if pos else [],
            lon=[pos[1]] if pos else [],
            mode="markers+text",
            marker=dict(size=11, color=op_colour[sat["operator_id"]],
                        line=dict(color="white", width=1.5)),
            text=[short] if pos else [],
            textposition="top center",
            textfont=dict(size=8, color="#2c3e50"),
            name=sat["sat_id"], showlegend=False,
            hovertemplate=f"{sat['sat_id']}<br>Lat: %{{lat:.2f}}°  Lon: %{{lon:.2f}}°"
                          f"<extra>{sat['operator_id']}</extra>",
        ))

    # Active ISL link lines
    il, iln, _ = _isl_lines(t0)
    data.append(go.Scattergeo(
        lat=il, lon=iln, mode="lines",
        line=dict(color="#F5C518", width=2.5), opacity=0.85,
        name="ISL active", showlegend=True, hoverinfo="skip",
    ))

    # Active GS uplink lines
    gl, gln = _gs_lines(t0)
    data.append(go.Scattergeo(
        lat=gl, lon=gln, mode="lines",
        line=dict(color="#54A24B", width=1.8, dash="dot"), opacity=0.80,
        name="GS uplink", showlegend=True, hoverinfo="skip",
    ))

    n_animated = len(satellites) + 2          # sat markers + ISL trace + GS trace
    animated_indices = list(range(n_static, n_static + n_animated))

    # ── Animation frames ───────────────────────────────────────────────────────
    frames = []
    for t in frame_times:
        fd: List[go.BaseTraceType] = []

        for sat in satellites:
            pos = sat_pos[sat["sat_id"]].get(t)
            short = sat["sat_id"].split("-", 1)[-1].upper()
            fd.append(go.Scattergeo(
                lat=[pos[0]] if pos else [],
                lon=[pos[1]] if pos else [],
                mode="markers+text",
                marker=dict(size=11, color=op_colour[sat["operator_id"]],
                            line=dict(color="white", width=1.5)),
                text=[short] if pos else [],
                textposition="top center",
                textfont=dict(size=8),
            ))

        il, iln, _ = _isl_lines(t)
        fd.append(go.Scattergeo(lat=il, lon=iln, mode="lines",
                                line=dict(color="#F5C518", width=2.5)))

        gl, gln = _gs_lines(t)
        fd.append(go.Scattergeo(lat=gl, lon=gln, mode="lines",
                                line=dict(color="#54A24B", width=1.8, dash="dot")))

        frames.append(go.Frame(
            data=fd,
            traces=animated_indices,
            name=str(int(t)),
            layout=go.Layout(
                title_text=(f"LIOS — Satellite Positions  "
                            f"T+{int(t)//60:02d}:{int(t)%60:02d}  "
                            f"(epoch {epoch} UTC)")
            ),
        ))

    # ── Slider steps ───────────────────────────────────────────────────────────
    slider_steps = [
        dict(
            args=[[str(int(t))], dict(frame=dict(duration=120, redraw=True),
                                     mode="immediate",
                                     transition=dict(duration=0))],
            label=f"{int(t)//60}m",
            method="animate",
        )
        for t in frame_times
    ]

    fig = go.Figure(data=data, frames=frames)
    fig.update_layout(
        title=(f"LIOS — Satellite Mobility & Contacts  "
               f"(epoch {epoch} UTC · {duration_sec/60:.0f} min)"),
        geo=dict(
            showland=True, landcolor="#e8eaed",
            showocean=True, oceancolor="#c8dff0",
            showcoastlines=True, coastlinecolor="#9aacb8",
            showlakes=True, lakecolor="#c8dff0",
            showcountries=True, countrycolor="#d0d4d8",
            projection_type="natural earth",
            bgcolor="#f5f6fa",
            lonaxis=dict(range=[-180, 180], showgrid=True, gridcolor="rgba(0,0,0,0.06)"),
            lataxis=dict(range=[-90, 90],   showgrid=True, gridcolor="rgba(0,0,0,0.06)"),
        ),
        height=600,
        margin=dict(t=80, b=80, l=0, r=0),
        legend=dict(x=0.01, y=0.99, bgcolor="rgba(255,255,255,0.88)",
                    bordercolor="#ccc", borderwidth=1, font=dict(size=10)),
        updatemenus=[dict(
            type="buttons", showactive=False,
            y=-0.08, x=0.5, xanchor="center", yanchor="top",
            pad=dict(t=5),
            buttons=[
                dict(label="▶ Play", method="animate",
                     args=[None, dict(frame=dict(duration=120, redraw=True),
                                     fromcurrent=True, transition=dict(duration=0))]),
                dict(label="⏸ Pause", method="animate",
                     args=[[None], dict(frame=dict(duration=0, redraw=False),
                                       mode="immediate", transition=dict(duration=0))]),
            ],
        )],
        sliders=[dict(
            active=0,
            currentvalue=dict(prefix="T+", suffix=" s", font=dict(size=11), visible=True),
            pad=dict(t=50, b=10),
            steps=slider_steps,
        )],
    )
    return fig


def _traffic_colour(kb: float, max_kb: float, zero_colour: str, lo: str, hi: str) -> str:
    """Interpolate a hex colour between lo and hi based on kb / max_kb."""
    if max_kb <= 0 or kb <= 0:
        return zero_colour
    t = min(1.0, kb / max_kb)
    lo_rgb = tuple(int(lo[i:i+2], 16) for i in (1, 3, 5))
    hi_rgb = tuple(int(hi[i:i+2], 16) for i in (1, 3, 5))
    r, g, b = (int(lo_rgb[i] + t * (hi_rgb[i] - lo_rgb[i])) for i in range(3))
    return f"#{r:02x}{g:02x}{b:02x}"


def _fig_contact_timeline(prop_log: Dict,
                          contact_traffic: Optional[Dict] = None) -> go.Figure:
    """Gantt chart of all ISL and GS contact windows.

    When contact_traffic is provided, bars are coloured by traffic intensity:
      ISL  — light→dark blue  (no traffic = grey)
      GS   — light→dark green (no traffic = grey)
    Traffic volume and flow count are shown in hover tooltips.
    """
    contacts     = prop_log["contacts"]
    duration_sec = prop_log.get("duration_sec", 5400)
    dur_min      = duration_sec / 60.0
    ct           = contact_traffic or {}

    if not contacts:
        return go.Figure().update_layout(title="No contact data available")

    isl = sorted(
        [c for c in contacts if c["node_type_from"] == "SAT" and c["node_type_to"] == "SAT"],
        key=lambda c: (c["from_node"], c["to_node"], c["start_time_sec"]),
    )
    gs_c = sorted(
        [c for c in contacts if c["node_type_from"] == "GS" or c["node_type_to"] == "GS"],
        key=lambda c: (c["from_node"] if c["node_type_from"] == "GS" else c["to_node"],
                       c["start_time_sec"]),
    )

    isl_pairs = sorted(set(f"{c['from_node']} ↔ {c['to_node']}" for c in isl))
    gs_nodes  = sorted(set(
        c["from_node"] if c["node_type_from"] == "GS" else c["to_node"]
        for c in gs_c
    ))

    # Traffic colour scales
    has_traffic   = bool(ct)
    max_isl_kb    = max((ct.get(c["contact_id"], {}).get("traffic_kb", 0) for c in isl),
                        default=0.0) if has_traffic else 0.0
    max_gs_kb     = max((ct.get(c["contact_id"], {}).get("traffic_kb", 0) for c in gs_c),
                        default=0.0) if has_traffic else 0.0
    GREY_UNUSED   = "#d4d4d4"
    ISL_LO, ISL_HI = "#c6dbef", "#084594"   # Blues
    GS_LO,  GS_HI  = "#c7e9c0", "#005a32"   # Greens

    n_isl_rows = max(len(isl_pairs), 1)
    n_gs_rows  = max(len(gs_nodes), 1)
    row_h_isl  = max(0.30, min(0.50, n_isl_rows / (n_isl_rows + n_gs_rows)))

    fig = make_subplots(
        rows=2, cols=1,
        subplot_titles=(
            f"ISL Contacts  ({len(isl)} windows · {len(isl_pairs)} pairs)",
            f"GS Contacts   ({len(gs_c)} windows · {len(gs_nodes)} stations)",
        ),
        row_heights=[row_h_isl, 1.0 - row_h_isl],
        vertical_spacing=0.10,
    )

    pair_idx = {p: i for i, p in enumerate(isl_pairs)}
    for c in isl:
        pair   = f"{c['from_node']} ↔ {c['to_node']}"
        dur    = c["end_time_sec"] - c["start_time_sec"]
        tdata  = ct.get(c["contact_id"], {})
        tkb    = tdata.get("traffic_kb", 0.0)
        tflows = tdata.get("flow_count", 0)
        colour = (
            _traffic_colour(tkb, max_isl_kb, GREY_UNUSED, ISL_LO, ISL_HI)
            if has_traffic
            else COLOURS[pair_idx[pair] % len(COLOURS)]
        )
        traffic_str = f"{tkb:.1f} KB ({tflows} flows)" if has_traffic else "n/a"
        fig.add_trace(go.Bar(
            x=[dur / 60], base=[c["start_time_sec"] / 60],
            y=[pair], orientation="h",
            marker_color=colour, marker_line_width=0,
            opacity=0.92, width=0.55,
            customdata=[[c["range_km"], c["capacity_kbps"], dur,
                         c["operator_from"], c["operator_to"],
                         tkb, tflows]],
            hovertemplate=(
                "<b>%{y}</b><br>"
                "Start: %{base:.1f} min · Duration: %{customdata[2]:.0f} s<br>"
                "Range: %{customdata[0]:.0f} km · Cap: %{customdata[1]:.0f} kbps<br>"
                "Ops: %{customdata[3]} ↔ %{customdata[4]}<br>"
                "<b>Traffic: %{customdata[5]:.1f} KB · %{customdata[6]} flows</b>"
                "<extra>ISL</extra>"
            ),
            showlegend=False,
        ), row=1, col=1)

    gs_idx = {g: i for i, g in enumerate(gs_nodes)}
    for c in gs_c:
        gn     = c["from_node"] if c["node_type_from"] == "GS" else c["to_node"]
        sat    = c["to_node"]   if c["node_type_from"] == "GS" else c["from_node"]
        dur    = c["end_time_sec"] - c["start_time_sec"]
        tdata  = ct.get(c["contact_id"], {})
        tkb    = tdata.get("traffic_kb", 0.0)
        tflows = tdata.get("flow_count", 0)
        colour = (
            _traffic_colour(tkb, max_gs_kb, GREY_UNUSED, GS_LO, GS_HI)
            if has_traffic
            else COLOURS[gs_idx[gn] % len(COLOURS)]
        )
        fig.add_trace(go.Bar(
            x=[dur / 60], base=[c["start_time_sec"] / 60],
            y=[gn], orientation="h",
            marker_color=colour, marker_line_width=0,
            opacity=0.90, width=0.45,
            customdata=[[sat, c["range_km"], dur, c["capacity_kbps"], tkb, tflows]],
            hovertemplate=(
                "<b>%{y}</b><br>"
                "Satellite: %{customdata[0]}<br>"
                "Start: %{base:.1f} min · Duration: %{customdata[2]:.0f} s<br>"
                "Range: %{customdata[1]:.0f} km · Cap: %{customdata[3]:.0f} kbps<br>"
                "<b>Traffic: %{customdata[4]:.1f} KB · %{customdata[5]} flows</b>"
                "<extra>GS uplink</extra>"
            ),
            showlegend=False,
        ), row=2, col=1)

    # Colour-scale legend annotations when traffic data is present
    annotations = []
    if has_traffic:
        annotations += [
            dict(text="■ dark = high traffic  □ grey = unused",
                 xref="paper", yref="paper", x=1.0, y=1.02,
                 xanchor="right", yanchor="bottom", showarrow=False,
                 font=dict(size=9, color="#555")),
        ]

    fig.update_xaxes(title_text="Simulation time (min)", range=[0, dur_min])
    fig.update_yaxes(tickfont=dict(size=9), automargin=True)
    total_h = max(460, 60 + n_isl_rows * 26 + n_gs_rows * 22)
    fig.update_layout(
        title="LIOS Protocol — Contact Timeline",
        barmode="overlay", height=total_h,
        margin=dict(t=80, b=60, l=180, r=30),
        showlegend=False,
        annotations=annotations,
    )
    return fig


# ── HTML assembly ──────────────────────────────────────────────────────────────

_TAB_STYLE = """
<style>
  body { font-family: 'Segoe UI', Arial, sans-serif; background: #f5f6fa; margin: 0; padding: 0; }
  h1   { text-align: center; padding: 24px 0 8px; color: #2c3e50;
         font-size: 1.7em; letter-spacing: 0.03em; }
  .subtitle { text-align: center; color: #7f8c8d; font-size: 0.92em; margin-bottom: 18px; }
  .tabs { display: flex; justify-content: center; gap: 6px; padding: 0 0 18px; flex-wrap: wrap; }
  .tab-btn {
    padding: 8px 20px; border: 2px solid #4C78A8; border-radius: 20px;
    background: white; color: #4C78A8; cursor: pointer; font-size: 0.88em;
    font-weight: 600; transition: all 0.2s;
  }
  .tab-btn:hover  { background: #dce9f7; }
  .tab-btn.active { background: #4C78A8; color: white; }
  .tab-panel { display: none; padding: 0 20px 30px; }
  .tab-panel.active { display: block; }
  .panel-card {
    background: white; border-radius: 10px; padding: 10px;
    box-shadow: 0 2px 10px rgba(0,0,0,0.07); margin-bottom: 20px;
  }
  .note { text-align: center; color: #e67e22; font-size: 0.82em; padding: 6px 0 2px; }
  .no-data { text-align: center; color: #95a5a6; font-size: 0.9em;
             padding: 40px 0; font-style: italic; }
</style>
"""

_TAB_JS = """
<script>
function switchTab(id) {
  document.querySelectorAll('.tab-panel').forEach(p => p.classList.remove('active'));
  document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
  document.getElementById('panel-' + id).classList.add('active');
  document.querySelector('[data-tab="' + id + '"]').classList.add('active');
}
</script>
"""


def _div(fig: go.Figure) -> str:
    return fig.to_html(
        full_html=False,
        include_plotlyjs=False,
        config={"displayModeBar": True, "responsive": True},
    )


def _no_data_div(msg: str) -> str:
    return f'<p class="no-data">{msg}</p>'


def build_dashboard(
    records: List[Dict[str, Any]],
    out_path: Path,
    prop_log: Optional[Dict] = None,
    contact_traffic: Optional[Dict] = None,
    synthetic: bool = False,
) -> None:
    has_mobility = prop_log is not None

    tabs = [
        ("overview",    "Overview"),
        ("traffic",     "Operator Traffic"),
        ("settlement",  "Settlement"),
        ("adversarial", "Adversarial"),
        ("throughput",  "Throughput vs Fairness"),
        ("overhead",    "Hash Chain Overhead"),
        ("globe",       "🌍 Satellite Globe"),
        ("timeline",    "📡 Contact Timeline"),
    ]

    figs: Dict[str, str] = {
        "overview":    _div(_fig_overview(records)),
        "traffic":     _div(_fig_operator_traffic(records)),
        "settlement":  _div(_fig_settlement(records)),
        "adversarial": _div(_fig_adversarial(records)),
        "throughput":  _div(_fig_throughput_fairness(records)),
        "overhead":    _div(_fig_forwarding_log_overhead(records)),
        "globe": (
            _div(_fig_satellite_globe(prop_log)) if has_mobility
            else _no_data_div(
                "No propagation log found. Run: "
                "<code>python contact_plan/compute_windows.py --propagation-log results/propagation_log.json ...</code>"
            )
        ),
        "timeline": (
            _div(_fig_contact_timeline(prop_log, contact_traffic)) if has_mobility
            else _no_data_div("No propagation log found.")
        ),
    }

    tab_buttons = "\n".join(
        f'    <button class="tab-btn{" active" if i == 0 else ""}" '
        f'data-tab="{tid}" onclick="switchTab(\'{tid}\')">{label}</button>'
        for i, (tid, label) in enumerate(tabs)
    )

    panels = "\n".join(
        f'<div id="panel-{tid}" class="tab-panel{" active" if i == 0 else ""}">'
        f'<div class="panel-card">{figs[tid]}</div></div>'
        for i, (tid, _) in enumerate(tabs)
    )

    n_exp = len(records)
    subtitle_extra = (
        f" &middot; {prop_log['epoch'][:10]} · "
        f"{len(prop_log['satellites'])} sats · "
        f"{len(prop_log['contacts'])} contacts"
        if has_mobility else ""
    )
    synthetic_note = (
        '<p class="note">&#9888; No experiment results found — showing synthetic demo data. '
        'Run <code>python evaluation/run_experiments.py</code> to populate real results.</p>'
        if synthetic else ""
    )

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>LIOS Protocol Dashboard</title>
  <script src="https://cdn.plot.ly/plotly-2.35.2.min.js"></script>
  {_TAB_STYLE}
</head>
<body>
  <h1>LIOS Protocol — Interactive Evaluation Dashboard</h1>
  <p class="subtitle">
    Lightweight Inter-operator Orbital Sharing Protocol
    &middot; {n_exp} experiment configuration{"s" if n_exp != 1 else ""}
    {subtitle_extra}
  </p>
  {synthetic_note}
  <div class="tabs">
{tab_buttons}
  </div>
  {panels}
  {_TAB_JS}
</body>
</html>"""

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(html, encoding="utf-8")
    print(f"Dashboard written → {out_path}  ({out_path.stat().st_size // 1024} KB)")


# ── CLI ────────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="Generate LIOS interactive HTML dashboard")
    parser.add_argument("--results",          default="results",
                        help="Results directory (contains logs/ and propagation_log.json)")
    parser.add_argument("--out",              default=None,
                        help="Output HTML path (default: <results>/dashboard.html)")
    parser.add_argument("--propagation-log",  default=None, metavar="PATH",
                        help="Explicit path to propagation_log.json (overrides auto-detect)")
    args = parser.parse_args()

    results_dir = Path(args.results)
    out_path    = Path(args.out) if args.out else results_dir / "dashboard.html"

    records   = load_results(results_dir)
    synthetic = False
    if not records:
        print("No experiment result files found — using synthetic demo data.")
        records   = _synthetic_results()
        synthetic = True
    else:
        print(f"Loaded {len(records)} experiment result(s) from {results_dir / 'logs'}/")

    prop_log_path = Path(args.propagation_log) if args.propagation_log else None
    prop_log = None
    if prop_log_path and prop_log_path.exists():
        with prop_log_path.open() as f:
            prop_log = json.load(f)
        print(f"Loaded propagation log: {prop_log_path}  "
              f"({len(prop_log['satellites'])} sats, {len(prop_log['contacts'])} contacts)")
    else:
        prop_log = load_propagation_log(results_dir)
        if prop_log:
            print(f"Loaded propagation log: {results_dir / 'propagation_log.json'}  "
                  f"({len(prop_log['satellites'])} sats, {len(prop_log['contacts'])} contacts)")
        else:
            print("No propagation log found — satellite globe and timeline panels will be empty.")
            print("  Generate one with: python contact_plan/compute_windows.py "
                  "--propagation-log results/propagation_log.json ...")

    contact_traffic = load_contact_traffic(results_dir)
    if contact_traffic:
        n_active = sum(1 for v in contact_traffic.values() if v.get("traffic_kb", 0) > 0)
        print(f"Loaded contact traffic: {n_active}/{len(contact_traffic)} contacts had traffic")
    else:
        print("No contact traffic file found — timeline will show contact windows only.")

    build_dashboard(records, out_path, prop_log=prop_log,
                    contact_traffic=contact_traffic, synthetic=synthetic)


if __name__ == "__main__":
    main()

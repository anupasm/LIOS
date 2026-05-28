#!/usr/bin/env python3
"""Experiment runner for LIOS protocol evaluation (§16).

Runs all 7 experiment configurations from §16.1, collects metrics,
generates publication-quality figures, and outputs a LaTeX table.

Usage:
  python run_experiments.py [--config baseline] [--out results/]
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

# Ensure lios/ is on sys.path
sys.path.insert(0, str(Path(__file__).parent.parent))

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

from contact_plan.gs_loader import GSLoader
from contact_plan.tle_loader import TLELoader
from contact_plan.window_calculator import WindowCalculator
from crypto.key_hierarchy import OperatorCA
from evaluation.adversarial import MaliciousSatelliteNode
from evaluation.metrics import MetricsCollector
from protocol.isl_state_machine import ISLStateMachine
from routing.cgr import CGR
from simulator.ground_station_node import FabricMock, GroundStationNode
from simulator.satellite_node import SatelliteNode, create_satellite
from simulator.simulator import EventLoop, EventType, SimEvent
from simulator.traffic_generator import TrafficGenerator


DATA_DIR = Path(__file__).parent.parent / "data"


# ── Experiment configurations (§16.1) ──────────────────────────────────────────

@dataclass
class ExperimentConfig:
    name: str
    duration_sec: float
    traffic_load_fraction: float
    adversarial_mode: str    # 'none' | 'rollback' | 'selective_forward'
    random_seed: int = 42
    isl_range_km: float = 2500.0
    time_step_sec: int = 30


@dataclass
class ExperimentResult:
    config: ExperimentConfig
    metrics: dict
    contact_plan_size: int
    duration_wall_sec: float


EXPERIMENT_CONFIGS: List[ExperimentConfig] = [
    # §16.1 Table — all 7 experiment configurations
    ExperimentConfig("baseline",        duration_sec=5_400,  traffic_load_fraction=0.50, adversarial_mode="none"),
    ExperimentConfig("depletion",       duration_sec=5_400,  traffic_load_fraction=0.95, adversarial_mode="none",  random_seed=43),
    ExperimentConfig("top_up",          duration_sec=86_400, traffic_load_fraction=0.80, adversarial_mode="none",  random_seed=44),
    ExperimentConfig("adversarial_1",   duration_sec=86_400, traffic_load_fraction=0.70, adversarial_mode="rollback",         random_seed=45),
    ExperimentConfig("adversarial_2",   duration_sec=86_400, traffic_load_fraction=0.70, adversarial_mode="selective_forward", random_seed=46),
    # Config 6: long-duration fairness (24 h, moderate load)
    ExperimentConfig("fairness_24h",    duration_sec=86_400, traffic_load_fraction=0.60, adversarial_mode="none",  random_seed=47),
    # Config 7: high-density — test throughput ceiling before fairness degrades
    ExperimentConfig("high_density",    duration_sec=5_400,  traffic_load_fraction=0.99, adversarial_mode="none",  random_seed=48, time_step_sec=10),
]


# ── Runner ─────────────────────────────────────────────────────────────────────

def run_experiment(config: ExperimentConfig, data_dir: Path = DATA_DIR) -> ExperimentResult:
    import time
    t0 = time.time()
    print(f"\n[EXP] {config.name}: duration={config.duration_sec}s load={config.traffic_load_fraction}")

    # 1. Load TLEs and ground stations
    operators_tles = TLELoader.load_all(data_dir)
    ground_stations = GSLoader.load_all(data_dir)
    operator_ids = list(operators_tles.keys())

    # 2. Compute contact plan (1 orbit = 90 min for baseline)
    epoch = datetime(2025, 11, 19, 0, 0, 0, tzinfo=timezone.utc)
    from datetime import timedelta
    t_end = epoch + timedelta(seconds=config.duration_sec)
    calc = WindowCalculator(epoch, t_end, config.time_step_sec, config.isl_range_km)
    cp = calc.compute(operators_tles, ground_stations)
    print(f"  Contact plan: {len(cp.contacts)} contacts")

    # 3. Build operator CAs and satellite nodes
    cas: Dict[str, OperatorCA] = {op: OperatorCA(op) for op in operator_ids}
    isl_fsm = ISLStateMachine()
    all_sats: Dict[str, SatelliteNode] = {}

    sat_op_map: Dict[str, str] = {}
    for op, sats in operators_tles.items():
        for sat_meta in sats:
            if config.adversarial_mode != "none" and op == operator_ids[0]:
                from evaluation.adversarial import MaliciousSatelliteNode
                priv_key_ca = cas[op]
                cert, priv = priv_key_ca.issue_for_new_key(sat_meta.sat_id, 90, operator_ids)
                from crypto.key_hierarchy import SatelliteKeyStore
                ks = SatelliteKeyStore()
                for oid, ca in cas.items():
                    ks.register_operator(oid, ca.public_key)
                node = MaliciousSatelliteNode(
                    satellite_id=sat_meta.sat_id,
                    operator_id=op,
                    cert=cert,
                    private_key=priv,
                    key_store=ks,
                    isl_fsm=isl_fsm,
                    attack_mode=config.adversarial_mode,
                    p_attack=0.5,
                )
            else:
                node = create_satellite(sat_meta.sat_id, op, cas[op], cas, isl_fsm, operator_ids)
            all_sats[sat_meta.sat_id] = node
            sat_op_map[sat_meta.sat_id] = op

    # 4. Build ground station nodes
    fabric = FabricMock()
    all_gs: Dict[str, GroundStationNode] = {}
    for op, gs_list in ground_stations.items():
        for gs_meta in gs_list:
            gs_node = GroundStationNode(gs_meta, fabric, isl_fsm)
            all_gs[gs_meta.gs_id] = gs_node

    # 5. Build CGR and traffic generator
    cgr = CGR(cp, operator_map=sat_op_map)
    tgen = TrafficGenerator(
        cp, cgr, sat_op_map,
        arrival_rate=config.traffic_load_fraction * 0.01,
        seed=config.random_seed,
    )

    # 6. Build DES event loop
    loop = EventLoop(seed=config.random_seed)
    for sat_id, sat_node in all_sats.items():
        loop.register_node(sat_id, sat_node.handle_event)
    for gs_id, gs_node in all_gs.items():
        loop.register_node(gs_id, gs_node.handle_event)

    loop.seed_contacts_from_plan(cp)

    # 7. Pre-schedule traffic flows
    schedule = tgen.generate_poisson_schedule(0.0, config.duration_sec)
    for arr_t, flow in schedule:
        if flow.path and flow.src_satellite:
            loop.schedule(SimEvent(
                time=arr_t,
                event_type=EventType.TRAFFIC_ARRIVE,
                from_node=flow.source_ground_node,
                to_node=flow.src_satellite,
                payload=flow,
            ))

    # 8. Run simulation
    stats = loop.run(until=config.duration_sec)

    # 9. Collect metrics
    metrics = MetricsCollector(operator_ids)
    for log_entry in loop.event_log:
        if log_entry.event_type == EventType.TRAFFIC_ARRIVE and log_entry.payload:
            flow = log_entry.payload
            from_op = sat_op_map.get(flow.src_satellite, "")
            to_op = sat_op_map.get(flow.dst_satellite, "")
            metrics.record_forwarding(flow.src_satellite, flow.dst_satellite, flow.size_kb, log_entry.time, from_op, to_op)

    total_isl_contact_sec = sum(
        c.duration_sec for c in cp.get_isl_contacts()
    )
    report = metrics.generate_report(total_isl_contact_sec)
    report["simulation_stats"] = stats.to_dict()
    report["traffic_gen"] = tgen.stats.summary()
    print(f"  Jain fairness: {report['jain_fairness_index']:.4f}")
    print(f"  Flows generated: {tgen.stats.flows_generated}")

    wall = time.time() - t0
    return ExperimentResult(config, report, len(cp.contacts), wall)


# ── Figure generation ──────────────────────────────────────────────────────────

def generate_paper_figures(results: List[ExperimentResult], out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    names = [r.config.name for r in results]

    # Figure 1: Jain fairness index per config
    fig, ax = plt.subplots(figsize=(10, 4))
    jain_vals = [r.metrics.get("jain_fairness_index", 0) for r in results]
    bars = ax.bar(names, jain_vals, color="steelblue", alpha=0.85)
    ax.axhline(0.95, linestyle="--", color="red", linewidth=1.0, label="Target (0.95)")
    ax.set_ylim(0, 1.05)
    ax.set_ylabel("Jain Fairness Index")
    ax.set_title("LIOS Protocol — Jain Fairness Index by Experiment")
    ax.legend()
    for bar, val in zip(bars, jain_vals):
        ax.text(bar.get_x() + bar.get_width() / 2, val + 0.01, f"{val:.3f}", ha="center", fontsize=9)
    fig.tight_layout()
    fig.savefig(out_dir / "fig1_jain_fairness.pdf")
    fig.savefig(out_dir / "fig1_jain_fairness.png", dpi=150)
    plt.close(fig)

    # Figure 2: OOS fraction per config
    fig, ax = plt.subplots(figsize=(10, 4))
    oos_vals = [r.metrics.get("oos_fraction", 0) * 100 for r in results]
    ax.bar(names, oos_vals, color="coral", alpha=0.85)
    ax.axhline(2.0, linestyle="--", color="green", linewidth=1.0, label="Target (<2%)")
    ax.set_ylabel("OOS Fraction (%)")
    ax.set_title("LIOS Protocol — ISL Out-of-Service Fraction by Experiment")
    ax.legend()
    fig.tight_layout()
    fig.savefig(out_dir / "fig2_oos_fraction.pdf")
    fig.savefig(out_dir / "fig2_oos_fraction.png", dpi=150)
    plt.close(fig)

    # Figure 3: Penalty events per config
    fig, ax = plt.subplots(figsize=(10, 4))
    pen_vals = [r.metrics.get("penalty_events", 0) for r in results]
    ax.bar(names, pen_vals, color="orchid", alpha=0.85)
    ax.set_ylabel("Penalty Events")
    ax.set_title("LIOS Protocol — Penalty Events by Experiment")
    fig.tight_layout()
    fig.savefig(out_dir / "fig3_penalty_events.pdf")
    fig.savefig(out_dir / "fig3_penalty_events.png", dpi=150)
    plt.close(fig)

    # Figure 2: Settlement latency CDF (overlay, all configs)
    latency_data = {
        r.config.name: sorted(
            [s.get("mean", 0) for s in [r.metrics.get("settlement_latency", {})]]
        )
        for r in results
        if r.metrics.get("settlement_latency", {}).get("count", 0) > 0
    }
    if latency_data:
        fig, ax = plt.subplots(figsize=(10, 4))
        for name, vals in latency_data.items():
            xs = np.linspace(0, 1, len(vals))
            ax.plot(xs, np.sort(vals), label=name)
        ax.set_xlabel("Percentile")
        ax.set_ylabel("Settlement latency (s)")
        ax.set_title("LIOS Protocol — Settlement Latency CDF")
        ax.legend(fontsize=8)
        ax.grid(True, alpha=0.3)
        fig.tight_layout()
        fig.savefig(out_dir / "fig2_settlement_latency_cdf.pdf")
        fig.savefig(out_dir / "fig2_settlement_latency_cdf.png", dpi=150)
        plt.close(fig)

    # Figure 4: Balance evolution over time (baseline only, operator forwarded bytes)
    baseline = next((r for r in results if r.config.name == "baseline"), None)
    if baseline:
        bfw = baseline.metrics.get("bytes_forwarded_by", {})
        if bfw:
            fig, ax = plt.subplots(figsize=(10, 4))
            ops = list(bfw.keys())
            vals = [bfw[op] / 1024 for op in ops]  # KB → MB
            ax.bar(ops, vals, color="mediumseagreen", alpha=0.85)
            ax.set_ylabel("Total bytes forwarded (MB)")
            ax.set_title("LIOS Protocol — Operator Forwarding Volume (Baseline)")
            ax.grid(True, axis="y", alpha=0.3)
            fig.tight_layout()
            fig.savefig(out_dir / "fig4_balance_evolution.pdf")
            fig.savefig(out_dir / "fig4_balance_evolution.png", dpi=150)
            plt.close(fig)

    # Figure 5: Penalty detection — penalty events per adversarial config
    adv_results = [r for r in results if r.config.adversarial_mode != "none"]
    if adv_results:
        fig, ax = plt.subplots(figsize=(8, 4))
        adv_names = [r.config.name for r in adv_results]
        adv_penalties = [r.metrics.get("penalty_events", 0) for r in adv_results]
        ax.bar(adv_names, adv_penalties, color="tomato", alpha=0.85)
        ax.set_ylabel("Penalty events")
        ax.set_title("LIOS Protocol — Penalty Events (Adversarial Configs)")
        ax.grid(True, axis="y", alpha=0.3)
        fig.tight_layout()
        fig.savefig(out_dir / "fig5_penalty_detection.pdf")
        fig.savefig(out_dir / "fig5_penalty_detection.png", dpi=150)
        plt.close(fig)

    # Figure 6: Throughput vs Fairness scatter
    fig, ax = plt.subplots(figsize=(8, 5))
    for r in results:
        jain = r.metrics.get("jain_fairness_index", 0)
        flows = r.metrics.get("total_forwarding_events", 0)
        ax.scatter(flows, jain, s=80, label=r.config.name, zorder=3)
    ax.axhline(0.95, linestyle="--", color="red", linewidth=0.8, label="Fairness target")
    ax.set_xlabel("Total forwarding events (throughput proxy)")
    ax.set_ylabel("Jain Fairness Index")
    ax.set_title("LIOS Protocol — Throughput vs Fairness Trade-off")
    ax.legend(fontsize=7, loc="lower right")
    ax.grid(True, alpha=0.3)
    fig.tight_layout()
    fig.savefig(out_dir / "fig6_throughput_vs_fairness.pdf")
    fig.savefig(out_dir / "fig6_throughput_vs_fairness.png", dpi=150)
    plt.close(fig)

    print(f"  Figures saved to {out_dir}/")


# ── LaTeX table ────────────────────────────────────────────────────────────────

def generate_latex_table(results: List[ExperimentResult]) -> str:
    header = (
        r"\begin{table}[ht]" + "\n"
        r"\centering" + "\n"
        r"\caption{LIOS Protocol Evaluation Results}" + "\n"
        r"\label{tab:results}" + "\n"
        r"\begin{tabular}{lcccc}" + "\n"
        r"\hline" + "\n"
        r"Config & Jain Index & OOS (\%) & Penalties & Settlement Events \\" + "\n"
        r"\hline" + "\n"
    )
    rows = ""
    for r in results:
        m = r.metrics
        rows += (
            f"{r.config.name.replace('_', '\\_')} & "
            f"{m.get('jain_fairness_index', 0):.3f} & "
            f"{m.get('oos_fraction', 0)*100:.2f} & "
            f"{m.get('penalty_events', 0)} & "
            f"{m.get('settlement_events', 0)} \\\\\n"
        )
    footer = r"\hline" + "\n" + r"\end{tabular}" + "\n" + r"\end{table}"
    return header + rows + footer


# ── CLI entry point ────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="Run LIOS protocol experiments")
    parser.add_argument("--config", help="Run only this experiment (by name)")
    parser.add_argument("--out", default="results", help="Output directory")
    parser.add_argument("--data", default=str(DATA_DIR), help="Data directory")
    args = parser.parse_args()

    out_dir = Path(args.out)
    data_dir = Path(args.data)
    configs = EXPERIMENT_CONFIGS
    if args.config:
        configs = [c for c in EXPERIMENT_CONFIGS if c.name == args.config]
        if not configs:
            print(f"Unknown config: {args.config!r}. Available: {[c.name for c in EXPERIMENT_CONFIGS]}")
            sys.exit(1)

    results: List[ExperimentResult] = []
    for cfg in configs:
        result = run_experiment(cfg, data_dir)
        results.append(result)

        # Save raw metrics
        metrics_path = out_dir / "logs" / f"{cfg.name}_metrics.json"
        metrics_path.parent.mkdir(parents=True, exist_ok=True)
        with metrics_path.open("w") as f:
            json.dump(result.metrics, f, indent=2, default=str)
        print(f"  Metrics → {metrics_path}")

    if results:
        generate_paper_figures(results, out_dir / "figures")
        print("\nLaTeX table:")
        print(generate_latex_table(results))


if __name__ == "__main__":
    main()

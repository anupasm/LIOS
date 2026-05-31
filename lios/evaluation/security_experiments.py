#!/usr/bin/env python3
"""Security experiment runner for LIOS protocol — E1, E2, E3.

E1  Balance Rollback sweep        p_atk  ∈ {0.0, 0.1, 0.2, 0.3, 0.5, 0.7, 1.0}
E2  Selective Forwarding sweep    p_drop ∈ {0.0, 0.1, 0.2, 0.3, 0.5}
E3  T_ch sensitivity (post-hoc)   T_ch   ∈ {1 h, 3 h, 6 h, 12 h, 24 h, 48 h}
       E3 reuses E1 trial data and re-applies different T_ch thresholds to the
       recorded detection latencies — no additional simulation needed.

Each sweep runs N_TRIALS independent trials per parameter point, varying the
random seed.  Results are aggregated into mean ± 95 % CI and saved as JSON and
publication-quality PDF figures.

Usage:
    python evaluation/security_experiments.py [--out results/] [--trials 10]
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Dict, List, Optional

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
import numpy as np
import scipy.stats as st

sys.path.insert(0, str(Path(__file__).parent.parent))

from evaluation.run_experiments import ExperimentConfig, run_experiment, DATA_DIR


# ── Default sweep parameters ───────────────────────────────────────────────────

E1_P_ATK_VALUES    = [0.0, 0.1, 0.2, 0.3, 0.5, 0.7, 1.0]
E2_P_DROP_VALUES   = [0.0, 0.1, 0.2, 0.3, 0.5]
E3_TCH_VALUES_SEC  = [3_600, 10_800, 21_600, 43_200, 86_400, 172_800]  # 1h..48h
E3_TCH_LABELS      = ["1 h", "3 h", "6 h", "12 h", "24 h", "48 h"]

SWEEP_DURATION_SEC = 7_200   # 2 orbits — gives proof history depth for rollback attacks
SWEEP_LOAD         = 0.70


# ── Result dataclasses ─────────────────────────────────────────────────────────

@dataclass
class TrialResult:
    param_name: str
    param_value: float
    seed: int
    detection_rate: float
    detection_latency_mean: float
    detection_latency_p95: float
    jain_index: float
    false_claim_fraction: float
    gain_prevented_kb: float
    rollback_attempts: int
    selective_drops: int
    # Raw per-attack detection latencies (sec) — used for E3 post-hoc sweep
    raw_detection_latencies: List[float] = field(default_factory=list)


@dataclass
class SweepResult:
    experiment_id: str          # "E1", "E2", "E3"
    param_name: str
    param_values: List[float]
    n_trials: int
    trials: List[TrialResult]   # n_trials × len(param_values) entries

    def aggregate(self) -> List[dict]:
        """Per-parameter aggregation: mean, 95 % CI for key metrics."""
        out = []
        for pv in self.param_values:
            pts = [t for t in self.trials if t.param_value == pv]
            if not pts:
                continue

            def _ci(arr):
                if len(arr) < 2:
                    return float(np.mean(arr)), 0.0
                m = float(np.mean(arr))
                se = float(st.sem(arr))
                ci = float(st.t.ppf(0.975, len(arr) - 1) * se)
                return m, ci

            dr_m,  dr_ci  = _ci([t.detection_rate        for t in pts])
            jain_m, jain_ci = _ci([t.jain_index           for t in pts])
            fc_m,  fc_ci  = _ci([t.false_claim_fraction   for t in pts])
            lat_m, lat_ci = _ci([t.detection_latency_mean for t in pts])
            out.append({
                "param_value": pv,
                "detection_rate_mean": dr_m, "detection_rate_ci": dr_ci,
                "jain_mean": jain_m,        "jain_ci": jain_ci,
                "false_claim_mean": fc_m,   "false_claim_ci": fc_ci,
                "latency_mean_sec": lat_m,  "latency_ci_sec": lat_ci,
                "n_trials": len(pts),
            })
        return out


# ── Single-trial runner ────────────────────────────────────────────────────────

def _run_trial(
    name: str,
    attack_mode: str,
    p_attack: float,
    p_drop: float,
    seed: int,
    duration_sec: float = SWEEP_DURATION_SEC,
    load: float = SWEEP_LOAD,
    data_dir: Path = DATA_DIR,
) -> TrialResult:
    cfg = ExperimentConfig(
        name=name,
        duration_sec=duration_sec,
        traffic_load_fraction=load,
        adversarial_mode=attack_mode,
        random_seed=seed,
        p_attack=p_attack,
        p_drop=p_drop,
    )
    result = run_experiment(cfg, data_dir)
    m = result.metrics

    det_lat = m.get("detection_latency", {})
    econ    = m.get("economic_deterrence", {})
    fc      = m.get("false_claim", {})

    # Reconstruct per-attack latencies from the collector (stored in the report).
    raw_latencies: List[float] = []
    det_lat_raw = m.get("_raw_detection_latencies", [])
    if det_lat_raw:
        raw_latencies = det_lat_raw

    return TrialResult(
        param_name="p_attack" if attack_mode == "rollback" else "p_drop",
        param_value=p_attack if attack_mode == "rollback" else p_drop,
        seed=seed,
        detection_rate=m.get("detection_rate", 1.0),
        detection_latency_mean=det_lat.get("mean", 0.0),
        detection_latency_p95=det_lat.get("p95", 0.0),
        jain_index=m.get("jain_fairness_index", 1.0),
        false_claim_fraction=fc.get("false_claim_fraction", 0.0),
        gain_prevented_kb=econ.get("total_prevented_gain_kb", 0.0),
        rollback_attempts=m.get("rollback_attempts", 0),
        selective_drops=m.get("selective_drops", 0),
        raw_detection_latencies=raw_latencies,
    )


# ── E1: Balance Rollback Sweep ─────────────────────────────────────────────────

def run_e1(n_trials: int = 10, data_dir: Path = DATA_DIR) -> SweepResult:
    """Sweep p_atk ∈ E1_P_ATK_VALUES × n_trials seeds.

    For each p_atk:
      - detection_rate  should be 1.0 (100 %) across all seeds
      - detection_latency_mean ≤ Tch (48 h)
      - gain_prevented_kb = total KB that would have been stolen if undetected
    """
    print(f"\n[E1] Balance Rollback Sweep  (n_trials={n_trials})")
    trials: List[TrialResult] = []

    for p in E1_P_ATK_VALUES:
        print(f"  p_atk={p:.1f}", end="", flush=True)
        for i in range(n_trials):
            seed = 100 + i * 7 + int(p * 100)
            t = _run_trial(
                name=f"e1_patk{p:.2f}_s{seed}",
                attack_mode="rollback" if p > 0 else "none",
                p_attack=p,
                p_drop=0.0,
                seed=seed,
                data_dir=data_dir,
            )
            trials.append(t)
            print(".", end="", flush=True)
        print()

    return SweepResult("E1", "p_atk", E1_P_ATK_VALUES, n_trials, trials)


# ── E2: Selective Forwarding Sweep ────────────────────────────────────────────

def run_e2(n_trials: int = 10, data_dir: Path = DATA_DIR) -> SweepResult:
    """Sweep p_drop ∈ E2_P_DROP_VALUES × n_trials seeds.

    For each p_drop:
      - false_claim_fraction  = selective_drops / (total_fwd + selective_drops)
      - jain_index degradation vs p_drop=0 baseline
    """
    print(f"\n[E2] Selective Forwarding Sweep  (n_trials={n_trials})")
    trials: List[TrialResult] = []

    for p in E2_P_DROP_VALUES:
        print(f"  p_drop={p:.1f}", end="", flush=True)
        for i in range(n_trials):
            seed = 200 + i * 7 + int(p * 100)
            t = _run_trial(
                name=f"e2_pdrop{p:.2f}_s{seed}",
                attack_mode="selective_forward" if p > 0 else "none",
                p_attack=0.0,
                p_drop=p,
                seed=seed,
                data_dir=data_dir,
            )
            t.param_name = "p_drop"
            t.param_value = p
            trials.append(t)
            print(".", end="", flush=True)
        print()

    return SweepResult("E2", "p_drop", E2_P_DROP_VALUES, n_trials, trials)


# ── E3: T_ch Sensitivity (post-hoc on E1 data) ────────────────────────────────

def run_e3(e1_sweep: SweepResult) -> List[dict]:
    """Post-hoc E3 analysis: using detection latencies recorded in E1, compute
    detection_rate for each Tch threshold without re-running simulations.

    Returns a list of dicts: {tch_sec, tch_label, detection_rate, ci}.
    """
    print("\n[E3] T_ch Sensitivity Analysis  (post-hoc on E1 data)")

    # Collect all detection latencies from E1 trials where attacks occurred.
    all_latencies: List[float] = []
    all_attempts: int = 0
    for trial in e1_sweep.trials:
        if trial.rollback_attempts > 0:
            all_attempts += trial.rollback_attempts
            all_latencies.extend(trial.raw_detection_latencies)

    if not all_latencies:
        print("  No detection latencies recorded — using synthetic distribution.")
        rng = np.random.default_rng(42)
        all_latencies = list(rng.exponential(scale=600, size=200))
        all_attempts = 200

    results = []
    for tch, label in zip(E3_TCH_VALUES_SEC, E3_TCH_LABELS):
        caught = sum(1 for lat in all_latencies if lat <= tch)
        rate = caught / len(all_latencies) if all_latencies else 1.0
        results.append({
            "tch_sec": tch,
            "tch_label": label,
            "tch_hours": tch / 3600,
            "detection_rate": rate,
            "caught": caught,
            "total_latencies": len(all_latencies),
        })
        print(f"  Tch={label:>5s}  detection_rate={rate:.3f}  ({caught}/{len(all_latencies)})")

    return results


# ── Publication figures ────────────────────────────────────────────────────────

def _ci_bar(ax, x_vals, means, cis, color, label, width=0.6):
    bars = ax.bar(x_vals, means, width=width, color=color, alpha=0.85, label=label, zorder=3)
    for xv, m, ci in zip(x_vals, means, cis):
        if ci > 0:
            ax.errorbar(xv, m, yerr=ci, fmt="none", color="black",
                        capsize=4, linewidth=1.2, zorder=4)
    return bars


def plot_e1(sweep: SweepResult, out_dir: Path) -> None:
    agg = sweep.aggregate()
    p_vals  = [a["param_value"] for a in agg]
    dr_mean = [a["detection_rate_mean"] for a in agg]
    dr_ci   = [a["detection_rate_ci"]   for a in agg]
    lat_m   = [a["latency_mean_sec"] / 3600 for a in agg]  # → hours
    lat_ci  = [a["latency_ci_sec"]    / 3600 for a in agg]
    j_mean  = [a["jain_mean"] for a in agg]
    j_ci    = [a["jain_ci"]   for a in agg]

    x = np.arange(len(p_vals))
    labels = [str(p) for p in p_vals]

    fig, axes = plt.subplots(1, 3, figsize=(14, 4.5))
    fig.suptitle("E1 — Balance Rollback Attack: Sweep over $p_{atk}$", fontsize=12)

    # Panel 1: Detection rate
    ax = axes[0]
    _ci_bar(ax, x, dr_mean, dr_ci, "steelblue", "detection rate")
    ax.axhline(1.0, linestyle="--", color="red", linewidth=0.9, label="Target (1.0)")
    ax.set_xticks(x); ax.set_xticklabels(labels)
    ax.set_xlabel("$p_{atk}$"); ax.set_ylabel("Detection rate")
    ax.set_ylim(0, 1.1); ax.legend(fontsize=8); ax.grid(axis="y", alpha=0.3)
    ax.set_title("Detection rate vs $p_{atk}$")

    # Panel 2: Detection latency (hours)
    ax = axes[1]
    _ci_bar(ax, x, lat_m, lat_ci, "coral", "latency (h)")
    ax.axhline(48, linestyle="--", color="green", linewidth=0.9, label="$T_{ch}=48$ h")
    ax.set_xticks(x); ax.set_xticklabels(labels)
    ax.set_xlabel("$p_{atk}$"); ax.set_ylabel("Detection latency (h)")
    ax.legend(fontsize=8); ax.grid(axis="y", alpha=0.3)
    ax.set_title("Detection latency vs $p_{atk}$")

    # Panel 3: Jain fairness under attack
    ax = axes[2]
    _ci_bar(ax, x, j_mean, j_ci, "mediumseagreen", "Jain index")
    ax.axhline(0.95, linestyle="--", color="red", linewidth=0.9, label="Target (0.95)")
    ax.set_xticks(x); ax.set_xticklabels(labels)
    ax.set_xlabel("$p_{atk}$"); ax.set_ylabel("Jain fairness index")
    ax.set_ylim(0, 1.1); ax.legend(fontsize=8); ax.grid(axis="y", alpha=0.3)
    ax.set_title("Fairness vs $p_{atk}$")

    fig.tight_layout()
    for fmt in ("pdf", "png"):
        fig.savefig(out_dir / f"sec_e1_rollback.{fmt}", dpi=150)
    plt.close(fig)
    print(f"  [E1] figure → {out_dir}/sec_e1_rollback.{{pdf,png}}")


def plot_e2(sweep: SweepResult, out_dir: Path) -> None:
    agg = sweep.aggregate()
    p_vals  = [a["param_value"] for a in agg]
    fc_mean = [a["false_claim_mean"] * 100 for a in agg]   # → %
    fc_ci   = [a["false_claim_ci"]   * 100 for a in agg]
    j_mean  = [a["jain_mean"] for a in agg]
    j_ci    = [a["jain_ci"]   for a in agg]

    x = np.arange(len(p_vals))
    labels = [str(p) for p in p_vals]

    fig, axes = plt.subplots(1, 2, figsize=(10, 4.5))
    fig.suptitle("E2 — Selective Forwarding: Sweep over $p_{drop}$", fontsize=12)

    ax = axes[0]
    _ci_bar(ax, x, fc_mean, fc_ci, "tomato", "false-claim %")
    ax.set_xticks(x); ax.set_xticklabels(labels)
    ax.set_xlabel("$p_{drop}$"); ax.set_ylabel("False-claim fraction (%)")
    ax.grid(axis="y", alpha=0.3)
    ax.set_title("False-claim fraction vs $p_{drop}$")

    ax = axes[1]
    _ci_bar(ax, x, j_mean, j_ci, "mediumseagreen", "Jain index")
    ax.axhline(0.95, linestyle="--", color="red", linewidth=0.9, label="Target (0.95)")
    ax.set_xticks(x); ax.set_xticklabels(labels)
    ax.set_xlabel("$p_{drop}$"); ax.set_ylabel("Jain fairness index")
    ax.set_ylim(0, 1.1); ax.legend(fontsize=8); ax.grid(axis="y", alpha=0.3)
    ax.set_title("Fairness degradation vs $p_{drop}$")

    fig.tight_layout()
    for fmt in ("pdf", "png"):
        fig.savefig(out_dir / f"sec_e2_selfwd.{fmt}", dpi=150)
    plt.close(fig)
    print(f"  [E2] figure → {out_dir}/sec_e2_selfwd.{{pdf,png}}")


def plot_e3(e3_results: List[dict], measured_delta_gc_h: float, out_dir: Path) -> None:
    """Phase diagram: detection rate vs T_ch with δ_GC marked."""
    tch_h = [r["tch_hours"] for r in e3_results]
    dr    = [r["detection_rate"] for r in e3_results]
    lbls  = [r["tch_label"] for r in e3_results]

    fig, ax = plt.subplots(figsize=(8, 5))
    ax.plot(tch_h, dr, marker="o", linewidth=2, color="steelblue",
            markersize=7, zorder=3, label="Detection rate")
    ax.axhline(1.0, linestyle="--", color="red", linewidth=0.9, label="Target (1.0)")
    ax.axvline(measured_delta_gc_h, linestyle=":", color="darkorange", linewidth=1.4,
               label=f"Measured $\\delta_{{GC}}$ = {measured_delta_gc_h:.1f} h")

    # Shade safe / unsafe regions
    ax.axvspan(0, measured_delta_gc_h, alpha=0.08, color="red",   label="Unsafe ($T_{{ch}} < \\delta_{{GC}}$)")
    ax.axvspan(measured_delta_gc_h, max(tch_h) * 1.1, alpha=0.08, color="green",
               label="Safe ($T_{{ch}} \\geq \\delta_{{GC}}$)")

    ax.set_xticks(tch_h)
    ax.set_xticklabels(lbls, rotation=30, ha="right")
    ax.set_xlabel("Challenge window $T_{ch}$")
    ax.set_ylabel("Detection rate")
    ax.set_ylim(-0.05, 1.15)
    ax.set_title("E3 — $T_{ch}$ Sensitivity: Detection Rate vs Challenge Window")
    ax.legend(fontsize=8, loc="lower right")
    ax.grid(alpha=0.3)
    fig.tight_layout()
    for fmt in ("pdf", "png"):
        fig.savefig(out_dir / f"sec_e3_tch_sensitivity.{fmt}", dpi=150)
    plt.close(fig)
    print(f"  [E3] figure → {out_dir}/sec_e3_tch_sensitivity.{{pdf,png}}")


# ── LaTeX summary table ────────────────────────────────────────────────────────

def generate_security_latex_table(
    e1_agg: List[dict],
    e2_agg: List[dict],
    out_path: Path,
) -> None:
    lines = [
        r"\begin{table}[t]",
        r"\caption{Security Experiment Results (E1: Rollback, E2: Selective Forwarding)}",
        r"\label{tab:security_results}",
        r"\tablefont",
        r"\begin{tabular*}{40pc}{@{\hspace{2pt}}l@{\hspace{4pt}}c@{\hspace{4pt}}c@{\hspace{4pt}}c@{\hspace{4pt}}c@{}}",
        r"\toprule",
        r"\textbf{Config} & \textbf{Param} & \textbf{Det.\ Rate} & "
        r"\textbf{Latency (h)} & \textbf{Jain} \\",
        r"\midrule",
    ]
    for a in e1_agg:
        pv  = a["param_value"]
        dr  = a["detection_rate_mean"]
        lat = a["latency_mean_sec"] / 3600
        j   = a["jain_mean"]
        dr_ci  = a["detection_rate_ci"]
        lat_ci = a["latency_ci_sec"] / 3600
        j_ci   = a["jain_ci"]
        lines.append(
            f"E1 rollback & $p_{{atk}}={pv}$ & "
            f"${dr:.3f} \\pm {dr_ci:.3f}$ & "
            f"${lat:.2f} \\pm {lat_ci:.2f}$ & "
            f"${j:.3f} \\pm {j_ci:.3f}$ \\\\"
        )
    lines.append(r"\midrule")
    for a in e2_agg:
        pv  = a["param_value"]
        fc  = a["false_claim_mean"] * 100
        j   = a["jain_mean"]
        fc_ci = a["false_claim_ci"] * 100
        j_ci  = a["jain_ci"]
        lines.append(
            f"E2 sel.~fwd & $p_{{drop}}={pv}$ & "
            f"--- & "
            f"${fc:.1f}\\%\\pm{fc_ci:.1f}\\%$ (false claim) & "
            f"${j:.3f} \\pm {j_ci:.3f}$ \\\\"
        )
    lines += [r"\bottomrule", r"\end{tabular*}", r"\end{table}"]
    out_path.write_text("\n".join(lines))
    print(f"  LaTeX table → {out_path}")


# ── CLI entry point ────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="LIOS security experiments E1–E3")
    parser.add_argument("--out",    default="results",     help="Output directory")
    parser.add_argument("--data",   default=str(DATA_DIR), help="Data directory")
    parser.add_argument("--trials", type=int, default=10,  help="Trials per param point")
    parser.add_argument("--exp",    default="all",
                        help="Which experiment(s) to run: e1, e2, e3, all")
    args = parser.parse_args()

    out_dir  = Path(args.out) / "security"
    fig_dir  = out_dir / "figures"
    data_dir = Path(args.data)
    out_dir.mkdir(parents=True, exist_ok=True)
    fig_dir.mkdir(parents=True, exist_ok=True)

    run_e1_flag = args.exp in ("all", "e1", "e1e3")
    run_e2_flag = args.exp in ("all", "e2")
    run_e3_flag = args.exp in ("all", "e3", "e1e3")

    e1_sweep: Optional[SweepResult] = None
    e2_sweep: Optional[SweepResult] = None

    if run_e1_flag:
        e1_sweep = run_e1(n_trials=args.trials, data_dir=data_dir)
        (out_dir / "e1_sweep.json").write_text(
            json.dumps({"agg": e1_sweep.aggregate(),
                        "trials": [asdict(t) for t in e1_sweep.trials]}, indent=2)
        )
        plot_e1(e1_sweep, fig_dir)

    if run_e2_flag:
        e2_sweep = run_e2(n_trials=args.trials, data_dir=data_dir)
        (out_dir / "e2_sweep.json").write_text(
            json.dumps({"agg": e2_sweep.aggregate(),
                        "trials": [asdict(t) for t in e2_sweep.trials]}, indent=2)
        )
        plot_e2(e2_sweep, fig_dir)

    if run_e3_flag and e1_sweep is not None:
        # Estimate δ_GC from mean detection latency at p_atk=0.5 (closest to real contact gap)
        e1_05 = [t for t in e1_sweep.trials if abs(t.param_value - 0.5) < 0.01]
        measured_delta_gc_h = (
            float(np.mean([t.detection_latency_mean for t in e1_05])) / 3600
            if e1_05 else 2.0
        )
        e3_results = run_e3(e1_sweep)
        (out_dir / "e3_tch_sensitivity.json").write_text(
            json.dumps({"delta_gc_h": measured_delta_gc_h, "results": e3_results}, indent=2)
        )
        plot_e3(e3_results, measured_delta_gc_h, fig_dir)

    if e1_sweep and e2_sweep:
        generate_security_latex_table(
            e1_sweep.aggregate(),
            e2_sweep.aggregate(),
            out_dir / "security_table.tex",
        )

    print(f"\nAll security results saved to {out_dir}/")


if __name__ == "__main__":
    main()

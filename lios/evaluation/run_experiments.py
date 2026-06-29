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
import pprint
import sys
from dataclasses import dataclass
from datetime import timedelta
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
from simulator.ground_station_node import FabricMock, GroundStationNode
from simulator.satellite_node import SatelliteNode, create_satellite
from simulator.simulator import EventLoop, EventType, SimEvent
from config import cfg as lios_cfg
from simulator.traffic_generator import TrafficGenerator, constellation_size_weights


DATA_DIR  = Path(__file__).parent.parent / "data"
CACHE_DIR = Path(__file__).parent.parent / "cache"


# ── Experiment configurations (§16.1) ──────────────────────────────────────────

@dataclass
class ExperimentConfig:
    name: str
    duration_sec: float
    traffic_arrival_rate: float
    adversarial_mode: str    # 'none' | 'rollback' | 'selective_forward'
    random_seed: int = lios_cfg.simulation.random_seed
    isl_range_km: float = lios_cfg.link.isl_max_range_km
    time_step_sec: int = lios_cfg.simulation.time_step_sec
    p_attack: float = lios_cfg.simulation.p_attack   # E1: rollback probability per settlement
    p_drop: float = 0.3                              # E2: selective-fwd drop probability
    baseline_protocol: str = "lios"                  # 'lios' | 'ground_reset' | 'greedy' | 't4t' | 'central'
    operator_load_weights: Optional[dict[str, float]] = None
    """Relative source load by operator.

    ``None`` derives normalized weights from the number of satellites loaded
    for each operator, keeping the workload aligned with constellation size.
    """


@dataclass
class ExperimentResult:
    config: ExperimentConfig
    metrics: dict
    contact_plan_size: int
    duration_wall_sec: float


EXPERIMENT_CONFIGS: List[ExperimentConfig] = [
    ExperimentConfig(
        "lios_constellation_weighted",
        duration_sec=86_400,
        traffic_arrival_rate=0.3,
        adversarial_mode="none",
        random_seed=42,
    ),
    ExperimentConfig(
        "ground_reset_constellation_weighted",
        duration_sec=86_400,
        traffic_arrival_rate=0.3,
        adversarial_mode="none",
        random_seed=42,
        baseline_protocol="ground_reset",
    )
]


# ── Contact traffic attribution ────────────────────────────────────────────────

def _compute_contact_traffic(cp, satellite_nodes) -> Dict[str, Dict]:
    """Return successful forwarding volume attributed by direct contact ID."""
    result: Dict[str, Dict] = {}
    for c in cp.contacts:
        result[c.contact_id] = {"traffic_kb": 0.0, "flow_count": 0}

    for node in satellite_nodes.values():
        for entry in node.event_log:
            if entry.get("event") != "TRAFFIC_FORWARDED":
                continue
            contact_id = entry.get("contact_id", "")
            if contact_id in result:
                result[contact_id]["traffic_kb"] += entry.get("bytes_kb", 0.0)
                result[contact_id]["flow_count"] += 1

    for cid in result:
        result[cid]["traffic_kb"] = round(result[cid]["traffic_kb"], 3)
    return result


# ── Settlement log ────────────────────────────────────────────────────────────
#
# Off-chain events (satellite peer-to-peer):
#   OFFCHAIN_PROOF_UPDATE  — balance proof updated after each forwarding hop
#   SETTLEMENT_QUEUED      — T1/T2/T7 triggers fired; payload queued for GS upload
#
# Ground settlement events (satellite→GS→Fabric):
#   SETTLEMENT_RECEIVED    — GS received payload and submitted initiateSettlement()
#   CHALLENGE_SUBMITTED    — GS submitted counter-proof to Fabric
#   CHALLENGE_EXPIRED      — challenge window closed; settlement finalised
#   SETTLEMENT_FINALIZED   — Fabric confirmed finalization

_OFFCHAIN_SAT_EVENTS = {"OFFCHAIN_PROOF_UPDATE", "SETTLEMENT_QUEUED", "ISL_RESUMED"}
_GROUND_GS_EVENTS    = {"SETTLEMENT_RECEIVED", "CHALLENGE_SUBMITTED",
                        "CHALLENGE_EXPIRED", "SETTLEMENT_FINALIZED",
                        "PEER_SETTLEMENT_NOTIFIED"}


def _compute_latency_summary(offchain: List[dict], ground: List[dict]) -> List[dict]:
    """Build per-channel latency breakdown from logged events.

    Two protocol latency components (orbital wait times are excluded as they
    depend on geometry, not the protocol):

      offchain_latency_sec  — ISL propagation delay for the proof exchange
                              (isl_prop_delay_sec at trigger time)
      onchain_latency_sec   — blockchain processing: SETTLEMENT_RECEIVED →
                              SETTLEMENT_FINALIZED (challenge window or immediate)

    Additional diagnostic fields (not part of protocol latency):
      wait_for_gs_sec       — satellite waited for next GS contact (orbital geometry)
      uplink_prop_delay_sec — uplink signal travel time (sat → GS)

    Propagation model: d/c (speed of light in vacuum).
    Ref: Bhattacherjee & Singla, "Network topology design at 27,000 km/hour,"
         ACM CoNEXT 2019, §3.1. https://doi.org/10.1145/3359989.3365407
    """
    queued: Dict[str, dict] = {}
    first_proof: Dict[str, float] = {}   # channel_id → t of first OFFCHAIN_PROOF_UPDATE
    received: Dict[str, dict] = {}
    finalized: Dict[str, float] = {}
    resumed: Dict[str, List[dict]] = {}

    for e in offchain:
        ch_id = e.get("channel_id", "")
        if e["event"] == "OFFCHAIN_PROOF_UPDATE":
            if ch_id not in first_proof:
                first_proof[ch_id] = e.get("t", 0.0)
        elif e["event"] == "SETTLEMENT_QUEUED" and ch_id not in queued:
            queued[ch_id] = {
                "queued_at": e.get("t"),
                "isl_range_km": e.get("isl_range_km"),
                "isl_prop_delay_sec": e.get("isl_prop_delay_sec"),
            }
        elif e["event"] == "ISL_RESUMED":
            resumed.setdefault(ch_id, []).append({
                "satellite": e.get("satellite"),
                "resumed_at": e.get("t"),
                "gs_sent_at": e.get("gs_sent_at"),
                "downlink_prop_delay_sec": e.get("downlink_prop_delay_sec"),
            })

    for e in ground:
        ch_id = e.get("channel_id", "")
        if e["event"] == "SETTLEMENT_RECEIVED":
            triggers = e.get("triggers") or []
            entry = {
                "gs_received_at": e.get("t"),
                "gs_range_km": e.get("gs_range_km"),
                "uplink_prop_delay_sec": e.get("uplink_prop_delay_sec"),
                "wait_for_gs_sec": e.get("wait_for_gs_sec"),
            }
            # Prefer the submission that carries non-empty triggers — that is the
            # initiating satellite's upload and gives the correct uplink delay and
            # wait_for_gs.  The cooperative peer submission (triggers=[]) arrives
            # within milliseconds and would otherwise win the first-seen guard and
            # anchor all latency metrics to the wrong satellite.
            if ch_id not in received or (triggers and not received[ch_id].get("_has_triggers")):
                entry["_has_triggers"] = bool(triggers)
                received[ch_id] = entry
        elif e["event"] == "SETTLEMENT_FINALIZED" and ch_id not in finalized:
            finalized[ch_id] = e.get("t", 0.0)

    summary = []
    for ch_id, q in queued.items():
        rec = received.get(ch_id, {})
        res_list = resumed.get(ch_id, [])

        isl_prop       = q.get("isl_prop_delay_sec") or 0.0
        uplink_prop    = rec.get("uplink_prop_delay_sec") or 0.0
        wait_for_gs    = rec.get("wait_for_gs_sec") or 0.0
        gs_received_at = rec.get("gs_received_at")
        finalized_at   = finalized.get(ch_id)
        first_t        = first_proof.get(ch_id)
        queued_at      = q.get("queued_at")

        blockchain_sec = (
            round(finalized_at - gs_received_at, 6)
            if finalized_at is not None and gs_received_at is not None
            else None
        )
        contact_duration_sec = (
            round(queued_at - first_t, 3)
            if queued_at is not None and first_t is not None
            else None
        )
        protocol_latency_sec = round(isl_prop + uplink_prop + (blockchain_sec or 0.0), 6)

        isl_resume_breakdown = []
        for r in res_list:
            gs_sent_at = r.get("gs_sent_at")
            wait_dl = (
                round(gs_sent_at - finalized_at, 3)
                if gs_sent_at is not None and finalized_at is not None
                else None
            )
            isl_resume_breakdown.append({
                "satellite":             r.get("satellite"),
                "wait_for_downlink_sec": wait_dl,
                "downlink_prop_delay_sec": r.get("downlink_prop_delay_sec"),
                "resumed_at":            r.get("resumed_at"),
            })

        summary.append({
            "channel_id": ch_id,
            "queued_at":  queued_at,
            "offchain": {
                "contact_duration_sec": contact_duration_sec,
                "isl_prop_delay_sec":   round(isl_prop, 6),
            },
            "ground": {
                "wait_for_gs_sec":       wait_for_gs,
                "uplink_prop_delay_sec": round(uplink_prop, 6),
                "blockchain_sec":        blockchain_sec,
                "finalized_at":          finalized_at,
            },
            "isl_resume": isl_resume_breakdown,
            "protocol_latency_sec": protocol_latency_sec,
        })
    return sorted(summary, key=lambda x: (x.get("queued_at") or 0))


def _stats_for(values: List[float]) -> dict:
    if not values:
        return {"mean": 0, "p50": 0, "p95": 0, "p99": 0, "max": 0, "count": 0}
    arr = sorted(values)
    n = len(arr)
    return {
        "mean": round(sum(arr) / n, 6),
        "p50": arr[n // 2],
        "p95": arr[min(int(n * 0.95), n - 1)],
        "p99": arr[min(int(n * 0.99), n - 1)],
        "max": arr[-1],
        "count": n,
    }


def _compute_latency_stats(latency_summary: List[dict]) -> dict:
    """Compute separate offchain and onchain latency statistics.

    Offchain: ISL proof-exchange propagation delay (isl_prop_delay_sec).
    Onchain:  SETTLEMENT_RECEIVED → SETTLEMENT_FINALIZED (blockchain processing).
    Protocol: combined protocol latency excluding all orbital waits.
    """
    offchain_vals  = [s["offchain"]["isl_prop_delay_sec"] for s in latency_summary
                      if s.get("offchain", {}).get("isl_prop_delay_sec") is not None]
    onchain_vals   = [s["ground"]["blockchain_sec"] for s in latency_summary
                      if s.get("ground", {}).get("blockchain_sec") is not None]
    protocol_vals  = [s["protocol_latency_sec"] for s in latency_summary
                      if s.get("protocol_latency_sec") is not None]
    return {
        "offchain": _stats_for(offchain_vals),
        "onchain":  _stats_for(onchain_vals),
        "protocol": _stats_for(protocol_vals),
    }


def _write_settlement_log(
    experiment_name: str,
    all_sats: dict,
    all_gs: dict,
    out_dir: Path,
) -> dict:
    """Write a three-section JSON log: off-chain events, ground settlement lifecycle,
    and per-channel end-to-end latency breakdown.

    Returns latency stats dict for inclusion in the experiment metrics.
    """
    offchain: List[dict] = []
    ground:   List[dict] = []

    for sat_id, sat_node in all_sats.items():
        for entry in sat_node.event_log:
            ev = entry.get("event")
            if ev in _OFFCHAIN_SAT_EVENTS:
                offchain.append({"source": "satellite", "node": sat_id, **entry})

    for gs_id, gs_node in all_gs.items():
        for entry in gs_node.event_log:
            ev = entry.get("event")
            if ev in _GROUND_GS_EVENTS:
                ground.append({"source": "ground_station", "node": gs_id, **entry})

    offchain.sort(key=lambda e: e.get("t", 0))
    ground.sort(key=lambda e: e.get("t", 0))

    latency_summary = _compute_latency_summary(offchain, ground)
    latency_stats = _compute_latency_stats(latency_summary)

    log = {
        "experiment": experiment_name,
        "offchain": offchain,
        "ground_settlement": ground,
        "latency_summary": latency_summary,
    }
    path = out_dir / "logs" / f"{experiment_name}_settlement_log.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as f:
        json.dump(log, f, indent=2)
    print(f"  Settlement log  → {path}"
          f"  (offchain: {len(offchain)}, ground: {len(ground)}, settlements: {len(latency_summary)})")
    return latency_stats


# ── Blockchain setup ────────────────────────────────────────────────────────────

def _blockchain_setup(
    fabric,           # FabricClient instance
    all_sats: dict,   # sat_id → SatelliteNode
    sat_op_map: dict, # sat_id → operator_id
    operator_ids: list,
) -> None:
    """Bulk-register satellite keys and open operator channels in one InitLedger tx.

    A single atomic invoke replaces the previous N-invokes-per-satellite loop,
    which was slow because each call had to wait for block ordering.
    """
    from itertools import combinations

    op_bal = lios_cfg.blockchain.operator_channel_balance_kb
    op_res = lios_cfg.blockchain.operator_channel_reserve_kb

    operator_pairs = [
        {"op_a": op_a, "op_b": op_b, "bal_a": op_bal, "bal_b": op_bal, "reserve": op_res}
        for op_a, op_b in combinations(sorted(operator_ids), 2)
    ]
    satellite_keys = [
        {
            "operator_id":  sat_op_map[sat_id],
            "satellite_id": sat_id,
            "pub_key_pem":  sat_node.cert.public_key_pem,
        }
        for sat_id, sat_node in all_sats.items()
    ]

    from blockchain.blockchain_client import FabricClient
    n_ch    = len(operator_pairs)
    n_sat   = len(satellite_keys)
    n_batch = max(1, (n_sat + FabricClient._SAT_BATCH_SIZE - 1) // FabricClient._SAT_BATCH_SIZE)
    print(f"  [blockchain] InitLedger: {n_ch} operator channels + {n_sat} satellite keys "
          f"in {n_batch} batch(es)...")
    fabric.bulk_init(operator_pairs, satellite_keys)

    # Spot-check: verify the first satellite key landed on-chain.
    first_sat = next(iter(all_sats))
    if not _verify_sat_key(fabric, first_sat):
        raise RuntimeError(
            f"InitLedger was committed but satellite key for {first_sat!r} is not "
            "visible on-chain — check peer endorsement policy and TLS config.\n"
            "Re-run ./lios/evaluation/start_network.sh --reset to clear state."
        )
    print(f"  [blockchain] InitLedger complete.")


def _verify_sat_key(fabric, sat_id: str) -> bool:
    """Return True if the satellite key for sat_id is visible on-chain."""
    try:
        return fabric.verify_satellite_key(sat_id)
    except Exception:
        return False


# ── Runner ─────────────────────────────────────────────────────────────────────

def run_experiment(
    config: ExperimentConfig,
    data_dir: Path = DATA_DIR,
    out_dir: Optional[Path] = None,
    use_blockchain: bool = False,
    cache_dir: Path = CACHE_DIR,
) -> ExperimentResult:
    import time
    t0 = time.time()
    _step_t = t0

    def _step(label: str) -> None:
        nonlocal _step_t
        elapsed = time.time() - _step_t
        print(f"  [{label}]  ({elapsed:.1f}s)")
        _step_t = time.time()

    cache_dir.mkdir(parents=True, exist_ok=True)
    print(
        f"\n[EXP] {config.name}: duration={config.duration_sec}s  "
        f"global_rate={config.traffic_arrival_rate}"
    )

    # 1. Load TLEs and ground stations
    print("  Loading TLEs and ground stations...")
    operators_tles = TLELoader.load_all(data_dir)
    ground_stations = GSLoader.load_all(data_dir)
    operator_ids = list(operators_tles.keys())
    n_sats_total = sum(len(v) for v in operators_tles.values())
    n_gs_total   = sum(len(v) for v in ground_stations.values())
    operator_load_weights = (
        constellation_size_weights(operators_tles)
        if config.operator_load_weights is None
        else dict(config.operator_load_weights)
    )
    print(f"  Operator load weights: {operator_load_weights}")
    _step(f"1/8  {len(operator_ids)} operators, {n_sats_total} sats, {n_gs_total} GS")

    # 2. Compute contact plan (load from cache when available, save on miss)
    from precompute import CONTACT_PLAN_EPOCH
    epoch = CONTACT_PLAN_EPOCH
    from precompute import load_contact_plan, save_contact_plan

    cp = load_contact_plan(
        cache_dir, config.duration_sec, config.time_step_sec, config.isl_range_km
    )
    if cp is None:
        t_end = epoch + timedelta(seconds=config.duration_sec)
        steps_est = int(config.duration_sec / config.time_step_sec) + 1
        print(f"  Computing contact plan ({n_sats_total} sats, {steps_est} steps, step={config.time_step_sec}s)...")
        prop_log_path = (
            out_dir / "logs" / f"{config.name}_propagation_log.json" if out_dir else None
        )
        calc = WindowCalculator(
            epoch, t_end, config.time_step_sec, config.isl_range_km,
            propagation_log_path=prop_log_path,
        )
        cp = calc.compute(operators_tles, ground_stations)
        save_contact_plan(
            cache_dir, cp, config.duration_sec, config.time_step_sec,
            config.isl_range_km,
        )
    _step(f"2/8  contact plan: {len(cp.contacts)} contacts")

    if out_dir is not None:
        cp_path = out_dir / "logs" / f"{config.name}_contact_plan.csv"
        cp.to_csv(cp_path)
        print(f"       → {cp_path}")

    # 3. Build operator CAs and satellite nodes
    print("  Building satellite nodes...")
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
                    p_attack=config.p_attack,
                    p_drop=config.p_drop,
                    attack_seed=config.random_seed,
                )
            elif config.baseline_protocol != "lios":
                from evaluation.baselines import (
                    GreedySatelliteNode, TitForTatNode, CentralSatelliteNode,
                    GroundResetSatelliteNode,
                )
                _cls = {"greedy": GreedySatelliteNode,
                        "t4t":    TitForTatNode,
                        "central": CentralSatelliteNode,
                        "ground_reset": GroundResetSatelliteNode}[config.baseline_protocol]
                node = create_satellite(sat_meta.sat_id, op, cas[op], cas, isl_fsm,
                                        operator_ids, node_class=_cls)
            else:
                node = create_satellite(sat_meta.sat_id, op, cas[op], cas, isl_fsm, operator_ids)
            all_sats[sat_meta.sat_id] = node
            sat_op_map[sat_meta.sat_id] = op

    _step(f"3/8  {len(all_sats)} satellite nodes  (adversarial={config.adversarial_mode})")

    # 4. Build ground station nodes
    print("  Setting up ground station nodes" + (" + blockchain" if use_blockchain else "") + "...")
    if use_blockchain:
        from blockchain.blockchain_client import FabricClient
        fabric = FabricClient()
        print("  [blockchain] Connected to Hyperledger Fabric network.")
        _blockchain_setup(fabric, all_sats, sat_op_map, operator_ids)
    elif config.baseline_protocol == "central":
        from evaluation.baselines import CentralFabricMock
        fabric = CentralFabricMock()
    elif config.baseline_protocol == "ground_reset":
        from evaluation.baselines import GroundResetFabricMock
        fabric = GroundResetFabricMock()
    else:
        fabric = FabricMock()

    # Federated GS access: any authorized GS may deliver pending notifications
    # to any satellite, even when the physical station belongs to another operator.
    shared_pending: Dict[str, List] = {}
    all_gs: Dict[str, GroundStationNode] = {}
    _gs_cls = GroundStationNode
    if config.baseline_protocol == "central":
        from evaluation.baselines import CentralGroundStationNode
        _gs_cls = CentralGroundStationNode
    elif config.baseline_protocol == "ground_reset":
        from evaluation.baselines import GroundResetGroundStationNode
        _gs_cls = GroundResetGroundStationNode
    for op, gs_list in ground_stations.items():
        for gs_meta in gs_list:
            gs_node = _gs_cls(gs_meta, fabric, isl_fsm,
                              shared_pending=shared_pending)
            all_gs[gs_meta.gs_id] = gs_node
    # Inject peer GS registry so each GS can push SETTLEMENT_TRIGGER to peer operators.
    gs_registry = {gn.operator_id: gs_id for gs_id, gn in all_gs.items()}
    for gn in all_gs.values():
        gn.gs_registry = gs_registry

    _step(f"4/8  {len(all_gs)} ground station nodes")

    # 5–7. Load or compute direct active-pair traffic schedule.
    from precompute import load_traffic_schedule
    from simulator.traffic_generator import TrafficStats

    arrival_rate = config.traffic_arrival_rate
    schedule = load_traffic_schedule(
        cache_dir, config.random_seed, arrival_rate,
        config.time_step_sec, config.isl_range_km, config.duration_sec,
        operator_load_weights,
    )

    if schedule is None:
        from precompute import save_traffic_schedule
        print("  Building direct active-pair traffic schedule...")
        tgen = TrafficGenerator(
            cp, sat_op_map,
            arrival_rate=arrival_rate,
            seed=config.random_seed,
            operator_load_weights=operator_load_weights,
        )
        schedule = tgen.generate_poisson_schedule(0.0, config.duration_sec)
        tgen_stats = tgen.stats
        save_traffic_schedule(
            cache_dir, schedule, config.random_seed, arrival_rate,
            config.time_step_sec, config.isl_range_km, config.duration_sec,
            operator_load_weights,
        )
        _step(f"5/8  active-pair traffic schedule ({len(schedule)} flows)")
    else:
        tgen_stats = TrafficStats()
        for _, flow in schedule:
            tgen_stats.record(flow)
        _step(f"5/8  traffic schedule loaded from cache ({len(schedule)} flows)")

    # 6. Seed DES event loop with contact plan events
    print(f"  Seeding DES event loop ({len(cp.contacts):,} contacts)...")
    loop = EventLoop(seed=config.random_seed)
    for sat_id, sat_node in all_sats.items():
        loop.register_node(sat_id, sat_node.handle_event)
    for gs_id, gs_node in all_gs.items():
        loop.register_node(gs_id, gs_node.handle_event)
    loop.seed_contacts_from_plan(cp)
    _step(f"6/8  DES contact events seeded")

    n_allocated = 0
    for arr_t, flow in schedule:
        if flow.src_satellite and flow.dst_satellite:
            loop.schedule(SimEvent(
                time=arr_t,
                event_type=EventType.TRAFFIC_ARRIVE,
                from_node=flow.src_satellite,
                to_node=flow.src_satellite,
                payload=flow,
            ))
            n_allocated += 1
    _step(f"7/8  {n_allocated} allocated flows queued into DES")

    # 8. Run simulation
    print(f"  Running simulation (t=0..{config.duration_sec:,}s)...")
    stats = loop.run(until=config.duration_sec)
    _step(f"7/8  simulation done: {stats.events_processed:,} events")

    # 9. Collect metrics
    metrics = MetricsCollector(operator_ids)
    for sat_id, sat_node in all_sats.items():
        for entry in sat_node.event_log:
            if entry.get("event") != "TRAFFIC_FORWARDED":
                continue
            peer_id = entry.get("peer_id", "")
            metrics.record_forwarding(
                sat_id,
                peer_id,
                entry.get("bytes_kb", 0.0),
                entry.get("t", 0.0),
                sat_op_map.get(sat_id, ""),
                sat_op_map.get(peer_id, ""),
            )

    total_isl_contact_sec = sum(
        c.duration_sec for c in cp.get_isl_contacts()
    )

    # ── Populate security records from adversarial nodes and FabricMock ──────
    if config.adversarial_mode != "none":
        fabric_penalties = fabric.get_penalty_events()
        # Build a lookup: channel_id → detection event from Fabric
        penalty_by_ch: dict = {}
        for pe in fabric_penalties:
            ch = pe.get("channel_id", "")
            penalty_by_ch.setdefault(ch, []).append(pe)

        for sat_node in all_sats.values():
            from evaluation.adversarial import MaliciousSatelliteNode as _MSN
            if not isinstance(sat_node, _MSN):
                continue
            for ar in sat_node.attack_log:
                if ar.attack_type == "rollback":
                    pen_list = penalty_by_ch.get(ar.channel_id, [])
                    # Match by attack_seq
                    matching = [p for p in pen_list if p.get("attack_seq") == ar.details.get("fake_seq")]
                    detected = len(matching) > 0
                    t_detected = matching[0]["t_detected"] if detected else 0.0
                    metrics.record_rollback(
                        channel_id=ar.channel_id,
                        attack_seq=ar.details.get("fake_seq", 0),
                        honest_seq=ar.details.get("real_seq", 0),
                        gain_kb=ar.details.get("gain_kb", 0.0),
                        t_attack=ar.sim_time,
                        detected=detected,
                        t_detected=t_detected,
                    )
                elif ar.attack_type == "selective_forward_drop":
                    metrics.record_selective_drop(
                        channel_id=ar.channel_id,
                        bytes_kb=ar.details.get("bytes_kb", 0.0),
                        sim_time=ar.sim_time,
                    )

    # ── Extract per-operator payments from finalized settlements ─────────────
    # channel_id = "<sat_a>__<sat_b>" (sorted); balance_a belongs to sat_a's operator.
    # initial per-side = channel_balance_kb / 2; positive delta_a → op_a received net payment.
    from simulator.ground_station_node import FabricMock as _FabricMock
    if isinstance(fabric, _FabricMock):
        initial_half_kb = lios_cfg.protocol.channel_balance_kb / 2.0
        for ch_id, rec in fabric._settlements.items():
            if rec.get("status") not in ("FINALIZED", "RESET", "MUTUAL_FINALIZED"):
                continue
            proof = rec.get("proof", {})
            bal_a = proof.get("balance_a_kb", initial_half_kb)
            parts = ch_id.split("__")
            if len(parts) != 2:
                continue
            sat_a, sat_b = parts
            op_a = sat_op_map.get(sat_a, "")
            op_b = sat_op_map.get(sat_b, "")
            if not op_a or not op_b or op_a == op_b:
                continue
            delta_a = bal_a - initial_half_kb
            if delta_a > 0:
                metrics.record_payment(from_op=op_b, to_op=op_a, amount_kb=delta_a)
            elif delta_a < 0:
                metrics.record_payment(from_op=op_a, to_op=op_b, amount_kb=-delta_a)

    report = metrics.generate_report(total_isl_contact_sec)
    report["simulation_stats"] = stats.to_dict()
    report["simulation_stats"]["traffic_arrival_rate"] = config.traffic_arrival_rate
    report["simulation_stats"]["traffic_allocation"] = (
        lios_cfg.simulation.traffic_allocation
    )
    report["simulation_stats"]["traffic_direction_bias"] = (
        lios_cfg.simulation.traffic_direction_bias
    )
    report["simulation_stats"]["operator_load_weights"] = (
        operator_load_weights
    )
    report["traffic_gen"] = tgen_stats.summary()
    print(f"  Jain fairness: {report['jain_fairness_index']:.4f}")
    print(f"  Utility Jain:  {report['utility_jain']:.4f}")
    if config.adversarial_mode != "none":
        print(f"  Detection rate: {report['detection_rate']:.3f}  "
              f"  Rollback attempts: {report['rollback_attempts']}"
              f"  Selective drops: {report['selective_drops']}")
    print(f"  Flows generated: {tgen_stats.flows_generated}")

    # 10. Per-contact traffic attribution
    contact_traffic = _compute_contact_traffic(cp, all_sats)
    if out_dir is not None:
        ct_path = out_dir / "logs" / f"{config.name}_contact_traffic.json"
        ct_path.parent.mkdir(parents=True, exist_ok=True)
        with ct_path.open("w") as f:
            json.dump(contact_traffic, f, separators=(",", ":"))
        n_active = sum(1 for v in contact_traffic.values() if v["traffic_kb"] > 0)
        print(f"  Contact traffic → {ct_path}  ({n_active}/{len(contact_traffic)} contacts had traffic)")

    # 11. Settlement trigger log (returns latency stats to override the placeholder zeros)
    if out_dir is not None:
        latency_stats = _write_settlement_log(config.name, all_sats, all_gs, out_dir)
        report["settlement_latency"] = latency_stats

    # 12. Balance events — OFFCHAIN_PROOF_UPDATE snapshots for post-run analysis
    if out_dir is not None:
        bal_events = [
            e for sat in all_sats.values()
            for e in sat.event_log
            if e.get("event") == "OFFCHAIN_PROOF_UPDATE"
        ]
        bal_path = out_dir / "logs" / f"{config.name}_balance_events.json"
        with bal_path.open("w") as f:
            json.dump(bal_events, f, separators=(",", ":"))
        print(f"  Balance events → {bal_path}  ({len(bal_events):,} snapshots)")

    wall = time.time() - t0
    _step(f"8/8  metrics + logs done")
    print(f"  Total wall time: {wall:.1f}s")
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

    # Figure 2: Settlement latency — offchain vs on-chain mean per config
    # Orbital wait time (wait_for_gs) is excluded; only protocol-driven latency shown.
    lat_names, lat_offchain, lat_onchain, lat_protocol = [], [], [], []
    for r in results:
        sl = r.metrics.get("settlement_latency", {})
        if sl.get("protocol", {}).get("count", 0) > 0:
            lat_names.append(r.config.name)
            lat_offchain.append(sl.get("offchain", {}).get("mean", 0))
            lat_onchain.append(sl.get("onchain",  {}).get("mean", 0))
            lat_protocol.append(sl.get("protocol", {}).get("mean", 0))
    if lat_names:
        x = np.arange(len(lat_names))
        width = 0.25
        fig, ax = plt.subplots(figsize=(max(8, len(lat_names) * 1.6), 5))
        ax.bar(x - width,     lat_offchain, width, label="Off-chain ISL propagation",    color="steelblue", alpha=0.85)
        ax.bar(x,             lat_onchain,  width, label="On-chain blockchain processing", color="coral",    alpha=0.85)
        ax.bar(x + width,     lat_protocol, width, label="Protocol total (excl. orbital waits)", color="mediumseagreen", alpha=0.85)
        ax.set_xticks(x)
        ax.set_xticklabels(lat_names, rotation=15, ha="right", fontsize=9)
        ax.set_ylabel("Mean latency (s)")
        ax.set_title("LIOS Protocol — Settlement Latency Breakdown\n(orbital wait excluded from all bars)")
        ax.legend(fontsize=9)
        ax.grid(True, axis="y", alpha=0.3)
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
    parser.add_argument("--cache", default=str(CACHE_DIR), help="Cache directory")
    parser.add_argument(
        "--blockchain",
        action="store_true",
        default=False,
        help=(
            "Connect settlements to a real Hyperledger Fabric network. "
            "Requires scripts/start_network.sh to have been run and "
            "fabric-gateway + grpcio to be installed."
        ),
    )
    args = parser.parse_args()

    out_dir = Path(args.out)
    data_dir = Path(args.data)
    cache_dir = Path(args.cache)
    configs = EXPERIMENT_CONFIGS
    if args.config:
        configs = [c for c in EXPERIMENT_CONFIGS if c.name == args.config]
        if not configs:
            print(f"Unknown config: {args.config!r}. Available: {[c.name for c in EXPERIMENT_CONFIGS]}")
            sys.exit(1)

    results: List[ExperimentResult] = []
    for cfg in configs:
        result = run_experiment(
            cfg,
            data_dir,
            out_dir=out_dir,
            use_blockchain=args.blockchain,
            cache_dir=cache_dir,
        )
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
        # print(generate_latex_table(results))


if __name__ == "__main__":
    main()

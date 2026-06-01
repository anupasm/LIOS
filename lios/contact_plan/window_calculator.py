"""Contact window calculator using SGP4 propagation.

Computes sat-sat ISL windows and GS-sat windows; returns a ContactPlan.
"""
from __future__ import annotations

import csv
import json
import math
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from multiprocessing import Pool
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np
from scipy.spatial import cKDTree
from sgp4.api import jday

from config import cfg
from contact_plan.gs_loader import GroundStation
from contact_plan.tle_loader import Satellite

C_KM_S         = cfg.link.c_km_s
C_MAX_ISL_KBPS = cfg.link.isl_max_kbps
C_MAX_GS_KBPS  = cfg.link.gs_max_kbps

_WGS84_RE = 6378.137          # equatorial radius km
_WGS84_E2 = 0.00669437999014  # first eccentricity squared


@dataclass
class Contact:
    contact_id: str
    from_node: str
    to_node: str
    start_time_sec: float
    end_time_sec: float
    capacity_kbps: float
    range_km: float
    node_type_from: str   # 'SAT' | 'GS'
    node_type_to: str
    operator_from: str
    operator_to: str

    @property
    def duration_sec(self) -> float:
        return self.end_time_sec - self.start_time_sec


@dataclass
class ContactPlan:
    contacts: List[Contact] = field(default_factory=list)
    epoch: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def get_contacts_at(self, t: float) -> List[Contact]:
        return [c for c in self.contacts if c.start_time_sec <= t < c.end_time_sec]

    def get_contacts_for_node(self, node_id: str) -> List[Contact]:
        return [c for c in self.contacts if c.from_node == node_id or c.to_node == node_id]

    def get_isl_contacts(self) -> List[Contact]:
        return [c for c in self.contacts if c.node_type_from == "SAT" and c.node_type_to == "SAT"]

    def get_gs_contacts(self) -> List[Contact]:
        return [c for c in self.contacts if c.node_type_from == "GS" or c.node_type_to == "GS"]

    def get_all_satellites(self) -> List[str]:
        sats: set[str] = set()
        for c in self.contacts:
            if c.node_type_from == "SAT":
                sats.add(c.from_node)
            if c.node_type_to == "SAT":
                sats.add(c.to_node)
        return sorted(sats)

    def get_ground_nodes(self) -> List[str]:
        gns: set[str] = set()
        for c in self.contacts:
            if c.node_type_from == "GS":
                gns.add(c.from_node)
            if c.node_type_to == "GS":
                gns.add(c.to_node)
        return sorted(gns)

    def get_ground_stations(self) -> List[str]:
        return self.get_ground_nodes()

    def get_reachable_satellites(
        self, ground_node: str, t_start: float, t_end: float
    ) -> List[str]:
        reachable: set[str] = set()
        for c in self.contacts:
            if c.end_time_sec < t_start or c.start_time_sec > t_end:
                continue
            if c.from_node == ground_node and c.node_type_to == "SAT":
                reachable.add(c.to_node)
            elif c.to_node == ground_node and c.node_type_from == "SAT":
                reachable.add(c.from_node)
        return sorted(reachable)

    def to_csv(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", newline="") as f:
            w = csv.writer(f)
            w.writerow([
                "contact_id", "from_node", "to_node",
                "start_time_sec", "end_time_sec", "capacity_kbps",
                "range_km", "node_type_from", "node_type_to",
                "operator_from", "operator_to",
            ])
            for c in self.contacts:
                w.writerow([
                    c.contact_id, c.from_node, c.to_node,
                    f"{c.start_time_sec:.2f}", f"{c.end_time_sec:.2f}",
                    f"{c.capacity_kbps:.2f}", f"{c.range_km:.2f}",
                    c.node_type_from, c.node_type_to,
                    c.operator_from, c.operator_to,
                ])

    @classmethod
    def from_csv(cls, path: Path, epoch: datetime) -> "ContactPlan":
        cp = cls(epoch=epoch)
        with path.open() as f:
            for row in csv.DictReader(f):
                cp.contacts.append(Contact(
                    contact_id=row["contact_id"],
                    from_node=row["from_node"],
                    to_node=row["to_node"],
                    start_time_sec=float(row["start_time_sec"]),
                    end_time_sec=float(row["end_time_sec"]),
                    capacity_kbps=float(row["capacity_kbps"]),
                    range_km=float(row["range_km"]),
                    node_type_from=row["node_type_from"],
                    node_type_to=row["node_type_to"],
                    operator_from=row["operator_from"],
                    operator_to=row["operator_to"],
                ))
        return cp


# ── geometry helpers ───────────────────────────────────────────────────────────

def _eci_position_km(sat: Satellite, jd: float, fr: float) -> Optional[np.ndarray]:
    e, r, _ = sat.satrec.sgp4(jd, fr)
    return None if e != 0 else np.array(r)


def _geodetic_to_eci(lat_deg: float, lon_deg: float, alt_m: float, gmst_rad: float) -> np.ndarray:
    lat, lon = math.radians(lat_deg), math.radians(lon_deg)
    alt_km = alt_m / 1000.0
    N = _WGS84_RE / math.sqrt(1 - _WGS84_E2 * math.sin(lat) ** 2)
    x_e = (N + alt_km) * math.cos(lat) * math.cos(lon)
    y_e = (N + alt_km) * math.cos(lat) * math.sin(lon)
    z_e = (N * (1 - _WGS84_E2) + alt_km) * math.sin(lat)
    cg, sg = math.cos(gmst_rad), math.sin(gmst_rad)
    return np.array([cg * x_e - sg * y_e, sg * x_e + cg * y_e, z_e])


def _gmst(jd: float, fr: float) -> float:
    T = (jd + fr - 2451545.0) / 36525.0
    theta = 67310.54841 + (876600 * 3600 + 8640184.812866) * T + 0.093104 * T ** 2
    return math.radians(theta % 86400 * 360 / 86400)


def _eci_to_geodetic(eci_km: np.ndarray, gmst_rad: float) -> Tuple[float, float, float]:
    """Bowring's iterative method; < 1 mm error for LEO."""
    x, y, z = float(eci_km[0]), float(eci_km[1]), float(eci_km[2])
    cg, sg = math.cos(gmst_rad), math.sin(gmst_rad)
    x_e, y_e, z_e = cg * x + sg * y, -sg * x + cg * y, z
    p = math.sqrt(x_e ** 2 + y_e ** 2)
    lon_rad = math.atan2(y_e, x_e)
    lat_rad = math.atan2(z_e, p * (1.0 - _WGS84_E2))
    for _ in range(10):
        N = _WGS84_RE / math.sqrt(1.0 - _WGS84_E2 * math.sin(lat_rad) ** 2)
        lat_new = math.atan2(z_e + _WGS84_E2 * N * math.sin(lat_rad), p)
        if abs(lat_new - lat_rad) < 1e-12:
            break
        lat_rad = lat_new
    N = _WGS84_RE / math.sqrt(1.0 - _WGS84_E2 * math.sin(lat_rad) ** 2)
    alt_km = (
        p / math.cos(lat_rad) - N
        if abs(math.cos(lat_rad)) > 1e-6
        else abs(z_e) / abs(math.sin(lat_rad)) - N * (1.0 - _WGS84_E2)
    )
    return math.degrees(lat_rad), math.degrees(lon_rad), alt_km


def _elevation_deg(
    gs_eci: np.ndarray, sat_eci: np.ndarray,
    gs_lat_rad: float, gs_lon_rad: float, gmst_rad: float,
) -> float:
    rng_norm = (sat_eci - gs_eci) / np.linalg.norm(sat_eci - gs_eci)
    cos_lat = math.cos(gs_lat_rad)
    eci_lon = gs_lon_rad + gmst_rad
    zenith = np.array([cos_lat * math.cos(eci_lon), cos_lat * math.sin(eci_lon), math.sin(gs_lat_rad)])
    return math.degrees(math.asin(max(-1.0, min(1.0, float(np.dot(rng_norm, zenith))))))


def _write_propagation_log(
    path: Path,
    cp: "ContactPlan",
    all_sats: list,
    all_gs: list,
    ts_arr: List[float],
    geo: List[List[Optional[Tuple[float, float, float]]]],
) -> None:
    satellites_out = [
        {
            "sat_id": sat.sat_id,
            "operator_id": sat.operator_id,
            "track": [
                [round(t, 1), round(g[0], 5), round(g[1], 5), round(g[2], 3)]
                for step_idx, t in enumerate(ts_arr)
                if (g := geo[si][step_idx]) is not None
            ],
        }
        for si, sat in enumerate(all_sats)
    ]
    t_step = ts_arr[1] - ts_arr[0] if len(ts_arr) > 1 else 0
    log = {
        "epoch": cp.epoch.isoformat(),
        "duration_sec": ts_arr[-1] if ts_arr else 0,
        "time_step_sec": t_step,
        "ground_stations": [
            {"gs_id": gs.gs_id, "operator_id": gs.operator_id,
             "lat": gs.lat_deg, "lon": gs.lon_deg, "alt_m": gs.alt_m}
            for gs in all_gs
        ],
        "satellites": satellites_out,
        "contacts": [
            {
                "contact_id": c.contact_id,
                "from_node": c.from_node, "to_node": c.to_node,
                "start_time_sec": round(c.start_time_sec, 1),
                "end_time_sec": round(c.end_time_sec, 1),
                "node_type_from": c.node_type_from, "node_type_to": c.node_type_to,
                "operator_from": c.operator_from, "operator_to": c.operator_to,
                "range_km": round(c.range_km, 2),
                "capacity_kbps": round(c.capacity_kbps, 2),
            }
            for c in cp.contacts
        ],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as f:
        json.dump(log, f, separators=(",", ":"))
    n_pts = sum(len(s["track"]) for s in satellites_out)
    print(f"  Propagation log → {path}  "
          f"({path.stat().st_size // 1024} KB, {len(satellites_out)} sats, "
          f"{n_pts} track points, {len(log['contacts'])} contacts)")


# ── checkpoint helpers ─────────────────────────────────────────────────────────

_CKPT_FIELDS = [
    "contact_id", "from_node", "to_node",
    "start_time_sec", "end_time_sec", "capacity_kbps",
    "range_km", "node_type_from", "node_type_to",
    "operator_from", "operator_to",
]


def _write_contacts_csv(path: Path, raw_contacts: List[dict]) -> None:
    """Write raw contact dicts to CSV without creating Contact dataclass objects."""
    with path.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(_CKPT_FIELDS)
        w.writerows(
            (f"C{idx:06d}",
             c["from_node"], c["to_node"],
             f"{c['start_time_sec']:.2f}", f"{c['end_time_sec']:.2f}",
             f"{c['capacity_kbps']:.2f}", f"{c['range_km']:.2f}",
             c["node_type_from"], c["node_type_to"],
             c["operator_from"], c["operator_to"])
            for idx, c in enumerate(raw_contacts, 1)
        )


def _read_contacts_csv(path: Path) -> List[dict]:
    """Read raw contact dicts from CSV without creating Contact dataclass objects."""
    with path.open() as f:
        return [
            {
                "from_node": r["from_node"], "to_node": r["to_node"],
                "start_time_sec": float(r["start_time_sec"]),
                "end_time_sec": float(r["end_time_sec"]),
                "capacity_kbps": float(r["capacity_kbps"]),
                "range_km": float(r["range_km"]),
                "node_type_from": r["node_type_from"], "node_type_to": r["node_type_to"],
                "operator_from": r["operator_from"], "operator_to": r["operator_to"],
            }
            for r in csv.DictReader(f)
        ]


# ── multiprocessing workers ────────────────────────────────────────────────────

_WORKER_POS_ARR: Optional[np.ndarray] = None   # (n_sats, steps, 3)
_WORKER_TS_ARR:  Optional[np.ndarray] = None   # (steps,)


def _init_isl_worker(pos_arr: np.ndarray, ts_arr: np.ndarray) -> None:
    global _WORKER_POS_ARR, _WORKER_TS_ARR
    _WORKER_POS_ARR = pos_arr
    _WORKER_TS_ARR  = ts_arr


def _isl_pair_worker(args: tuple) -> List[dict]:
    """Vectorised ISL contact detector; reads pos_arr from process globals."""
    i, j, from_id, to_id, from_op, to_op, isl_max_range_km = args
    pos_i, pos_j = _WORKER_POS_ARR[i], _WORKER_POS_ARR[j]
    ts = _WORKER_TS_ARR

    valid = ~(np.isnan(pos_i[:, 0]) | np.isnan(pos_j[:, 0]))
    dists = np.where(valid, np.linalg.norm(pos_i - pos_j, axis=1), np.inf)
    in_range = valid & (dists < isl_max_range_km)

    bordered = np.concatenate(([0], in_range.astype(np.int8), [0]))
    delta = np.diff(bordered)
    starts_idx = np.where(delta ==  1)[0]
    ends_idx   = np.where(delta == -1)[0]

    contacts: List[dict] = []
    for s, e in zip(starts_idx, ends_idx):
        mean_range = float(np.mean(dists[s:e]))
        contacts.append(dict(
            from_node=from_id, to_node=to_id,
            start_time_sec=float(ts[s]),
            end_time_sec=float(ts[e - 1]),
            capacity_kbps=C_MAX_ISL_KBPS * max(0.0, 1.0 - mean_range / isl_max_range_km),
            range_km=mean_range,
            node_type_from="SAT", node_type_to="SAT",
            operator_from=from_op, operator_to=to_op,
        ))
    return contacts


def _gs_sat_worker(args: tuple) -> List[dict]:
    """GS-sat contact detector; reads pos_arr from process globals."""
    (gs_id, gs_op, gs_lat_deg, gs_lon_deg, gs_alt_m, gs_min_elev,
     si, epoch_jd, epoch_fr, sat_id, sat_op) = args
    gs_lat_rad = math.radians(gs_lat_deg)
    gs_lon_rad = math.radians(gs_lon_deg)
    sat_pos = _WORKER_POS_ARR[si]
    ts      = _WORKER_TS_ARR
    contacts: List[dict] = []
    in_contact = False
    contact_start = 0.0
    ranges_in_contact: List[float] = []

    def _flush(end_sec: float) -> None:
        nonlocal in_contact
        mean_range = float(np.mean(ranges_in_contact))
        contacts.append(dict(
            from_node=gs_id, to_node=sat_id,
            start_time_sec=contact_start, end_time_sec=end_sec,
            capacity_kbps=C_MAX_GS_KBPS * max(0.0, 1.0 - mean_range / 3000.0),
            range_km=mean_range,
            node_type_from="GS", node_type_to="SAT",
            operator_from=gs_op, operator_to=sat_op,
        ))
        in_contact = False

    for step_idx in range(len(ts)):
        p_sat = sat_pos[step_idx]
        if np.isnan(p_sat[0]):
            if in_contact:
                _flush(float(ts[step_idx - 1]) if step_idx > 0 else contact_start)
            continue
        t_sec = float(ts[step_idx])
        fr_step = epoch_fr + t_sec / 86400.0
        jd_step = epoch_jd + math.floor(fr_step)
        fr_step -= math.floor(fr_step)
        gmst   = _gmst(jd_step, fr_step)
        gs_eci = _geodetic_to_eci(gs_lat_deg, gs_lon_deg, gs_alt_m, gmst)
        el     = _elevation_deg(gs_eci, p_sat, gs_lat_rad, gs_lon_rad, gmst)
        dist   = float(np.linalg.norm(p_sat - gs_eci))
        if el >= gs_min_elev:
            if not in_contact:
                in_contact = True
                contact_start = t_sec
                ranges_in_contact = [dist]
            else:
                ranges_in_contact.append(dist)
        elif in_contact:
            _flush(float(ts[step_idx - 1]) if step_idx > 0 else t_sec)

    if in_contact:
        _flush(float(ts[-1]))
    return contacts


# ── main calculator ────────────────────────────────────────────────────────────

class WindowCalculator:

    def __init__(
        self,
        t_start: datetime,
        t_end: datetime,
        time_step_sec: int = 10,
        isl_max_range_km: float = 2500.0,
        propagation_log_path: Optional[Path] = None,
        checkpoint_dir: Optional[Path] = None,
    ):
        self.t_start = t_start
        self.t_end = t_end
        self.time_step_sec = time_step_sec
        self.isl_max_range_km = isl_max_range_km
        self.propagation_log_path = propagation_log_path
        self.checkpoint_dir = checkpoint_dir

    def _ckpt(self, name: str) -> Optional[Path]:
        if self.checkpoint_dir is None:
            return None
        self.checkpoint_dir.mkdir(parents=True, exist_ok=True)
        stem = f"step{self.time_step_sec}_range{self.isl_max_range_km:.0f}"
        return self.checkpoint_dir / f"ckpt_{stem}_{name}"

    def compute(
        self,
        operators: Dict[str, List[Satellite]],
        ground_stations: Dict[str, List[GroundStation]],
        n_workers: Optional[int] = None,
    ) -> ContactPlan:
        cp      = ContactPlan(epoch=self.t_start)
        all_sats: List[Satellite]       = [s  for sats in operators.values()       for s  in sats]
        all_gs:   List[GroundStation]   = [gs for gss  in ground_stations.values() for gs in gss]

        duration_sec = (self.t_end - self.t_start).total_seconds()
        steps  = int(duration_sec / self.time_step_sec) + 1
        ts_arr = [i * self.time_step_sec for i in range(steps)]

        epoch_jd, epoch_fr = jday(
            self.t_start.year, self.t_start.month, self.t_start.day,
            self.t_start.hour, self.t_start.minute,
            self.t_start.second + self.t_start.microsecond / 1e6,
        )

        _logging = self.propagation_log_path is not None
        n_sats  = len(all_sats)
        workers = n_workers or os.cpu_count() or 1

        ckpt_p1  = self._ckpt("pos.npy")
        ckpt_p15 = self._ckpt("candidates.npz")
        ckpt_p2  = self._ckpt("isl_contacts.csv")
        ckpt_p3  = self._ckpt("all_contacts.csv")

        _geo: List[List[Optional[Tuple[float, float, float]]]] = (
            [[None] * steps for _ in range(n_sats)] if _logging else []
        )

        # Precompute JD/FR for all timesteps (avoids repeated floor/mod inside the loop)
        _deltas      = np.array(ts_arr, dtype=np.float64) / 86400.0
        _fr_combined = epoch_fr + _deltas
        _jd_int      = np.floor(_fr_combined)
        jd_all = (epoch_jd + _jd_int).astype(np.float64)
        fr_all = (_fr_combined - _jd_int).astype(np.float64)

        # ── Phase 1: SGP4 propagation → pos_arr ──────────────────────────────
        if ckpt_p1 and ckpt_p1.exists():
            print(f"  [ckpt] Phase 1: loading {ckpt_p1.name} ...", flush=True)
            pos_arr = np.load(str(ckpt_p1))
            print(f"  [ckpt] Phase 1: loaded  {pos_arr.shape}  "
                  f"({pos_arr.nbytes // 1024 // 1024} MB)")
        else:
            pos_arr = np.full((n_sats, steps, 3), np.nan, dtype=np.float64)
            print(f"    Phase 1/3 propagation: {n_sats} sats × {steps} steps", flush=True)
            t0 = time.perf_counter()
            _last = t0 - 30
            for step_idx in range(steps):
                jd_s = float(jd_all[step_idx])
                fr_s = float(fr_all[step_idx])
                gmst = _gmst(jd_s, fr_s) if _logging else 0.0
                for si, sat in enumerate(all_sats):
                    pos = _eci_position_km(sat, jd_s, fr_s)
                    if pos is not None:
                        pos_arr[si, step_idx] = pos
                    if _logging and pos is not None:
                        _geo[si][step_idx] = _eci_to_geodetic(pos, gmst)
                now = time.perf_counter()
                if step_idx == steps - 1 or now - _last >= 30:
                    print(f"    Phase 1/3 propagation: {(step_idx+1)*100//steps:3d}%  "
                          f"({step_idx+1}/{steps} steps, {now-t0:.1f}s)   ",
                          end="\r", flush=True)
                    _last = now
            print(f"    Phase 1/3 propagation: 100%  ({steps}/{steps} steps) done")
            if ckpt_p1:
                np.save(str(ckpt_p1), pos_arr)
                print(f"  [ckpt] Phase 1 saved → {ckpt_p1.name}  "
                      f"({pos_arr.nbytes // 1024 // 1024} MB)")

        ts_np      = np.array(ts_arr, dtype=np.float64)
        n_pairs_max = n_sats * (n_sats - 1) // 2

        # ── Phase 1.5: Spatial candidate-pair pruning ─────────────────────────
        if ckpt_p15 and ckpt_p15.exists():
            print(f"  [ckpt] Phase 1.5: loading {ckpt_p15.name} ...", flush=True)
            _d = np.load(str(ckpt_p15))
            i_arr, j_arr = _d["i"], _d["j"]
            total_isl = len(i_arr)
            print(f"  [ckpt] Phase 1.5: loaded  {total_isl:,} candidate pairs")
        else:
            # Boolean matrix marks pairs seen in range at any sampled timestep.
            # cKDTree reduces O(n²) checks to O(n·k) per step (k = local neighbors).
            candidate_matrix = np.zeros((n_sats, n_sats), dtype=bool)
            print(f"    Phase 1.5/3 spatial index: {steps} steps, "
                  f"{n_pairs_max:,} O(n²) pairs ...", flush=True)
            t0 = time.perf_counter()
            _last = t0 - 30
            for step_idx in range(steps):
                valid_idx = np.where(~np.isnan(pos_arr[:, step_idx, 0]))[0]
                if len(valid_idx) < 2:
                    continue
                step_pairs = cKDTree(pos_arr[valid_idx, step_idx, :]).query_pairs(
                    self.isl_max_range_km, output_type="ndarray"
                )
                if len(step_pairs):
                    gi, gj = valid_idx[step_pairs[:, 0]], valid_idx[step_pairs[:, 1]]
                    candidate_matrix[np.minimum(gi, gj), np.maximum(gi, gj)] = True
                now = time.perf_counter()
                if step_idx == steps - 1 or now - _last >= 30:
                    print(
                        f"    Phase 1.5/3 spatial index: {(step_idx+1)*100//steps:3d}%  "
                        f"({step_idx+1}/{steps} steps, "
                        f"{int(candidate_matrix.sum()):,} pairs, {now-t0:.1f}s)   ",
                        end="\r", flush=True,
                    )
                    _last = now
            elapsed_cand = time.perf_counter() - t0
            i_arr, j_arr = np.where(candidate_matrix)
            total_isl = len(i_arr)
            del candidate_matrix
            print(
                f"    Phase 1.5/3 spatial index: done  "
                f"{total_isl:,} / {n_pairs_max:,} pairs in {elapsed_cand:.1f}s "
                f"({100.0 * total_isl / max(1, n_pairs_max):.2f}% of O(n²))"
            )
            if ckpt_p15:
                np.savez_compressed(str(ckpt_p15), i=i_arr, j=j_arr)
                print(f"  [ckpt] Phase 1.5 saved → {ckpt_p15.name}  ({total_isl:,} pairs)")

        # ── Phase 2: Parallel ISL pair processing ─────────────────────────────
        if ckpt_p2 and ckpt_p2.exists():
            print(f"  [ckpt] Phase 2: loading {ckpt_p2.name} ...", flush=True)
            raw_contacts: List[dict] = _read_contacts_csv(ckpt_p2)
            print(f"  [ckpt] Phase 2: loaded  {len(raw_contacts):,} ISL contacts")
        else:
            raw_contacts = []
            if total_isl > 0:
                print(f"    Phase 2/3 ISL pairs:   {total_isl:,} pairs, {workers} workers",
                      flush=True)

                def _isl_args_gen():
                    for k in range(total_isl):
                        i, j = int(i_arr[k]), int(j_arr[k])
                        yield (i, j,
                               all_sats[i].sat_id, all_sats[j].sat_id,
                               all_sats[i].operator_id, all_sats[j].operator_id,
                               self.isl_max_range_km)

                done = 0
                t0   = time.perf_counter()
                _last = t0 - 30
                chunksize = max(1, min(500, total_isl // (workers * 40)))
                with Pool(processes=min(workers, total_isl),
                          initializer=_init_isl_worker,
                          initargs=(pos_arr, ts_np)) as pool:
                    for pair_contacts in pool.imap_unordered(
                        _isl_pair_worker, _isl_args_gen(), chunksize=chunksize
                    ):
                        raw_contacts.extend(pair_contacts)
                        done += 1
                        now = time.perf_counter()
                        if done == total_isl or now - _last >= 30:
                            pct     = done * 100 // total_isl
                            elapsed = now - t0
                            suffix  = " done" if done == total_isl else ""
                            print(
                                f"    Phase 2/3 ISL pairs:   {pct:3d}%  "
                                f"({done:,}/{total_isl:,} pairs, "
                                f"{len(raw_contacts):,} contacts, "
                                f"{elapsed:.1f}s, {done/max(elapsed,1e-6):.0f} pairs/s){suffix}",
                                flush=True,
                            )
                            _last = now

            if ckpt_p2:
                print(f"  [ckpt] Phase 2: saving {ckpt_p2.name} "
                      f"({len(raw_contacts):,} contacts) ...", flush=True)
                _write_contacts_csv(ckpt_p2, raw_contacts)
                print(f"  [ckpt] Phase 2 saved → {ckpt_p2.name}")

        # ── Phase 3: Parallel GS-sat processing ───────────────────────────────
        total_gs = len(all_gs) * n_sats
        if ckpt_p3 and ckpt_p3.exists():
            print(f"  [ckpt] Phase 3: loading {ckpt_p3.name} ...", flush=True)
            raw_contacts = _read_contacts_csv(ckpt_p3)
            print(f"  [ckpt] Phase 3: loaded  {len(raw_contacts):,} total contacts")
        elif total_gs > 0:
            n_isl = len(raw_contacts)
            print(f"    Phase 3/3 GS-sat:      {total_gs:,} pairs, {workers} workers",
                  flush=True)

            def _gs_args_gen():
                for gs in all_gs:
                    for si, sat in enumerate(all_sats):
                        yield (gs.gs_id, gs.operator_id, gs.lat_deg, gs.lon_deg,
                               gs.alt_m, gs.min_elevation_deg, si,
                               epoch_jd, epoch_fr, sat.sat_id, sat.operator_id)

            done  = 0
            t0    = time.perf_counter()
            _last = t0 - 30
            chunksize_gs = max(1, min(1000, total_gs // (workers * 20)))
            with Pool(processes=min(workers, total_gs),
                      initializer=_init_isl_worker,
                      initargs=(pos_arr, ts_np)) as pool:
                for gs_contacts in pool.imap_unordered(
                    _gs_sat_worker, _gs_args_gen(), chunksize=chunksize_gs
                ):
                    raw_contacts.extend(gs_contacts)
                    done += 1
                    now = time.perf_counter()
                    if done == total_gs or now - _last >= 30:
                        pct     = done * 100 // total_gs
                        elapsed = now - t0
                        suffix  = " done" if done == total_gs else ""
                        print(
                            f"    Phase 3/3 GS-sat:      {pct:3d}%  "
                            f"({done:,}/{total_gs:,} pairs, "
                            f"{len(raw_contacts)-n_isl:,} GS contacts, "
                            f"{elapsed:.1f}s, {done/max(elapsed,1e-6):.0f} pairs/s){suffix}",
                            flush=True,
                        )
                        _last = now

            if ckpt_p3:
                print(f"  [ckpt] Phase 3: saving {ckpt_p3.name} "
                      f"({len(raw_contacts):,} contacts) ...", flush=True)
                _write_contacts_csv(ckpt_p3, raw_contacts)
                print(f"  [ckpt] Phase 3 saved → {ckpt_p3.name}")

        # Assign stable IDs and return
        raw_contacts.sort(key=lambda c: c["start_time_sec"])
        for idx, c_dict in enumerate(raw_contacts, 1):
            cp.contacts.append(Contact(contact_id=f"C{idx:06d}", **c_dict))

        if _logging:
            _write_propagation_log(
                self.propagation_log_path, cp, all_sats, all_gs, ts_arr, _geo
            )

        return cp

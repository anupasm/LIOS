"""Contact window calculator using SGP4 propagation.

Computes:
  - Sat-sat ISL windows (all pairs where separation < isl_max_range_km).
  - GS-sat windows (elevation >= gs.min_elevation_deg).

Produces a ContactPlan from the computed windows.
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

C_KM_S         = cfg.link.c_km_s          # speed of light (km/s) for propagation delay
C_MAX_ISL_KBPS = cfg.link.isl_max_kbps    # default ISL link capacity
C_MAX_GS_KBPS  = cfg.link.gs_max_kbps     # default GS link capacity


@dataclass
class Contact:
    contact_id: str
    from_node: str
    to_node: str
    start_time_sec: float    # seconds from simulation epoch
    end_time_sec: float
    capacity_kbps: float
    range_km: float          # mean range during contact
    node_type_from: str      # 'SAT' | 'GS'
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

    # ── query helpers ──────────────────────────────────────────────────────────

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
        """Alias for get_ground_nodes() — spec-compatible name."""
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
            reader = csv.DictReader(f)
            for row in reader:
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


# ── internal helpers ───────────────────────────────────────────────────────────

def _eci_position_km(sat: Satellite, jd: float, fr: float) -> Optional[np.ndarray]:
    """Propagate satellite to (jd, fr) Julian date; return ECI position in km."""
    e, r, _ = sat.satrec.sgp4(jd, fr)
    if e != 0:
        return None  # propagation error (decayed orbit, etc.)
    return np.array(r)


def _geodetic_to_eci(lat_deg: float, lon_deg: float, alt_m: float, gmst_rad: float) -> np.ndarray:
    """Convert geodetic coords to ECI Cartesian (km)."""
    lat = math.radians(lat_deg)
    lon = math.radians(lon_deg)
    alt_km = alt_m / 1000.0
    RE = 6378.137  # km, WGS84 equatorial radius
    e2 = 0.00669437999014  # WGS84 first eccentricity squared
    N = RE / math.sqrt(1 - e2 * math.sin(lat) ** 2)
    x_ecef = (N + alt_km) * math.cos(lat) * math.cos(lon)
    y_ecef = (N + alt_km) * math.cos(lat) * math.sin(lon)
    z_ecef = (N * (1 - e2) + alt_km) * math.sin(lat)
    # Rotate ECEF → ECI by GMST
    cos_g = math.cos(gmst_rad)
    sin_g = math.sin(gmst_rad)
    x_eci = cos_g * x_ecef - sin_g * y_ecef
    y_eci = sin_g * x_ecef + cos_g * y_ecef
    z_eci = z_ecef
    return np.array([x_eci, y_eci, z_eci])


def _gmst(jd: float, fr: float) -> float:
    """Approximate Greenwich Mean Sidereal Time in radians."""
    T = (jd + fr - 2451545.0) / 36525.0
    theta = 67310.54841 + (876600 * 3600 + 8640184.812866) * T + 0.093104 * T**2
    return math.radians(theta % 86400 * 360 / 86400)


def _eci_to_geodetic(eci_km: np.ndarray, gmst_rad: float) -> Tuple[float, float, float]:
    """Convert ECI position (km) to geodetic (lat_deg, lon_deg, alt_km).

    Uses Bowring's iterative method; accurate to < 1 mm for LEO altitudes.
    """
    x, y, z = float(eci_km[0]), float(eci_km[1]), float(eci_km[2])
    # ECI → ECEF: rotate by -GMST
    cg, sg = math.cos(gmst_rad), math.sin(gmst_rad)
    x_e =  cg * x + sg * y
    y_e = -sg * x + cg * y
    z_e = z
    # ECEF → geodetic
    RE = 6378.137          # WGS84 equatorial radius km
    e2 = 0.00669437999014  # first eccentricity squared
    p = math.sqrt(x_e ** 2 + y_e ** 2)
    lon_rad = math.atan2(y_e, x_e)
    lat_rad = math.atan2(z_e, p * (1.0 - e2))
    for _ in range(10):
        N = RE / math.sqrt(1.0 - e2 * math.sin(lat_rad) ** 2)
        lat_new = math.atan2(z_e + e2 * N * math.sin(lat_rad), p)
        if abs(lat_new - lat_rad) < 1e-12:
            break
        lat_rad = lat_new
    N = RE / math.sqrt(1.0 - e2 * math.sin(lat_rad) ** 2)
    alt_km = (
        p / math.cos(lat_rad) - N
        if abs(math.cos(lat_rad)) > 1e-6
        else abs(z_e) / abs(math.sin(lat_rad)) - N * (1.0 - e2)
    )
    return math.degrees(lat_rad), math.degrees(lon_rad), alt_km


def _write_propagation_log(
    path: Path,
    cp: "ContactPlan",
    all_sats: list,
    all_gs: list,
    ts_arr: List[float],
    geo: List[List[Optional[Tuple[float, float, float]]]],
) -> None:
    """Write satellite mobility and contact log JSON for visualization."""
    satellites_out = []
    for si, sat in enumerate(all_sats):
        track = []
        for step_idx, t in enumerate(ts_arr):
            g = geo[si][step_idx]
            if g is not None:
                # [t_sec, lat_deg, lon_deg, alt_km] — compact array format
                track.append([round(t, 1), round(g[0], 5), round(g[1], 5), round(g[2], 3)])
        satellites_out.append({
            "sat_id": sat.sat_id,
            "operator_id": sat.operator_id,
            "track": track,
        })

    ground_stations_out = [
        {
            "gs_id": gs.gs_id,
            "operator_id": gs.operator_id,
            "lat": gs.lat_deg,
            "lon": gs.lon_deg,
            "alt_m": gs.alt_m,
        }
        for gs in all_gs
    ]

    contacts_out = [
        {
            "contact_id": c.contact_id,
            "from_node": c.from_node,
            "to_node": c.to_node,
            "start_time_sec": round(c.start_time_sec, 1),
            "end_time_sec": round(c.end_time_sec, 1),
            "node_type_from": c.node_type_from,
            "node_type_to": c.node_type_to,
            "operator_from": c.operator_from,
            "operator_to": c.operator_to,
            "range_km": round(c.range_km, 2),
            "capacity_kbps": round(c.capacity_kbps, 2),
        }
        for c in cp.contacts
    ]

    t_step = ts_arr[1] - ts_arr[0] if len(ts_arr) > 1 else 0
    log = {
        "epoch": cp.epoch.isoformat(),
        "duration_sec": ts_arr[-1] if ts_arr else 0,
        "time_step_sec": t_step,
        "ground_stations": ground_stations_out,
        "satellites": satellites_out,
        "contacts": contacts_out,
    }

    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as f:
        json.dump(log, f, separators=(",", ":"))
    size_kb = path.stat().st_size // 1024
    n_sats = len(satellites_out)
    n_track_pts = sum(len(s["track"]) for s in satellites_out)
    print(f"  Propagation log → {path}  ({size_kb} KB, {n_sats} sats, {n_track_pts} track points, {len(contacts_out)} contacts)")


def _elevation_deg(gs_eci: np.ndarray, sat_eci: np.ndarray, gs_lat_rad: float, gs_lon_rad: float, gmst_rad: float) -> float:
    """Compute elevation angle (degrees) of satellite above ground station horizon."""
    rng = sat_eci - gs_eci
    # Unit vector from GS to satellite
    rng_norm = rng / np.linalg.norm(rng)
    # Local zenith vector at GS in ECI (rotate ECEF zenith)
    cos_lat = math.cos(gs_lat_rad)
    sin_lat = math.sin(gs_lat_rad)
    cos_lon_eci = math.cos(gs_lon_rad + gmst_rad)
    sin_lon_eci = math.sin(gs_lon_rad + gmst_rad)
    zenith = np.array([cos_lat * cos_lon_eci, cos_lat * sin_lon_eci, sin_lat])
    sin_elev = float(np.dot(rng_norm, zenith))
    return math.degrees(math.asin(max(-1.0, min(1.0, sin_elev))))


# ── multiprocessing worker functions (module-level required for pickling) ──────

_WORKER_POS_ARR: Optional[np.ndarray] = None   # shape (n_sats, steps, 3)
_WORKER_TS_ARR:  Optional[np.ndarray] = None   # shape (steps,)


def _init_isl_worker(pos_arr: np.ndarray, ts_arr: np.ndarray) -> None:
    global _WORKER_POS_ARR, _WORKER_TS_ARR
    _WORKER_POS_ARR = pos_arr
    _WORKER_TS_ARR  = ts_arr


def _isl_pair_worker(args: tuple) -> List[dict]:
    """Vectorised ISL-pair contact detector.

    Reads positions from the process-global _WORKER_POS_ARR so no position
    data is pickled per task — only 2 indices + a handful of scalars.
    """
    i, j, from_id, to_id, from_op, to_op, isl_max_range_km = args
    pos_i = _WORKER_POS_ARR[i]   # (steps, 3) — NaN where propagation failed
    pos_j = _WORKER_POS_ARR[j]
    ts    = _WORKER_TS_ARR        # (steps,)

    # Valid steps: both satellites propagated successfully
    valid = ~(np.isnan(pos_i[:, 0]) | np.isnan(pos_j[:, 0]))
    # Vectorised distance — np.inf for invalid steps so they never trigger
    diff_xyz = pos_i - pos_j
    dists = np.where(valid, np.linalg.norm(diff_xyz, axis=1), np.inf)
    in_range = valid & (dists < isl_max_range_km)

    # Edge-detect contact boundaries
    bordered = np.concatenate(([0], in_range.astype(np.int8), [0]))
    delta = np.diff(bordered)
    starts_idx = np.where(delta == 1)[0]   # first in-range step
    ends_idx   = np.where(delta == -1)[0]  # first out-of-range step

    contacts: List[dict] = []
    for s, e in zip(starts_idx, ends_idx):
        mean_range = float(np.mean(dists[s:e]))
        frac = max(0.0, 1.0 - mean_range / isl_max_range_km)
        contacts.append(dict(
            from_node=from_id, to_node=to_id,
            start_time_sec=float(ts[s]),
            end_time_sec=float(ts[e - 1]),
            capacity_kbps=C_MAX_ISL_KBPS * frac,
            range_km=mean_range,
            node_type_from="SAT", node_type_to="SAT",
            operator_from=from_op, operator_to=to_op,
        ))
    return contacts


def _gs_sat_worker(args: tuple) -> List[dict]:
    """GS-sat contact detector.

    Uses process-global _WORKER_POS_ARR[si] and _WORKER_TS_ARR set by
    _init_isl_worker — no per-task position data in the pickle.
    """
    (gs_id, gs_op, gs_lat_deg, gs_lon_deg, gs_alt_m, gs_min_elev,
     si, epoch_jd, epoch_fr, sat_id, sat_op) = args
    gs_lat_rad = math.radians(gs_lat_deg)
    gs_lon_rad = math.radians(gs_lon_deg)
    sat_pos = _WORKER_POS_ARR[si]   # (steps, 3); NaN rows = propagation failed
    ts      = _WORKER_TS_ARR        # (steps,)
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
        if np.isnan(p_sat[0]):          # propagation failed at this step
            if in_contact:
                _flush(float(ts[step_idx - 1]) if step_idx > 0 else contact_start)
            continue
        t_sec = float(ts[step_idx])
        delta_days = t_sec / 86400.0
        fr_step = epoch_fr + delta_days
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


class WindowCalculator:
    """Propagates all satellites and computes contact windows."""

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
        """Return a checkpoint path, or None if checkpointing is disabled."""
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
        cp = ContactPlan(epoch=self.t_start)
        all_sats: List[Satellite] = [s for sats in operators.values() for s in sats]
        all_gs: List[GroundStation] = [gs for gss in ground_stations.values() for gs in gss]

        duration_sec = (self.t_end - self.t_start).total_seconds()
        steps = int(duration_sec / self.time_step_sec) + 1
        ts_arr = [i * self.time_step_sec for i in range(steps)]

        epoch_jd, epoch_fr = jday(
            self.t_start.year, self.t_start.month, self.t_start.day,
            self.t_start.hour, self.t_start.minute,
            self.t_start.second + self.t_start.microsecond / 1e6,
        )

        _logging = self.propagation_log_path is not None
        n_sats = len(all_sats)
        workers = n_workers or os.cpu_count() or 1

        # ── checkpoint paths ──────────────────────────────────────────────────
        ckpt_p1  = self._ckpt("pos.npy")
        ckpt_p15 = self._ckpt("candidates.npz")
        ckpt_p2  = self._ckpt("isl_contacts.csv")
        ckpt_p3  = self._ckpt("all_contacts.csv")

        _geo: List[List[Optional[Tuple[float, float, float]]]] = (
            [[None] * steps for _ in range(n_sats)] if _logging else []
        )

        # Pre-compute JD/FR arrays once for all timesteps
        _deltas = np.array(ts_arr, dtype=np.float64) / 86400.0
        _fr_combined = epoch_fr + _deltas
        _jd_int = np.floor(_fr_combined)
        jd_all = (epoch_jd + _jd_int).astype(np.float64)
        fr_all = (_fr_combined - _jd_int).astype(np.float64)

        # ── Phase 1: SGP4 propagation → pos_arr ──────────────────────────────
        if ckpt_p1 and ckpt_p1.exists():
            print(f"  [ckpt] Phase 1: loading {ckpt_p1.name} ...", flush=True)
            pos_arr = np.load(str(ckpt_p1))
            print(f"  [ckpt] Phase 1: loaded  pos_arr {pos_arr.shape}  "
                  f"({pos_arr.nbytes // 1024 // 1024} MB)")
        else:
            pos_arr = np.full((n_sats, steps, 3), np.nan, dtype=np.float64)
            print(f"    Phase 1/3 propagation: {n_sats} sats × {steps} steps", flush=True)
            t0_p1 = time.perf_counter()
            _last_print_p1 = t0_p1 - 30
            for step_idx in range(steps):
                jd_step = float(jd_all[step_idx])
                fr_step = float(fr_all[step_idx])
                gmst = _gmst(jd_step, fr_step) if _logging else 0.0
                for si, sat in enumerate(all_sats):
                    pos = _eci_position_km(sat, jd_step, fr_step)
                    if pos is not None:
                        pos_arr[si, step_idx] = pos
                    if _logging and pos is not None:
                        _geo[si][step_idx] = _eci_to_geodetic(pos, gmst)
                now = time.perf_counter()
                if step_idx == steps - 1 or now - _last_print_p1 >= 30:
                    pct = (step_idx + 1) * 100 // steps
                    elapsed = now - t0_p1
                    print(f"    Phase 1/3 propagation: {pct:3d}%  "
                          f"({step_idx + 1}/{steps} steps, {elapsed:.1f}s)   ",
                          end="\r", flush=True)
                    _last_print_p1 = now
            print(f"    Phase 1/3 propagation: 100%  ({steps}/{steps} steps) done")
            if ckpt_p1:
                np.save(str(ckpt_p1), pos_arr)
                print(f"  [ckpt] Phase 1 saved → {ckpt_p1.name}  "
                      f"({pos_arr.nbytes // 1024 // 1024} MB)")

        ts_np = np.array(ts_arr, dtype=np.float64)
        n_pairs_max = n_sats * (n_sats - 1) // 2

        # ── Phase 1.5: Spatial candidate-pair pruning via cKDTree ────────────
        if ckpt_p15 and ckpt_p15.exists():
            print(f"  [ckpt] Phase 1.5: loading {ckpt_p15.name} ...", flush=True)
            _d = np.load(str(ckpt_p15))
            i_arr, j_arr = _d["i"], _d["j"]
            total_isl = len(i_arr)
            print(f"  [ckpt] Phase 1.5: loaded  {total_isl:,} candidate pairs")
        else:
            candidate_matrix = np.zeros((n_sats, n_sats), dtype=bool)
            print(f"    Phase 1.5/3 spatial index: {steps} steps, "
                  f"{n_pairs_max:,} O(n²) pairs ...", flush=True)
            t0_cand = time.perf_counter()
            _last_print_cand = t0_cand - 30
            for step_idx in range(steps):
                valid_mask = ~np.isnan(pos_arr[:, step_idx, 0])
                valid_idx = np.where(valid_mask)[0]
                if len(valid_idx) < 2:
                    continue
                pts = pos_arr[valid_idx, step_idx, :]
                tree = cKDTree(pts)
                step_pairs = tree.query_pairs(self.isl_max_range_km, output_type="ndarray")
                if len(step_pairs):
                    gi = valid_idx[step_pairs[:, 0]]
                    gj = valid_idx[step_pairs[:, 1]]
                    candidate_matrix[np.minimum(gi, gj), np.maximum(gi, gj)] = True
                now = time.perf_counter()
                if step_idx == steps - 1 or now - _last_print_cand >= 30:
                    pct = (step_idx + 1) * 100 // steps
                    elapsed = now - t0_cand
                    print(
                        f"    Phase 1.5/3 spatial index: {pct:3d}%  "
                        f"({step_idx + 1}/{steps} steps, "
                        f"{int(candidate_matrix.sum()):,} candidate pairs, {elapsed:.1f}s)   ",
                        end="\r", flush=True,
                    )
                    _last_print_cand = now
            elapsed_cand = time.perf_counter() - t0_cand
            i_arr, j_arr = np.where(candidate_matrix)
            total_isl = len(i_arr)
            del candidate_matrix
            print(
                f"    Phase 1.5/3 spatial index: done  "
                f"{total_isl:,} / {n_pairs_max:,} candidate pairs in {elapsed_cand:.1f}s "
                f"({100.0 * total_isl / max(1, n_pairs_max):.2f}% of O(n²))"
            )
            if ckpt_p15:
                np.savez_compressed(str(ckpt_p15), i=i_arr, j=j_arr)
                print(f"  [ckpt] Phase 1.5 saved → {ckpt_p15.name}  ({total_isl:,} pairs)")

        # ── Phase 2: Parallel ISL pair processing ──────────────────────────────
        if ckpt_p2 and ckpt_p2.exists():
            print(f"  [ckpt] Phase 2: loading {ckpt_p2.name} ...", flush=True)
            _isl_cp = ContactPlan.from_csv(ckpt_p2, self.t_start)
            raw_contacts: List[dict] = [
                dict(
                    from_node=c.from_node, to_node=c.to_node,
                    start_time_sec=c.start_time_sec, end_time_sec=c.end_time_sec,
                    capacity_kbps=c.capacity_kbps, range_km=c.range_km,
                    node_type_from=c.node_type_from, node_type_to=c.node_type_to,
                    operator_from=c.operator_from, operator_to=c.operator_to,
                )
                for c in _isl_cp.contacts
            ]
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

                done_isl = 0
                t0_isl = time.perf_counter()
                _last_print = t0_isl - 30
                chunksize = max(1, min(500, total_isl // (workers * 40)))
                with Pool(
                    processes=min(workers, total_isl),
                    initializer=_init_isl_worker,
                    initargs=(pos_arr, ts_np),
                ) as pool:
                    for pair_contacts in pool.imap_unordered(
                        _isl_pair_worker, _isl_args_gen(), chunksize=chunksize
                    ):
                        raw_contacts.extend(pair_contacts)
                        done_isl += 1
                        now = time.perf_counter()
                        if done_isl == total_isl or now - _last_print >= 30:
                            pct = done_isl * 100 // total_isl
                            elapsed = now - t0_isl
                            rate = done_isl / max(elapsed, 1e-6)
                            suffix = " done" if done_isl == total_isl else ""
                            print(
                                f"    Phase 2/3 ISL pairs:   {pct:3d}%  "
                                f"({done_isl:,}/{total_isl:,} pairs, "
                                f"{len(raw_contacts):,} contacts, "
                                f"{elapsed:.1f}s, {rate:.0f} pairs/s){suffix}",
                                flush=True,
                            )
                            _last_print = now

            if ckpt_p2:
                _isl_cp = ContactPlan(epoch=self.t_start)
                for idx, c_dict in enumerate(raw_contacts, 1):
                    _isl_cp.contacts.append(Contact(contact_id=f"C{idx:06d}", **c_dict))
                _isl_cp.to_csv(ckpt_p2)
                print(f"  [ckpt] Phase 2 saved → {ckpt_p2.name}  "
                      f"({len(raw_contacts):,} ISL contacts)")

        # ── Phase 3: Parallel GS-sat processing ──────────────────────────────
        total_gs = len(all_gs) * n_sats
        if ckpt_p3 and ckpt_p3.exists():
            print(f"  [ckpt] Phase 3: loading {ckpt_p3.name} ...", flush=True)
            _all_cp = ContactPlan.from_csv(ckpt_p3, self.t_start)
            raw_contacts = [
                dict(
                    from_node=c.from_node, to_node=c.to_node,
                    start_time_sec=c.start_time_sec, end_time_sec=c.end_time_sec,
                    capacity_kbps=c.capacity_kbps, range_km=c.range_km,
                    node_type_from=c.node_type_from, node_type_to=c.node_type_to,
                    operator_from=c.operator_from, operator_to=c.operator_to,
                )
                for c in _all_cp.contacts
            ]
            print(f"  [ckpt] Phase 3: loaded  {len(raw_contacts):,} total contacts "
                  f"(ISL + GS)")
        elif total_gs > 0:
            n_isl = len(raw_contacts)
            print(f"    Phase 3/3 GS-sat:      {total_gs:,} pairs, {workers} workers",
                  flush=True)
            t0_gs = time.perf_counter()
            _last_print_gs = t0_gs - 30

            def _gs_args_gen():
                for gs in all_gs:
                    for si, sat in enumerate(all_sats):
                        yield (gs.gs_id, gs.operator_id, gs.lat_deg, gs.lon_deg,
                               gs.alt_m, gs.min_elevation_deg, si,
                               epoch_jd, epoch_fr, sat.sat_id, sat.operator_id)

            done_gs = 0
            chunksize_gs = max(1, min(1000, total_gs // (workers * 20)))
            with Pool(
                processes=min(workers, total_gs),
                initializer=_init_isl_worker,
                initargs=(pos_arr, ts_np),
            ) as pool:
                for gs_contacts in pool.imap_unordered(
                    _gs_sat_worker, _gs_args_gen(), chunksize=chunksize_gs
                ):
                    raw_contacts.extend(gs_contacts)
                    done_gs += 1
                    now = time.perf_counter()
                    if done_gs == total_gs or now - _last_print_gs >= 30:
                        pct = done_gs * 100 // total_gs
                        elapsed = now - t0_gs
                        rate = done_gs / max(elapsed, 1e-6)
                        suffix = " done" if done_gs == total_gs else ""
                        print(
                            f"    Phase 3/3 GS-sat:      {pct:3d}%  "
                            f"({done_gs:,}/{total_gs:,} pairs, "
                            f"{len(raw_contacts) - n_isl:,} GS contacts, "
                            f"{elapsed:.1f}s, {rate:.0f} pairs/s){suffix}",
                            flush=True,
                        )
                        _last_print_gs = now

            if ckpt_p3:
                _all_cp = ContactPlan(epoch=self.t_start)
                for idx, c_dict in enumerate(raw_contacts, 1):
                    _all_cp.contacts.append(Contact(contact_id=f"C{idx:06d}", **c_dict))
                _all_cp.to_csv(ckpt_p3)
                print(f"  [ckpt] Phase 3 saved → {ckpt_p3.name}  "
                      f"({len(raw_contacts):,} total contacts)")

        # Assign stable contact IDs after sorting
        raw_contacts.sort(key=lambda c: c["start_time_sec"])
        for idx, c_dict in enumerate(raw_contacts, 1):
            cp.contacts.append(Contact(contact_id=f"C{idx:06d}", **c_dict))

        if _logging:
            _write_propagation_log(
                self.propagation_log_path, cp, all_sats, all_gs, ts_arr, _geo
            )

        return cp
"""Contact window calculator using SGP4 propagation.

Computes:
  - Sat-sat ISL windows (all pairs where separation < isl_max_range_km).
  - GS-sat windows (elevation >= gs.min_elevation_deg).

Produces a ContactPlan from the computed windows.
"""
from __future__ import annotations

import csv
import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np
from sgp4.api import jday

from contact_plan.gs_loader import GroundStation
from contact_plan.tle_loader import Satellite

# Speed of light (km/s) for propagation delay
C_KM_S = 299_792.458

# Default link capacities
C_MAX_ISL_KBPS = 10_000.0
C_MAX_GS_KBPS = 50_000.0


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


class WindowCalculator:
    """Propagates all satellites and computes contact windows."""

    def __init__(
        self,
        t_start: datetime,
        t_end: datetime,
        time_step_sec: int = 10,
        isl_max_range_km: float = 2500.0,
    ):
        self.t_start = t_start
        self.t_end = t_end
        self.time_step_sec = time_step_sec
        self.isl_max_range_km = isl_max_range_km

    def compute(
        self,
        operators: Dict[str, List[Satellite]],
        ground_stations: Dict[str, List[GroundStation]],
    ) -> ContactPlan:
        cp = ContactPlan(epoch=self.t_start)
        all_sats: List[Satellite] = [s for sats in operators.values() for s in sats]
        all_gs: List[GroundStation] = [gs for gss in ground_stations.values() for gs in gss]

        duration_sec = (self.t_end - self.t_start).total_seconds()
        steps = int(duration_sec / self.time_step_sec) + 1
        ts_arr = [i * self.time_step_sec for i in range(steps)]

        # Pre-compute epoch in Julian date
        epoch_jd, epoch_fr = jday(
            self.t_start.year, self.t_start.month, self.t_start.day,
            self.t_start.hour, self.t_start.minute,
            self.t_start.second + self.t_start.microsecond / 1e6,
        )

        # Propagate all satellites at every time step
        n_sats = len(all_sats)
        positions: List[List[Optional[np.ndarray]]] = [[None] * steps for _ in range(n_sats)]

        for step_idx, t_sec in enumerate(ts_arr):
            delta_days = t_sec / 86400.0
            fr_step = epoch_fr + delta_days
            jd_step = epoch_jd + math.floor(fr_step)
            fr_step = fr_step - math.floor(fr_step)
            for si, sat in enumerate(all_sats):
                positions[si][step_idx] = _eci_position_km(sat, jd_step, fr_step)

        contact_counter = 0

        # ── Sat-sat contacts ───────────────────────────────────────────────────
        for i in range(n_sats):
            for j in range(i + 1, n_sats):
                sat_i = all_sats[i]
                sat_j = all_sats[j]
                in_contact = False
                contact_start = 0.0
                ranges_in_contact: List[float] = []

                for step_idx, t_sec in enumerate(ts_arr):
                    pi = positions[i][step_idx]
                    pj = positions[j][step_idx]
                    if pi is None or pj is None:
                        in_contact = False
                        continue
                    dist = float(np.linalg.norm(pi - pj))
                    if dist < self.isl_max_range_km:
                        if not in_contact:
                            in_contact = True
                            contact_start = t_sec
                            ranges_in_contact = [dist]
                        else:
                            ranges_in_contact.append(dist)
                    else:
                        if in_contact:
                            contact_end = ts_arr[step_idx - 1] if step_idx > 0 else t_sec
                            mean_range = float(np.mean(ranges_in_contact))
                            frac = 1.0 - mean_range / self.isl_max_range_km
                            cap = C_MAX_ISL_KBPS * max(0.0, frac)
                            contact_counter += 1
                            cp.contacts.append(Contact(
                                contact_id=f"C{contact_counter:06d}",
                                from_node=sat_i.sat_id,
                                to_node=sat_j.sat_id,
                                start_time_sec=contact_start,
                                end_time_sec=contact_end,
                                capacity_kbps=cap,
                                range_km=mean_range,
                                node_type_from="SAT",
                                node_type_to="SAT",
                                operator_from=sat_i.operator_id,
                                operator_to=sat_j.operator_id,
                            ))
                            in_contact = False

                if in_contact:
                    mean_range = float(np.mean(ranges_in_contact))
                    frac = 1.0 - mean_range / self.isl_max_range_km
                    cap = C_MAX_ISL_KBPS * max(0.0, frac)
                    contact_counter += 1
                    cp.contacts.append(Contact(
                        contact_id=f"C{contact_counter:06d}",
                        from_node=sat_i.sat_id,
                        to_node=sat_j.sat_id,
                        start_time_sec=contact_start,
                        end_time_sec=ts_arr[-1],
                        capacity_kbps=cap,
                        range_km=mean_range,
                        node_type_from="SAT",
                        node_type_to="SAT",
                        operator_from=sat_i.operator_id,
                        operator_to=sat_j.operator_id,
                    ))

        # ── GS-sat contacts ────────────────────────────────────────────────────
        for gs in all_gs:
            gs_lat_rad = math.radians(gs.lat_deg)
            gs_lon_rad = math.radians(gs.lon_deg)

            for si, sat in enumerate(all_sats):
                in_contact = False
                contact_start = 0.0
                ranges_in_contact = []

                for step_idx, t_sec in enumerate(ts_arr):
                    p_sat = positions[si][step_idx]
                    if p_sat is None:
                        in_contact = False
                        continue
                    delta_days = t_sec / 86400.0
                    fr_step = epoch_fr + delta_days
                    jd_step = epoch_jd + math.floor(fr_step)
                    fr_step = fr_step - math.floor(fr_step)
                    gmst = _gmst(jd_step, fr_step)
                    gs_eci = _geodetic_to_eci(gs.lat_deg, gs.lon_deg, gs.alt_m, gmst)
                    el = _elevation_deg(gs_eci, p_sat, gs_lat_rad, gs_lon_rad, gmst)
                    dist = float(np.linalg.norm(p_sat - gs_eci))

                    if el >= gs.min_elevation_deg:
                        if not in_contact:
                            in_contact = True
                            contact_start = t_sec
                            ranges_in_contact = [dist]
                        else:
                            ranges_in_contact.append(dist)
                    else:
                        if in_contact:
                            contact_end = ts_arr[step_idx - 1] if step_idx > 0 else t_sec
                            mean_range = float(np.mean(ranges_in_contact))
                            frac = 1.0 - mean_range / 3000.0
                            cap = C_MAX_GS_KBPS * max(0.0, frac)
                            contact_counter += 1
                            cp.contacts.append(Contact(
                                contact_id=f"C{contact_counter:06d}",
                                from_node=gs.gs_id,
                                to_node=sat.sat_id,
                                start_time_sec=contact_start,
                                end_time_sec=contact_end,
                                capacity_kbps=cap,
                                range_km=mean_range,
                                node_type_from="GS",
                                node_type_to="SAT",
                                operator_from=gs.operator_id,
                                operator_to=sat.operator_id,
                            ))
                            in_contact = False

                if in_contact:
                    mean_range = float(np.mean(ranges_in_contact))
                    frac = 1.0 - mean_range / 3000.0
                    cap = C_MAX_GS_KBPS * max(0.0, frac)
                    contact_counter += 1
                    cp.contacts.append(Contact(
                        contact_id=f"C{contact_counter:06d}",
                        from_node=gs.gs_id,
                        to_node=sat.sat_id,
                        start_time_sec=contact_start,
                        end_time_sec=ts_arr[-1],
                        capacity_kbps=cap,
                        range_km=mean_range,
                        node_type_from="GS",
                        node_type_to="SAT",
                        operator_from=gs.operator_id,
                        operator_to=sat.operator_id,
                    ))

        cp.contacts.sort(key=lambda c: c.start_time_sec)
        return cp

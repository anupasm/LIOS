"""Ground station loader — reads *.txt files from lios/data/gss/.

Each file is named <operator>.txt and contains one record per line:
  <operator>-<name>,<lat_deg>,<lon_deg>

Altitude defaults to 0 m; minimum elevation defaults to 5 degrees.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List

from config import cfg


@dataclass
class GroundStation:
    gs_id: str           # "<operator>-<normalised_name>"
    operator_id: str
    lat_deg: float
    lon_deg: float
    alt_m: float = 0.0
    min_elevation_deg: float = cfg.link.gs_min_elevation_deg


class GSLoader:
    """Loads ground station config files and returns per-operator GS lists."""

    GS_DIR_NAME = "gss"

    @classmethod
    def load_all(cls, data_dir: Path) -> Dict[str, List[GroundStation]]:
        """Scan data_dir/gss/ for *.txt files; parse each into GroundStation objects."""
        gs_dir = data_dir / cls.GS_DIR_NAME
        if not gs_dir.exists():
            raise FileNotFoundError(f"GS directory not found: {gs_dir}")

        result: Dict[str, List[GroundStation]] = {}
        for path in sorted(gs_dir.glob("*.txt")):
            operator = path.stem.lower()
            stations = cls._parse_file(path, operator)
            result[operator] = stations
        return result

    @classmethod
    def _parse_file(cls, path: Path, operator: str) -> List[GroundStation]:
        stations: List[GroundStation] = []
        for raw_line in path.read_text().splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split(",")
            if len(parts) < 3:
                raise ValueError(f"Malformed GS line in {path}: {raw_line!r}")
            gs_id = parts[0].strip().lower().replace(" ", "_")
            lat = float(parts[1])
            lon = float(parts[2])
            alt = float(parts[3]) if len(parts) > 3 else 0.0
            min_el = float(parts[4]) if len(parts) > 4 else 5.0
            stations.append(
                GroundStation(
                    gs_id=gs_id,
                    operator_id=operator,
                    lat_deg=lat,
                    lon_deg=lon,
                    alt_m=alt,
                    min_elevation_deg=min_el,
                )
            )
        return stations

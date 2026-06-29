"""TLE loader — reads *.txt and *.tle files from lios/data/tles/.

Each file is named <operator>.txt or <operator>.tle and contains one or more
3-line TLE entries:
  NAME LINE
  1 ...
  2 ...
Operator name is inferred from the filename stem.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List

from sgp4.api import Satrec, WGS84


@dataclass
class Satellite:
    sat_id: str          # "<operator>-<normalised_name>"
    operator_id: str
    name: str            # raw TLE name line
    satrec: Satrec       # sgp4 satellite record


class TLELoader:
    """Loads TLE files and returns per-operator satellite lists."""

    TLE_DIR_NAME = "tles"

    @staticmethod
    def _normalise(name: str) -> str:
        return name.strip().replace(" ", "_").lower()

    @classmethod
    def satellite_id(cls, operator: str, raw_name: str) -> str:
        return f"{operator}-{cls._normalise(raw_name)}"

    @classmethod
    def load_all(cls, data_dir: Path) -> Dict[str, List[Satellite]]:
        """Scan data_dir/tles/ for TLE files; parse each into Satellite objects."""
        tle_dir = data_dir / cls.TLE_DIR_NAME
        if not tle_dir.exists():
            raise FileNotFoundError(f"TLE directory not found: {tle_dir}")

        result: Dict[str, List[Satellite]] = {}
        paths = sorted([*tle_dir.glob("*.txt"), *tle_dir.glob("*.tle")])
        seen_operators: set[str] = set()
        for path in paths:
            operator = path.stem.lower()
            if operator in seen_operators:
                raise ValueError(
                    f"Duplicate TLE operator file stem {operator!r} in {tle_dir}; "
                    "keep only one of .txt or .tle for each operator"
                )
            seen_operators.add(operator)
            sats = cls._parse_file(path, operator)
            result[operator] = sats
        return result

    @classmethod
    def _parse_file(cls, path: Path, operator: str) -> List[Satellite]:
        lines = [l.rstrip("\n") for l in path.read_text().splitlines() if l.strip()]
        if len(lines) % 3 != 0:
            raise ValueError(
                f"TLE file {path} has {len(lines)} non-empty lines; expected multiple of 3"
            )
        sats: List[Satellite] = []
        for i in range(0, len(lines), 3):
            name_line = lines[i]
            line1 = lines[i + 1]
            line2 = lines[i + 2]
            satrec = Satrec.twoline2rv(line1, line2, WGS84)
            sat_id = cls.satellite_id(operator, name_line)
            sats.append(Satellite(sat_id=sat_id, operator_id=operator, name=name_line, satrec=satrec))
        return sats

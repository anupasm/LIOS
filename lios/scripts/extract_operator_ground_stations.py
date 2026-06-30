#!/usr/bin/env python3
"""Extract ground-station catalog rows into per-operator GS files.

The source ``ground_station.csv`` has station names, organizations, locations,
and coordinates.  This script uses conservative organization/name patterns to
write ``gss/<operator>.txt`` files compatible with ``contact_plan.GSLoader``:

    <gs_id>,<lat_deg>,<lon_deg>,<alt_m>,<min_elevation_deg>

Usage:
  python3 lios/scripts/extract_operator_ground_stations.py
  python3 lios/scripts/extract_operator_ground_stations.py \
      --ground-station-csv lios/data/ground_station.csv \
      --out-dir lios/data/gss
"""
from __future__ import annotations

import argparse
import csv
import re
from dataclasses import dataclass
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_GS_CSV = REPO_ROOT / "lios/data/ground_station.csv"
DEFAULT_OUT_DIR = REPO_ROOT / "lios/data/gss"
DEFAULT_ALT_M = 0.0
DEFAULT_MIN_ELEVATION_DEG = 5.0


@dataclass(frozen=True)
class OperatorPattern:
    display_name: str
    filename: str
    patterns: tuple[str, ...]
    notes: str = ""


OPERATORS: tuple[OperatorPattern, ...] = (
    OperatorPattern("SpaceX (Starlink)", "spacex_starlink.txt", (r"\bSPACEX\b", r"\bSTARLINK\b")),
    OperatorPattern("Eutelsat Group (OneWeb)", "eutelsat_oneweb.txt", (r"\bONEWEB\b", r"\bEUTELSAT\b")),
    OperatorPattern("Amazon / AWS", "amazon_project_kuiper.txt", (r"\bAMAZON\b", r"\bAWS\b", r"\bKUIPER\b")),
    OperatorPattern("Telesat", "telesat_lightspeed.txt", (r"\bTELESAT\b", r"\bLIGHTSPEED\b")),
    OperatorPattern("SES S.A.", "ses.txt", (r"\bSES\b", r"\bO3B\b", r"\bASTRA\b")),
    OperatorPattern("Viasat", "viasat.txt", (r"\bVIASAT\b",)),
    OperatorPattern("Inmarsat", "inmarsat.txt", (r"\bINMARSAT\b",)),
    OperatorPattern("Intelsat", "intelsat.txt", (r"\bINTELSAT\b",)),
    OperatorPattern("Iridium Communications", "iridium_communications.txt", (r"\bIRIDIUM\b",)),
    OperatorPattern("Globalstar", "globalstar.txt", (r"\bGLOBALSTAR\b",)),
    OperatorPattern("Planet Labs", "planet_labs.txt", (r"\bPLANET LABS\b", r"\bPLANET\b")),
    OperatorPattern("Spire Global", "spire_global.txt", (r"\bSPIRE\b",)),
    OperatorPattern("Kepler Communications", "kepler_communications.txt", (r"\bKEPLER\b",)),
    OperatorPattern("Lynk Global", "lynk_global.txt", (r"\bLYNK\b",)),
    OperatorPattern("Orbcomm", "orbcomm.txt", (r"\bORBCOMM\b",)),
    OperatorPattern("KSAT", "ksat.txt", (r"\bKSAT\b", r"\bKONGSBERG SATELLITE\b")),
    OperatorPattern("SSC", "ssc.txt", (r"\bSSC\b", r"\bSWEDISH SPACE CORPORATION\b")),
    OperatorPattern("ATLAS Space Operations", "atlas.txt", (r"\bATLAS\b",)),
    OperatorPattern("Leaf Space", "leafspace.txt", (r"\bLEAFSPACE\b", r"\bLEAF SPACE\b")),
    OperatorPattern("ESA", "esa.txt", (r"\bESA\b", r"\bEUROPEAN SPACE AGENCY\b")),
    OperatorPattern("NASA", "nasa.txt", (r"\bNASA\b",)),
    OperatorPattern("ISRO", "isro.txt", (r"\bISRO\b",)),
    OperatorPattern("JAXA", "jaxa.txt", (r"\bJAXA\b",)),
    OperatorPattern("CNSA / China", "cnsa_china.txt", (r"\bCNSA\b", r"\bCHINA SATCOM\b")),
)


def normalise_token(value: str) -> str:
    token = value.strip().lower()
    token = re.sub(r"[^a-z0-9]+", "_", token)
    token = re.sub(r"_+", "_", token).strip("_")
    return token or "ground_station"


def station_id(row: dict[str, str]) -> str:
    name = row.get("Name", "").strip()
    location = row.get("Location", "").strip()
    base = normalise_token(name)
    if base == "ground_station" and location:
        base = normalise_token(location)
    return base


def load_rows(path: Path) -> list[dict[str, str]]:
    try:
        with path.open(newline="", encoding="utf-8") as handle:
            reader = csv.DictReader(handle)
            rows = list(reader)
            fieldnames = list(reader.fieldnames or [])
    except FileNotFoundError as exc:
        raise SystemExit(f"Ground-station catalog not found: {path}") from exc

    required = {"Name", "Organization", "Latitude", "Longitude"}
    missing = required - set(fieldnames)
    if missing:
        raise SystemExit(f"{path} is missing required column(s): {', '.join(sorted(missing))}")
    return rows


def row_text(row: dict[str, str]) -> str:
    return " ".join(
        [
            row.get("Name", ""),
            row.get("Organization", ""),
            row.get("Type", ""),
            row.get("Location", ""),
        ]
    )


def valid_coordinate(row: dict[str, str]) -> bool:
    try:
        lat = float(row.get("Latitude", ""))
        lon = float(row.get("Longitude", ""))
    except ValueError:
        return False
    return -90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0


def match_rows(rows: list[dict[str, str]], operator: OperatorPattern) -> list[dict[str, str]]:
    regexes = [re.compile(pattern, re.IGNORECASE) for pattern in operator.patterns]
    return [
        row
        for row in rows
        if valid_coordinate(row) and any(regex.search(row_text(row)) for regex in regexes)
    ]


def write_ground_stations(
    path: Path,
    rows: list[dict[str, str]],
    min_elevation_deg: float,
) -> int:
    if not rows:
        if path.exists():
            path.unlink()
        return 0

    path.parent.mkdir(parents=True, exist_ok=True)
    used_ids: set[str] = set()
    written = 0
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            if not valid_coordinate(row):
                continue
            gs_id = station_id(row)
            if gs_id in used_ids:
                suffix = 2
                candidate = f"{gs_id}_{suffix}"
                while candidate in used_ids:
                    suffix += 1
                    candidate = f"{gs_id}_{suffix}"
                gs_id = candidate
            used_ids.add(gs_id)
            lat = float(row["Latitude"])
            lon = float(row["Longitude"])
            handle.write(
                f"{gs_id},{lat:.7f},{lon:.7f},{DEFAULT_ALT_M:.1f},{min_elevation_deg:.1f}\n"
            )
            written += 1
    return written


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Extract per-operator ground-station files from ground_station.csv."
    )
    parser.add_argument(
        "--ground-station-csv",
        type=Path,
        default=DEFAULT_GS_CSV,
        help=f"Input ground-station CSV (default: {DEFAULT_GS_CSV})",
    )
    parser.add_argument(
        "--out-dir",
        type=Path,
        default=DEFAULT_OUT_DIR,
        help=f"Output directory for per-operator GS files (default: {DEFAULT_OUT_DIR})",
    )
    parser.add_argument(
        "--min-elevation-deg",
        type=float,
        default=DEFAULT_MIN_ELEVATION_DEG,
        help=f"Minimum elevation mask to write in each row (default: {DEFAULT_MIN_ELEVATION_DEG})",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    rows = load_rows(args.ground_station_csv)
    print(f"Loaded {len(rows):,} ground-station rows from {args.ground_station_csv}")

    for operator in OPERATORS:
        matches = match_rows(rows, operator)
        out_path = args.out_dir / operator.filename
        written = write_ground_stations(out_path, matches, args.min_elevation_deg)
        examples = ", ".join(row.get("Name", "").strip() for row in matches[:3])
        suffix = f"  e.g. {examples}" if examples else ""
        print(f"{operator.display_name:<35} {written:>5} -> {out_path.name}{suffix}")


if __name__ == "__main__":
    main()

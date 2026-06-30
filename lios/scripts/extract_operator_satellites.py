#!/usr/bin/env python3
"""Extract active satellite catalog rows into per-operator TLE files.

The source CelesTrak-style ``active.csv`` has object names and TLE fields, but
no owner/operator column.  This script therefore uses conservative name
patterns for known constellations and fleet names.

Usage:
  python3 lios/scripts/extract_operator_satellites.py
  python3 lios/scripts/extract_operator_satellites.py \
      --active-csv lios/data/tles/active.csv \
      --out-dir lios/data/tles
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_ACTIVE_CSV = REPO_ROOT / "lios/data/active.csv"
DEFAULT_OUT_DIR = REPO_ROOT / "lios/data/tles"
DEFAULT_MAX_APOGEE_KM = 2000.0
EARTH_EQUATORIAL_RADIUS_KM = 6378.137
EARTH_GRAVITATIONAL_PARAMETER_KM3_S2 = 398600.4418


@dataclass(frozen=True)
class OperatorPattern:
    display_name: str
    filename: str
    patterns: tuple[str, ...]
    notes: str = ""


OPERATORS: tuple[OperatorPattern, ...] = (
    OperatorPattern("SpaceX (Starlink)", "spacex_starlink.tle", (r"\bSTARLINK\b",)),
    OperatorPattern("Eutelsat Group (OneWeb)", "eutelsat_oneweb.tle", (r"\bONEWEB\b",)),
    OperatorPattern("Iridium Communications", "iridium_communications.tle", (r"\bIRIDIUM\b",)),
    OperatorPattern("Globalstar", "globalstar.tle", (r"\bGLOBALSTAR\b",)),
    OperatorPattern(
        "Planet Labs",
        "planet_labs.tle",
        (r"\bFLOCK\b", r"\bSKYSAT\b", r"\bDOVE\b"),
    ),
    OperatorPattern("Spire Global", "spire_global.tle", (r"\bLEMUR\b",)),
    OperatorPattern("Amazon (Project Kuiper)", "amazon_project_kuiper.tle", (r"\bKUIPER\b",)),
    OperatorPattern("Telesat (Lightspeed)", "telesat_lightspeed.tle", (r"\bTELESAT\b", r"\bLIGHTSPEED\b")),
    OperatorPattern(
        "China Satellite Network Group (Guowang)",
        "china_satnet_guowang.tle",
        (r"\bGUOWANG\b", r"\bSATNET\b"),
    ),
    OperatorPattern("G60 Starlink", "g60_starlink.tle", (r"\bQIANFAN\b", r"\bG60\b")),
    OperatorPattern(
        "European Union (IRIS)",
        "european_union_iris.tle",
        (r"\bIRIS2\b", r"\bIRIS-?2\b", r"\bIRIS\^?2\b"),
        "Does not match plain 'IRIS' to avoid unrelated IRIS spacecraft.",
    ),
    OperatorPattern("Rivada Space Networks (OuterNet)", "rivada_outer_net.tle", (r"\bRIVADA\b", r"\bOUTERNET\b")),
    OperatorPattern("E-Space", "e_space.tle", (r"\bE-SPACE\b", r"\bESPACE\b")),
    OperatorPattern("SES S.A.", "ses.tle", (r"\bSES\b", r"\bO3B\b", r"\bASTRA\b")),
    OperatorPattern("Viasat", "viasat.tle", (r"\bVIASAT\b",)),
    OperatorPattern("Inmarsat", "inmarsat.tle", (r"\bINMARSAT\b",)),
    OperatorPattern("EchoStar", "echostar.tle", (r"\bECHOSTAR\b", r"\bJUPITER\b")),
    OperatorPattern("Intelsat", "intelsat.tle", (r"\bINTELSAT\b", r"\bIS-\d", r"\bGALAXY\b")),
    OperatorPattern("LeoSat", "leosat.tle", (r"\bLEOSAT\b",)),
    OperatorPattern("Kepler Communications", "kepler_communications.tle", (r"\bKEPLER\b",)),
    OperatorPattern("AST SpaceMobile", "ast_spacemobile.tle", (r"\bBLUEWALKER\b", r"\bAST SPACEMOBILE\b")),
    OperatorPattern("Lynk Global", "lynk_global.tle", (r"\bLYNK\b",)),
    OperatorPattern(
        "Swarm Technologies",
        "swarm_technologies.tle",
        (r"\bSPACEBEE\b",),
        "SpaceBEE is used for Swarm Technologies; plain SWARM is ESA's Swarm mission.",
    ),
    OperatorPattern("Orbcomm", "orbcomm.tle", (r"\bORBCOMM\b",)),
    OperatorPattern("Sky and Space Global", "sky_and_space_global.tle", (r"\bSKY AND SPACE\b", r"\b3DIAMONDS\b")),
)


def load_rows(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    try:
        with path.open(newline="", encoding="utf-8") as handle:
            reader = csv.DictReader(handle)
            rows = list(reader)
            fieldnames = list(reader.fieldnames or [])
    except FileNotFoundError as exc:
        raise SystemExit(f"Active catalog not found: {path}") from exc

    if not fieldnames or "OBJECT_NAME" not in fieldnames:
        raise SystemExit(f"{path} must contain an OBJECT_NAME column")
    return fieldnames, rows


def load_operator_csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        rows = list(reader)
        fieldnames = list(reader.fieldnames or [])
    if not fieldnames or "OBJECT_NAME" not in fieldnames:
        raise SystemExit(f"{path} must contain an OBJECT_NAME column")
    return rows


def match_rows(rows: list[dict[str, str]], operator: OperatorPattern) -> list[dict[str, str]]:
    regexes = [re.compile(pattern, re.IGNORECASE) for pattern in operator.patterns]
    return [
        row
        for row in rows
        if any(regex.search(row["OBJECT_NAME"]) for regex in regexes)
    ]


def apogee_altitude_km(row: dict[str, str]) -> float:
    """Calculate osculating apogee altitude from catalog mean motion and eccentricity."""
    mean_motion_rev_day = float(row["MEAN_MOTION"])
    eccentricity = float(row["ECCENTRICITY"])
    if mean_motion_rev_day <= 0.0 or not 0.0 <= eccentricity < 1.0:
        raise ValueError(
            f"invalid orbit for {row.get('OBJECT_NAME', '<unknown>')!r}: "
            f"mean_motion={mean_motion_rev_day}, eccentricity={eccentricity}"
        )
    mean_motion_rad_sec = mean_motion_rev_day * 2.0 * math.pi / 86400.0
    semi_major_axis_km = (
        EARTH_GRAVITATIONAL_PARAMETER_KM3_S2 / mean_motion_rad_sec**2
    ) ** (1.0 / 3.0)
    return semi_major_axis_km * (1.0 + eccentricity) - EARTH_EQUATORIAL_RADIUS_KM


def filter_leo_rows(
    rows: list[dict[str, str]], max_apogee_km: float
) -> list[dict[str, str]]:
    """Keep satellites whose complete orbit remains within the LEO boundary."""
    return [row for row in rows if apogee_altitude_km(row) <= max_apogee_km]


def tle_checksum(line: str) -> str:
    total = 0
    for char in line[:68]:
        if char.isdigit():
            total += int(char)
        elif char == "-":
            total += 1
    return str(total % 10)


def format_epoch(epoch: str) -> str:
    dt = datetime.fromisoformat(epoch.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    dt = dt.astimezone(timezone.utc)
    year = dt.year % 100
    start_of_year = datetime(dt.year, 1, 1, tzinfo=timezone.utc)
    day_of_year = (dt - start_of_year).total_seconds() / 86400.0 + 1.0
    return f"{year:02d}{day_of_year:012.8f}"


def format_intl_designator(object_id: str) -> str:
    # OBJECT_ID is normally YYYY-NNNAAA. TLE line 1 uses YYNNNAAA.
    value = object_id.strip()
    match = re.fullmatch(r"(\d{4})-(\d{3})([A-Z]{1,3})", value)
    if not match:
        return "        "
    year, launch_number, piece = match.groups()
    return f"{int(year) % 100:02d}{launch_number}{piece:<3}"[:8]


def format_tle_decimal(value: str) -> str:
    number = float(value)
    text = f"{number: .8f}"
    if text.startswith(" 0."):
        text = " ." + text[3:]
    elif text.startswith("-0."):
        text = "-." + text[3:]
    return text.rjust(10)[:10]


def format_tle_exponential(value: str) -> str:
    number = float(value)
    if number == 0.0:
        return " 00000+0"

    sign = "-" if number < 0 else " "
    mantissa_text, exponent_text = f"{abs(number):.5E}".split("E")
    exponent = int(exponent_text)
    mantissa = float(mantissa_text)

    # TLE compact exponential fields have an implied leading decimal point.
    if mantissa >= 1.0:
        mantissa /= 10.0
        exponent += 1
    mantissa_digits = int(round(mantissa * 100000))
    if mantissa_digits >= 100000:
        mantissa_digits = 10000
        exponent += 1

    exponent_sign = "+" if exponent >= 0 else "-"
    return f"{sign}{mantissa_digits:05d}{exponent_sign}{abs(exponent):1d}"[-8:]


def tle_lines_for_row(row: dict[str, str]) -> tuple[str, str, str]:
    name = row["OBJECT_NAME"].strip()[:24]
    satnum = int(row["NORAD_CAT_ID"])
    classification = (row.get("CLASSIFICATION_TYPE") or "U").strip()[:1] or "U"
    intl_designator = format_intl_designator(row.get("OBJECT_ID", ""))
    epoch = format_epoch(row["EPOCH"])
    mean_motion_dot = format_tle_decimal(row["MEAN_MOTION_DOT"])
    mean_motion_ddot = format_tle_exponential(row["MEAN_MOTION_DDOT"])
    bstar = format_tle_exponential(row["BSTAR"])
    ephemeris_type = int(float(row["EPHEMERIS_TYPE"]))
    element_set_no = int(float(row["ELEMENT_SET_NO"]))

    line1_body = (
        f"1 {satnum:05d}{classification} {intl_designator} {epoch} "
        f"{mean_motion_dot} {mean_motion_ddot} {bstar} "
        f"{ephemeris_type:1d} {element_set_no:4d}"
    )
    line1_body = line1_body[:68].ljust(68)
    line1 = f"{line1_body}{tle_checksum(line1_body)}"

    eccentricity = int(round(float(row["ECCENTRICITY"]) * 10_000_000))
    rev_at_epoch = int(float(row["REV_AT_EPOCH"]))
    line2_body = (
        f"2 {satnum:05d} "
        f"{float(row['INCLINATION']):8.4f} "
        f"{float(row['RA_OF_ASC_NODE']):8.4f} "
        f"{eccentricity:07d} "
        f"{float(row['ARG_OF_PERICENTER']):8.4f} "
        f"{float(row['MEAN_ANOMALY']):8.4f} "
        f"{float(row['MEAN_MOTION']):11.8f}"
        f"{rev_at_epoch:5d}"
    )
    line2_body = line2_body[:68].ljust(68)
    line2 = f"{line2_body}{tle_checksum(line2_body)}"
    return name, line1, line2


def write_tle(path: Path, rows: list[dict[str, str]]) -> None:
    if not rows:
        if path.exists():
            path.unlink()
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            name, line1, line2 = tle_lines_for_row(row)
            handle.write(f"{name}\n{line1}\n{line2}\n")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Extract per-operator satellite CSVs from active.csv."
    )
    parser.add_argument(
        "--active-csv",
        type=Path,
        default=DEFAULT_ACTIVE_CSV,
        help=f"Input active satellite CSV (default: {DEFAULT_ACTIVE_CSV})",
    )
    parser.add_argument(
        "--out-dir",
        type=Path,
        default=DEFAULT_OUT_DIR,
        help=f"Output directory for per-operator TLEs (default: {DEFAULT_OUT_DIR})",
    )
    parser.add_argument(
        "--max-apogee-km",
        type=float,
        default=DEFAULT_MAX_APOGEE_KM,
        help=(
            "Keep only satellites at or below this apogee altitude in km "
            f"(default: {DEFAULT_MAX_APOGEE_KM:.0f}, the LEO boundary)"
        ),
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if not args.active_csv.exists():
        source_dir = args.active_csv.parent
        if not source_dir.exists():
            raise SystemExit(f"Active catalog not found: {args.active_csv}")

        print(
            f"Active catalog not found: {args.active_csv}\n"
            f"Converting existing per-operator CSV files in {source_dir}"
        )
        converted = 0
        for operator in OPERATORS:
            csv_path = source_dir / operator.filename.replace(".tle", ".csv")
            if not csv_path.exists():
                continue
            rows = load_operator_csv(csv_path)
            matched_count = len(rows)
            rows = filter_leo_rows(rows, args.max_apogee_km)
            tle_path = args.out_dir / operator.filename
            write_tle(tle_path, rows)
            converted += 1
            print(
                f"{operator.display_name:<45} {len(rows):>6} LEO "
                f"({matched_count - len(rows):>4} excluded) -> {tle_path.name}"
            )
        if converted == 0:
            raise SystemExit(
                f"No existing operator CSV files found in {source_dir}; "
                f"restore {args.active_csv} and rerun."
            )
        return

    fieldnames, rows = load_rows(args.active_csv)
    summary = []

    print(f"Loaded {len(rows):,} active catalog rows from {args.active_csv}")
    print(f"Keeping satellites with apogee <= {args.max_apogee_km:g} km")
    for operator in OPERATORS:
        matches = match_rows(rows, operator)
        matched_count = len(matches)
        matches = filter_leo_rows(matches, args.max_apogee_km)
        tle_path = args.out_dir / operator.filename
        write_tle(tle_path, matches)
        examples = [row["OBJECT_NAME"] for row in matches[:5]]
        summary.append(
            {
                "operator": operator.display_name,
                "tle_filename": str(tle_path),
                "count": len(matches),
                "patterns": list(operator.patterns),
                "examples": examples,
                "notes": operator.notes,
            }
        )
        print(
            f"{operator.display_name:<45} {len(matches):>6} LEO "
            f"({matched_count - len(matches):>4} excluded) -> {tle_path.name}"
        )



if __name__ == "__main__":
    main()

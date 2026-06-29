#!/usr/bin/env python3
"""Remove identical TLE records from lios/data/tles.

The script reads every .tle/.txt file as three-line records:
  name
  line 1
  line 2

It keeps the first occurrence of each identical (line 1, line 2) pair and
removes later duplicates, even if the duplicate is in a different file.

Dry run by default:
  python3 lios/scripts/deduplicate_tles.py

Rewrite files:
  python3 lios/scripts/deduplicate_tles.py --apply
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_TLE_DIR = REPO_ROOT / "lios/data/tles"


@dataclass(frozen=True)
class TLERecord:
    name: str
    line1: str
    line2: str

    @property
    def key(self) -> tuple[str, str]:
        return self.line1.strip(), self.line2.strip()


def read_tle_file(path: Path) -> list[TLERecord]:
    lines = [line.rstrip("\n") for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    if len(lines) % 3 != 0:
        raise SystemExit(
            f"{path} has {len(lines)} non-empty lines; expected a multiple of 3"
        )
    return [
        TLERecord(lines[i], lines[i + 1], lines[i + 2])
        for i in range(0, len(lines), 3)
    ]


def write_tle_file(path: Path, records: list[TLERecord]) -> None:
    if not records:
        path.unlink()
        return
    with path.open("w", encoding="utf-8") as handle:
        for record in records:
            handle.write(f"{record.name}\n{record.line1}\n{record.line2}\n")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Remove duplicate TLE records from data/tles."
    )
    parser.add_argument(
        "--tle-dir",
        type=Path,
        default=DEFAULT_TLE_DIR,
        help=f"TLE directory (default: {DEFAULT_TLE_DIR})",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Rewrite files. Without this flag, only report duplicate counts.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    paths = sorted([*args.tle_dir.glob("*.tle"), *args.tle_dir.glob("*.txt")])
    if not paths:
        raise SystemExit(f"No .tle or .txt files found in {args.tle_dir}")

    seen: dict[tuple[str, str], tuple[Path, str]] = {}
    kept_by_file: dict[Path, list[TLERecord]] = {}
    removed_by_file: dict[Path, int] = {}
    total_records = 0
    total_removed = 0

    for path in paths:
        records = read_tle_file(path)
        total_records += len(records)
        kept: list[TLERecord] = []
        removed = 0

        for record in records:
            previous = seen.get(record.key)
            if previous is None:
                seen[record.key] = (path, record.name)
                kept.append(record)
            else:
                removed += 1
                total_removed += 1

        kept_by_file[path] = kept
        removed_by_file[path] = removed

    print(f"Scanned {len(paths)} files in {args.tle_dir}")
    print(f"Total records       : {total_records:,}")
    print(f"Unique records      : {len(seen):,}")
    print(f"Duplicate records   : {total_removed:,}")

    changed = [(path, removed_by_file[path], len(kept_by_file[path])) for path in paths if removed_by_file[path]]
    if changed:
        print("\nFiles with duplicates:")
        for path, removed, kept in changed:
            action = "would keep" if not args.apply else "kept"
            print(f"  {path.name:<35} removed {removed:>6}, {action} {kept:>6}")
    else:
        print("\nNo duplicate TLE records found.")

    if not args.apply:
        print("\nDry run only. Re-run with --apply to rewrite files.")
        return

    for path in paths:
        if removed_by_file[path]:
            write_tle_file(path, kept_by_file[path])
    print("\nRewrote files with duplicates removed.")


if __name__ == "__main__":
    main()

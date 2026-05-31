#!/usr/bin/env python3
"""
Generate lios/blockchain/fablo/fablo-config.json from the TLE data directory.

Each .txt file in <data-dir>/tles/ becomes one peer org.  Stems are
lower-cased and sorted; each stem's capitalize() becomes the org name
(e.g. intelsat → Intelsat, oneweb → Oneweb).

Usage (called automatically by lios/evaluation/start_network.sh):

    python3 lios/blockchain/gen_fablo_config.py \
        --data-dir /path/to/lios/data \
        --out      /path/to/lios/blockchain/fablo/fablo-config.json
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_SCHEMA = "https://github.com/hyperledger-labs/fablo/releases/download/1.2.0/schema.json"
_FABRIC_VERSION = "2.5.15"
_CHANNEL = "isl-settlement"
_CHAINCODE_DIR = "../../chaincode/isl_settlement"


def _org(stem: str) -> dict:
    name = stem.capitalize()
    return {"name": name, "msp": f"{name}MSP", "domain": f"{stem}.lios.example.com"}


def build_config(stems: list[str]) -> dict:
    orgs = [_org(s) for s in stems]

    fablo_orgs = [
        {
            "organization": {"name": "Orderer", "domain": "orderer.lios.example.com"},
            "orderers": [
                {"groupName": "orderer-group", "prefix": "orderer", "type": "raft", "instances": 3}
            ],
        }
    ]
    for o in orgs:
        fablo_orgs.append(
            {
                "organization": {
                    "name": o["name"],
                    "mspName": o["msp"],
                    "domain": o["domain"],
                },
                "ca": {"prefix": "ca"},
                "peer": {"instances": 2, "db": "CouchDb"},
            }
        )

    endorsement = "AND(" + ",".join(f"'{o['msp']}.member'" for o in orgs) + ")"

    return {
        "$schema": _SCHEMA,
        "global": {"fabricVersion": _FABRIC_VERSION, "tls": True, "tools": {"explorer": True}},
        "orgs": fablo_orgs,
        "channels": [
            {
                "name": _CHANNEL,
                "orgs": [{"name": o["name"], "peers": ["peer0", "peer1"]} for o in orgs],
            }
        ],
        "chaincodes": [
            {
                "name": _CHANNEL,
                "version": "1.0",
                "lang": "golang",
                "channel": _CHANNEL,
                "directory": _CHAINCODE_DIR,
                "endorsement": endorsement,
            }
        ],
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Generate fablo-config.json from TLE operator files"
    )
    parser.add_argument("--data-dir", required=True, help="Path to lios/data/")
    parser.add_argument("--out", required=True, help="Output path for fablo-config.json")
    args = parser.parse_args()

    tle_dir = Path(args.data_dir).resolve() / "tles"
    if not tle_dir.exists():
        print(f"ERROR: TLE directory not found: {tle_dir}", file=sys.stderr)
        sys.exit(1)

    stems = sorted(p.stem.lower() for p in tle_dir.glob("*.txt"))
    if not stems:
        print(f"ERROR: no .txt files found in {tle_dir}", file=sys.stderr)
        sys.exit(1)

    config = build_config(stems)

    out_path = Path(args.out).resolve()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(config, indent=2))

    names = [s.capitalize() for s in stems]
    print(f"Generated {out_path}")
    print(f"  Operators ({len(stems)}): {', '.join(names)}")
    print(f"  Endorsement: AND({', '.join(f'{n}MSP' for n in names)})")


if __name__ == "__main__":
    main()

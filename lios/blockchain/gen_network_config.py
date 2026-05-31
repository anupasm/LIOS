#!/usr/bin/env python3
"""
Generate lios/blockchain/network_config.json.

Derives the operator-name → Fablo-org mapping by matching the sorted list of
TLE operator names (alpha, beta, gamma …) to the sorted list of peer org names
in fablo-config.json (OperatorA, OperatorB, OperatorC …).  Orderer-only orgs
(those without a "peer" key) are excluded from the mapping.

Also captures per-org CLI container names, peer0 addresses, and the orderer
address by inspecting the running Docker containers — these are stored in
network_config.json so FabricClient can build correct docker-exec invocations.

Usage (called automatically by lios/evaluation/start_network.sh):

    python3 lios/blockchain/gen_network_config.py \
        --fablo-dir /path/to/lios/blockchain/fablo \
        --data-dir  /path/to/lios/data \
        --out       /path/to/lios/blockchain/network_config.json
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path


def _cli_env(container: str) -> dict[str, str]:
    """Return the environment variables of a running container as a dict."""
    result = subprocess.run(
        ["docker", "inspect", "--format", "{{json .Config.Env}}", container],
        capture_output=True, text=True, timeout=10,
    )
    if result.returncode != 0:
        return {}
    try:
        return dict(e.split("=", 1) for e in json.loads(result.stdout) if "=" in e)
    except Exception:
        return {}


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Write network_config.json for the LIOS Fablo network"
    )
    parser.add_argument("--fablo-dir", required=True, help="Path to lios/blockchain/fablo/")
    parser.add_argument("--data-dir",  required=True, help="Path to lios/data/")
    parser.add_argument("--out",       required=True, help="Output path for network_config.json")
    args = parser.parse_args()

    fablo_dir = Path(args.fablo_dir).resolve()
    data_dir  = Path(args.data_dir).resolve()
    out_path  = Path(args.out).resolve()
    out_path.parent.mkdir(parents=True, exist_ok=True)

    # ── Read Fablo org names from fablo-config.json ────────────────────────────
    fablo_cfg_path = fablo_dir / "fablo-config.json"
    if not fablo_cfg_path.exists():
        print(f"ERROR: fablo-config.json not found at {fablo_cfg_path}", file=sys.stderr)
        sys.exit(1)

    fablo_cfg = json.loads(fablo_cfg_path.read_text())

    # Peer orgs only (exclude orderer-only orgs that have no "peer" key).
    peer_orgs = [org for org in fablo_cfg.get("orgs", []) if "peer" in org]
    fablo_orgs = sorted(org["organization"]["name"] for org in peer_orgs)

    if not fablo_orgs:
        print("ERROR: no peer organisations found in fablo-config.json", file=sys.stderr)
        sys.exit(1)

    # ── Read operator IDs from TLE filenames ──────────────────────────────────
    tle_dir = data_dir / "tles"
    if not tle_dir.exists():
        print(f"ERROR: TLE directory not found: {tle_dir}", file=sys.stderr)
        sys.exit(1)

    tle_ops = sorted(p.stem.lower() for p in tle_dir.glob("*.txt"))

    if not tle_ops:
        print(f"ERROR: no TLE files found in {tle_dir}", file=sys.stderr)
        sys.exit(1)

    # Each TLE stem maps directly to the org whose name is stem.capitalize().
    # gen_fablo_config.py uses the same rule, so fablo-config.json and the TLE
    # directory are always in sync when start_network.sh is used.
    org_name_map = {stem: stem.capitalize() for stem in tle_ops}

    missing = set(org_name_map.values()) - set(fablo_orgs)
    if missing:
        print(
            f"ERROR: org(s) {sorted(missing)} derived from TLE stems are not in "
            f"fablo-config.json ({fablo_orgs}).  Re-run start_network.sh to regenerate "
            "fablo-config.json from the current data directory.",
            file=sys.stderr,
        )
        sys.exit(1)

    # ── Collect per-org CLI / peer metadata from running containers ───────────
    # CLI container name = cli.{domain}  (Fablo convention).
    # CORE_PEER_ADDRESS env var inside the CLI container gives peer0's address.
    # TLS_CA_CERT_PATH gives the orderer TLS cert path used in peer commands.
    # Peer TLS cert path follows Fablo's standard mount:
    #   /var/hyperledger/cli/crypto-peer/{peer0-hostname}/tls/ca.crt
    org_meta: dict[str, dict] = {}
    orderer_tls_cert = ""

    for org in peer_orgs:
        name   = org["organization"]["name"]
        domain = org["organization"]["domain"]
        cli    = f"cli.{domain}"

        env           = _cli_env(cli)
        peer0_address = env.get("CORE_PEER_ADDRESS", "")
        peer0_host    = peer0_address.split(":")[0] if peer0_address else f"peer0.{domain}"
        peer0_tls     = f"/var/hyperledger/cli/crypto-peer/{peer0_host}/tls/ca.crt"

        if not orderer_tls_cert:
            orderer_tls_cert = env.get("TLS_CA_CERT_PATH", "")

        org_meta[name] = {
            "cli":            cli,
            "peer0_address":  peer0_address,
            "peer0_tls_cert": peer0_tls,
        }

        if peer0_address:
            print(f"  {name}: cli={cli}  peer0={peer0_address}")
        else:
            print(f"  {name}: WARNING — could not inspect {cli}", file=sys.stderr)

    # ── Orderer address from generated commands script ─────────────────────────
    # commands-generated.sh contains the literal orderer address string used by
    # all channel/chaincode setup functions, e.g.:
    #   'orderer0.orderer-group.orderer.lios.example.com:7030'
    orderer_address = ""
    cmds_path = fablo_dir / "fablo-target" / "fabric-docker" / "commands-generated.sh"
    if cmds_path.exists():
        m = re.search(r"'(orderer\d+\.[^']+:\d+)'", cmds_path.read_text())
        if m:
            orderer_address = m.group(1)
    if orderer_address:
        print(f"  Orderer: {orderer_address}")
    else:
        print("  WARNING — could not determine orderer address", file=sys.stderr)

    # ── Write config ───────────────────────────────────────────────────────────
    config = {
        "channel":          "isl-settlement",
        "chaincode":        "isl-settlement",
        "default_org":      fablo_orgs[0],
        "org_name_map":     org_name_map,
        "org_meta":         org_meta,
        "orderer_address":  orderer_address,
        "orderer_tls_cert": orderer_tls_cert,
    }

    out_path.write_text(json.dumps(config, indent=2))

    print("\n  Operator → Fablo org mapping:")
    for op, org in org_name_map.items():
        print(f"    {op}  →  {org}")
    print(f"\n  Network config written to {out_path}")


if __name__ == "__main__":
    main()

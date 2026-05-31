#!/usr/bin/env bash
# start_network.sh — Spin up a fresh LIOS Hyperledger Fabric network via Fablo.
#
# Usage:
#   ./lios/evaluation/start_network.sh [--reset]
#
# --reset  Tear down any existing network containers and start from scratch.
#          Without this flag the script reuses a running network.
#
# On success writes lios/blockchain/network_config.json with the fablo directory
# path and operator-to-org name mapping.  The Python FabricClient reads this
# file at runtime and issues all blockchain calls through fablo invoke/query.
#
# Prerequisites: Docker (running), python3
#
# Tested with Fablo 1.2.0 and Fabric 2.5.0.

set -euo pipefail

# Ensure ~/.local/bin is in PATH so the docker-compose → docker compose shim
# is found by Fablo's generated scripts when the standalone binary is absent.
export PATH="$HOME/.local/bin:$PATH"

# ── Paths ──────────────────────────────────────────────────────────────────────

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$(dirname "$SCRIPT_DIR")")"
FABLO_DIR="$PROJECT_ROOT/lios/blockchain/fablo"
FABLO_BIN="$FABLO_DIR/fablo"
FABLO_VERSION="1.2.0"
FABLO_CONFIG="$FABLO_DIR/fablo-config.json"
DATA_DIR="$PROJECT_ROOT/lios/data"
NETWORK_CFG_OUT="$PROJECT_ROOT/lios/blockchain/network_config.json"

# ── Parse arguments ────────────────────────────────────────────────────────────

RESET=false
for arg in "$@"; do
  [[ "$arg" == "--reset" ]] && RESET=true
done

# ── Colour helpers ─────────────────────────────────────────────────────────────

GREEN='\033[0;32m'; YELLOW='\033[1;33m'; RED='\033[0;31m'; NC='\033[0m'
info()  { echo -e "${GREEN}[INFO]${NC}  $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC}  $*"; }
error() { echo -e "${RED}[ERROR]${NC} $*" >&2; }

# ── Prerequisite checks ────────────────────────────────────────────────────────

info "Checking prerequisites..."

if ! docker info &>/dev/null; then
  error "Docker daemon is not running.  Start Docker and retry."
  exit 1
fi

if ! command -v python3 &>/dev/null; then
  error "python3 not found.  Install Python 3.11+ and retry."
  exit 1
fi

# ── Download Fablo if not present ─────────────────────────────────────────────

if [[ ! -x "$FABLO_BIN" ]]; then
  info "Downloading Fablo $FABLO_VERSION → $FABLO_BIN"
  curl -fsSL \
    "https://github.com/hyperledger-labs/fablo/releases/download/$FABLO_VERSION/fablo.sh" \
    -o "$FABLO_BIN"
  chmod +x "$FABLO_BIN"
  info "Fablo downloaded."
else
  info "Fablo binary found at $FABLO_BIN"
fi

# ── Derive operators from data directory ───────────────────────────────────────

# Read TLE stems (sorted, lower-case) — one per operator.
mapfile -t OPERATORS < <(
  ls "$DATA_DIR/tles/"*.txt 2>/dev/null \
    | xargs -I{} basename {} .txt \
    | tr '[:upper:]' '[:lower:]' \
    | sort
)

if [[ ${#OPERATORS[@]} -eq 0 ]]; then
  error "No TLE files found in $DATA_DIR/tles/ — cannot determine operators."
  exit 1
fi

info "Detected ${#OPERATORS[*]} operator(s): ${OPERATORS[*]}"

# ── Generate fablo-config.json from current data directory ────────────────────

info "Generating $FABLO_CONFIG from TLE operator list..."
python3 "$PROJECT_ROOT/lios/blockchain/gen_fablo_config.py" \
    --data-dir "$DATA_DIR" \
    --out      "$FABLO_CONFIG"

# ── Detect running network ─────────────────────────────────────────────────────

FIRST_PEER="peer0.${OPERATORS[0]}.lios.example.com"
ALREADY_RUNNING=false
if docker ps --format '{{.Names}}' 2>/dev/null | grep -q "$FIRST_PEER"; then
  ALREADY_RUNNING=true
fi

# ── Start / recreate network ───────────────────────────────────────────────────

cd "$FABLO_DIR"

if $RESET; then
  info "Tearing down existing network (--reset)..."

  rm -rf "$FABLO_DIR/fablo-target"
  "$FABLO_BIN" prune 2>/dev/null || true

  docker rm -f $(docker ps -a -q) 2>/dev/null || true

  info "Starting fresh network (this may take 2–4 minutes)..."
  "$FABLO_BIN" up "$FABLO_CONFIG"

elif ! $ALREADY_RUNNING; then
  info "No running network detected.  Starting (this may take 2–4 minutes)..."
  "$FABLO_BIN" up "$FABLO_CONFIG"

else
  info "Network already running.  Use --reset to recreate."
fi

# ── Wait for peer containers ───────────────────────────────────────────────────

info "Waiting for peer containers to become healthy..."
for STEM in "${OPERATORS[@]}"; do
  CONTAINER="peer0.${STEM}.lios.example.com"
  WAIT=0
  until docker inspect --format='{{.State.Running}}' "$CONTAINER" 2>/dev/null \
        | grep -q true; do
    sleep 3; WAIT=$((WAIT+3))
    if [[ $WAIT -ge 120 ]]; then
      error "Container $CONTAINER did not start within 120 s."
      docker ps; exit 1
    fi
  done
  info "  $CONTAINER is up"
done

# ── Probe chaincode availability ───────────────────────────────────────────────

FIRST_ORG="${OPERATORS[0]^}"   # capitalize first letter (bash 4+)

info "Probing isl-settlement chaincode (up to 2 min)..."
RETRIES=24   # 24 × 5 s = 120 s

until "$FABLO_BIN" query isl-settlement isl-settlement \
      '{"Args":["GetSatelliteKey","__probe__"]}' "$FIRST_ORG" \
      &>/dev/null 2>&1 || [[ $RETRIES -eq 0 ]]; do
  sleep 5
  RETRIES=$((RETRIES-1))
  info "  Chaincode not ready yet... ($RETRIES retries left)"
done

if [[ $RETRIES -eq 0 ]]; then
  warn "Chaincode probe timed out.  If experiments fail, wait a minute and retry."
else
  info "Chaincode is responding."
fi

# ── Generate Python network config ────────────────────────────────────────────

info "Generating $NETWORK_CFG_OUT ..."
(
  cd "$PROJECT_ROOT"
  python3 lios/blockchain/gen_network_config.py \
    --fablo-dir "$FABLO_DIR"   \
    --data-dir  "$DATA_DIR"    \
    --out       "$NETWORK_CFG_OUT"
)

# ── Done ───────────────────────────────────────────────────────────────────────

echo ""
echo -e "${GREEN}================================================${NC}"
echo -e "${GREEN}  LIOS Fabric network is ready                  ${NC}"
echo -e "${GREEN}================================================${NC}"
echo ""
echo "  Config  : $NETWORK_CFG_OUT"
echo "  Channel : isl-settlement"
echo "  Managed : via fablo invoke / fablo query"
echo ""
echo "  Run experiments:"
echo "    cd lios"
echo "    python evaluation/run_experiments.py --config baseline --blockchain"
echo ""
echo "  Stop the network:"
echo "    cd lios/blockchain/fablo && ./fablo down"
echo ""

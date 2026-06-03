# LIOS — Lightweight Inter-operator Orbital Sharing Protocol

A research implementation of LIOS, a blockchain-anchored fair-traffic-sharing protocol for multi-operator LEO satellite constellations.  Satellites from different operators form Inter-Satellite Links (ISLs); LIOS ensures each operator's traffic is forwarded fairly and that any cheating is detected and penalised on-chain.

---

## Architecture

```
lios/
├── contact_plan/        # TLE loader, GS loader, SGP4 window calculator
├── routing/             # Contact Graph Routing (CGR) — Dijkstra + Yen's K-shortest
├── crypto/              # ECDSA P-256 key hierarchy
├── protocol/            # Off-chain payment channel, auth handshake, ISL FSM
├── simulator/           # Discrete-event simulation (DES) core, satellite/GS nodes
├── ground/              # Ground-station settlement manager
├── evaluation/          # Metrics (Jain fairness), adversarial nodes, experiment runner
├── chaincode/           # Hyperledger Fabric Go chaincode (ISL settlement)
├── fablo/               # Fablo network config (3-operator Raft network)
├── data/
│   ├── tles/            # TLE files — alpha.txt, beta.txt, gamma.txt
│   └── gss/             # Ground station files — alpha.txt, beta.txt, gamma.txt
└── tests/               # Unit + integration test suite (81 tests)
```

### Key design choices

| Concern | Solution |
|---|---|
| Fair traffic accounting | Off-chain bilateral payment channel with co-signed `BalanceProof` |
| On-chain settlement | Fabric chaincode; operator-level channels; co-signed balance reset (T1) or 48-hour dispute window (T7) |
| Satellite authentication | Ephemeral ECDH (P-256) → AES-256-GCM; mutual cert verification |
| Routing | Earliest-arrival Dijkstra + Yen's K-shortest over time-expanded contact graph |
| Fairness metric | Jain index J = (Σx_i)² / (n·Σx_i²), target ≥ 0.95 |

---

## Quick start

### Requirements

- Python ≥ 3.11 (virtualenv at `.env/`)
- Go ≥ 1.21 (for chaincode only)
- [Fablo](https://github.com/hyperledger-labs/fablo) ≥ 1.2.0 + Docker (for on-chain tests only)

### Python setup

```bash
python3 -m venv .env
source .env/bin/activate
pip install -r lios/requirements.txt
```

### Run tests

```bash
# Unit + integration (fast, no Docker)
python -m pytest -m "not slow"

# Include slow 90-min DES fairness test
python -m pytest
```

### Compute a contact plan

```bash
source .env/bin/activate
cd lios
python contact_plan/compute_windows.py \
  --start 2025-11-19T00:00:00Z \
  --end   2025-11-19T01:30:00Z \
  --step  30 \
  --out   results/contact_plan.csv
```

### Run a simulation experiment

```bash
source .env/bin/activate
cd lios

# Single config
python evaluation/run_experiments.py --config baseline --out results/

# All 7 configs + paper figures + LaTeX table
python evaluation/run_experiments.py --out results/
```

Available configs: `baseline`, `depletion`, `top_up`, `adversarial_1`, `adversarial_2`, `fairness_24h`, `high_density`.

Output: `results/logs/<config>_metrics.json`, `results/figures/fig[1-6]_*.{pdf,png}`.

---

## Data format

### TLE files (`data/tles/<operator>.txt`)

Standard 3-line TLE blocks, one per satellite:

```
SATELLITE NAME
1 NNNNNC NNNNNAAA NNNNN.NNNNNNNN ...
2 NNNNN NNN.NNNN NNN.NNNN ...
```

### Ground station files (`data/gss/<operator>.txt`)

CSV, one ground station per line:

```
<gs-name>,<lat_deg>,<lon_deg>[,<alt_m>[,<min_elevation_deg>]]
```

Defaults: `alt_m = 0.0`, `min_elevation_deg = 5.0`.

---

## Module reference

### `contact_plan/`

| Module | Purpose |
|---|---|
| `tle_loader.py` | Parse TLE files → `Dict[operator, List[Satellite]]` |
| `gs_loader.py` | Parse GS CSV files → `Dict[operator, List[GroundStation]]` |
| `window_calculator.py` | SGP4 propagation → `ContactPlan` (ISL + GS windows) |
| `compute_windows.py` | CLI wrapper for `window_calculator` |

### `routing/`

| Module | Purpose |
|---|---|
| `cgr.py` | `CGR.route(src, dst, t_start, k)` — earliest-arrival Dijkstra + Yen's K-shortest |

### `crypto/`

| Module | Purpose |
|---|---|
| `key_hierarchy.py` | `OperatorCA`, `SatelliteCert`, `SatelliteKeyStore` |

### `protocol/`

| Module | Purpose |
|---|---|
| `offchain.py` | `OffChainProtocol` — bilateral channel state, `BalanceProof`, settlement triggers |
| `auth.py` | `ContactAuthSession` — ECDH auth handshake, AES-256-GCM session encryption |
| `isl_state_machine.py` | `ISLStateMachine` — ACTIVE → PAUSED → RESUME → ACTIVE FSM |

### `simulator/`

| Module | Purpose |
|---|---|
| `simulator.py` | `EventLoop` — priority-queue DES; `SimEvent`, `SimulationClock` |
| `satellite_node.py` | `SatelliteNode` — handles ISL open/close, forwarding, settlement triggers |
| `ground_station_node.py` | `GroundStationNode`, `FabricMock` — settlement submission, notification relay |
| `traffic_generator.py` | Poisson arrivals, log-normal flow sizes, weighted satellite selection |

### `ground/`

| Module | Purpose |
|---|---|
| `settlement_manager.py` | `SettlementManager` — full orchestration: receive TX log, submit to Fabric, challenge, top-up, notifications |

### `evaluation/`

| Module | Purpose |
|---|---|
| `metrics.py` | `MetricsCollector` — Jain fairness, latency stats, OOS fraction, plots |
| `adversarial.py` | `MaliciousSatelliteNode` — rollback and selective-forward attacks; `run_adversarial_scenario()` |
| `run_experiments.py` | Experiment runner; 7 configs; generates 6 paper figures + LaTeX table |

---

## Hyperledger Fabric network

### Start the network

```bash
cd lios/fablo
fablo up fablo-config.json
```

This generates crypto material and starts 3 organisations (OperatorA/B/C), each with 2 CouchDB peers and 1 Raft orderer, plus the `isl-settlement` channel.

### Deploy the chaincode

```bash
fablo chaincode install isl-settlement
```

### Tear down

```bash
fablo down
```

The Go chaincode is in `lios/chaincode/isl_settlement/` and implements:
`EnsureOperatorChannel`, `SubmitCoSignedSettlement`, `SubmitBalanceReset`, `InitiateSettlement`, `ChallengeSettlement`, `FinalizeSettlement`, `RequestTopUp`, `ConfirmTopUp`, `UpsertSatelliteKey`, `RevokeSatelliteKey`, `GetSatelliteKey`, `RecordISLPause`, `RecordISLResume`, `GetPendingNotifications`, `AcknowledgeNotification`.

Satellite-pair sub-channels are **not** registered on-chain.  A lightweight
`SettlementRecord` (keyed by satellite pair) is created lazily on first use.
Dispute state lives in an ephemeral `DisputeRecord` deleted on resolution.
Satellite public keys are resolved from the on-chain `SatKey` registry.

`SubmitBalanceReset` is the T1 (balance-low) path.  It requires a **co-signed**
`BalanceProof` (both `sigA` and `sigB` mandatory), verifies both signatures
against the `SatKey` registry, then debits the net transfer from the operator
channel: `δ = (b_A + b_B)/2 − b_A`; the satellite that forwarded more has
lower balance, so its operator is credited and the peer operator is debited.
After on-chain confirmation both satellites reset to equal-split balance.

---

## Settlement protocol summary

```
ISL contact open
  └─ ContactAuthSession (ECDH) ──► shared AES-256-GCM session key
  └─ OffChainProtocol.sync()   ──► agree on latest BalanceProof

Per forwarding batch
  └─ record_forwarding()       ──► BalanceProof (half-signed, PROOF_PROP to peer)
  └─ peer: cosign_proof()      ──► PROOF_ACK returned; both sides hold co-signed proof

ISL contact close  (trigger evaluation)
  ├─ T1 (balance low) ──► BALANCE RESET PATH
  │    └─ PROOF_PROP ─► peer cosigns ─► PROOF_ACK (co-signed proof required)
  │    └─ both satellites pause traffic
  │    └─ GS submits SubmitBalanceReset() to Fabric
  │         └─ verifies both sigs; debits net transfer δ from operator channel
  │         └─ emits BalanceReset event ──► peer GS notified
  │    └─ ISL resumes with equal-split balance once both sats ACK
  │
  └─ T7 (traffic cap) ──► STANDARD SETTLEMENT PATH
       └─ GS submits SubmitCoSignedSettlement() to Fabric (co-signed proof)
          or InitiateSettlement() if proof is single-signed (48h dispute window)
       └─ ISL FSM: ACTIVE → PAUSED → (both-ACK) → ACTIVE
```

---

## Citation

If you use this implementation in your research, please cite:

```
@misc{lios2025,
  title  = {LIOS: Lightweight Inter-operator Orbital Sharing Protocol},
  author = {Anupa Samarasinghe},
  year   = {2025},
  note   = {PhD research implementation, University College Dublin}
}
```

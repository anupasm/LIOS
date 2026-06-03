# LIOS Protocol — Evaluation Test Suite

This document describes the full evaluation methodology for the LIOS protocol: the
simulation infrastructure, constellation dataset, protocol parameters, the seven
experiment configurations, and the metrics used to assess fairness, latency, and
security.

---

## 1. Constellation Dataset

Real-world Two-Line Element (TLE) data is used throughout.  The four operators
currently loaded are:

| Operator   | Satellites | Ground Stations | Orbit type        |
|------------|------------|-----------------|-------------------|
| Intelsat   | 59         | 6               | GEO / MEO         |
| Iridium    | 80         | 7               | LEO (780 km)      |
| OneWeb     | 651        | 23              | LEO (1,200 km)    |
| Telesat    | 18         | 6               | MEO / LEO         |
| **Total**  | **808**    | **42**          |                   |

TLE files live in `lios/data/tles/<operator>.txt` (standard 3-line format).
Ground station files live in `lios/data/gss/<operator>.txt`
(`name,lat,lon[,alt_m[,min_elev_deg]]`).  The fablo network configuration is
generated automatically from the TLE directory so adding or removing an operator
only requires dropping or removing the corresponding files.

---

## 2. Contact Plan Computation

The contact plan is the foundation of every experiment.  It enumerates all time
windows during which two nodes (satellite–satellite or ground–satellite) can
communicate.

### Method

- **Propagation model** — SGP4 via `sgp4` library, propagated at a configurable
  time step (default 30 s).
- **ISL window criterion** — inter-satellite range < `isl_max_range_km` (2,500 km).
- **GS window criterion** — satellite elevation above ground station ≥
  `gs_min_elevation_deg` (5°).
- **Epoch** — 2025-11-19 00:15:00 UTC for all experiments.

### Contact capacity model

| Link type | Peak capacity | Degradation |
|-----------|--------------|-------------|
| ISL laser | 10 Mbps      | Linear with range: `cap = 10 000 × (1 − d / 2 500)` kbps |
| GS uplink | 50 Mbps      | Linear with range: `cap = 50 000 × (1 − d / 3 000)` kbps |

### Parameter rationale

`isl_max_range_km = 2500 km` is drawn from the SpaceX Starlink ISL specification
(Bhattacherjee & Singla, CoNEXT 2019).  `gs_min_elevation_deg = 5°` is the ITU
minimum mask for non-geostationary systems.

---

## 3. Protocol Parameters

All parameters are in `lios/config.toml`.

### 3.1 Settlement triggers

| Trigger | Symbol | Value | Rationale |
|---------|--------|-------|-----------|
| Balance-low fraction | T1 (`t_low_fraction`) | 5 % of initial capacity | Triggers balance reset before channel exhaustion; 5 % leaves enough margin for in-flight traffic to drain |
| Hash-chain length cap | T2 (`h_max`) | 10,000 entries | Bounds the O(n) replay-prevention cost; at typical ISL rates this corresponds to ~10 s of forwarding |
| Cumulative bytes cap | T7 (`s_max_kb`) | 100 GB | Guards against very long sessions accumulating unbounded credit imbalance |
| Challenge window | `t_challenge_sec` | 48 h (172,800 s) | Allows peer GS time to observe the on-chain dispute even with long GS contact gaps in polar orbits |
| Top-up confirmation | `t_topup_confirm_sec` | 24 h (86,400 s) | Matches typical operator SLA response time |

### 3.2 Channel economics

| Parameter | Value | Rationale |
|-----------|-------|-----------|
| Initial per-side balance | 1 TB (1,048,576 KB) | Covers ~105 s of continuous 10 Mbps ISL forwarding before T1 fires; gives headroom for bursty traffic |
| Operator channel balance | 1 PB | Effectively unlimited; prevents operator-channel depletion from masking satellite-level fairness |
| Operator penalty reserve | 10 GB | Calibrated to ~10× the maximum single-session forwarding imbalance |

### 3.3 Authentication

| Parameter | Value | Rationale |
|-----------|-------|-----------|
| ECDH nonce length | 32 bytes | 256-bit entropy; meets NIST SP 800-57 guidance for ephemeral key exchange |
| Timestamp tolerance | 30 s | Balances clock-drift resilience with replay-attack resistance at LEO orbital speeds |
| Certificate validity | 90 days | Typical operational rotation period; shorter than the challenge window |

### 3.4 Traffic model

| Parameter | Value | Rationale |
|-----------|-------|-----------|
| Arrival process | Poisson | Standard teletraffic model; memoryless inter-arrivals simplify reproducibility |
| Base arrival rate λ | 0.01 flows / s / GS node | Calibrated so that at 50 % load the system is well below ISL capacity |
| Effective rate | λ × load_fraction × n_GS | Scales linearly with the number of ground nodes in the dataset |
| Flow size | Log-normal (median 1 MB, σ = 1) | Empirically fits internet flow-size distributions (Claffy et al.) |
| Flow size bounds | [10 KB, 200 MB] | Lower bound prevents degenerate 0-byte flows; upper bound matches channel balance |
| Cross-operator bias | 70 % | Ensures that most flows require inter-operator forwarding, exercising the settlement protocol |
| CGR lookahead | 600 s | 10-minute window balances route quality with computation cost |

### 3.5 Routing

Contact Graph Routing (CGR), earliest-arrival Dijkstra with Yen's K-shortest
(K = 1 in traffic generation; K = 3 in DES).  The adjacency structure is
precomputed once per experiment so Dijkstra operates on O(degree) neighbours
rather than the full contact list.  Routes are cached in 5-minute buckets so
the Dijkstra cost is amortised across many flows with the same (src, dst) pair.

---

## 4. Experiment Configurations

Seven configurations are defined in `lios/evaluation/run_experiments.py`.

### E0 — Baseline

| Parameter | Value |
|-----------|-------|
| Duration | 1 h (3,600 s) |
| Traffic load | 50 % |
| Adversarial mode | None |
| Seed | 42 |

**Purpose**: Establishes the normal-operation baseline.  Verifies that the
settlement protocol fires correctly under moderate load, that channels do not
deplete, and that the Jain fairness index meets the ≥ 0.95 target.

**Expected behaviour**: ISL contacts are established, traffic flows across
operator boundaries, and T1/T2 settlements occur.  No disputes.  Jain index
should be close to 1.0 because load is symmetric.

---

### E1 — Channel Depletion

| Parameter | Value |
|-----------|-------|
| Duration | 1.5 h (5,400 s) |
| Traffic load | 95 % |
| Adversarial mode | None |
| Seed | 43 |

**Purpose**: Drives the system to channel depletion by running at 95 % of
capacity.  Validates that T1 (balance-low) triggers fire in time, that
`SubmitBalanceReset` is issued before a channel reaches zero, and that the ISL
resumes correctly after on-chain confirmation.

**Rationale for 95 % load**: At 100 % load the arrival rate exceeds ISL
capacity and most flows are dropped immediately, which masks settlement
behaviour.  95 % creates genuine depletion pressure while keeping most flows
routable.

---

### E2 — Top-Up

| Parameter | Value |
|-----------|-------|
| Duration | 24 h (86,400 s) |
| Traffic load | 80 % |
| Adversarial mode | None |
| Seed | 44 |

**Purpose**: Tests the long-run channel replenishment path (T_topup).  Over 24
hours at 80 % load, operator channels should deplete to the point where
`RequestTopUp` / `ConfirmTopUp` transactions are needed.  Validates that the
24-hour confirmation deadline is met and that ISL service is not disrupted.

**Rationale for 24 h**: A single orbital period is ~95 min; 24 h covers ~15
full orbits, ensuring every GS–satellite geometry is exercised and that
long-duration drift in operator balances is observable.

---

### E3 — Adversarial: Rollback Attack

| Parameter | Value |
|-----------|-------|
| Duration | 24 h (86,400 s) |
| Traffic load | 70 % |
| Adversarial mode | `rollback` |
| Attack probability p_attack | 50 % per eligible settlement |
| Seed | 45 |

**Purpose**: Injects a malicious satellite (belonging to the first operator)
that submits a stale `BalanceProof` on settlement — one with a higher
self-balance than the true latest state.  The test measures:

- **Detection rate** — fraction of rollback attempts caught by the honest GS
  submitting a fresher counter-proof within T_challenge.
- **Penalty application** — whether the on-chain penalty reserve is slashed for
  the dishonest operator.

**Rationale for 70 % load**: High enough to generate many settlements (and thus
many rollback opportunities) while keeping the network functional so that the
honest GS can reach on-chain to challenge.

**Attack model**: The malicious node stores proof history and, with probability
`p_attack`, replaces the current proof at settlement time with an older one
where its own balance is higher.  The gain from a successful rollback equals the
balance differential.

---

### E4 — Adversarial: Selective-Forward Drop

| Parameter | Value |
|-----------|-------|
| Duration | 24 h (86,400 s) |
| Traffic load | 70 % |
| Adversarial mode | `selective_forward` |
| Drop probability p_drop | 30 % per forwarded flow |
| Seed | 46 |

**Purpose**: Models a satellite that accepts traffic, records it in its hash
chain (claiming credit), but silently drops 30 % of packets.  The test
measures whether the accounting discrepancy is detected at settlement time via
hash-chain verification.

**Rationale for 30 % drop rate**: Low enough that flows still complete
(preventing immediate routing failures that would mask the accounting attack)
but high enough to create a detectable balance divergence at the next settlement.

---

### E5 — Long-Duration Fairness

| Parameter | Value |
|-----------|-------|
| Duration | 24 h (86,400 s) |
| Traffic load | 60 % |
| Adversarial mode | None |
| Seed | 47 |

**Purpose**: Tests whether the fairness invariant (Jain index ≥ 0.95) holds
over a full day of operation across heterogeneous operators (Intelsat GEO,
Iridium LEO, OneWeb LEO, Telesat MEO).  The heterogeneity in orbital altitude,
GS coverage, and satellite count creates natural traffic asymmetry; this
experiment validates that the settlement protocol corrects it.

**Rationale for 60 % load**: Moderate load keeps per-operator byte counts
growing at a measurable rate without saturating channels, making fairness
trends clearly visible in the results.

---

### E6 — High-Density / Throughput Ceiling

| Parameter | Value |
|-----------|-------|
| Duration | 1.5 h (5,400 s) |
| Traffic load | 99 % |
| Adversarial mode | None |
| Time step | 10 s (finer propagation for dense contacts) |
| Seed | 48 |

**Purpose**: Stress-tests the system at near-saturation (99 % of capacity) with
a finer SGP4 propagation step (10 s instead of 30 s) to capture short-duration
contact windows that coarser steps would miss.  Measures throughput ceiling and
identifies where fairness begins to degrade.

**Rationale for 10 s step**: At near-saturation, the routing decisions become
sensitive to contact-window timing.  A 10 s step provides ~3× higher fidelity
in window edge detection at the cost of a larger contact plan.

---

## 5. Metrics

All metrics are computed by `lios/evaluation/metrics.py`.

### 5.1 Jain Fairness Index

```
J = (Σ xᵢ)² / (n · Σ xᵢ²)
```

where `xᵢ` is the total bytes forwarded by operator `i` and `n` is the number
of operators.  `J = 1.0` means all operators forwarded exactly equal volumes;
`J = 1/n` means one operator monopolised all forwarding.  Target: **J ≥ 0.95**.

**Why Jain and not max–min fairness**: Jain is a single scalar that aggregates
across all operators, making it suitable for a summary LaTeX table.  Max–min
fairness would require per-pair reporting.

### 5.2 Out-of-Service (OOS) Fraction

Fraction of total ISL contact time during which the ISL was in PAUSED state
(waiting for on-chain settlement confirmation).  Target: **OOS ≤ 2 %**.

```
OOS = Σ pause_duration / Σ contact_duration
```

A high OOS fraction indicates that settlement is too slow relative to contact
duration, causing throughput loss.

### 5.3 Settlement Latency

Three components are measured separately:

| Component | Definition |
|-----------|-----------|
| Off-chain ISL propagation | Time for the half-signed proof to travel from forwarder to peer (ISL range / c) |
| On-chain blockchain processing | `SETTLEMENT_RECEIVED → SETTLEMENT_FINALIZED` (block ordering + endorsement) |
| Protocol total | Off-chain + on-chain; orbital wait for GS contact is excluded |

Orbital wait time is excluded because it is a function of orbital geometry, not
the protocol itself.

Reported as mean, p50, p95, p99, max across all settled channels.

### 5.4 Security Metrics (E3 / E4 only)

| Metric | Definition |
|--------|-----------|
| Detection rate | `detected_rollbacks / total_rollback_attempts` |
| Rollback attempts | Count of stale proofs submitted by the malicious node |
| Selective drops | Count of flows dropped but credited in the hash chain |
| Penalty events | Count of on-chain penalty-reserve slashings |

### 5.5 Traffic Statistics

| Metric | Definition |
|--------|-----------|
| Total forwarding events | Flows successfully routed across at least one ISL hop |
| Flows with path | Fraction of generated flows that found a CGR route |
| Operator pair distribution | Per-operator-pair forwarding volume (for cross-operator bias validation) |

---

## 6. Blockchain Integration

When run with `--blockchain`, each experiment connects to a live Hyperledger
Fabric network managed by Fablo.

### Network topology

- **Orderer**: 3-node Raft consensus (`orderer.lios.example.com`)
- **Peer orgs**: One peer org per operator, 2 CouchDB peers each
- **Channel**: `isl-settlement`
- **Chaincode**: `isl-settlement` (Go, `lios/chaincode/isl_settlement/`)
- **Endorsement**: `AND(IntelsatMSP, IridiumMSP, OnewebMSP, TelesatMSP)` — all
  operators must endorse every transaction

The network configuration is generated dynamically from the TLE directory by
`lios/blockchain/gen_fablo_config.py`, so adding a new operator to `data/tles/`
automatically creates a new peer org on the next `start_network.sh` run.

### Initialization

On startup, `InitLedger` is called once with all operator channel pairs and all
satellite public keys in a single atomic transaction (batched at 50 satellites
per invoke to stay within the OS argument-list limit).  This replaces the
previous per-satellite `UpsertSatelliteKey` loop which required one block
confirmation per satellite (~800 sequential block waits for the full dataset).

### Settlement paths

| Path | Chaincode function | Trigger |
|------|--------------------|---------|
| Co-signed balance reset | `SubmitBalanceReset` | T1 (balance-low) |
| Co-signed settlement | `SubmitCoSignedSettlement` | T2 / T7 (honest counterpart available) |
| Unilateral dispute | `InitiateSettlement` → 48 h window → `FinalizeSettlement` | T2 / T7 (counterpart unreachable) |
| Challenge | `ChallengeSettlement` | Honest GS submits fresher proof within T_challenge |
| Top-up | `RequestTopUp` → `ConfirmTopUp` | Operator channel balance below replenishment threshold |

---

## 7. Reproducing the Experiments

### Without blockchain (simulation only)

```bash
source .env/bin/activate
cd lios

# Single experiment
python evaluation/run_experiments.py --config baseline --out results/

# All seven experiments + figures
python evaluation/run_experiments.py --out results/
```

### With Hyperledger Fabric

```bash
# Terminal 1 — start network (first time takes 3-5 min)
./lios/evaluation/start_network.sh

# Terminal 2 — run experiments
source .env/bin/activate
cd lios
python evaluation/run_experiments.py --config baseline --out results/ --blockchain
```

To reset the ledger state between runs:

```bash
./lios/evaluation/start_network.sh --reset
```

### Output files

| File | Contents |
|------|----------|
| `results/logs/<config>_metrics.json` | Full metrics dict (fairness, latency, security, traffic stats) |
| `results/logs/<config>_settlement_log.json` | Per-channel off-chain and ground-settlement event timeline |
| `results/logs/<config>_contact_plan.csv` | All computed contact windows |
| `results/logs/<config>_contact_traffic.json` | Per-contact traffic attribution |
| `results/logs/<config>_propagation_log.json` | Satellite tracks + contact list (for visualisation) |
| `results/figures/fig1_jain_fairness.{pdf,png}` | Jain index bar chart across all configs |
| `results/figures/fig2_settlement_latency_cdf.{pdf,png}` | Settlement latency breakdown |
| `results/figures/fig2_oos_fraction.{pdf,png}` | OOS fraction bar chart |
| `results/figures/fig3_penalty_events.{pdf,png}` | Penalty events (adversarial configs) |
| `results/figures/fig4_balance_evolution.{pdf,png}` | Operator forwarding volume (baseline) |
| `results/figures/fig6_throughput_vs_fairness.{pdf,png}` | Throughput vs Jain scatter |

---

## 8. Known Limitations and Design Decisions

### Simulation fidelity

- **No RF interference model** — link capacity degrades only with range, not
  with atmospheric conditions or interference.
- **No orbital manoeuvre model** — TLEs are propagated as-is; station-keeping
  manoeuvres are not simulated.
- **Latency model** — only propagation delay (d/c) is modelled; queuing,
  processing, and retransmission delays are not included.

### Settlement latency measurements

The `settlement_latency` values in `*_metrics.json` show zeros when no
settlements reached the on-chain finalisation path during a run.  This happens
when:
1. The simulation duration is too short for T1/T2/T7 triggers to fire, or
2. The FabricMock (used without `--blockchain`) does not emit the
   `SETTLEMENT_FINALIZED` events that drive the latency timeline.

The depletion experiment (`E1`) should produce the highest settlement count; if
latency is still zero there, increase `traffic_load_fraction` or extend duration.

### Adversarial detection gap

The current detection mechanism relies on the honest GS submitting a
counter-proof within the 48-hour challenge window.  If the honest GS has no
ground contact during this window (can happen for polar-orbit operators), the
rollback succeeds undetected.  This is a known limitation noted in the protocol
design (§13.4) and motivates the long T_challenge value.

### Operator heterogeneity

The four operators differ significantly in satellite count (18 to 651), orbital
altitude, and GS density.  This means ISL contact rates are not symmetric:
OneWeb sats are far more likely to appear as intermediate hops than Telesat sats.
The cross-operator bias parameter (70 %) ensures that this asymmetry is reflected
in the traffic mix rather than being hidden by same-operator routing.

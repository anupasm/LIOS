# LIOS Protocol — Complete Evaluation Plan
## State-of-the-Art Comparative Evaluation with Phase-by-Phase Implementation

---

## Table of Contents

1. [Current State](#1-current-state)
2. [Evaluation Architecture Overview](#2-evaluation-architecture-overview)
3. [Part A — Baseline and State-of-the-Art Comparisons](#3-part-a--baseline-and-state-of-the-art-comparisons)
4. [Part B — Extended Metrics](#4-part-b--extended-metrics)
5. [Part C — Adversarial Scenarios (Extended)](#5-part-c--adversarial-scenarios-extended)
6. [Part D — Scalability Study](#6-part-d--scalability-study)
7. [Part E — Parameter Sensitivity](#7-part-e--parameter-sensitivity)
8. [Part F — Network Topology and Traffic Model Sensitivity](#8-part-f--network-topology-and-traffic-model-sensitivity)
9. [Part G — Ground Contact Overhead](#9-part-g--ground-contact-overhead)
10. [Part H — Real Blockchain Integration](#10-part-h--real-blockchain-integration)
11. [Phase-by-Phase Implementation Plan](#11-phase-by-phase-implementation-plan)
12. [Paper Section Mapping](#12-paper-section-mapping)
13. [Master Experiment Table](#13-master-experiment-table)

---

## 1. Current State

### 1.1 What Is Already Implemented

| Component | Location | Status |
|-----------|----------|--------|
| 7-config experiment runner | `lios/evaluation/run_experiments.py` | Done |
| Jain fairness index | `lios/evaluation/metrics.py` | Done |
| OOS fraction | `lios/evaluation/metrics.py` | Done |
| Settlement latency (mean/p50/p95/p99) | `lios/evaluation/run_experiments.py:151` | Done |
| Free-rider prevention rate | `lios/evaluation/metrics.py` | Done |
| Hash chain storage overhead | `lios/evaluation/metrics.py` | Done |
| Rollback attack | `lios/evaluation/adversarial.py` | Done |
| Selective forward attack | `lios/evaluation/adversarial.py` | Done |
| 6 publication figures | `lios/evaluation/run_experiments.py:468` | Done |
| GS propagation delay model | `lios/simulator/ground_station_node.py:187` | Done |
| `wait_for_gs_sec` tracking | `lios/simulator/ground_station_node.py:252` | Done |
| Contact traffic attribution | `lios/evaluation/run_experiments.py:84` | Done |

### 1.2 Constellation Scale

```
Operators  : alpha, beta, gamma   (3)
Satellites : 1 per operator       (3 total)
Ground stations : 1 per operator  (Melbourne / Boca Chica / Singapore)
ISL range  : 2500 km
```

> **Note**: the current single-GS-per-operator configuration makes the ground contact
> overhead dimension (Part G) the most critical gap in the evaluation — a settlement
> payload queued at the end of a Melbourne contact may wait up to a full orbital period
> before the next pass.

### 1.3 What Is Missing

- No comparison against any competing or baseline protocol.
- No scalability data beyond 3 satellites.
- No GS scarcity sweep; `wait_for_gs_sec` is recorded but never analysed systematically.
- No GS uplink bandwidth model (link treated as unconstrained).
- No measurement of protocol message overhead.
- No crypto operation latency benchmark.
- No collusion, grinding, or GS-offline adversarial scenarios.
- No parameter sensitivity analysis.
- No real Fabric network measurement (FabricMock only).

---

## 2. Evaluation Architecture Overview

```
lios/evaluation/
├── run_experiments.py       ← extend: sweep runner, baseline configs, GS configs
├── metrics.py               ← extend: 7 new metrics
├── adversarial.py           ← extend: collusion, grinding, gs_delay modes
├── baselines.py             ← NEW: GreedyNode, TitForTatNode, PayWordProtocol
├── sweep.py                 ← NEW: parameter and GS scarcity sweep runner
├── ground_overhead.py       ← NEW: GS bandwidth model, contact gap analysis
├── benchmarks.py            ← NEW: pytest-benchmark for ECDSA / SHA-256
└── figures.py               ← NEW: decomposed latency stacks, heatmaps, CDFs
```

All new code is additive. The existing `run_experiments.py` pipeline and 7 configs remain
unchanged and continue to produce the correctness-focused results.

---

## 3. Part A — Baseline and State-of-the-Art Comparisons

Five competing approaches bracket LIOS from above and below on the fairness–overhead
trade-off curve.

### A.1 No-Protocol Greedy (`cmp_greedy`)

Satellites forward all traffic without any accounting. There is no `BalanceProof`,
no hash chain, no settlement trigger, and no ISL pause. This is the anarchy lower bound:
optimal throughput, zero fairness enforcement, zero protocol overhead.

**Reference**: Nash equilibrium of non-cooperation in commons management
(Ostrom, *Governing the Commons*, 1990).

**Implementation** — `lios/evaluation/baselines.py`:
```python
class GreedySatelliteNode(SatelliteNode):
    def _on_isl_close(self, event):
        # No settlement triggers, no pause
        self._active_isls.pop(event.from_node, None)
        return []
    def get_pending_settlement_payloads(self):
        return []
```
No changes to `GroundStationNode` needed; `FabricMock` will receive zero transactions.

**Expected outcome**: Jain fairness drifts to 0.2–0.5 under asymmetric load because
operators that receive more traffic never compensate the forwarding operators.

**Metrics of interest**: Jain index over time (sliding-window), bytes imbalance Gini
coefficient (Part B.5), total forwarded volume (upper bound for throughput).

---

### A.2 Centralised Authority (`cmp_central`)

A single trusted `CentralSettlementAuthority` ground node aggregates all satellites'
forwarding logs and issues signed balance credits via batch settlement every
`T_batch` seconds. Analogous to IATA CASS clearing for airline revenue sharing or
inter-carrier roaming settlement (3GPP TS 32.296).

**Reference**: Roaming settlement in mobile networks; IATA CASS bilateral clearing.

**Implementation** — `lios/evaluation/baselines.py`:
```python
class CentralSettlementAuthority:
    """Receives forwarding logs from all satellites every T_batch seconds.
    Computes bilateral net transfers and issues signed BalanceProofs.
    Single point of trust — represents the centralised alternative to LIOS.
    """
    def __init__(self, all_sats, fabric, T_batch=3600):
        self.T_batch = T_batch
        self._logs = []   # (from_sat, to_sat, kb, t)

    def receive_log(self, from_sat, to_sat, kb, t):
        self._logs.append((from_sat, to_sat, kb, t))

    def run_batch_settlement(self, t):
        # Compute net transfers; issue BalanceProofs; submit to Fabric
        ...
```
Add a periodic `BATCH_SETTLE` event to the DES loop fired every `T_batch` seconds.

**Expected outcome**: Jain → 1.0 (all data visible to authority), settlement latency =
`T_batch` (deterministic), OOS = 0% (no ISL pause), overhead = high (full log uplink
every batch period), but requires trusting the authority and is a single point of failure.

**Configuration variants**:
- `cmp_central_1h` — batch every 3600 s (same as one orbit)
- `cmp_central_6h` — batch every 21600 s
- `cmp_central_24h` — batch every 86400 s

---

### A.3 Tit-for-Tat (`cmp_t4t`)

Direct reciprocation per ISL contact: satellite A only forwards for B if B has forwarded
an equivalent byte count within the same contact window. Inspired by BitTorrent's
choking algorithm (Cohen, 2003) and the Tit-for-Tat strategy in repeated Prisoner's
Dilemma (Axelrod, 1984).

**Reference**: Cohen, B. *Incentives Build Robustness in BitTorrent*, 2003.

**Implementation** — `lios/evaluation/baselines.py`:
```python
class TitForTatNode(SatelliteNode):
    """Tracks per-contact byte deficit; refuses to forward when deficit > threshold."""
    def __init__(self, *args, deficit_threshold_kb=100, **kwargs):
        super().__init__(*args, **kwargs)
        self._contact_fwd: Dict[str, float] = {}   # peer_id → kb forwarded this contact
        self._contact_rcv: Dict[str, float] = {}   # peer_id → kb received this contact
        self.deficit_threshold_kb = deficit_threshold_kb

    def _on_isl_open(self, event):
        peer = event.from_node
        self._contact_fwd[peer] = 0.0
        self._contact_rcv[peer] = 0.0
        return super()._on_isl_open(event)

    def _on_traffic_arrive(self, event):
        peer = event.from_node
        kb = getattr(event.payload, 'size_kb', 0.0)
        deficit = self._contact_fwd.get(peer, 0) - self._contact_rcv.get(peer, 0)
        if deficit > self.deficit_threshold_kb:
            return []   # choke: refuse forwarding until peer catches up
        self._contact_fwd[peer] = self._contact_fwd.get(peer, 0) + kb
        return super()._on_traffic_arrive(event)

    def _on_isl_close(self, event):
        peer = event.from_node
        self._contact_fwd.pop(peer, None)
        self._contact_rcv.pop(peer, None)
        # No settlement needed — accounting is purely per-contact
        return []
```

**Expected outcome**: near-perfect fairness within a single contact, but no credit
carries across contacts; operators with infrequent or asymmetric contact patterns
accumulate long-term imbalances. Jain fairness degrades over 24 h. OOS = 0% (no
settlement pause). Protocol overhead ≈ 0 (no signatures, no hash chain).

---

### A.4 Hash-Chain Micropayments Without Blockchain (`cmp_payword`)

Satellites exchange signed hash-chain tokens representing forwarding credit
(PayWord / MicroMint paradigm, Rivest & Shamir 1997), but settlement is done bilaterally
by ground stations exchanging cryptographic proofs directly — **no permissioned ledger**.
There is no challenge window and no on-chain arbitration.

**Reference**: Rivest, R. & Shamir, A. *PayWord and MicroMint: Two Simple Micropayment
Schemes*, CryptoBytes 1997.

**Implementation** — swap `FabricMock` for a `DirectSettlement` stub in `GroundStationNode`:
```python
class DirectSettlementStub:
    """Peer GSs exchange co-signed BalanceProofs directly (no blockchain).
    Accepts the highest seq_num proof it receives; no dispute window.
    """
    def initiate_settlement(self, ch_id, proof, submitted_by, t):
        # Store proof; notify peer GS directly (no Fabric event queue)
        self._proofs[ch_id] = proof
        self._notify_peer_directly(ch_id, proof, t)
        return str(uuid.uuid4())

    def finalize_settlement(self, ch_id, t):
        # Immediate: no challenge window
        self._settled[ch_id] = True
```

**Expected outcome**: lower settlement latency than LIOS (no Fabric endorsement round,
no challenge window), near-identical fairness for honest operators, but zero dispute
resolution capability — a dishonest operator can submit a stale proof with no recourse.

---

### A.5 HTLC State Channels (`cmp_htlc`)

Hash-time-locked contracts as used in the Lightning Network (Poon & Dryja, 2016) adapted
for traffic-denominated bilateral channels. Requires atomic preimage reveals for
multi-hop settlement — modelled in the DES as a three-message exchange
(LOCK → REVEAL → SETTLE) with configurable timelock `T_htlc`.

**Reference**: Poon, J. & Dryja, T. *The Bitcoin Lightning Network*, 2016.

**Key DES challenge**: HTLC timelocks assume reliable message delivery. In LEO, the
GS contact gap means a satellite may miss its reveal deadline. This must be modelled
explicitly as a failed settlement (HTLC timeout path).

**Implementation** — add `HTLCProtocol` to `lios/evaluation/baselines.py`:
- Channel state adds `htlc_preimage`, `htlc_hash`, `htlc_timeout_sec`.
- On ISL close: satellite sends `LOCK(hash, amount, T_htlc)` to peer.
- Peer sends `REVEAL(preimage)` within `T_htlc`; if missed, channel rolls back.
- GS submits REVEAL to Fabric; if `T_htlc` passes without GS contact, channel enters
  the timeout path (satellite keeps balance, no transfer).

**Expected outcome**: atomicity guaranteed for same-contact multi-hop paths; high OOS
fraction when GS contact gap > `T_htlc`; complex timeout management required for sparse
GS deployments (makes HTLC poorly suited for single-GS operators).

---

### A.6 Comparison Matrix

| Property | Greedy | Central | T4T | PayWord | HTLC | **LIOS** |
|----------|--------|---------|-----|---------|------|----------|
| Jain fairness (24 h) | ≈ 0.3 | ≈ 1.0 | ≈ 0.7 | ≈ 0.95 | ≈ 0.95 | **≥ 0.95** |
| Free-rider prevention | 0% | 100%† | per-contact | 0% | partial | **100%** |
| Dispute resolution | None | Central | None | None | Timelock | **On-chain** |
| OOS fraction | 0% | ~0% | 0% | < 1% | high‡ | **< 2%** |
| Settlement latency | None | T_batch | None | Low | Medium | **Bounded** |
| Trust requirement | None | TTP | None | Bilateral | Bilateral | **Consortium** |
| GS gap sensitivity | None | Low | None | Low | **Very High** | Moderate |
| Protocol overhead | 0 | High | ~0 | Medium | High | **Low** |

† Requires trusting the central authority — the assumption being replaced.
‡ HTLC timeout rate increases sharply when GS gap > T_htlc.

---

## 4. Part B — Extended Metrics

Add these to `MetricsCollector.generate_report` in `lios/evaluation/metrics.py`.

### B.1 Protocol Message Overhead Ratio

Total bytes consumed by the accounting protocol per KB of payload delivered.

```
overhead_ratio = accounting_bytes / payload_kb_delivered
```

**Accounting bytes per settlement event**:
- BalanceProof JSON: ≈ 400 bytes
- ECDSA DER signature (P-256): 72 bytes × 2 sigs = 144 bytes
- Hash chain entry: 256 bytes × seq_num entries since last settlement
- AES-256-GCM nonce + tag: 28 bytes per encrypted ISL message
- NotificationBundle (downlink): ≈ 300 bytes per settlement

Track in `MetricsCollector`:
```python
def record_protocol_message(self, msg_type: str, size_bytes: int, t: float): ...
def compute_overhead_ratio(self) -> float:
    return self._protocol_bytes_total / (self._payload_kb_total * 1024)
```

**Target**: < 1% overhead ratio at nominal load with `H_max = 10,000`.

---

### B.2 Operator-Pair Imbalance Gini Coefficient

The Jain index aggregates all operators into a single scalar; it can hide asymmetric
pairs where one operator consistently under-contributes.

For each ordered operator pair (A, B):
```
imbalance(A,B) = |fwd_A_for_B - fwd_B_for_A| / max(fwd_A_for_B, fwd_B_for_A)
```

The Gini coefficient over all pairs gives the distribution of per-pair fairness:
a perfectly fair system has Gini = 0 regardless of N operators.

```python
def compute_imbalance_gini(self) -> float:
    from itertools import combinations
    imbalances = []
    for op_a, op_b in combinations(self.operators, 2):
        fwd_ab = self._forwarded_for[(op_a, op_b)]
        fwd_ba = self._forwarded_for[(op_b, op_a)]
        denom = max(fwd_ab, fwd_ba)
        if denom > 0:
            imbalances.append(abs(fwd_ab - fwd_ba) / denom)
    if not imbalances:
        return 0.0
    n = len(imbalances)
    imbalances.sort()
    return sum((2*i - n - 1) * x for i, x in enumerate(imbalances, 1)) / (n * sum(imbalances))
```

---

### B.3 Channel Utilisation Efficiency

Fraction of potential ISL contact capacity used for actual payload delivery:
```
utilisation = payload_kb_delivered / (total_isl_contact_kb * (1 - oos_fraction))
```
where `total_isl_contact_kb = sum(contact.capacity_kbps * contact.duration_sec / 8)`.

A low value indicates the traffic generator is under-loading the network;
a value > 1.0 indicates a misconfiguration (more traffic scheduled than link capacity).

---

### B.4 Settlement Trigger Frequency Distribution

For each trigger type T1–T7, the number of times fired per operator-pair per day.
Determines which triggers dominate at a given load level and guides parameter tuning.

```python
def record_trigger(self, trigger: str, channel_id: str, t: float): ...
def trigger_frequency_per_day(self) -> Dict[str, float]:
    ...
```

---

### B.5 GS Contact Gap Distribution

Distribution of `wait_for_gs_sec` across all settlement events. This is the time
a settlement payload queues on a satellite before a GS contact window opens.

```python
def compute_gs_gap_stats(self) -> dict:
    vals = sorted(self._wait_for_gs_sec)
    return {"p50": ..., "p95": ..., "p99": ..., "max": ..., "mean": ...}
```

Also track `peer_wait_for_gs_sec` — the time the peer satellite waits for its own GS
to deliver the ISL_RESUME notification (currently inferable from `resumed_at - gs_sent_at`
in the settlement log).

---

### B.6 GS Uplink Protocol Overhead Fraction

```
uplink_overhead_fraction = settlement_bytes_uploaded / total_uplink_capacity_used
```

Settlement bytes per upload = `len(payload.hash_chain_bytes) + sizeof(BalanceProof)`.
Upload duration = `settlement_bytes / (gs_link_capacity_kbps * 125)` seconds.

Track in `GroundStationNode`:
```python
self._settlement_bytes_uploaded: int = 0
self._total_uplink_bytes: int = 0
```

---

### B.7 Penalty-to-Attack Ratio

```
penalty_to_attack = len(penalty_events) / total_attack_attempts
```

Target: 1.0 for all single-operator attack modes.
`total_attack_attempts` is already available from `MaliciousSatelliteNode.attack_log`.

```python
def compute_penalty_to_attack_ratio(self) -> float:
    if self._total_attack_attempts == 0:
        return 1.0
    return len(self.penalty_events) / self._total_attack_attempts
```

---

### B.8 Settlement Backlog Size

Number of settlement payloads queued on all satellites at any point in simulated time.
Peak value indicates worst-case satellite storage demand; sustained high values indicate
the GS contact rate is insufficient for the settlement trigger rate.

```python
def record_settlement_queued(self, sat_id: str, t: float): ...
def record_settlement_uploaded(self, sat_id: str, t: float): ...
def compute_max_backlog(self) -> int: ...
```

---

## 5. Part C — Adversarial Scenarios (Extended)

Extend `MaliciousSatelliteNode` in `lios/evaluation/adversarial.py` with three new modes.

### C.1 Collusion Attack (`adversarial_collusion`)

Two operators coordinate to consistently under-report forwarding for the third.
Both colluding satellites co-sign a fabricated BalanceProof that inflates their own
balances at the expense of the honest operator, then both submit identical stale proofs
to their ground stations.

**Detection mechanism**: the honest operator's satellite holds a co-signed proof
contradicting both colluding proofs. The hash chain makes fabrication detectable
because the chain head embedded in the stale proof does not match the honest party's
chain head at the claimed seq_num.

**Implementation**:
```python
class CollusionGroup:
    """Shared state between two MaliciousNodes enabling coordinated false reporting."""
    def __init__(self, op_a_sats: List[str], op_b_sats: List[str]):
        self._shared_false_proofs: Dict[str, BalanceProof] = {}

    def register_false_proof(self, channel_id: str, proof: BalanceProof): ...
    def get_false_proof(self, channel_id: str) -> Optional[BalanceProof]: ...

# MaliciousNode with attack_mode='collusion' uses CollusionGroup to
# agree on a stale proof before GS submission.
```

**Metrics**:
- Fraction of colluded channels where honest operator successfully challenges.
- Minimum collusion size N_collude that evades detection (theoretical lower bound = N_operators - 1 since the honest party always holds a contradicting proof).
- Jain fairness degradation under failed detection.

---

### C.2 Settlement Grinding Attack (`adversarial_grinding`)

Malicious operator fires spurious T5 (manual override) settlement triggers at high
frequency to force the honest peer into repeated ISL pauses — a denial-of-service
attack on channel availability without actually cheating on balances.

**Implementation**:
```python
def evaluate_settlement_triggers(self, channel_id):
    if self.attack_mode == 'grinding' and self._rng.random() < self.p_attack:
        return ['T5']   # always trigger regardless of actual channel state
    return super().evaluate_settlement_triggers(channel_id)
```

**Mitigation to evaluate**: introduce `T_settle_cooldown` (minimum seconds between
settlements on the same channel). Measure OOS fraction with and without the cooldown
at various grinding rates (p_attack ∈ {0.1, 0.3, 0.5, 0.9}).

**Key metric**: OOS fraction vs. grinding rate; optimal `T_settle_cooldown` value
that limits OOS to < 2% even under maximum-rate grinding.

---

### C.3 GS Offline / Delayed Challenge (`adversarial_gs_delay`)

Honest operator's GS is offline for a configurable gap `T_outage`. A rollback attack
is launched during this window to test whether `T_challenge` is sufficient.

**Implementation** — add to `GroundStationNode`:
```python
def set_offline_window(self, t_start: float, t_end: float):
    self._offline_start = t_start
    self._offline_end = t_end

def handle_event(self, event):
    if self._offline_start <= event.time <= self._offline_end:
        return []   # GS offline — drop all events
    return super().handle_event(event)
```

**Experiment**: run `adversarial_1` (rollback) with varying `T_outage` ∈ {1h, 3h, 6h,
12h, 24h} against the current single-GS configuration. Record:
- Whether the stale proof is challenged within `T_challenge = 48h`.
- The maximum `T_outage` that still allows successful challenge.
- Relationship: safe if `T_outage < T_challenge - max_orbital_period`.

---

### C.4 Key Compromise and Recovery

Simulate a satellite key compromise: operator revokes the key on-chain mid-simulation.
Measure CRL propagation lag and any forwarding that occurs on the revoked key during
the lag window.

**Sequence**:
1. At `t = T_sim / 2`, fire a `KEY_REVOKED` event for one satellite.
2. The revoking GS appends the satellite ID to its `_crl_delta`.
3. CRL delta reaches peer satellites only on their next GS contact.
4. Measure the lag: `t_revoked → t_last_crl_delivery` across all peers.
5. Count forwarding events that occurred during the lag (should be zero if authentication
   checks the revocation cache before every PROOF_PROP).

---

## 6. Part D — Scalability Study

### D.1 Operator Count Sweep

Fix sats/operator = 1 (current); vary N_operators ∈ {2, 3, 5, 7, 10}.

Generate synthetic TLE sets for operators beyond the current three by perturbing the
existing alpha TLE (different RAAN values to spread satellites around the orbit plane).
Generate synthetic GS sets by distributing stations at longitude increments of 360°/N.

| N_ops | Total sats | Cross-op pairs | Operator channels | GS contacts/orbit |
|-------|-----------|---------------|-------------------|-------------------|
| 2 | 2 | 1 | 1 | 2 |
| 3 | 3 | 3 | 3 | 3 |
| 5 | 5 | 10 | 10 | 5 |
| 7 | 7 | 21 | 21 | 7 |
| 10 | 10 | 45 | 45 | 10 |

**Metrics**: Jain fairness index, settlement latency p95, OOS fraction, Fabric
transaction rate (settlements/min), simulation wall-clock time.

**Expected outcome**: fairness should be stable (> 0.95) since the protocol scales
per operator-pair independently. Settlement latency should be stable (dominated by
GS gap, not constellation size). Fabric transaction rate grows as O(N²) with N_ops.

---

### D.2 Satellite Density Sweep

Fix N_ops = 3; vary sats/operator ∈ {1, 3, 5, 10, 20}.

Add satellites by cloning each operator's TLE with different mean anomalies (evenly
spaced around the orbital plane). Each new satellite gets a key pair from the existing
operator CA.

**Metrics**: contact plan size (#contacts), simulation wall-clock time, hash chain
memory footprint per satellite (bytes), settlement trigger frequency per satellite/day,
total operator channel balance consumed per day.

**Expected outcome**: as satellite count grows, ISL contacts increase as O(N²),
contact plan computation time increases, and per-satellite settlement frequency may
decrease (more satellites share the load). Memory footprint per satellite should be
constant (hash chain is per-channel, capped at H_max entries).

---

### D.3 Throughput vs. Fairness Curve

Fix N_ops = 3, sats/op = 1; vary `traffic_load_fraction` ∈ {0.1, 0.2, 0.3, 0.5,
0.7, 0.9, 0.95, 0.99}.

Run each load level for 24 simulated hours. Plot Jain fairness index vs. total
forwarding events (continuous curve, extending Fig. 6 from 7 scatter points to
a smooth curve).

**Expected shape**: Jain stays > 0.95 up to roughly 80% load; beyond that, T1 triggers
fire more frequently, OOS pauses accumulate, and throughput-fairness degrades together.

---

## 7. Part E — Parameter Sensitivity

### E.1 T_challenge Sweep × GS Count Grid

`t_challenge_sec` ∈ {1800, 7200, 21600, 86400, 172800} (0.5h, 2h, 6h, 24h, 48h)
crossed with GS count ∈ {1, 2, 3, 5}.

For each (T_challenge, GS_count) pair, run `adversarial_1` (rollback) and record:
- Challenge success rate (rollback attacks caught before window expires).
- Mean OOS duration per channel (longer window = longer ISL pause).
- Fraction of honest parties that missed the window due to GS gap.

**Key finding to derive empirically**:
```
T_challenge_min = max_GS_contact_gap_p99 × 2 + T_fabric_commit + safety_margin
```

Plot as a 5×4 heatmap of challenge success rate over (T_challenge, GS_count).

---

### E.2 T_low_fraction Sweep

`t_low_fraction` ∈ {0.01, 0.02, 0.05, 0.10, 0.20}.

Fix `baseline` config (1 orbit, 50% load). Measure:
- T1 trigger frequency (increases with higher T_low_fraction — settles earlier).
- OOS fraction (increases proportionally with trigger frequency).
- Mean channel balance at trigger time (should equal `T_low_fraction × initial_capacity`).

---

### E.3 H_max Sweep × GS Contact Duration Grid

`h_max` ∈ {100, 500, 1000, 5000, 10000} crossed with GS count ∈ {1, 3, 5}.

For each (H_max, GS_count) pair, measure:
- Hash chain upload size: `H_max × 256 bytes`.
- Upload duration: `hash_chain_bytes / gs_link_capacity_kbps`.
- **Fraction of settlements that complete in a single GS contact window** (critical —
  if `upload_duration > contact_window_duration`, settlement defers to the next orbit,
  adding one full orbital period to OOS time).

**Boundary condition**:
```
H_max × 256 bytes / (gs_max_kbps × 125 B/kbps) < min_contact_window_sec
```
At `gs_max_kbps = 50000` and `min_contact_window = 300 s`:
```
H_max < 300 × 50000 × 125 / 256 ≈ 7,324,219
```
So even `H_max = 10,000` is safe — but this must be verified empirically for the
actual contact windows computed from the real TLEs.

---

### E.4 Penalty Reserve Fraction Sweep

`reserve_fraction` = `operator_channel_reserve_kb / operator_channel_balance_kb`
∈ {0.01, 0.05, 0.10, 0.25, 0.50}.

Run `adversarial_1` at each reserve level. Compute the **break-even threshold**:
```
reserve > max_gain_from_rollback = max(balance_a, balance_b) at trigger time
```
At `T_low_fraction = 5%` and full balance = 2000 KB, max gain ≈ 1900 KB per channel.
Therefore reserve must exceed 1900 KB / 2000 KB ≈ 95% of initial balance — the current
10% reserve is insufficient for a determined attacker. This is a **key security finding**
to report and motivate the economic deterrence argument formally.

---

## 8. Part F — Network Topology and Traffic Model Sensitivity

### F.1 Walker Delta vs. Walker Star

Generate two synthetic constellation types for 3 operators × 3 sats:
- **Walker Delta** (53° inclination, Starlink-like): good mid-latitude ISL density,
  sparse at poles.
- **Walker Star** (98° Sun-synchronous): uniform coverage, fewer cross-track ISL
  contacts but better polar coverage.

Measure: ISL contact duration distribution, GS contact gap distribution (Melbourne /
Boca Chica / Singapore see very different pass rates for polar orbits), Jain fairness.

---

### F.2 ISL Range Threshold Sweep

`isl_max_range_km` ∈ {1000, 1500, 2000, 2500, 3000, 4000}.

Measure: number of ISL contacts in the contact plan, total ISL contact seconds,
Jain fairness, routing path lengths (hop count), OOS fraction.

**Expected outcome**: larger range → more ISL contacts → more settlement events → higher
Fabric transaction rate but also better fairness enforcement. At very large range, ISL
quality degrades (link capacity scales inversely with range in the current model).

---

### F.3 Poisson vs. Pareto Traffic

Current generator uses Poisson arrivals with log-normal flow sizes. Add a
`ParetoTrafficGenerator` (`alpha = 1.5`) to model heavy-tailed bursty traffic.

```python
class ParetoTrafficGenerator(TrafficGenerator):
    def _sample_flow_size(self) -> float:
        return min(
            cfg.simulation.flow_size_max_kb,
            max(cfg.simulation.flow_size_min_kb,
                self._rng.paretovariate(1.5) * cfg.simulation.flow_size_min_kb)
        )
```

**Expected outcome**: Pareto traffic causes sharper balance depletions (a single large
flow can trigger T1 immediately), higher peak settlement frequency, but similar Jain
fairness over long periods since the total bytes forwarded equalise over time.

---

### F.4 Asymmetric Traffic

One operator generates 3× or 10× more flows than the other two.

```python
ExperimentConfig("asymmetric_3x", duration_sec=86400,
    traffic_load_fraction=0.60, adversarial_mode="none",
    operator_traffic_weights={"alpha": 3.0, "beta": 1.0, "gamma": 1.0})
```

**Expected outcome**: Jain index degrades because the overloaded operator uses far more
forwarding credit; T1 (depletion) fires frequently on alpha↔beta and alpha↔gamma channels;
top-up requests dominate settlement events.

---

### F.5 Diurnal Traffic Pattern

Traffic intensity follows a sinusoidal pattern over 24 hours (peak at 12:00 UTC, trough
at 00:00 UTC). Implemented as a time-varying `arrival_rate` multiplier in
`TrafficGenerator.generate_poisson_schedule`.

Use `MetricsCollector.plot_fairness_over_time` (already implemented) to plot the
sliding-window Jain index over 24 hours and verify fairness is maintained even during
traffic surges.

---

## 9. Part G — Ground Contact Overhead

Ground stations are the only path from satellite to blockchain. With sparse or
geographically concentrated GS infrastructure, settlement payloads queue for one
or more orbital periods before delivery. The GS uplink also carries competing user
traffic. Neither effect is currently swept or modeled as a resource constraint.

### G.1 GS Contact Gap — Core Analysis

**What it measures**: the time a settlement payload spends waiting on a satellite
(`wait_for_gs_sec`) as a function of GS count and placement.

**Current state**: 1 GS per operator (Melbourne / Boca Chica / Singapore). A satellite
at 550 km, 53° inclination has roughly a 7–10 minute contact window per 90-minute orbit
over Melbourne — but only when the plane passes within range of the station.
Worst-case gap between successive Melbourne contacts can exceed 6 hours.

**GS deployment configurations**:

| Config ID | GSs/op | Placement strategy |
|-----------|-------|--------------------|
| `gs_1_midlat` | 1 | Single mid-latitude (current baseline) |
| `gs_2_antipodal` | 2 | Antipodal pair (e.g., Melbourne + London) |
| `gs_3_mixed` | 3 | Equatorial + 2 polar (e.g., Nairobi, Svalbard, McMurdo) |
| `gs_5_global` | 5 | 5 globally distributed (current plan intent) |
| `gs_10_global` | 10 | 10 globally distributed (dense deployment) |

For each configuration:
- Plot the **CDF of `wait_for_gs_sec`** across all settlement events.
- Report p50, p95, p99, and max.
- Flag: fraction of settlements where `wait_for_gs_sec > T_challenge / 2` (if this
  fraction > 0, the honest counterparty may miss the challenge window).

**Implementation** — extend `lios/data/gss/*.txt` with additional GS entries per
config; pass `gs_config` as a parameter to `run_experiment`:

```python
GS_CONFIGS = {
    "gs_1_midlat":    {"alpha": [(-37.81, 144.96)],           # Melbourne
                       "beta":  [(25.99,  -97.15)],            # Boca Chica
                       "gamma": [(1.35,   103.82)]},            # Singapore
    "gs_2_antipodal": {"alpha": [(-37.81, 144.96), (51.5, -0.1)],  # + London
                       ...},
    ...
}
```

---

### G.2 End-to-End Settlement Latency Decomposition

The existing `_compute_latency_summary` produces five components per settlement event.
Present them as a **stacked latency breakdown** across GS configurations.

```
e2e_latency = isl_prop_delay_sec          ← ~5 ms at 1500 km (negligible)
            + wait_for_gs_sec             ← DOMINANT: minutes to hours
            + uplink_prop_delay_sec       ← ~3 ms at 900 km slant range
            + fabric_commit_sec           ← ~1–3 s on real Fabric; 0 in mock
            + peer_gs_notification_sec    ← ~0.01 s (inter-GS direct message)
            + peer_wait_for_gs_sec        ← SECOND DOMINANT: peer's GS gap
            + downlink_prop_delay_sec     ← ~3 ms
```

**`peer_wait_for_gs_sec`** is currently not explicitly tracked. Add it:
```python
# In ground_station_node.py _on_settlement_trigger:
peer_wait = event.time - t_settlement_finalized   # available from event.payload
self._log("PEER_GS_WAIT", peer_wait_sec=peer_wait, ...)
```

**Key figure**: stacked bar chart showing that `wait_for_gs_sec + peer_wait_for_gs_sec`
accounts for > 95% of total e2e latency in the single-GS configuration.

---

### G.3 T_challenge Sizing from GS Gap Statistics

The challenge window must satisfy:
```
T_challenge ≥ peer_wait_for_gs_sec_p99 + T_fabric_commit + safety_margin_sec
```

**Experiment**: cross (T_challenge × GS_count) grid from Part E.1. Add the empirically
derived `peer_wait_for_gs_sec_p99` from G.1 as a reference line on the heatmap.

The current `T_challenge = 172800 s (48 h)` is conservative for 5-GS deployments
(typical p99 wait < 2 h) but **necessary** for single-GS deployments where worst-case
wait can reach 12 h or more.

**Formal derivation to include in paper**:

> Let `G(n)` be the p99 GS contact gap for `n` globally distributed ground stations.
> From simulation, `G(1) ≈ 12 h`, `G(3) ≈ 3 h`, `G(5) ≈ 1.5 h`, `G(10) ≈ 45 min`.
> The minimum safe challenge window is `T_challenge_min(n) = 2·G(n) + T_fabric + 300 s`.
> Operators should select `n` and `T_challenge` jointly to satisfy this constraint.

---

### G.4 GS Uplink Bandwidth Budget

#### Current model
The GS uplink is treated as unconstrained — settlement upload is instantaneous.
The physical capacity is `gs_max_kbps = 50000 kbps` (50 Mbps).

#### Upload duration model
Add to `GroundStationNode._on_settlement_upload`:

```python
hash_chain_bytes = len(payload.hash_chain_bytes)
bal_proof_bytes  = 600                               # BalanceProof JSON + sigs
total_bytes      = hash_chain_bytes + bal_proof_bytes

# GS uplink capacity in bytes/sec
gs_capacity_bps  = cfg.link.gs_max_kbps * 125
upload_sec       = total_bytes / gs_capacity_bps

# Check: does upload fit within the remaining contact window?
contact_remaining = self._sat_contact_info[sat_id].get("contact_end") - event.time
if upload_sec > contact_remaining:
    self._log("SETTLEMENT_DEFERRED", channel_id=..., reason="CONTACT_TOO_SHORT",
              required_sec=upload_sec, available_sec=contact_remaining, ...)
    self._pending_settlement_carry[channel_id] = payload   # defer to next contact
    return []

# Schedule the Fabric submission to fire after the upload completes
return [SimEvent(
    time=event.time + upload_sec,
    event_type=EventType.SETTLEMENT_UPLOAD_COMPLETE,
    from_node=sat_id, to_node=self.gs_id, payload=payload
)]
```

At `H_max = 10,000` and `gs_max_kbps = 50,000`:
```
upload_sec = (10000 × 256 + 600) / (50000 × 125) = 2,560,600 / 6,250,000 ≈ 0.41 s
```
This is well within any realistic contact window, but the calculation must be performed
for each (H_max, GS_count, orbit altitude) combination because contact window durations
vary.

#### Competing traffic model

Add a `GSLinkScheduler` component that enforces a shared bandwidth constraint across
settlement uploads and user traffic flows during a GS contact:

```python
class GSLinkScheduler:
    """Weighted Fair Queue over GS uplink capacity."""
    def __init__(self, capacity_bps: float, settlement_weight: float = 0.05):
        self._capacity = capacity_bps
        self._settlement_weight = settlement_weight  # 5% reserved for protocol
        self._queue: List[Tuple[float, str, int]] = []  # (priority, type, bytes)

    def schedule_upload(self, msg_type: str, bytes_: int, t: float) -> float:
        """Return the time at which this upload will complete."""
        ...
```

Three scheduling policies to compare:
- **Settlement priority**: protocol messages preempt user traffic.
- **Traffic priority**: user flows get first claim; settlement defers if GS is busy.
- **WFQ (recommended)**: settlement guaranteed 5% of 50 Mbps = 2.5 Mbps minimum;
  more than sufficient for hash chain uploads; remaining 95% for user traffic.

**Metric**: OOS fraction and uplink utilisation under `high_density` config for each
scheduling policy.

---

### G.5 GS Outage Resilience

**Experiment**: during `top_up` config (24 h, 80% load), force one operator's GS offline
for `T_outage` ∈ {1h, 3h, 6h, 12h, 24h}.

Measure per outage duration:
- **Settlement backlog size** at the moment the GS comes back online.
- **OOS duration spike**: channels that triggered settlement during the outage remain
  paused until the GS recovers; the excess OOS is `T_outage + wait_to_next_contact`.
- **Proof stale risk**: for adversarial configs, whether any rollback finalises
  during the outage window (safety boundary: `T_outage < T_challenge`).
- **Backlog drain rate**: settlements cleared per GS contact window after recovery;
  bounded by GS contact window rate × upload bandwidth from G.4.

**Key invariant**: if `T_outage < T_challenge`, no rollback succeeds uncontested.
Verify this empirically for all (T_outage, T_challenge) pairs.

---

### G.6 Multi-Operator GS Sharing (Federation)

An operator with sparse GS coverage can upload settlement payloads through a peer
operator's GS when the peer's GS is in contact. The payload is cryptographically
protected (co-signed BalanceProof); the relaying operator cannot modify it without
invalidating both signatures.

**Implementation**:
```python
# In SatelliteNode._on_gs_contact_start:
# Allow upload even if the contacting GS belongs to a different operator,
# provided the payload is fully co-signed.
def _on_gs_contact_start(self, event):
    gs_node = self._gs_registry.get(event.from_node)
    if gs_node and gs_node.operator_id != self.operator_id:
        # Cross-operator GS: only upload co-signed (T1/T2) payloads
        return self._upload_cosigned_only(event)
    return super()._on_gs_contact_start(event)
```

**Experiment**: run `gs_1_midlat` (1 GS/op) with and without federation.

**Expected outcome**: effective GS count triples (3 operators × 1 GS = 3 total, all
usable). `wait_for_gs_sec` drops dramatically — from a worst case of ~12 h to roughly
the global maximum of ~4 h. This is a practical deployment recommendation for sparse
operators.

---

### G.7 Summary Metrics for Part G

Add to `MetricsCollector.generate_report`:

```python
"gs_contact_gap": {
    "p50": ..., "p95": ..., "p99": ..., "max": ..., "mean": ...
},
"peer_gs_contact_gap": {
    "p50": ..., "p95": ..., "p99": ..., "max": ...
},
"uplink_overhead_fraction":    ...,   # settlement bytes / total uplink bytes
"settlement_upload_duration": {
    "p50": ..., "p95": ..., "max": ...
},
"settlements_deferred":        ...,   # payloads too large for one contact window
"gs_outage_max_backlog":       ...,   # peak queued payloads during outage (if applicable)
"federation_relay_events":     ...,   # cross-operator GS relays (if enabled)
```

---

## 10. Part H — Real Blockchain Integration

All current experiments use `FabricMock`. Part H validates the mock's latency
assumptions against the actual Hyperledger Fabric network.

### H.1 Fabric Transaction Latency Benchmark

Run `baseline` config with `--blockchain` flag. The network must be started first:
```bash
cd lios/fablo && fablo up fablo-config.json
bash lios/evaluation/start_network.sh
python evaluation/run_experiments.py --config baseline --out results/ --blockchain
```

For each chaincode function, measure the wall-clock time from SDK invocation to ledger
confirmation:

| Function | Expected latency | Notes |
|----------|-----------------|-------|
| `SubmitCoSignedSettlement` | 1–3 s | 2-of-3 org endorsement |
| `SubmitBalanceReset` | 1–3 s | same endorsement policy |
| `InitiateSettlement` | 1–3 s | single-org submit |
| `ChallengeSettlement` | 1–3 s | |
| `FinalizeSettlement` | 2–5 s | MAJORITY endorsement |
| `UpsertSatelliteKey` | 1–3 s | |
| `GetPendingNotifications` | < 0.5 s | read-only |

Compare against the mock's assumption of ~0 latency. Patch `FabricMock` to inject
the measured median latency as a constant delay:
```python
FABRIC_LATENCY_SEC = 2.0   # derived from H.1 benchmark
```

---

### H.2 Fabric Throughput Ceiling

Run `high_density` config (`--blockchain`). Measure max sustainable TPS and compare
against the modelled settlement rate.

At 3 operators × 1 satellite × 1 GS:
```
max_settlement_rate ≈ (ISL contacts per day) × (fraction triggering settlement)
```
At 90-min orbit, ~16 contacts/day/pair, ~3 pairs = 48 contacts/day × 30% trigger
rate ≈ 14–15 settlements/day ≈ 0.0002 TPS — several orders of magnitude below
Fabric's 2000+ TPS ceiling. Fabric is not a bottleneck at the current scale.

This scales to O(N²) pairs: at 10 operators × 50 sats, roughly 45 operator pairs ×
3 sats/pair × 16 contacts/day ≈ 2160 contacts/day, ~0.025 TPS. Still well within limits.

---

## 11. Phase-by-Phase Implementation Plan

### Phase 1 — Foundation (no external dependencies)
*Goal: all new code scaffolded; existing pipeline unmodified.*
*Estimated time: 3–4 days.*

#### 1.1 `lios/evaluation/baselines.py` (NEW)

```
Create lios/evaluation/baselines.py with:
  - GreedySatelliteNode(SatelliteNode)
      Override _on_isl_close → return []  (no settlement)
      Override get_pending_settlement_payloads → return []

  - TitForTatNode(SatelliteNode)
      Add _contact_fwd: Dict[str, float], _contact_rcv: Dict[str, float]
      Override _on_isl_open → reset per-contact counters
      Override _on_traffic_arrive → choke if deficit > threshold
      Override _on_isl_close → clear counters; no settlement

  - PayWordSettlement (not a SatelliteNode subclass)
      Replaces FabricMock; DirectSettlementStub
      initiate_settlement → store proof; notify_peer immediately
      finalize_settlement → immediate (no challenge window)

  - CollusionGroup
      Shared false-proof registry for two MaliciousNodes

Dependencies: SatelliteNode, OffChainProtocol, FabricMock
Tests to add: tests/test_baselines.py — verify GreedyNode never triggers settlement;
              TitForTatNode refuses to forward beyond deficit threshold.
```

#### 1.2 `lios/evaluation/metrics.py` — extend `MetricsCollector`

```
Add to MetricsCollector.__init__:
  self._protocol_bytes_total: int = 0
  self._payload_kb_total: float = 0.0
  self._wait_for_gs_sec: List[float] = []
  self._peer_wait_for_gs_sec: List[float] = []
  self._contact_fwd_by: Dict[Tuple[str,str], float] = {}   # (op_a, op_b) → kb
  self._trigger_counts: Dict[str, int] = {}
  self._settlement_bytes_uploaded: int = 0
  self._total_uplink_bytes: int = 0
  self._attack_attempts: int = 0
  self._backlog_snapshots: List[Tuple[float, int]] = []

Add methods:
  record_protocol_message(msg_type, size_bytes, t)
  record_gs_gap(wait_sec: float, is_peer: bool)
  record_trigger(trigger: str, channel_id: str, t: float)
  record_uplink(settlement_bytes: int, total_bytes: int)
  record_attack_attempt()
  record_backlog_snapshot(t: float, depth: int)
  compute_overhead_ratio() → float
  compute_imbalance_gini() → float
  compute_utilisation_efficiency(total_isl_contact_kb: float) → float
  compute_gs_gap_stats() → dict
  compute_penalty_to_attack_ratio() → float
  compute_max_backlog() → int

Extend generate_report() to include all new metrics.

Tests: tests/test_metrics_extended.py
```

#### 1.3 `lios/evaluation/benchmarks.py` (NEW)

```
Create lios/evaluation/benchmarks.py:
  Benchmark harness using pytest-benchmark for:
    - ECDSA P-256 sign (1 KB payload): target < 1 ms
    - ECDSA P-256 verify: target < 2 ms
    - AES-256-GCM encrypt 1 KB: target < 0.1 ms
    - SHA-256 hash (256 bytes): target < 0.01 ms
    - BalanceProof serialise/deserialise round-trip

  Run with: pytest lios/evaluation/benchmarks.py --benchmark-only

  Output: JSON report consumed by paper table generator.
```

#### 1.4 GS configuration data

```
Create lios/data/gss_configs/gs_2_antipodal/{alpha,beta,gamma}.txt
Create lios/data/gss_configs/gs_3_mixed/{alpha,beta,gamma}.txt
Create lios/data/gss_configs/gs_5_global/{alpha,beta,gamma}.txt
Create lios/data/gss_configs/gs_10_global/{alpha,beta,gamma}.txt

Format: same CSV as existing lios/data/gss/*.txt
  <operator>-<name>,<lat_deg>,<lon_deg>

Placement strategy (globally distributed):
  N=2: original site + antipodal (lon + 180°)
  N=3: original + equatorial east + polar (lat 78°)
  N=5: evenly spaced in longitude (72° apart)
  N=10: 36° longitude spacing

Add GSLoader.load_config(data_dir, gs_config_name) → Dict[str, List[GroundStation]]
```

---

### Phase 2 — Sweep Infrastructure
*Goal: single entry-point to run any sweep; experiment configs for all comparisons.*
*Estimated time: 3–4 days.*
*Requires: Phase 1 complete.*

#### 2.1 `lios/evaluation/sweep.py` (NEW)

```
Create lios/evaluation/sweep.py:

  @dataclass
  class SweepAxis:
      name: str
      param: str               # config field to vary
      values: List[Any]

  def run_sweep(
      base_config: ExperimentConfig,
      axes: List[SweepAxis],
      data_dir: Path,
      out_dir: Path,
      baseline_protocol: str = "lios",    # 'lios' | 'greedy' | 't4t' | 'payword' | 'htlc' | 'central'
      gs_config: str = "gs_1_midlat",
  ) -> pd.DataFrame:
      """Grid sweep over one or two axes. Returns tidy DataFrame for plotting."""
      ...

  Predefined sweeps (callable via CLI: python sweep.py --sweep <name>):
    T_CHALLENGE_GS_GRID   → axes=[t_challenge_sec, gs_count]
    LOAD_SWEEP            → axes=[traffic_load_fraction]
    SCALABILITY_OPS       → axes=[n_operators]
    SCALABILITY_SATS      → axes=[n_sats_per_op]
    H_MAX_GS_GRID         → axes=[h_max, gs_count]
    RESERVE_SWEEP         → axes=[reserve_fraction]
    T_LOW_SWEEP           → axes=[t_low_fraction]
    BASELINE_COMPARISON   → axes=[protocol] over all 5 baselines
    OUTAGE_SWEEP          → axes=[T_outage_sec]
    GRINDING_SWEEP        → axes=[p_attack_grinding, T_settle_cooldown]
```

#### 2.2 Extend `lios/evaluation/adversarial.py`

```
Add to MaliciousSatelliteNode:
  attack_mode options: 'rollback' | 'selective_forward' | 'collusion' | 'grinding' | 'gs_delay'

  For 'collusion':
    __init__ takes collusion_group: CollusionGroup
    Override get_pending_settlement_payloads → coordinate stale proof via collusion_group

  For 'grinding':
    Override evaluate_settlement_triggers → return ['T5'] with probability p_attack

  For 'gs_delay' (applied to GroundStationNode, not SatelliteNode):
    GroundStationNode.set_offline_window(t_start, t_end)
    handle_event → drop all events during offline window

Add: run_adversarial_scenario supports new modes.
```

#### 2.3 Extend `lios/simulator/ground_station_node.py`

```
Add to GroundStationNode:
  _offline_start: float = -1.0
  _offline_end: float = -1.0
  _contact_end_times: Dict[str, float] = {}   # sat_id → contact end time
  _settlement_bytes_uploaded: int = 0
  _pending_settlement_carry: Dict[str, SettlementPayload] = {}  # deferred settlements

  set_offline_window(t_start, t_end)
  _model_upload_duration(payload: SettlementPayload) → float
  _contact_end_time(sat_id: str) → float    # from contact plan lookup

Modify handle_event: return [] if in offline window.

Modify _on_settlement_upload:
  compute upload_sec = _model_upload_duration(payload)
  check upload_sec < remaining_contact_window
  if exceeds: defer; log SETTLEMENT_DEFERRED
  else: schedule SETTLEMENT_UPLOAD_COMPLETE at t + upload_sec

Add EventType.SETTLEMENT_UPLOAD_COMPLETE to simulator.py
```

#### 2.4 Extend `lios/evaluation/run_experiments.py`

```
Add ExperimentConfig fields:
  gs_config: str = "gs_1_midlat"           # GS deployment config
  baseline_protocol: str = "lios"           # which protocol to instantiate
  n_operators: int = 3                      # for scalability sweep
  n_sats_per_op: int = 1                    # for scalability sweep
  T_settle_cooldown: float = 0.0            # grinding mitigation
  gs_outage_op: str = ""                    # operator to make GS offline
  gs_outage_t_start: float = 0.0
  gs_outage_t_end: float = 0.0
  enable_federation: bool = False           # cross-operator GS sharing

Modify _instantiate_satellite_nodes() to select node class based on baseline_protocol.
Modify _build_ground_stations() to load GS data from the named gs_config.
Add _generate_synthetic_tles(n_ops, sats_per_op) for scalability sweep.
```

---

### Phase 3 — Ground Contact Overhead Module
*Goal: complete Part G modelling.*
*Estimated time: 2–3 days.*
*Requires: Phase 2 complete.*

#### 3.1 `lios/evaluation/ground_overhead.py` (NEW)

```
Create lios/evaluation/ground_overhead.py:

  def compute_gs_gap_cdf(settlement_log_path: Path) -> Dict[str, List[float]]:
      """Read settlement log; return {gs_config: sorted_wait_for_gs_sec_list}."""

  def compute_t_challenge_required(
      gs_gap_p99_sec: float,
      fabric_commit_sec: float = 2.0,
      safety_margin_sec: float = 300.0
  ) -> float:
      return 2 * gs_gap_p99_sec + fabric_commit_sec + safety_margin_sec

  def run_gs_scarcity_sweep(
      base_config: ExperimentConfig,
      gs_configs: List[str],
      out_dir: Path,
  ) -> pd.DataFrame:
      """Run baseline experiment under each GS configuration.
      Returns tidy DataFrame with columns:
        gs_config, wait_p50, wait_p95, wait_p99, wait_max,
        peer_wait_p99, e2e_latency_p95, oos_fraction, t_challenge_min.
      """

  def run_outage_sweep(
      base_config: ExperimentConfig,
      outage_durations_sec: List[float],
      gs_config: str,
      adv_mode: str,
      out_dir: Path,
  ) -> pd.DataFrame:
      """Cross (T_outage × T_challenge) grid. Records challenge_success_rate."""
```

#### 3.2 `lios/evaluation/figures.py` — extended figure set (NEW)

```
Create lios/evaluation/figures.py:

  plot_gs_gap_cdf(data: Dict[str, List[float]], out_path)
      Overlay CDF curves for each GS configuration.
      Mark T_challenge/2 as a vertical reference line.

  plot_latency_decomposition(results: List[ExperimentResult], out_path)
      Stacked bar: isl_prop + wait_gs + uplink_prop + fabric + peer_wait + downlink
      One bar per GS configuration; dominance of wait_gs visible immediately.

  plot_t_challenge_gs_heatmap(df: pd.DataFrame, out_path)
      5×4 heatmap: challenge_success_rate over (t_challenge × gs_count).
      Overlay the computed T_challenge_min contour from G.3.

  plot_uplink_budget(df: pd.DataFrame, out_path)
      Bar chart: uplink_overhead_fraction per (H_max, GS_count) pair.
      Mark threshold where upload_sec > min_contact_window.

  plot_outage_backlog(df: pd.DataFrame, out_path)
      Line chart: max backlog depth vs. T_outage.
      Annotate T_challenge boundary.

  plot_baseline_comparison(results: Dict[str, ExperimentResult], out_path)
      Grouped bars: Jain | OOS | settlement_latency_p95 | overhead_ratio
      One group per protocol (greedy, central, t4t, payword, htlc, lios).
```

---

### Phase 4 — Scalability and Parameter Sweeps
*Goal: all scalability and sensitivity experiments runnable.*
*Estimated time: 2–3 days.*
*Requires: Phase 2 complete.*

#### 4.1 Synthetic TLE and GS Generator

```
Add lios/evaluation/constellation_generator.py:

  def generate_walker_delta(
      n_planes: int,
      sats_per_plane: int,
      inclination_deg: float,
      altitude_km: float,
      epoch: datetime,
  ) -> List[Tuple[str, str]]:
      """Generate (sat_name, tle_block) tuples for a Walker Delta constellation.
      Uses SGP4 mean elements; varies RAAN and mean anomaly evenly."""

  def generate_gs_ring(
      n_stations: int,
      operator_id: str,
      lat_deg: float = 0.0,
  ) -> List[str]:
      """Generate N GS evenly spaced in longitude at the given latitude.
      Returns list of CSV lines for gss/*.txt."""

  write_operator_tles(operator_id, tles, data_dir)
  write_operator_gss(operator_id, gss, data_dir)
```

#### 4.2 Scalability Experiment Configs

```
Add to run_experiments.py SCALABILITY_CONFIGS:

  [ExperimentConfig(f"scale_ops_{n}", n_operators=n, n_sats_per_op=1, ...)
   for n in [2, 3, 5, 7, 10]]

  [ExperimentConfig(f"scale_sats_{n}", n_operators=3, n_sats_per_op=n, ...)
   for n in [1, 3, 5, 10, 20]]
```

#### 4.3 Parameter Sweep CLI

```
Extend sweep.py CLI:
  python sweep.py --sweep T_CHALLENGE_GS_GRID --out results/sweeps/
  python sweep.py --sweep LOAD_SWEEP --out results/sweeps/
  python sweep.py --sweep H_MAX_GS_GRID --out results/sweeps/
  python sweep.py --sweep RESERVE_SWEEP --out results/sweeps/
  python sweep.py --sweep BASELINE_COMPARISON --out results/sweeps/
  python sweep.py --sweep SCALABILITY_OPS --out results/sweeps/
```

---

### Phase 5 — Figure Generation and Paper Tables
*Goal: publication-quality figures for all evaluation parts.*
*Estimated time: 2 days.*
*Requires: Phases 3 and 4 complete.*

#### 5.1 Paper Figure Inventory

| Fig # | Description | Source data | Generator |
|-------|-------------|-------------|-----------|
| 1 | Jain fairness by config (existing) | `baseline_metrics.json` | `run_experiments.py` |
| 2 | Settlement latency CDF (existing) | `*_settlement_log.json` | `run_experiments.py` |
| 3 | Penalty events by config (existing) | metrics | `run_experiments.py` |
| 4 | Balance evolution baseline (existing) | metrics | `run_experiments.py` |
| 5 | Throughput vs. Fairness scatter (existing) | all configs | `run_experiments.py` |
| 6 | **Baseline comparison** grouped bars | sweep results | `figures.py` |
| 7 | **GS contact gap CDF** per GS config | ground overhead sweep | `figures.py` |
| 8 | **Latency decomposition** stacked bar | GS sweep | `figures.py` |
| 9 | **T_challenge × GS count heatmap** | param sweep | `figures.py` |
| 10 | **Uplink budget** bar chart | H_max × GS sweep | `figures.py` |
| 11 | **Scalability** Jain vs. N_ops/N_sats | scalability sweep | `figures.py` |
| 12 | **Throughput vs. Fairness curve** (continuous) | load sweep | `figures.py` |

#### 5.2 LaTeX Table Generator

Extend `generate_latex_table` in `run_experiments.py` to produce:

**Table 1** — Existing 7-config correctness results (already generates).

**Table 2** — Baseline protocol comparison:
```
Protocol | Jain | OOS(%) | Settlement lat. p95(s) | Overhead ratio | Disputes resolved
```

**Table 3** — Scalability summary:
```
Scale | Jain | OOS(%) | Fabric TPS | Wall time(s)
```

**Table 4** — Ground contact overhead:
```
GS/op | wait_p95(s) | peer_wait_p99(s) | e2e_p95(s) | T_challenge_min(s)
```

**Table 5** — Parameter sensitivity (T_challenge): rows = T_challenge, cols = GS count,
cells = challenge success rate.

---

### Phase 6 — Real Fabric Experiments
*Goal: replace mock latency assumption with measured values.*
*Estimated time: 1–2 days.*
*Requires: Docker, Fablo, fabric-gateway, grpcio installed.*

```bash
# Start the network
cd lios/fablo && fablo up fablo-config.json

# Run the blockchain start script (registers keys, opens channels)
bash lios/evaluation/start_network.sh

# Run baseline with real Fabric
cd lios && python evaluation/run_experiments.py \
    --config baseline --out results/blockchain/ --blockchain

# Run high-density stress test
python evaluation/run_experiments.py \
    --config high_density --out results/blockchain/ --blockchain
```

Patch `FabricMock` with the measured median commit latency after this phase to make
all simulated results consistent with real Fabric timing.

---

### Implementation Dependency Graph

```
Phase 1 ──────────────────────────────────────────────┐
  baselines.py                                         │
  metrics.py (extended)                                │
  benchmarks.py                                        │
  GS config data                                       │
        │                                              │
        ▼                                              │
Phase 2 ────────────────────────────────────────────── │
  sweep.py                                             │
  adversarial.py (extended)                            │
  ground_station_node.py (bandwidth model)             │
  run_experiments.py (extended)                        │
        │                                              │
        ├──────────────────┐                           │
        ▼                  ▼                           │
Phase 3                Phase 4                         │
  ground_overhead.py   constellation_generator.py      │
  figures.py           scalability configs             │
        │                  │                           │
        └──────────┬────────┘                          │
                   ▼                                   │
              Phase 5                                  │
              Paper figures + LaTeX tables             │
                   │                                   │
                   ▼                                   │
              Phase 6                                  │
              Real Fabric experiments                  │
                   │                                   │
              ──────────────────────────────────────── │
              PAPER SUBMISSION READY                   │
```

---

## 12. Paper Section Mapping

| Evaluation Part | Paper Section | Primary claim | Key figure |
|----------------|---------------|---------------|------------|
| Current 7 configs | §Correctness | LIOS achieves J ≥ 0.95, OOS < 2%, 100% attack detection | Fig 1–5 |
| Part A (baselines) | §Comparative Evaluation | LIOS uniquely combines fairness + dispute resolution without a TTP | Fig 6 (grouped bars) |
| Part G (GS scarcity) | §Ground Contact Overhead | GS gap is the dominant latency term; T_challenge ≥ 2×G(n) sizing rule | Fig 7–10 |
| Part D (scalability) | §Scalability | Jain stable with N_ops; Fabric not a bottleneck at 10 ops | Fig 11 |
| Part F.3 (load sweep) | §Throughput-Fairness | Fairness degrades gracefully; floor at 0.92 at 99% load | Fig 12 |
| Part C (adversarial+) | §Security | 100% individual attack detection; collusion detectable via hash chain | Fig 3 (extended) |
| Part E.1 (T_challenge) | §Parameter Analysis | T_challenge_min derived empirically matches G(n) bound | Fig 9 (heatmap) |
| Part H (Fabric) | §Implementation | Mock latency assumptions are conservative; Fabric not a bottleneck | Table in §H |

---

## 13. Master Experiment Table

| Config ID | Protocol | Duration | Load | Adv. mode | GS/op | N_ops | N_sats/op | New in eval |
|-----------|----------|----------|------|-----------|-------|-------|-----------|-------------|
| baseline | LIOS | 1h | 0.50 | none | 1 | 3 | 1 | — |
| depletion | LIOS | 1.5h | 0.95 | none | 1 | 3 | 1 | — |
| top_up | LIOS | 24h | 0.80 | none | 1 | 3 | 1 | — |
| adversarial_1 | LIOS | 24h | 0.70 | rollback | 1 | 3 | 1 | — |
| adversarial_2 | LIOS | 24h | 0.70 | sel_fwd | 1 | 3 | 1 | — |
| fairness_24h | LIOS | 24h | 0.60 | none | 1 | 3 | 1 | — |
| high_density | LIOS | 1.5h | 0.99 | none | 1 | 3 | 1 | — |
| cmp_greedy | Greedy | 24h | 0.60 | none | 1 | 3 | 1 | **A.1** |
| cmp_central_1h | Central | 24h | 0.60 | none | 1 | 3 | 1 | **A.2** |
| cmp_central_24h | Central | 24h | 0.60 | none | 1 | 3 | 1 | **A.2** |
| cmp_t4t | T4T | 24h | 0.60 | none | 1 | 3 | 1 | **A.3** |
| cmp_payword | PayWord | 24h | 0.60 | none | 1 | 3 | 1 | **A.4** |
| cmp_htlc | HTLC | 24h | 0.60 | none | 1 | 3 | 1 | **A.5** |
| adv_collusion | LIOS | 24h | 0.70 | collusion | 1 | 3 | 1 | **C.1** |
| adv_grinding_* | LIOS | 24h | 0.60 | grinding | 1 | 3 | 1 | **C.2** |
| adv_gs_delay_* | LIOS | 24h | 0.70 | rollback | 1 | 3 | 1 | **C.3** |
| adv_key_revoke | LIOS | 24h | 0.60 | key_revoke| 1 | 3 | 1 | **C.4** |
| scale_ops_* | LIOS | 24h | 0.60 | none | 1 | 2–10 | 1 | **D.1** |
| scale_sats_* | LIOS | 24h | 0.60 | none | 1 | 3 | 1–20 | **D.2** |
| load_sweep_* | LIOS | 24h | 0.1–0.99 | none | 1 | 3 | 1 | **D.3** |
| gs_scarcity_* | LIOS | 24h | 0.60 | none | 1–10 | 3 | 1 | **G.1** |
| gs_outage_* | LIOS | 24h | 0.80 | none | 1 | 3 | 1 | **G.5** |
| gs_federation | LIOS | 24h | 0.60 | none | 1 + fed | 3 | 1 | **G.6** |
| gs_htlc_sparse | HTLC | 24h | 0.60 | none | 1 | 3 | 1 | **A.5+G** |
| param_tchallenge_* | LIOS | 24h | 0.70 | rollback | 1–5 | 3 | 1 | **E.1** |
| param_tlow_* | LIOS | 1h | 0.50 | none | 1 | 3 | 1 | **E.2** |
| param_hmax_* | LIOS | 24h | 0.60 | none | 1–5 | 3 | 1 | **E.3** |
| param_reserve_* | LIOS | 24h | 0.70 | rollback | 1 | 3 | 1 | **E.4** |
| topo_walker_delta | LIOS | 24h | 0.60 | none | 1 | 3 | 3 | **F.1** |
| topo_walker_star | LIOS | 24h | 0.60 | none | 1 | 3 | 3 | **F.1** |
| traffic_pareto | LIOS | 24h | 0.60 | none | 1 | 3 | 1 | **F.3** |
| traffic_asymmetric | LIOS | 24h | 0.60 | none | 1 | 3 | 1 | **F.4** |
| traffic_diurnal | LIOS | 24h | variable | none | 1 | 3 | 1 | **F.5** |
| blockchain_baseline | LIOS | 1h | 0.50 | none | 1 | 3 | 1 | **H.1** |
| blockchain_stress | LIOS | 1.5h | 0.99 | none | 1 | 3 | 1 | **H.2** |

---

*End of evaluation plan.*

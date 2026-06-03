# LIOS Protocol — Lightweight Inter-operator Orbital Sharing Protocol: Blockchain-Based Fair Traffic Sharing for Inter-Satellite Links Across Multi-Operator LEO Constellations

> **A Comprehensive Research Plan with Implementation Roadmap**

---

## Table of Contents

1. [Abstract](#1-abstract)
2. [Research Motivation and Problem Statement](#2-research-motivation-and-problem-statement)
3. [Research Objectives](#3-research-objectives)
4. [State-of-the-Art Review](#4-state-of-the-art-review)
5. [System Architecture Overview](#5-system-architecture-overview)
6. [Threat Model and Trust Assumptions](#6-threat-model-and-trust-assumptions)
7. [Cryptographic Protocol Design (Off-Chain)](#7-cryptographic-protocol-design-off-chain)
8. [Hyperledger Fabric Smart Contract Design](#8-hyperledger-fabric-smart-contract-design)
9. [Operator-Level Channel Establishment](#9-operator-level-channel-establishment)
10. [Satellite Authentication via Operator Key Hierarchy](#10-satellite-authentication-via-operator-key-hierarchy)
11. [Ground Settlement Protocol](#11-ground-settlement-protocol)
12. [ISL Pause/Resume and Out-of-Service Logging](#12-isl-pauseresume-and-out-of-service-logging)
13. [Ground Node Notification Mechanism](#13-ground-node-notification-mechanism)
14. [Discrete LEO Emulation Framework](#14-discrete-leo-emulation-framework)
15. [Contact Plan, Traffic Generation, and Routing](#15-contact-plan-traffic-generation-and-routing)
16. [Experimental Design and Evaluation Metrics](#16-experimental-design-and-evaluation-metrics)
17. [Implementation Roadmap and Milestones](#17-implementation-roadmap-and-milestones)
18. [GitHub Copilot Prompts for Implementation](#18-github-copilot-prompts-for-implementation)
19. [Directory Structure](#19-directory-structure)
20. [References](#20-references)

---

## 1. Abstract

Low Earth Orbit (LEO) satellite constellations from competing commercial operators increasingly co-exist and must share inter-satellite link (ISL) capacity to provide seamless global connectivity. Without a fair, verifiable settlement mechanism, rational operators face strong incentives to free-ride — forwarding traffic from peers while minimizing their own forwarding obligations. This research proposes **LIOS Protocol** (Lightweight Inter-operator Orbital Sharing Protocol), a decentralized, blockchain-based ISL traffic-sharing framework that:

- Enables **off-chain, real-time bilateral traffic accounting** between pairs of satellites using a cryptographic payment-channel protocol inspired by the Bitcoin Lightning Network, adapted for the delay-tolerant, intermittently-connected LEO environment.
- Anchors **on-chain settlement** on a permissioned **Hyperledger Fabric** blockchain operated by ground stations, preserving auditability, non-repudiation, and penalty enforcement.
- Employs a **two-tier key hierarchy** (operator keys → satellite keys) so that peers need only store a small set of operator public keys rather than the full satellite fleet roster.
- Defines **settlement triggers** (channel depletion, storage overflow, scheduled ground contact) and a **mutual-pause/resume** protocol for ISL during contested settlement windows.
- Supports **multi-hop forwarding** across satellites from different operators, with ground nodes as traffic sources and destinations.

A discrete-event emulator for LEO networks with synthetic traffic, contact-plan-based routing, and full Hyperledger Fabric integration is developed as a research testbed.

---

## 2. Research Motivation and Problem Statement

### 2.1 The Multi-Operator LEO Landscape

The number of LEO satellites in orbit has surpassed 10,000 as of 2025, with constellations operated by SpaceX (Starlink), Amazon (Kuiper), Eutelsat (OneWeb), Telesat, AST SpaceMobile, and many national programs. ISLs provide latency advantages over terrestrial fibre for long-haul routes and enable connectivity in oceanic and polar regions. However, ISL capacity is finite. When two satellites from different operators are within link range, there is mutual benefit in forwarding each other's traffic — but only if the arrangement is fair.

### 2.2 The Free-Rider Problem

In the absence of an enforcement mechanism, a selfish operator will:
- Accept traffic forwarded by peers to its satellites.
- Throttle or drop peer traffic, claiming link congestion.
- Dispute historical forwarding records to avoid payment.

Classical economic theory predicts that without a credible enforcement mechanism, ISL cooperation will collapse to the Nash equilibrium of non-cooperation, eliminating potential welfare gains. This is an instance of the classic **free-rider problem** in commons management.

### 2.3 Why Blockchain?

Inter-operator settlement currently relies on bilateral SLA contracts adjudicated through arbitration — a slow, costly, and trust-intensive process. Blockchain provides:

| Property | Role in LIOS Protocol |
|---|---|
| Immutability | Historical traffic records cannot be altered retroactively |
| Smart contracts | Deterministic, code-enforced penalty and payment logic |
| Decentralisation | No single operator controls the ledger |
| Permissioned access | Only authorised operators participate (Hyperledger Fabric) |
| Auditability | Regulators and arbiters can inspect on-chain history |

### 2.4 Why Off-Chain Channels?

Satellites have intermittent ground connectivity. Submitting every forwarded packet as a blockchain transaction is infeasible. Payment channels (Lightning Network paradigm) allow satellites to exchange signed balance-update messages in real time during an ISL contact, committing only aggregated state to the blockchain when a ground contact occurs.

### 2.5 Research Gap

No prior work combines:
1. Payment-channel protocols for satellite-to-satellite traffic settlement.
2. A two-tier key hierarchy suited for large fleets with limited on-board key storage.
3. A ground-contact-triggered settlement state machine with mutual ISL pause.
4. Multi-hop routing over a contact-plan-based LEO network with multi-operator path diversity.
5. A complete emulation testbed validating all of the above.

---

## 3. Research Objectives

| ID | Objective |
|---|---|
| O1 | Design a cryptographic off-chain protocol for bilateral ISL traffic accounting |
| O2 | Design a Hyperledger Fabric smart contract for channel lifecycle and dispute resolution |
| O3 | Define operator-level channel establishment with penalty reserves |
| O4 | Design a satellite authentication mechanism using a two-tier key hierarchy |
| O5 | Define ground settlement triggers, mutual ISL pause/resume, and out-of-service logging |
| O6 | Design a ground node notification mechanism for post-settlement updates |
| O7 | Implement a discrete LEO emulation framework with synthetic traffic |
| O8 | Implement contact-plan-based shortest-path routing with multi-operator multi-hop paths |
| O9 | Evaluate fairness, overhead, latency, and resilience against adversarial operators |

---

## 4. State-of-the-Art Review

### 4.1 Payment Channel Networks

The Bitcoin Lightning Network (Poon & Dryja, 2016) introduced bidirectional payment channels secured by hash-time-locked contracts (HTLCs). Subsequent work generalized this to state channels (Miller et al., 2017) and identified the capital efficiency cost. The key difference in LIOS Protocol is that:

- Channels are **traffic-denominated** (megabytes or packets) rather than monetary.
- The "closing" event is not user-initiated but **event-driven** (contact plan, storage limits).
- The **revocation** mechanism uses a hash chain for auditability, not just revocation keys.

### 4.2 Blockchain for Satellite Networks

Recent work (Bera et al., 2021; Qian et al., 2022) has explored blockchain for satellite authentication and data provenance but has not addressed inter-operator ISL payment channels. Delay-tolerant blockchain synchronization for satellite IoT has been explored (Li et al., 2023) but focuses on sensor data integrity, not traffic settlement.

### 4.3 LEO Contact Plans and Routing

Contact Graph Routing (CGR) (Burleigh, 2011) is the standard algorithm for Delay-Tolerant Networks (DTN) including LEO constellations. The Contact Plan — a schedule of predicted link availability between nodes — drives routing decisions. Shortest-path computation over the contact graph yields minimum-latency routes. State-of-the-art implementations include ION-DTN and NASA's implementation aboard deep-space probes. For multi-operator constellations, CGR must account for operator-policy constraints.

### 4.4 Hyperledger Fabric for Multi-Party Settlement

Hyperledger Fabric's permissioned architecture, with MSP (Membership Service Provider) roles, channel-level privacy, and endorsement policies, is well suited for an oligopolistic operator consortium where all parties are known but mutually distrusting. Prior work on trade finance (Marco Polo, Contour) demonstrates viability for multi-party bilateral settlement on Fabric.

### 4.5 Key Hierarchy in Satellite Systems

CCSDS Security Working Group defines a hierarchical key management approach for space missions. LIOS Protocol adapts this: operator root keys (offline, HSM-stored) sign satellite operational keys (rotated per mission phase), yielding a certificate chain that enables any operator's satellite to verify any peer's satellite without storing all peer satellite keys.

---

## 5. System Architecture Overview

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                          SPACE SEGMENT                                      │
│                                                                             │
│  Operator A Satellites          Operator B Satellites                       │
│  ┌──────────┐                   ┌──────────┐                                │
│  │  Sat A1  │◄── ISL Contact ──►│  Sat B3  │                               │
│  │          │  Off-chain proto  │          │                                │
│  │BalanceDB │  signed messages  │BalanceDB │                                │
│  │HashChain │                   │HashChain │                                │
│  └────┬─────┘                   └─────┬────┘                               │
│       │ Ground                        │ Ground                              │
│       │ Contact                       │ Contact                             │
└───────┼───────────────────────────────┼─────────────────────────────────────┘
        │                               │
┌───────┼───────────────────────────────┼─────────────────────────────────────┐
│       │      GROUND SEGMENT           │                                      │
│  ┌────▼──────────────┐    ┌───────────▼────────────┐                        │
│  │  Ground Station A │    │  Ground Station B      │                        │
│  │  (Fabric Peer)    │    │  (Fabric Peer)         │                        │
│  │                   │    │                        │                        │
│  │ - Receive TX log  │    │ - Receive TX log       │                        │
│  │ - Submit chaincode│    │ - Submit chaincode     │                        │
│  │ - Notify sats     │    │ - Notify sats          │                        │
│  └────────┬──────────┘    └────────────┬───────────┘                        │
│           │                            │                                    │
│  ┌────────▼────────────────────────────▼───────────┐                        │
│  │            Hyperledger Fabric Network            │                        │
│  │   ┌─────────┐  ┌─────────┐  ┌─────────────┐    │                        │
│  │   │Orderer 1│  │Orderer 2│  │Orderer 3    │    │                        │
│  │   └─────────┘  └─────────┘  └─────────────┘    │                        │
│  │                                                  │                        │
│  │   Chaincode: ChannelManager / DisputeResolver   │                        │
│  └──────────────────────────────────────────────────┘                       │
│                                                                             │
│  Ground Traffic Sources/Sinks (non-GS nodes)                               │
│  [Generate synthetic traffic destined for satellite paths]                  │
└─────────────────────────────────────────────────────────────────────────────┘
```

### 5.1 Component Summary

| Component | Role |
|---|---|
| Satellite node | Runs off-chain payment-channel protocol, maintains balance state, hash chain, signs balance proofs |
| Ground Station (GS) | Fabric peer node, receives TX logs from satellites on contact, submits/queries chaincode, notifies satellites of updates |
| Hyperledger Fabric | Permissioned ledger for channel lifecycle: open, top-up, settle, dispute, penalty |
| Contact Plan | Pre-computed schedule of ISL and satellite-GS contacts used for routing and settlement scheduling |
| Traffic Source (ground) | Generates synthetic traffic flows; sources and sinks are ground nodes; paths traverse satellite ISLs |

---

## 6. Threat Model and Trust Assumptions

### 6.1 Participants

- **Operators** (N ≤ 20): legally registered, financially accountable entities. Each operator runs one or more Fabric peer nodes and a fleet of satellites.
- **Satellites**: semi-trusted compute nodes owned by operators. A malicious operator may instruct its satellites to behave adversarially.
- **Ground Stations**: trusted nodes within their operator's administrative domain; assumed honest (but verifiable on-chain).

### 6.2 Adversarial Behaviours Considered

| Attack | Description | Mitigation |
|---|---|---|
| **Balance rollback** | Malicious satellite publishes an outdated (higher self-balance) balance proof | Hash-chain revocation; counterparty presents newer proof; penalty enforced |
| **Traffic inflation** | Satellite claims to have forwarded more traffic than it did | Signed receipts from receiving satellite required in balance proof |
| **Selective forwarding** | Satellite drops peer traffic while recording it as forwarded | Acknowledgement-based accounting; end-to-end verification via source ground node |
| **Sybil operator** | New operator creates fake satellites to drain penalty reserves | Operator identity verified by consortium MSP; limited operator count |
| **Settlement racing** | Operator submits settlement just before ground contact of counterpart | Settlement challenge window (T_challenge) enforced in smart contract |
| **Key compromise** | Satellite key is stolen | Short-lived satellite keys; revocation via operator root key on-chain |
| **Replay attack** | Old signed message replayed | Monotonically increasing sequence numbers in each balance update |

### 6.3 Trust Assumptions

- Operator root keys are held in HSMs and are not compromised.
- The Fabric ordering service is operated by a quorum of operators and is BFT-tolerant.
- Ground station hardware is physically secured by the operator.

---

## 7. Cryptographic Protocol Design (Off-Chain)

### 7.1 Channel State

Each bilateral ISL channel between satellites S_i (Operator A) and S_j (Operator B) is defined by a **channel state tuple**:

```
σ = (channel_id, seq_num, balance_A, balance_B, hash_A, hash_B, timestamp)
```

Where:
- `channel_id`: derived from the operator-level channel ID and the satellite pair identifiers.
- `seq_num`: monotonically increasing counter; incremented on every balance update.
- `balance_A` / `balance_B`: remaining forwarding quota (in KB) each satellite may demand from the other.
- `hash_A` / `hash_B`: the current head of each satellite's hash chain (for auditability).
- `timestamp`: GPS time of last update.

### 7.2 Hash Chain for Auditability

Each satellite independently maintains a **hash chain** over its forwarding history:

```
H_0 = Hash(channel_id || operator_id || satellite_id || nonce_0)
H_n = Hash(H_{n-1} || seq_num_n || bytes_forwarded_n || timestamp_n || sig_peer_n)
```

The chain head `H_n` is included in every balance proof. This means:

1. The full forwarding history can be reconstructed from the satellite's local log.
2. Any modification of a historical record invalidates all subsequent chain entries — detectable by the counterparty or the smart contract.
3. The smart contract does not need to store the full history (storage efficiency); it only verifies chain-head consistency on settlement.

### 7.3 Balance Proof (BalProof)

A **BalProof** is the signed assertion of the current channel state:

```
BalProof = {
    channel_id,
    seq_num,
    balance_A,        // KB remaining for A to receive from B
    balance_B,        // KB remaining for B to receive from A
    hash_chain_head_A,
    hash_chain_head_B,
    sig_A,            // ECDSA over (channel_id || seq_num || balance_A || balance_B || hash_A || hash_B)
    sig_B
}
```

Both signatures are required for a valid BalProof. This prevents either party from unilaterally publishing a fabricated state.

### 7.4 Off-Chain Protocol State Machine

```
         ┌─────────────────────────────────────────────────┐
         │              ISL CONTACT BEGINS                  │
         └──────────────────────┬──────────────────────────┘
                                │
                    ┌───────────▼────────────┐
                    │  PHASE 1: HELLO        │
                    │  Exchange:             │
                    │  - Satellite identity  │
                    │  - Operator cert       │
                    │  - Last known seq_num  │
                    │  - Last BalProof       │
                    └───────────┬────────────┘
                                │
                    ┌───────────▼────────────┐
                    │  PHASE 2: SYNC         │
                    │  - Verify peer cert    │
                    │  - Verify BalProof sig │
                    │  - Agree on latest     │
                    │    seq_num             │
                    └───────────┬────────────┘
                                │
                    ┌───────────▼────────────┐
                    │  PHASE 3: FORWARD      │◄──────────┐
                    │  - Exchange data       │           │
                    │  - Increment balance   │           │  Repeat per
                    │  - Update hash chain   │           │  forwarding
                    │  - Sign new BalProof   │           │  batch
                    └───────────┬────────────┘           │
                                │                        │
                    ┌───────────▼────────────┐           │
                    │  PHASE 4: COMMIT       │           │
                    │  - Both sign BalProof  │───────────┘
                    │  - Store locally       │
                    └───────────┬────────────┘
                                │
                    ┌───────────▼────────────┐
                    │  CONTACT END / TRIGGER │
                    │  Settlement trigger?   │
                    │  (see §11)             │
                    └────────────────────────┘
```

### 7.5 Revocation Mechanism (Anti-Rollback)

When a new BalProof (seq=n+1) is co-signed, the previous BalProof (seq=n) is **implicitly revoked**. If a malicious satellite later submits seq=n to the smart contract, the counterparty has a **challenge window** `T_challenge` to submit seq=n+1. The smart contract:

1. Verifies that seq=n+1 > seq=n.
2. Verifies both signatures.
3. Awards the **penalty reserve** to the honest party.
4. Uses the newer BalProof for final settlement.

### 7.6 Penalty Reserve

During operator-level channel establishment (§9), both operators lock a **penalty reserve** `P` on-chain. This reserve is slashed if a party submits a revoked BalProof and loses the challenge. The penalty amount must be large enough to deter cheating:

```
P > max(balance_A_initial, balance_B_initial)
```

This ensures the economic cost of cheating exceeds the maximum possible gain.

### 7.7 Mutual Top-Up Function

When a satellite's balance on a channel drops below a threshold `T_low`, a **top-up request** can be initiated:

1. S_i sends a signed `TopUpRequest(channel_id, amount, new_balance_A, seq_num+1)` to S_j.
2. S_j verifies the request and signs a new BalProof reflecting the increased balance.
3. The top-up is logged in the hash chain for auditability.
4. When either satellite contacts its ground station, the top-up is reported. The ground station submits a `requestTopUp` transaction to the smart contract (§8.6), which adjusts the on-chain channel record.
5. The counterpart's ground station is notified (§13) and acknowledges the top-up on-chain.

**Constraint**: A top-up is only valid if both operators' ground stations confirm it on-chain within `T_topup_confirm` seconds. If confirmation does not arrive, the channel reverts to the pre-top-up balance.

### 7.8 Multi-Hop Forwarding Accounting

For a path A → B → C (three operators), LIOS Protocol maintains **pairwise bilateral channels** on each hop:

- Channel (A,B): S_a pays S_b for forwarding toward C.
- Channel (B,C): S_b pays S_c for last-mile forwarding.

There is no atomic multi-hop swap (no HTLC-style atomicity is needed because traffic is fungible and the source ground node is the payer). Each hop's accounting is independent. The source ground node (Operator A) bears the total cost; Operator B's satellite earns on both channels (receives on the A→B channel, pays on the B→C channel), netting to a transit fee.

### 7.9 Off-Chain Protocol Transaction Graph

Complete message-passing sequence for one ISL contact between S_i (Operator A) and S_j (Operator B). All messages after step [4] are encrypted with the derived session key K.

```
S_i  (Operator A)                                        S_j  (Operator B)
─────────────────────────────────────────────────────────────────────────────
       │                                                         │
       │        ◄── ISL CONTACT BEGINS (separation < 2500 km) ──►
       │                                                         │
── PHASE 1 : AUTHENTICATION ──────────────────────────────────────────────────
       │                                                         │
[1]    │── HELLO( cert_i, nonce_i, last_seq_i ) ───────────────►│
       │                                                         │ ▸ verify cert_i:
       │                                                         │   sig vs OpA root key
       │                                                         │   validity window
       │                                                         │   local revoc. cache
[2]    │◄─ HELLO( cert_j, nonce_j, last_seq_j ) ────────────────│
       │  ▸ verify cert_j (same checks)                          │
       │                                                         │
[3]    │── AUTH( ECDH_pub_i, sig_i(nonce_i ‖ nonce_j) ) ───────►│
[4]    │◄─ AUTH( ECDH_pub_j, sig_j(nonce_j ‖ nonce_i) ) ────────│
       │                                                         │
       │  K = ECDH(priv_i, ECDH_pub_j)    K = ECDH(priv_j, ECDH_pub_i)
       │                                                         │
       │   ~~ all subsequent messages encrypted with K ~~        │
       │                                                         │
── PHASE 2 : SYNC ────────────────────────────────────────────────────────────
       │                                                         │
[5]    │── Enc_K[ SYNC_REQ( σ_latest_i ) ] ───────────────────►│
[6]    │◄─ Enc_K[ SYNC_ACK( σ_latest_j ) ] ────────────────────│
       │                                                         │
       │  agreed_seq = max(seq_i, seq_j)                         │
       │  if seq_j > seq_i : adopt σ_j as working state          │
       │  if seq_i > seq_j : S_j adopts σ_i as working state     │
       │                                                         │
── PHASE 3 + 4 : FORWARD + COMMIT  (one iteration per batch) ─────────────────
       │                                                         │
       │  ╔═══════════════ forwarding batch n ════════════════╗  │
       │  ║                                                   ║  │
[7]    │──║── Enc_K[ DATA( flow_id, bytes_kb, payload ) ] ───║─►│
       │  ║                                                   ║  │ ▸ deliver payload
       │  ║                                                   ║  │ ▸ H_j_n = SHA256(
       │  ║                                                   ║  │     H_j_{n-1} ‖ seq
       │  ║                                                   ║  │     ‖ bytes ‖ ts)
[8]    │◄─║── Enc_K[ FWD_ACK( H_j_n, bytes_delivered ) ] ───║──│
       │  ║ ▸ H_i_n = SHA256( H_i_{n-1} ‖ seq ‖ bytes       ║  │
       │  ║              ‖ ts ‖ sig_peer_n )                  ║  │
       │  ║ ▸ build new proof σ':                             ║  │
       │  ║     seq'    = seq + 1                             ║  │
       │  ║     bal_A'  = bal_A − bytes_kb                    ║  │
       │  ║     bal_B'  = bal_B + bytes_kb                    ║  │
       │  ║     hash_A  = H_i_n,  hash_B = H_j_n             ║  │
       │  ║     ts'     = GPS_now                             ║  │
       │  ║ ▸ sig_i = ECDSA( priv_i, canonical_JSON(σ') )    ║  │
       │  ║                                                   ║  │
[9]    │──║── Enc_K[ PROOF_PROP( σ', sig_i ) ] ──────────────║─►│
       │  ║                                                   ║  │ ▸ verify sig_i
       │  ║                                                   ║  │ ▸ check balance cons.
       │  ║                                                   ║  │ ▸ sig_j = ECDSA(
       │  ║                                                   ║  │     priv_j, σ' )
[10]   │◄─║── Enc_K[ PROOF_ACK( sig_j ) ] ───────────────────║──│
       │  ║ ▸ σ_n = { σ', sig_i, sig_j }  ← co-signed        ║  │ ▸ store σ_n
       │  ║   σ_{n-1} implicitly revoked                      ║  │   σ_{n-1} revoked
       │  ║                                                   ║  │
       │  ╚═══════════════════════════════════════════════════╝  │
       │                                                         │
       │           ↻  repeat [7]–[10] for each batch             │
       │                                                         │
── CONTACT END ────────────────────────────────────────────────────────────────
       │                                                         │
       │  ▸ OutOfServiceEntry.pausedAt = contact_end_time        │
       │  ▸ evaluate settlement triggers T1–T7 (§11.1)           │
       │  ▸ queue SettlementPayload{ σ_final, Δhash_chain }       │
       │    → transmitted on next GS uplink                      │
       │                                                         │
       ╧  ISL paused — no forwarding until both GSs confirm       ╧

Legend:
  ──►  /  ◄──   unencrypted ISL message
  ═══►  / ◄═══  Enc_K encrypted ISL message
  ▸               local computation (not transmitted)
  σ_n             co-signed BalProof at sequence n
  H_x_n           SHA-256 hash chain head for satellite x at step n
```

#### Balance Conservation Invariant

At every step, before and after each forwarding batch:

```
bal_A + bal_B = initial_capacity   (enforced by both satellites and the smart contract)
```

A satellite that detects a proposed `σ'` violating this invariant **must reject** the PROOF_PROP and log a T6 (anomaly) trigger.

#### Optional: Mutual Top-Up Mid-Session

If `bal_A < T_low` or `bal_B < T_low` between batches, either satellite may initiate an in-session top-up:

```
S_i (Operator A)                                        S_j (Operator B)
       │                                                        │
[T1]   │── Enc_K[ TOPUP_REQ( Δ_A, Δ_B, new_σ_proposal ) ] ──►│
       │                                                        │ ▸ verify amounts
       │                                                        │   sign new_σ
[T2]   │◄─ Enc_K[ TOPUP_ACK( sig_j(new_σ) ) ] ────────────────│
       │  ▸ store new_σ; resume forwarding                      │
       │  ▸ report top-up in next GS uplink                     │
       │    (GS confirms on-chain within T_topup_confirm)        │
```

The top-up is provisional until both operators' ground stations confirm it on-chain (§7.7). If on-chain confirmation does not arrive within `T_topup_confirm`, the channel reverts to the pre-top-up BalProof.

---

## 8. Hyperledger Fabric Smart Contract Design

### 8.1 Network Configuration

The network is defined in `lios/fablo/fablo-config.json` and bootstrapped with `fablo up`.

```
Consortium:       ISLConsortium
Organisations:    OperatorA, OperatorB, ..., OperatorN (N ≤ 20)
Channel:          isl-settlement
Endorsement:      MAJORITY of channel member MSPs
Ordering service: Raft, 3 orderer nodes (one per operator for decentralisation)
State database:   CouchDB (rich queries on channel state by satChannelId, operatorId)
Chaincode:        Go (fabric-contract-api-go v1.2.1), deployed via `fablo chaincode install`
```

### 8.2 Data Model

Defined in `lios/chaincode/isl_settlement/types.go`:

```go
// ── Operator-Level Channel ──────────────────────────────────────────────────
type OperatorChannel struct {
    ID             string  `json:"channelId"`          // "opA_opB_YYYYMM"
    OperatorA      string  `json:"operatorA"`          // MSP ID
    OperatorB      string  `json:"operatorB"`          // MSP ID
    BalanceA       float64 `json:"balanceA"`           // KB
    BalanceB       float64 `json:"balanceB"`           // KB
    PenaltyReserveA float64 `json:"penaltyReserveA"`  // locked KB
    PenaltyReserveB float64 `json:"penaltyReserveB"`
    Status         string  `json:"status"`             // OPEN|SETTLING|DISPUTED|CLOSED
    OpenedAt       int64   `json:"openedAt"`           // epoch ms
    ClosedAt       *int64  `json:"closedAt,omitempty"`
}

// ── Satellite-Pair Sub-Channel ──────────────────────────────────────────────
type SatChannel struct {
    ID               string             `json:"satChannelId"`
    OperatorChannelID string            `json:"operatorChannelId"`
    SatA             string             `json:"satelliteA"`
    SatB             string             `json:"satelliteB"`
    SeqNum           int64              `json:"currentSeqNum"`
    BalanceA         float64            `json:"balanceA"`           // KB
    BalanceB         float64            `json:"balanceB"`           // KB
    HashHeadA        string             `json:"hashChainHeadA"`     // hex
    HashHeadB        string             `json:"hashChainHeadB"`     // hex
    LastUpdated      int64              `json:"lastUpdated"`        // epoch ms
    Status           string             `json:"status"`             // ACTIVE|PAUSED|PENDING_SETTLEMENT|SETTLED
    OutOfServiceLog  []OutOfServiceEntry `json:"outOfServiceLog"`
}

// ── Balance Proof (submitted for settlement) ────────────────────────────────
type BalanceProof struct {
    ChannelID   string  `json:"satChannelId"`
    SeqNum      int64   `json:"seqNum"`
    BalanceA    float64 `json:"balanceA"`
    BalanceB    float64 `json:"balanceB"`
    HashHeadA   string  `json:"hashChainHeadA"`
    HashHeadB   string  `json:"hashChainHeadB"`
    SigA        string  `json:"sigA"`           // DER hex ECDSA P-256
    SigB        string  `json:"sigB"`           // DER hex ECDSA P-256
    SubmittedBy string  `json:"submittedBy"`    // operator MSP ID
    SubmittedAt int64   `json:"submittedAt"`
}

// ── Dispute Record ──────────────────────────────────────────────────────────
type DisputeRecord struct {
    ID           string        `json:"disputeId"`
    ChannelID    string        `json:"satChannelId"`
    ClaimedProof BalanceProof  `json:"claimedProof"`
    CounterProof *BalanceProof `json:"counterProof,omitempty"`
    Status       string        `json:"status"`    // OPEN|RESOLVED_PENALTY|RESOLVED_VALID
    OpenedAt     int64         `json:"openedAt"`
    ResolvedAt   *int64        `json:"resolvedAt,omitempty"`
    PenaltyPaid  bool          `json:"penaltyPaid"`
}

// ── Out-of-Service Log Entry ────────────────────────────────────────────────
type OutOfServiceEntry struct {
    ChannelID   string  `json:"satChannelId"`
    PausedAt    int64   `json:"pausedAt"`            // contact-end time (satellite-authoritative)
    ResumedAt   *int64  `json:"resumedAt,omitempty"` // max(SatAAckAt, SatBAckAt)
    SatAAckAt   *int64  `json:"satA_ackAt,omitempty"`
    SatBAckAt   *int64  `json:"satB_ackAt,omitempty"`
    Reason      string  `json:"reason"`              // SETTLEMENT|BALANCE_DEPLETED|OPERATOR_REQUEST
    InitiatedBy string  `json:"initiatedBy"`         // operator MSP ID
}

// ── Top-Up Request ──────────────────────────────────────────────────────────
type TopUpRequest struct {
    ID                string  `json:"topUpId"`
    OperatorChannelID string  `json:"operatorChannelId"`
    RequestedBy       string  `json:"requestedBy"`
    AmountA           float64 `json:"amountA"`
    AmountB           float64 `json:"amountB"`
    ConfirmedBy       string  `json:"confirmedBy,omitempty"`
    Status            string  `json:"status"`     // PENDING|CONFIRMED|EXPIRED
    RequestedAt       int64   `json:"requestedAt"`
    ExpiresAt         int64   `json:"expiresAt"`  // requestedAt + T_TOPUP_CONFIRM
}

// ── Settlement Notification ─────────────────────────────────────────────────
type SettlementNotification struct {
    ID             string `json:"notifId"`
    TargetOperator string `json:"targetOperator"`
    TargetSatellite string `json:"targetSatellite,omitempty"`
    Type           string `json:"type"`       // SETTLEMENT_STARTED|SETTLEMENT_COMPLETE|...
    Payload        string `json:"payload"`    // JSON string
    CreatedAt      int64  `json:"createdAt"`
    Acknowledged   bool   `json:"acknowledged"`
}

// ── Satellite Key Record ────────────────────────────────────────────────────
type SatKey struct {
    SatelliteID  string `json:"satelliteId"`
    OperatorID   string `json:"operatorId"`
    PubKeyPEM    string `json:"pubKeyPEM"`
    IsRevoked    bool   `json:"isRevoked"`
    RegisteredAt int64  `json:"registeredAt"`
    RevokedAt    *int64 `json:"revokedAt,omitempty"`
}
```

### 8.3 Chaincode Functions

Defined in `lios/chaincode/isl_settlement/chaincode.go` using `fabric-contract-api-go`:

```go
// ── CHANNEL LIFECYCLE ───────────────────────────────────────────────────────

// OpenOperatorChannel — called jointly by both operators (both must endorse).
// Locks penalty reserves; stores OperatorChannel with status OPEN.
func (c *ISLSettlementChaincode) OpenOperatorChannel(
    ctx contractapi.TransactionContextInterface,
    operatorA, operatorB string,
    balanceA, balanceB, reserveA, reserveB float64) error

// RegisterSatChannel — ground station registers a satellite sub-channel
// under an existing operator channel.  Deducts from operator balance pool.
func (c *ISLSettlementChaincode) RegisterSatChannel(
    ctx contractapi.TransactionContextInterface,
    operatorChannelID, satA, satB string,
    balA, balB float64) error

// InitiateSettlement — submits latest BalProof; starts T_challenge window.
// Verifies ECDSA signatures SigA and SigB; validates SeqNum advancement.
func (c *ISLSettlementChaincode) InitiateSettlement(
    ctx contractapi.TransactionContextInterface,
    satChannelID string, proof BalanceProof) error

// ChallengeSettlement — counterpart submits newer BalProof within T_challenge.
// Slashes penalty reserve of initiating party if counter SeqNum is higher.
func (c *ISLSettlementChaincode) ChallengeSettlement(
    ctx contractapi.TransactionContextInterface,
    satChannelID string, counter BalanceProof) error

// FinalizeSettlement — called after T_challenge expires.
// Uses latest valid proof; updates operator channel balance totals.
func (c *ISLSettlementChaincode) FinalizeSettlement(
    ctx contractapi.TransactionContextInterface,
    satChannelID string) error

// ── TOP-UP ──────────────────────────────────────────────────────────────────

// RequestTopUp — creates a TopUpRequest (PENDING).
// Counterpart must confirm within T_TOPUP_CONFIRM seconds or it expires.
func (c *ISLSettlementChaincode) RequestTopUp(
    ctx contractapi.TransactionContextInterface,
    operatorChannelID string, amountA, amountB float64) error

// ConfirmTopUp — counterpart confirms; updates operator channel balances.
func (c *ISLSettlementChaincode) ConfirmTopUp(
    ctx contractapi.TransactionContextInterface,
    topUpID string) error

// ── PAUSE / RESUME ──────────────────────────────────────────────────────────

// RecordISLPause — appends OutOfServiceEntry; sets SatChannel status PAUSED.
func (c *ISLSettlementChaincode) RecordISLPause(
    ctx contractapi.TransactionContextInterface,
    satChannelID, reason string) error

// RecordISLResume — records per-satellite ACK (SatAAckAt or SatBAckAt).
// Sets ResumedAt = max(SatAAckAt, SatBAckAt) and emits ISL_RESUMED
// only once BOTH satellites have acknowledged via their respective ground stations.
func (c *ISLSettlementChaincode) RecordISLResume(
    ctx contractapi.TransactionContextInterface,
    satChannelID, satelliteID string) error

// ── NOTIFICATIONS ───────────────────────────────────────────────────────────

// GetPendingNotifications — CouchDB rich query: targetOperator=operatorID AND acknowledged=false.
func (c *ISLSettlementChaincode) GetPendingNotifications(
    ctx contractapi.TransactionContextInterface,
    operatorID string) ([]*SettlementNotification, error)

// AcknowledgeNotification — marks notification as acknowledged.
func (c *ISLSettlementChaincode) AcknowledgeNotification(
    ctx contractapi.TransactionContextInterface,
    notifID string) error

// ── KEY MANAGEMENT ──────────────────────────────────────────────────────────

// RegisterSatelliteKey — verifies operator cert signature over (satelliteID||pubKeyPEM);
// stores SatKey on-chain for peer signature verification.
func (c *ISLSettlementChaincode) RegisterSatelliteKey(
    ctx contractapi.TransactionContextInterface,
    operatorID, satelliteID, pubKeyPEM, operatorCertSig string) error

// RevokeSatelliteKey — sets IsRevoked=true; emits KEY_REVOKED for CRL propagation.
func (c *ISLSettlementChaincode) RevokeSatelliteKey(
    ctx contractapi.TransactionContextInterface,
    operatorID, satelliteID string) error

// GetSatelliteKey — returns SatKey for a given satellite ID.
func (c *ISLSettlementChaincode) GetSatelliteKey(
    ctx contractapi.TransactionContextInterface,
    satelliteID string) (*SatKey, error)
```

### 8.4 Endorsement Policy

```
CHANNEL OPEN:       AND(OperatorA.admin, OperatorB.admin)
SAT CHANNEL REG:    OR(OperatorA.peer, OperatorB.peer)
SETTLE INITIATE:    OR(any Operator peer)
SETTLE CHALLENGE:   OR(any Operator peer)
SETTLE FINALIZE:    AND(majority of operators) -- prevents premature finalization
TOP-UP REQUEST:     OR(any Operator peer)
TOP-UP CONFIRM:     OR(counterpart Operator peer)
KEY REGISTER:       AND(Operator.admin, MSP CA)
KEY REVOKE:         AND(Operator.admin)
```

### 8.5 Events Emitted by Chaincode

Emitted via `ctx.GetStub().SetEvent(name, payload)` in Go:

```go
const (
    EventSettlementInitiated  = "SettlementInitiated"
    EventSettlementChallenged = "SettlementChallenged"
    EventSettlementFinalized  = "SettlementFinalized"
    EventDisputeOpened        = "DisputeOpened"
    EventDisputeResolved      = "DisputeResolved"
    EventPenaltyApplied       = "PenaltyApplied"
    EventTopUpRequested       = "TopUpRequested"
    EventTopUpConfirmed       = "TopUpConfirmed"
    EventTopUpExpired         = "TopUpExpired"
    EventISLPaused            = "ISLPaused"
    EventISLResumed           = "ISLResumed"
    EventKeyRegistered        = "KeyRegistered"
    EventKeyRevoked           = "KeyRevoked"
)
```

---

## 9. Operator-Level Channel Establishment

### 9.1 Pre-Conditions

- Both operators have joined the ISL Consortium Hyperledger Fabric channel.
- Both operators have registered their root public keys with the Fabric MSP CA.
- Both operators have agreed (off-chain via SLA) on initial balance allocations and penalty reserve amounts.

### 9.2 Establishment Protocol

```
Operator A                    Fabric Network              Operator B
    │                               │                          │
    │──── proposeChannel(A,B) ─────►│                          │
    │     (draft tx, not submitted) │                          │
    │                               │◄── endorseChannel(A,B) ──│
    │◄──── channelProposal ─────────│                          │
    │                               │                          │
    │──── signAndSubmit ────────────►│                          │
    │     openOperatorChannel(      │                          │
    │       balA, balB,             │                          │
    │       penA, penB)             │                          │
    │                               │──── validateEndorsement ─►│
    │                               │                          │
    │◄──── channelOpened ──────────── ──── channelOpened ──────►│
    │      (OperatorChannel ID)     │    (OperatorChannel ID)  │
```

### 9.3 Balance Allocation to Satellite Pairs

Once an operator channel is open:

1. Operators distribute the aggregate balance across their satellite fleets based on constellation coverage overlap analysis (pre-computed from the contact plan).
2. Each satellite pair (S_i, S_j) that will have ISL contacts registers a SatChannel with a fraction of the operator channel balance.
3. The sum of all SatChannel balances under an OperatorChannel must not exceed the OperatorChannel's total balance.

### 9.4 Channel Parameters

| Parameter | Symbol | Typical Value | Description |
|---|---|---|---|
| Initial balance per operator channel | B_0 | 1 TB (in KB) | Traffic quota each operator extends to the other |
| Penalty reserve | P | 10% of B_0 | Locked on-chain, slashed on cheating |
| Challenge window | T_challenge | 48 hours | Time for counterpart to submit newer proof |
| Top-up confirmation window | T_topup | 24 hours | Time for counterpart to confirm top-up |
| Low-balance threshold | T_low | 5% of B_0 | Triggers automatic top-up request |
| Max hash chain entries before offload | H_max | 10,000 entries | Triggers ground settlement |

---

## 10. Satellite Authentication via Operator Key Hierarchy

### 10.1 Design Rationale

A constellation may have hundreds of satellites per operator. Storing a full X.509 certificate for every satellite on every peer satellite is infeasible due to:

- Limited on-board storage (especially on small sats).
- Dynamic fleet changes (launches, deorbits).
- Key rotation requirements.

**Solution**: A two-tier PKI where each satellite only needs to store the **operator root public keys** of all consortium members (N ≤ 20 keys, typically 256–384 bytes each ≈ 8 KB total). Satellite keys are issued and revoked by the operator root key.

### 10.2 Key Hierarchy

```
Operator Root Key (offline HSM)
        │
        ▼
  Operator Intermediate Key (online CA, rotated annually)
        │
        ├──► Satellite S1 Operational Key (valid 90 days)
        ├──► Satellite S2 Operational Key (valid 90 days)
        └──► Satellite SN Operational Key (valid 90 days)
```

### 10.3 Satellite Certificate Format

```json
{
  "version": 1,
  "satellite_id": "OpA-Sat-042",
  "operator_id": "OperatorA",
  "public_key": "-----BEGIN EC PUBLIC KEY-----\n...",
  "valid_from": "2025-01-01T00:00:00Z",
  "valid_until": "2025-04-01T00:00:00Z",
  "permitted_operators": ["OperatorA", "OperatorB", "OperatorC"],
  "signature": "<ECDSA sig by OperatorA Intermediate Key>"
}
```

### 10.4 On-Contact Authentication Protocol

```
S_i (Operator A)                    S_j (Operator B)
     │                                    │
     │──── Hello(cert_i, nonce_i) ───────►│
     │                                    │ Verify cert_i:
     │                                    │  1. Check sig against OpA root key
     │                                    │  2. Check validity window
     │                                    │  3. Check revocation (local cache)
     │◄─── Hello(cert_j, nonce_j) ────────│
     │ Verify cert_j (same process)       │
     │                                    │
     │──── Auth(ECDH_pub_i, sig_i) ──────►│
     │◄─── Auth(ECDH_pub_j, sig_j) ───────│
     │                                    │
     │     Derive shared session key K    │
     │     K = ECDH(priv_i, pub_j)        │
     │     Encrypt all subsequent msgs    │
```

### 10.5 Revocation Propagation

1. Operator revokes a satellite key on-chain (Fabric: `revokeSatelliteKey`).
2. Fabric emits `KeyRevoked` event.
3. Ground stations of all operators receive the event and update their local revocation cache.
4. Ground stations transmit the revocation list to their satellites on next ground contact.
5. Satellites cache the revocation list; on ISL contact, step 1c of the authentication protocol checks this cache.
6. Revocation list uses a compact **Certificate Revocation List (CRL)** format, transmitted as a signed delta.

---

## 11. Ground Settlement Protocol

### 11.1 Settlement Triggers

A satellite initiates a ground settlement request when **any** of the following conditions hold:

| Trigger ID | Condition | Rationale |
|---|---|---|
| T1 | `balance_A < T_low` OR `balance_B < T_low` | Channel nearly depleted; needs top-up or closure |
| T2 | `hash_chain_length ≥ H_max` | On-board storage limit for TX history reached |
| T3 | ISL contact has ended AND `seq_num` has increased since last settlement | Periodic offload after each contact |
| T4 | Satellite scheduled deorbit within 30 days | Graceful channel closure |
| T5 | Operator command (manual override) | Administrative action |
| T6 | Detected anomaly in peer behavior (e.g., seq_num regression) | Security response |
| T7 | Cumulative forwarded traffic > `S_max` (e.g., 100 GB per session) | Periodic settlement to limit exposure |

### 11.2 Mutual vs. Unilateral Settlement

#### Mutual Settlement (preferred)

Both operators' ground stations submit the same BalProof to Fabric simultaneously (within `T_challenge`). Since they agree, settlement is instant — no challenge window penalty risk.

**Constraint**: Mutual settlement must be initiated **at the end of an ISL contact**, not during active forwarding. The final co-signed BalProof from contact-end is used as the settlement proof. Immediately after the contact ends, both satellites log the ISL as **out-of-service** (`OutOfServiceEntry.pausedAt` is recorded). The ISL remains paused until **both** satellites have been contacted by their respective ground nodes and received the settlement completion notification — only at that point is the OOS entry closed (`resumedAt` recorded) and the ISL re-activated. This guarantees that no new forwarding occurs on the channel while settlement is in-flight, and that both sides are fully informed before traffic resumes.

#### Unilateral Settlement

If coordination is not possible (e.g., only one operator has ground contact), one operator's ground station submits their BalProof unilaterally. The challenge window `T_challenge` starts. The `T_challenge` window is deliberately sized to allow the **counterpart's ground node time to**:
  1. Receive the `SETTLEMENT_INITIATED` event from Fabric.
  2. Wait for the next ground contact with its satellite and retrieve the satellite's latest BalProof.
  3. Compare the submitted proof against the latest local state.
  4. Submit a challenge (with a newer proof) or formally acknowledge if the submitted proof is correct.

The counterpart's ground station must therefore:

1. Receive the `SETTLEMENT_INITIATED` event from Fabric.
2. Retrieve the latest BalProof from its satellite on the next ground contact.
3. If the submitted proof is outdated, submit the newer proof within `T_challenge`.

### 11.3 Settlement State Machine (Ground)

```
ACTIVE ──T1/T2/T3/T4/T5/T6/T7──► PENDING_SETTLEMENT
         (satellite reports to GS)
              │
              ▼
         GS submits initiateSettlement() on Fabric
              │
              ├──── Mutual: counterpart GS agrees ───► SETTLING (no challenge)
              │                                            │
              └──── Unilateral: wait T_challenge ─────────┘
                        │
                        ├── No challenge ──────────────► finalizeSettlement()
                        │                                     │
                        └── Challenge submitted ──────────────┘
                              (dispute path: §8.3)
              │
              ▼
         SETTLED ──► ISL RESUME NOTIFICATION ──► ACTIVE (new channel)
```

### 11.4 Settlement at End of Contact

- When a settlement trigger fires, the satellite **does not interrupt the current ISL contact**; real-time forwarding continues uninterrupted until the natural end of the contact.
- At contact-end, the satellite records the final co-signed BalProof and **immediately marks the ISL channel as paused** (`OutOfServiceEntry.pausedAt` = contact-end timestamp). No further forwarding is permitted on this channel until settlement completes.
- The settlement payload (latest BalProof + hash chain delta) is queued and transmitted to the ground station on the **next ground contact** of the same operator.
- The ISL out-of-service duration is logged from the moment the contact ends until the moment **both** satellites receive a resume notification from their respective ground nodes.
- This design maximises ISL utilization during the contact while ensuring a clean, unambiguous settlement boundary at contact-end.

---

## 12. ISL Pause/Resume and Out-of-Service Logging

### 12.1 Pause Protocol

The ISL pause is always triggered **at the end of an ISL contact**, never mid-contact:

1. When the ISL contact ends and a settlement trigger has fired, each satellite **locally records `pausedAt`** in its `OutOfServiceEntry` for that channel (timestamp = contact-end time). No further traffic is accepted on the channel.
2. Each satellite includes the pause event in the next uplink to its ground station. The ground station then emits `recordISLPause()` to the Fabric ledger, which records the `OutOfServiceEntry` on-chain.
3. This approach ensures the out-of-service duration is logged accurately even before the ground contact occurs — the satellite is the authoritative clock for the pause start.
4. For emergency or administratively-initiated pauses, a direct RF command from the ground station may also trigger the pause; the `pausedAt` timestamp in that case reflects when the satellite receives and acknowledges the command.

### 12.2 Resume Protocol

After settlement is finalized (or a new channel is opened):

1. Fabric emits `SETTLEMENT_FINALIZED` (and subsequently `ISL_RESUMED` once both sides confirm).
2. Both ground stations receive the event.
3. Each ground station includes a **resume notification** in the next downlink bundle to its own satellite.
4. The ISL resumes only after **both** satellites have been contacted by their respective ground nodes and have acknowledged the resume notification. A satellite must not restart forwarding on the channel until it has received explicit confirmation from its own ground node — this ensures both sides are in sync before traffic flows again.
5. Once a satellite acknowledges the resume, its ground station calls `recordISLResume()` on Fabric. The `resumedAt` timestamp in the `OutOfServiceEntry` is set when the **last** of the two satellites acknowledges (i.e., the full OOS period ends only when both sides are ready).

### 12.3 Out-of-Service Logging

Each ISL channel that undergoes settlement has its out-of-service time logged. `pausedAt` is set at contact-end (by the satellite, then confirmed on-chain by the GS). `resumedAt` is set only once **both** satellites have acknowledged the resume via their respective ground nodes.

Each `OutOfServiceEntry` on-chain records:

```json
{
  "satChannelId": "satA1_satB3",
  "pausedAt": 1735689600000,
  "resumedAt": 1735696800000,
  "durationSeconds": 7200,
  "reason": "SETTLEMENT",
  "initiatedBy": "OperatorA",
  "satA_ack_at": 1735693200000,
  "satB_ack_at": 1735696800000
}
```

- `pausedAt`: timestamp of contact-end when the settlement trigger fired (satellite-authoritative).
- `satA_ack_at` / `satB_ack_at`: timestamps when each satellite acknowledged the resume notification from its ground node.
- `resumedAt`: equals `max(satA_ack_at, satB_ack_at)` — the ISL is only considered back in service when the slower of the two ground contacts completes.
- The aggregate out-of-service time per channel is queryable for SLA compliance reporting.

### 12.4 Resumption Conditions

| Scenario | Resumption Condition |
|---|---|
| Mutual settlement | `finalizeSettlement()` confirmed on Fabric **AND** both satellites individually contacted by their own GS and acknowledged the resume notification; `resumedAt` logged when the second satellite ACKs |
| Unilateral settlement (no dispute) | `T_challenge` expires + `finalizeSettlement()` + **counterpart GS contacts its satellite** and satellite ACKs (guarantees counterpart is informed before traffic resumes) |
| Unilateral settlement (dispute) | Dispute resolved + new BalProof agreed + both satellites contacted by their respective GSs and both ACK resume |
| Top-up required | Top-up confirmed on-chain by both GSs; both satellites notified and ACK on next ground contact |
| Balance depletion | New operator channel balance allocated; new SatChannel registered; both satellites notified by their GSs |

---

## 13. Ground Node Notification Mechanism

### 13.1 Design Purpose

The notification mechanism serves two distinct roles depending on whether settlement is mutual or unilateral:

- **Mutual settlement**: both ground nodes are already coordinating. Notifications confirm settlement completion and authorise each satellite to resume the ISL. The ISL remains paused until each satellite has been contacted by its own ground node and received the resume signal.

- **Non-mutual (unilateral) settlement**: the counterpart's ground node may not have been online when settlement was initiated. The `T_challenge` window exists precisely to give the counterpart's ground node sufficient time to (1) receive the `SETTLEMENT_INITIATED` event from Fabric, (2) establish contact with its satellite on the next orbital pass, (3) download the satellite's latest BalProof, and (4) file a challenge or complaint if the submitted proof does not match the satellite's records. Without this window and the associated notification mechanism, an unilateral settlement could be finalised before the counterpart has any opportunity to contest it.

### 13.2 Push vs. Pull

Fabric supports **event subscriptions**: ground station nodes subscribe to chaincode events using the Fabric SDK. When a relevant event fires (e.g., `SETTLEMENT_INITIATED` by a counterpart), the subscribing ground station's event handler is called immediately.

### 13.3 Satellite Notification Mechanism

Ground stations relay Fabric events to satellites through two mechanisms:

1. **Scheduled downlink**: On every ground contact, the GS transmits a **notification bundle** containing:
   - Pending `SettlementNotification` records.
   - Latest revocation list delta.
   - Top-up confirmations.
   - New BalProofs from counterpart (if available).

2. **Emergency uplink command** (if direct uplink is available): For time-critical events (e.g., `DISPUTE_OPENED` with expiring challenge window), the GS may issue an emergency uplink to alert the satellite immediately.

### 13.4 Notification Bundle Format

```json
{
  "bundle_id": "uuid",
  "generated_at": 1735689600000,
  "target_satellite": "OpA-Sat-042",
  "notifications": [
    {
      "type": "SETTLEMENT_INITIATED",
      "satChannelId": "satA1_satB3",
      "submittedProof": { ... },
      "challengeDeadline": 1735862400000
    },
    {
      "type": "TOPUP_CONFIRMED",
      "operatorChannelId": "opA_opB_202501",
      "newBalance": 1024000
    }
  ],
  "crl_delta": ["revoked_sat_id_1", ...],
  "signature": "<GS ECDSA sig>"
}
```

### 13.5 Acknowledgement

Satellites acknowledge notification bundles by including an `ACK(bundle_id)` in their next uplink. The ground station records this in the Fabric ledger (via `acknowledgeNotification()`), providing a verifiable delivery audit trail.

---

## 14. Discrete LEO Emulation Framework

### 14.1 Design Philosophy

The emulator is a **discrete-event simulation (DES)** that models:

- Satellite orbital dynamics propagated from real TLEs using `sgp4`/`skyfield`.
- ISL contact windows computed from TLE-derived positions (separation < 2500 km).
- Ground station contact windows computed from TLE positions + real GS lat/lon/alt (elevation angle ≥ minimum_elevation_deg).
- Synthetic traffic flows (generation, routing, queuing, delivery).
- Off-chain cryptographic protocol execution (signing, hashing, verification).
- Ground settlement transactions on a local Hyperledger Fabric test network.

**Data inputs** live in `lios/data/`. Each operator has two files named after it:
- `<operator>.tle` — Two-line element set file (standard 3-line TLE format) for all satellites belonging to that operator.
- `<operator>.json` — Ground station config: array of `{name, lat_deg, lon_deg, alt_m, min_elevation_deg}` objects.

The operator name is inferred from the filename stem (e.g., `starlink.tle` → operator `starlink`). All operators present in `data/` are loaded automatically; no hardcoded operator list.

The emulator is **not** a physics simulator; it propagates the real TLEs and event-drives the protocol state machines from the computed contact windows.

### 14.2 Emulator Components

```
┌────────────────────────────────────────────────────────────────────────────┐
│                      LIOS Protocol Emulator                                │
│                                                                            │
│  ┌──────────────────┐    ┌──────────────────┐    ┌──────────────────┐     │
│  │ ContactPlanParser │    │ OrbitalScheduler │    │ TrafficGenerator │     │
│  │                  │    │                  │    │                  │     │
│  │ Reads CCSDS/STK  │───►│ Events:          │    │ Poisson arrival  │     │
│  │ contact plan CSV │    │ ISL_OPEN         │    │ Random src/dst   │     │
│  │                  │    │ ISL_CLOSE        │    │ Flow sizes       │     │
│  └──────────────────┘    │ GS_CONTACT_START │    │ Multi-operator   │     │
│                          │ GS_CONTACT_END   │    └────────┬─────────┘     │
│                          └────────┬─────────┘             │               │
│                                   │ Events                │               │
│                          ┌────────▼─────────────────────┐ │               │
│                          │       Event Queue            │◄┘               │
│                          │   (priority queue by time)   │                 │
│                          └────────┬─────────────────────┘                 │
│                                   │                                       │
│            ┌──────────────────────▼────────────────────────┐              │
│            │              Satellite Node                    │              │
│            │  ┌───────────┐ ┌──────────┐ ┌─────────────┐  │              │
│            │  │BalanceDB  │ │HashChain │ │AuthModule   │  │              │
│            │  │ (in-mem)  │ │Logger    │ │(key verify) │  │              │
│            │  └───────────┘ └──────────┘ └─────────────┘  │              │
│            │  ┌───────────────────────────────────────────┐│              │
│            │  │    OffChainProtocol (§7 state machine)    ││              │
│            │  └───────────────────────────────────────────┘│              │
│            └───────────────────────────────────────────────┘              │
│                                                                            │
│            ┌──────────────────────────────────────────────┐               │
│            │              Ground Station Node              │               │
│            │  ┌───────────┐ ┌──────────────────────────┐  │               │
│            │  │FabricSDK  │ │SettlementManager         │  │               │
│            │  │Client     │ │(trigger evaluation)      │  │               │
│            │  └───────────┘ └──────────────────────────┘  │               │
│            │  ┌───────────────────────────────────────────┐│               │
│            │  │    NotificationManager (§13)              ││               │
│            │  └───────────────────────────────────────────┘│               │
│            └──────────────────────────────────────────────┘               │
│                                                                            │
│  ┌──────────────────────────────────────────────────────────────────────┐ │
│  │                   Hyperledger Fabric (local Docker)                  │ │
│  │        Orderer · PeerA · PeerB · ... · PeerN · CouchDB              │ │
│  └──────────────────────────────────────────────────────────────────────┘ │
│                                                                            │
│  ┌──────────────────────────────────────────────────────────────────────┐ │
│  │                      Metrics Collector                               │ │
│  │  Throughput · Fairness (Jain Index) · Settlement latency ·          │ │
│  │  Out-of-service time · Penalty events · Hash chain sizes            │ │
│  └──────────────────────────────────────────────────────────────────────┘ │
└────────────────────────────────────────────────────────────────────────────┘
```

### 14.3 Satellite Node Internal Structure

```python
class SatelliteNode:
    satellite_id: str
    operator_id: str
    private_key: ECPrivateKey        # operational key (90-day rotation)
    operator_cert: OperatorCert      # signed by operator CA
    peer_operator_keys: Dict[str, ECPublicKey]  # N operator root keys
    peer_revocation_cache: Set[str]  # revoked satellite IDs

    channels: Dict[str, SatChannelState]  # keyed by sat_channel_id
    hash_chain: Dict[str, HashChainLog]   # per channel
    pending_notifications: List[NotificationBundle]

    def on_isl_open(self, peer: SatelliteNode): ...
    def on_isl_close(self, peer_id: str): ...
    def on_gs_contact_start(self, gs: GroundStationNode): ...
    def on_gs_contact_end(self, gs_id: str): ...
    def forward_traffic(self, flow: TrafficFlow): ...
    def sign_balance_proof(self, channel_id: str) -> BalProof: ...
    def evaluate_settlement_triggers(self, channel_id: str) -> List[TriggerID]: ...
```

### 14.4 Ground Station Node Internal Structure

```python
class GroundStationNode:
    gs_id: str
    operator_id: str
    fabric_client: FabricSDKClient
    event_listener: FabricEventListener

    def on_satellite_contact(self, sat: SatelliteNode): ...
    def receive_tx_log(self, log: TXLog): ...
    def evaluate_settlement(self, sat_channel_id: str): ...
    def submit_settlement(self, proof: BalProof): ...
    def transmit_notifications(self, sat: SatelliteNode): ...
    def on_fabric_event(self, event: ChainEvent): ...
```

---

## 15. Contact Plan, Traffic Generation, and Routing

### 15.1 Contact Plan Format

The contact plan is a CSV/JSON file in CCSDS Contact Graph format:

```csv
# contact_plan.csv
contact_id, from_node, to_node, start_time, end_time, capacity_kbps, range_km
C001, OpA-Sat-001, OpA-Sat-002, 0, 180, 10000, 1200
C002, OpA-Sat-001, OpB-Sat-003, 240, 420, 8000, 1500
C003, OpA-Sat-001, GS-OpA-London, 600, 900, 50000, 800
...
```

The contact plan is computed at startup by the window calculator from:
- **Real TLEs** in `lios/data/<operator>.tle` — propagated using `sgp4` (or `skyfield` if available).
- **Real ground station positions** in `lios/data/<operator>.json` — `{name, lat_deg, lon_deg, alt_m, min_elevation_deg}`.

STK/GMAT exports are also accepted as a drop-in replacement for the computed CSV.

### 15.2 Traffic Generation

#### Source and Destination Selection

Per the requirements:
- Traffic sources are **ground nodes** (not necessarily ground stations).
- Selected **source and destination** are **satellites** chosen randomly from among those that have a contact with the ground node within a lookahead window.
- Multi-hop paths may traverse satellites from **multiple operators**.

```python
def generate_traffic_flow(
    contact_plan: ContactPlan,
    t_now: float,
    lookahead: float = 600.0  # seconds
) -> TrafficFlow:

    # 1. Identify all ground nodes with upcoming satellite contacts
    ground_nodes = contact_plan.get_ground_nodes()
    source_gn = random.choice(ground_nodes)

    # 2. Get reachable satellites from source in lookahead window
    reachable_sats = contact_plan.get_reachable_satellites(
        source_gn, t_now, t_now + lookahead
    )
    src_sat = random.choice(reachable_sats)

    # 3. Get random destination satellite (different operator preferred)
    all_sats = contact_plan.get_all_satellites()
    dst_sat = random.choice([s for s in all_sats if s != src_sat])

    # 4. Find shortest path
    path = contact_graph_routing(contact_plan, src_sat, dst_sat, t_now)

    return TrafficFlow(
        flow_id=uuid4(),
        source_ground=source_gn,
        src_sat=src_sat,
        dst_sat=dst_sat,
        path=path,
        size_kb=random.expovariate(1/1024),  # exponential dist, mean 1MB
        generated_at=t_now
    )
```

### 15.3 Contact Graph Routing (State-of-the-Art CGR)

LIOS Protocol uses a modified **Yen's K-Shortest Paths** algorithm over the contact graph, adapted from CGR:

```python
def contact_graph_routing(
    cp: ContactPlan,
    src: str,
    dst: str,
    t_start: float,
    k: int = 3  # find K shortest paths
) -> List[Path]:
    """
    Modified CGR using time-expanded graph representation.
    Each contact is a directed edge with:
      - weight = latency (propagation + queuing)
      - capacity = link capacity × contact duration
      - earliest_start = contact start time
      - latest_start = contact end time - transmission_time

    Algorithm:
    1. Build time-expanded graph G_t from contact plan.
    2. Apply Dijkstra from (src, t_start) to (dst, any_time).
    3. Edge traversal constraint: arrival_time ≤ contact.start_time.
    4. Return K paths ordered by end-to-end latency.
    5. Select path with minimum operator-hops for fairness (breaks ties).
    """
```

#### State-of-the-Art Enhancement: Policy-Aware CGR

LIOS Protocol extends standard CGR with **operator-policy constraints**:

- **Channel availability**: a hop (S_i, S_j) is only traversable if a SatChannel between them is ACTIVE and has sufficient balance.
- **Fairness weighting**: paths are weighted to balance load across operator pairs, implementing a max-min fairness objective.
- **Predictive balance**: routing avoids hops where the predicted balance at contact time is below `T_low`.

### 15.4 Multi-Operator Path Accounting

For a 3-hop path: GN → S_a (OpA) → S_b (OpB) → S_c (OpC) → GN_dst:

```
Traffic flow:   GN ──► S_a ──► S_b ──► S_c ──► GN_dst
                        OpA     OpB     OpC

ISL accounting:       [A→B ch]  [B→C ch]
BalProof updates:     σ(A,B)    σ(B,C)
Settlement:           per pair, independent
```

S_b acts as a transit node: its balance on the A→B channel increases (receives forwarding credit), while its balance on the B→C channel decreases (pays for onward forwarding). Net fairness is maintained across the full contact plan horizon.

---

## 16. Experimental Design and Evaluation Metrics

### 16.1 Experiment Configurations

| Config | Operators | Sats/Op | Duration | Traffic Load | Adversarial? |
|---|---|---|---|---|---|
| Baseline | 3 | 10 | 1 orbit (90 min) | 50% capacity | No |
| Medium scale | 6 | 20 | 24 hours | 70% capacity | No |
| Full scale | 10 | 50 | 7 days | Variable | No |
| Adversarial-1 | 6 | 20 | 24 hours | 70% | 1 rollback attacker |
| Adversarial-2 | 6 | 20 | 24 hours | 70% | 1 selective forwarder |
| Depletion | 3 | 10 | 1 orbit | 95% capacity | No |
| Top-up | 3 | 10 | 24 hours | 80% (with top-ups) | No |

### 16.2 Metrics

| Metric | Definition | Target |
|---|---|---|
| **Jain's Fairness Index** | J = (Σx_i)² / (n·Σx_i²) where x_i = forwarded/received ratio | J ≥ 0.95 |
| **Free-rider prevention rate** | % of attempted free-riding attacks penalized | 100% |
| **Settlement latency** | Time from trigger to `finalizeSettlement()` | ≤ T_challenge + 1 orbit |
| **ISL out-of-service time** | Total pause duration / total potential contact time | ≤ 2% |
| **Hash chain storage overhead** | Bytes per forwarding event | ≤ 256 bytes |
| **Authentication overhead** | Crypto ops per ISL contact setup | ≤ 10ms (simulated) |
| **Top-up success rate** | Confirmed top-ups / requested top-ups | ≥ 99% |
| **Routing efficiency** | Delivered flows / generated flows | ≥ 98% |
| **Penalty detection time** | Time from rollback attempt to penalty enforcement | ≤ T_challenge |

---

## 17. Implementation Roadmap and Milestones

### Phase 1: Foundation (Months 1–2)

- [ ] M1.1: Literature review and gap analysis (finalize §4)
- [ ] M1.2: TLE and GS data loader (`lios/data/`); SGP4-based sat-sat and GS-sat window calculator; ContactPlan CSV output
- [ ] M1.3: Core DES event loop (time-stepped, priority queue)
- [ ] M1.4: Satellite and ground station node skeletons
- [ ] M1.5: Basic traffic generator (Poisson flows, random src/dst selection, exponential sizes)
- [ ] M1.6: CGR shortest-path implementation (Dijkstra over time-expanded graph)

**Deliverable**: Running emulator that loads real TLE data, computes contact windows, generates traffic, and routes it over the contact plan without any cryptography or settlement.

### Phase 2: Cryptographic Off-Chain Protocol (Months 3–4)

- [ ] M2.1: ECDSA key generation and certificate format
- [ ] M2.2: Two-tier key hierarchy (operator CA → satellite operational keys)
- [ ] M2.3: On-contact authentication protocol (Hello/Auth exchange)
- [ ] M2.4: BalProof construction, signing, and verification
- [ ] M2.5: Hash chain implementation (SHA-256 chain over forwarding events)
- [ ] M2.6: Revocation cache and CRL delta propagation
- [ ] M2.7: Off-chain state machine (HELLO → SYNC → FORWARD → COMMIT)
- [ ] M2.8: Balance update on each forwarding batch

**Deliverable**: Satellites authenticating each other, maintaining cryptographically signed balance state, and hash-chaining all forwarding events.

### Phase 3: Hyperledger Fabric Smart Contracts (Months 4–5)

- [ ] M3.1: Local Fabric network setup via Fablo (`lios/fablo/fablo-config.json`, `fablo up`)
- [ ] M3.2: MSP configuration for each operator
- [ ] M3.3: Chaincode: `openOperatorChannel`, `registerSatChannel`
- [ ] M3.4: Chaincode: `initiateSettlement`, `challengeSettlement`, `finalizeSettlement`
- [ ] M3.5: Chaincode: `requestTopUp`, `confirmTopUp`
- [ ] M3.6: Chaincode: `recordISLPause`, `recordISLResume`
- [ ] M3.7: Chaincode: `getPendingNotifications`, `acknowledgeNotification`
- [ ] M3.8: Chaincode: `registerSatelliteKey`, `revokeSatelliteKey`, `getSatelliteKey`
- [ ] M3.9: Event subscription in ground station nodes

**Deliverable**: Full Fabric chaincode deployed and tested with unit tests covering all functions and adversarial scenarios.

### Phase 4: Settlement Integration (Months 5–6)

- [ ] M4.1: Settlement trigger evaluation engine in satellite nodes
- [ ] M4.2: TX log offloading to ground station on contact
- [ ] M4.3: Ground station settlement manager (submit, monitor, finalize)
- [ ] M4.4: Mutual settlement coordination protocol
- [ ] M4.5: Unilateral settlement with challenge window monitoring
- [ ] M4.6: ISL pause/resume state machine
- [ ] M4.7: Notification bundle construction and delivery
- [ ] M4.8: Top-up request flow (satellite → GS → Fabric → counterpart GS → satellite)

**Deliverable**: End-to-end settlement working in emulator; penalties applied correctly in adversarial scenarios.

### Phase 5: Evaluation and Validation (Months 7–8)

- [ ] M5.1: Metrics collection framework
- [ ] M5.2: Run all experiment configurations (§16.1)
- [ ] M5.3: Adversarial scenario validation
- [ ] M5.4: Scalability analysis (up to 10 operators × 50 sats)
- [ ] M5.5: Sensitivity analysis (T_challenge, H_max, P values)
- [ ] M5.6: Comparison with baseline (no settlement) and centralised settlement

**Deliverable**: Full evaluation results; paper draft.

### Phase 6: Thesis / Paper Writeup (Months 9–10)

- [ ] M6.1: Protocol specification (formal notation)
- [ ] M6.2: Security proofs (informal: revocation completeness, penalty sufficiency)
- [ ] M6.3: Results analysis and visualisation
- [ ] M6.4: Conclusion and future work

---

## 18. GitHub Copilot Prompts for Implementation

The following prompts are ordered by implementation phase. Each prompt is self-contained and can be pasted directly into GitHub Copilot Chat.

---

### Phase 1 Prompts

#### P1.1 — TLE Loader, GS Loader, and Contact Window Calculator

```
Create three Python modules in `lios/contact_plan/`:

─── tle_loader.py ────────────────────────────────────────────────────────────
Load TLE files from `lios/data/`.  Each file is named `<operator>.tle` and
contains one or more 3-line TLE entries (name line + TLE line 1 + TLE line 2).
The operator name is the file stem.

TLELoader class:
  - load_all(data_dir: Path) -> Dict[str, List[Satellite]]
      Scans data_dir for *.tle files; parses each with `sgp4` (EarthSatellite).
      Returns {operator_name: [EarthSatellite, ...]}.
  - satellite_id(sat: EarthSatellite) -> str
      Returns "<operator>-<sat.name>" (spaces → underscores, lower-case).

─── gs_loader.py ─────────────────────────────────────────────────────────────
Load ground station configs from `lios/data/`.  Each file is named
`<operator>.json` and contains an array of objects:
  { "name": str, "lat_deg": float, "lon_deg": float,
    "alt_m": float, "min_elevation_deg": float }

GSLoader class:
  - load_all(data_dir: Path) -> Dict[str, List[GroundStation]]
      Scans data_dir for *.json files; parses each entry into a GroundStation
      dataclass.  Operator name from filename stem.
  GroundStation dataclass:
    gs_id: str          # "<operator>-<name>" (normalised)
    operator_id: str
    lat_deg: float
    lon_deg: float
    alt_m: float
    min_elevation_deg: float

─── window_calculator.py ─────────────────────────────────────────────────────
Compute sat-sat and GS-sat contact windows from loaded TLEs and GS positions.
Use `sgp4`/`skyfield` for position propagation; use `wgs84` geodetic model.

Parameters:
  t_start: datetime (UTC)          # simulation start
  t_end:   datetime (UTC)          # simulation end
  time_step_sec: int = 10          # propagation resolution (seconds)
  isl_max_range_km: float = 2500   # sat-sat contact threshold

WindowCalculator class:
  - compute(operators: Dict[str, List[Satellite]],
            ground_stations: Dict[str, List[GroundStation]]) -> ContactPlan:

    1. Propagate all satellites at each time step over [t_start, t_end].
    2. Sat-sat contacts: for every cross-operator pair (OpX-satI, OpY-satJ),
       record a contact window when separation_km < isl_max_range_km.
       Also compute same-operator ISL windows for intra-constellation links.
    3. GS-sat contacts: for each (gs, sat) pair, compute elevation angle using
       the topos-observer model; record a window when elevation > gs.min_elevation_deg.
    4. Merge consecutive time steps into [start_time_sec, end_time_sec] intervals.
    5. Estimate capacity_kbps as a function of range: capacity = C_max * (1 - range/max_range)
       where C_max = 10000 for ISL, 50000 for GS links (kbps).

  Output CSV columns (written to `lios/contact_plan/contact_plan.csv`):
    contact_id, from_node, to_node, start_time_sec, end_time_sec,
    capacity_kbps, range_km, node_type_from (SAT/GS),
    node_type_to (SAT/GS), operator_from, operator_to

ContactPlan class (wraps the CSV):
  - get_contacts_at(t: float) -> List[Contact]
  - get_contacts_for_node(node_id: str) -> List[Contact]
  - get_reachable_satellites(ground_node: str, t_start: float, t_end: float) -> List[str]
  - get_all_satellites() -> List[str]
  - get_ground_stations() -> List[str]

Entry point `lios/contact_plan/compute_windows.py`:
  Reads data_dir (default: `lios/data/`), t_start, t_end, time_step from CLI args;
  runs TLELoader + GSLoader + WindowCalculator; writes contact_plan.csv.
  Example:
    python compute_windows.py --start 2025-01-01T00:00:00Z \
                               --end   2025-01-02T00:00:00Z \
                               --step  10
```

#### P1.2 — Discrete Event Simulator Core

```
Create a Python module `simulator.py` implementing a discrete-event simulation (DES) engine.

Requirements:
  - Priority queue (heapq) for events ordered by simulation time.
  - Event types: ISL_OPEN, ISL_CLOSE, GS_CONTACT_START, GS_CONTACT_END, 
    TRAFFIC_ARRIVE, SETTLEMENT_TRIGGER, NOTIFICATION_DELIVER.
  - Event dataclass: {event_id, time, type, from_node, to_node, payload}.
  - SimulationClock: current_time, advance(dt).
  - EventLoop.run(until: float): processes events in time order, dispatching 
    to registered handlers via observer pattern.
  - Node registry: register_node(node_id, handler_object).
  - Statistics collector: records all events with timestamps for post-analysis.
  - Reproducible runs via configurable random seed.

Nodes handle events via: node.handle_event(event: SimEvent) -> List[SimEvent]
The returned events are enqueued for future processing.
```

#### P1.3 — Contact Graph Routing (CGR)

```
Implement `cgr.py`: Contact Graph Routing for DTN/LEO networks.

Inputs:
  - contact_plan: ContactPlan object
  - src_satellite: str
  - dst_satellite: str  
  - t_start: float (current simulation time)
  - k: int = 3 (number of paths to return)

Algorithm:
  1. Build time-expanded directed graph from contact plan.
     - Node: (node_id, contact_id) tuple.
     - Edge weight: propagation delay + estimated queuing delay.
     - Edge constraint: departure_time >= contact.start_time.
  2. Apply modified Dijkstra (earliest-arrival algorithm) to find minimum-latency path.
  3. Apply Yen's K-shortest paths to find K alternatives.
  4. Policy filter: exclude hops where sat_channel status != ACTIVE (query ChannelRegistry).
  5. Fairness tie-breaking: prefer paths that balance load across operator pairs 
     (max-min fairness over inter-operator hop counts).

Return: List[Path] where Path = {hops: List[str], contacts: List[str], 
        latency_sec: float, operator_sequence: List[str]}

Include unit tests using a toy 3-operator 6-satellite contact plan.
```

#### P1.4 — Traffic Generator

```
Implement `traffic_generator.py` for synthetic ISL traffic generation.

TrafficFlow dataclass:
  flow_id, source_ground_node, src_satellite, dst_satellite, 
  path, size_kb, generated_at, priority (int 1-3)

TrafficGenerator class:
  - Poisson arrival process (configurable rate lambda in flows/sec per ground node).
  - For each arrival:
    1. Randomly select a ground node from the contact plan.
    2. Find satellites reachable from that ground node in [t_now, t_now + lookahead_sec].
    3. Randomly select src_satellite from reachable set (weighted by upcoming contact duration).
    4. Randomly select dst_satellite from all satellites (different operator preferred, 
       configurable bias factor).
    5. Compute path using CGR.
    6. Flow size: log-normal distribution (mean=1MB, sigma=2MB, min=10KB, max=100MB).
  - Returns: TrafficFlow or None (if no valid path found).
  - Log statistics: flows generated, flows with no path, mean path hops, 
    operator-pair distribution of paths.
```

---

### Phase 2 Prompts

#### P2.1 — Key Hierarchy and Certificate Format

```
Implement `crypto/key_hierarchy.py` using Python `cryptography` library.

OperatorCA class:
  - Generates ECDSA P-256 root key pair.
  - issue_satellite_cert(satellite_id, pubkey_pem, valid_days=90) -> SatelliteCert
  - revoke(satellite_id) -> void (adds to local CRL)
  - get_crl_delta(since: datetime) -> List[str]  # revoked satellite IDs

SatelliteCert dataclass:
  version, satellite_id, operator_id, public_key_pem, 
  valid_from, valid_until, permitted_operators, 
  signature (ECDSA by operator intermediate key over DER-encoded fields)

SatelliteKeyStore class (runs on satellite):
  - Stores N operator root public keys (N ≤ 20).
  - verify_peer_cert(cert: SatelliteCert) -> bool
    1. Look up operator root key by cert.operator_id.
    2. Verify cert.signature.
    3. Check validity window.
    4. Check revocation cache.
  - update_revocation_cache(crl_delta: List[str]) -> void

Include unit tests:
  - Valid cert verifies correctly.
  - Expired cert fails.
  - Revoked cert fails after cache update.
  - Cert from unknown operator fails.
```

#### P2.2 — Hash Chain Logger

```
Implement `crypto/hash_chain.py` for per-channel forwarding history.

HashChainEntry dataclass:
  seq_num: int
  prev_hash: str         # hex SHA-256
  channel_id: str
  bytes_forwarded: int
  direction: str         # 'A_to_B' or 'B_to_A'
  timestamp: float       # simulation time
  peer_sig: str          # ECDSA sig by forwarding peer over (prev_hash, seq, bytes, ts)
  entry_hash: str        # SHA-256 of all above fields

HashChainLog class:
  - append(channel_id, bytes_fwd, direction, peer_sig) -> HashChainEntry
  - get_head() -> str  # current chain head hash
  - verify_chain() -> bool  # validates full chain integrity
  - get_entries_since(seq_num: int) -> List[HashChainEntry]
  - serialise(since_seq: int = 0) -> bytes  # for offloading to GS
  - length() -> int  # number of entries

Invariants (enforce with assertions):
  - Each entry's prev_hash == previous entry's entry_hash.
  - seq_num is strictly monotonically increasing.
  - entry_hash is correctly computed.

Include unit tests for append, verify, tamper detection.
```

#### P2.3 — Balance Proof and Off-Chain Protocol

```
Implement `protocol/offchain.py` — the off-chain ISL payment channel protocol.

BalanceProof dataclass:
  channel_id, seq_num, balance_a_kb, balance_b_kb,
  hash_chain_head_a, hash_chain_head_b,
  sig_a, sig_b  (both ECDSA P-256, over canonical JSON of other fields)

SatChannelState dataclass:
  channel_id, operator_channel_id,
  satellite_a_id, satellite_b_id,
  my_role: Literal['A', 'B'],
  balance_a_kb, balance_b_kb,
  seq_num, hash_chain: HashChainLog,
  latest_proof: Optional[BalanceProof],
  status: Literal['ACTIVE', 'PAUSED', 'PENDING_SETTLEMENT', 'SETTLED']

OffChainProtocol class (one instance per satellite):
  - hello(peer_cert, peer_last_proof) -> HelloResponse
      Authenticate peer, compare seq_nums, return own last proof.
  - sync(peer_hello_response) -> SyncResult
      Agree on latest seq_num; flag if peer has stale proof.
  - record_forwarding(channel_id, bytes_kb, direction) -> BalanceProof
      Deduct from own balance, increment peer balance, update hash chain,
      create new BalProof, sign it, return for peer cosignature.
  - cosign_proof(channel_id, proof: BalanceProof) -> BalanceProof
      Verify sig_a, add sig_b, update local state.
  - evaluate_settlement_triggers(channel_id) -> List[str]
      Return list of triggered condition IDs (T1..T7).
  - get_settlement_payload(channel_id) -> SettlementPayload
      Bundle: latest BalProof + hash chain entries since last settlement.

Strict invariants:
  - balance_a + balance_b must equal original allocated capacity (conservation).
  - seq_num only increases.
  - Cannot forward if balance insufficient.
```

#### P2.4 — On-Contact Authentication State Machine

```
Implement `protocol/auth.py` — the satellite-to-satellite authentication handshake.

AuthState enum: IDLE, HELLO_SENT, HELLO_RECEIVED, AUTHENTICATED, FAILED

ContactAuthSession class:
  - Manages one authentication session per ISL contact.
  - Uses ECDH (ephemeral) for session key derivation.
  - After AUTHENTICATED, all messages encrypted with AES-256-GCM using session key.

Methods:
  - initiate(my_cert, my_privkey, peer_operator_id) 
      -> HelloMessage(cert, ecdh_pub, nonce, timestamp)
  - handle_hello(hello_msg: HelloMessage, key_store: SatelliteKeyStore) 
      -> HelloResponse or AuthFailure
  - complete_auth(hello_response: HelloMessage) 
      -> SessionKey or AuthFailure
  - encrypt_message(plaintext: bytes) -> EncryptedMessage
  - decrypt_message(msg: EncryptedMessage) -> bytes

Security requirements:
  - Nonce prevents replay (store seen nonces per session, TTL = contact duration).
  - Timestamp within ±30 seconds of simulated clock.
  - ECDH provides forward secrecy.
  - Both sides verify each other's cert before completing auth.
```

---

### Phase 3 Prompts

#### P3.1 — Hyperledger Fabric Network Setup (Fablo)

```
Create `lios/fablo/fablo-config.json` — a Fablo network definition for the
LIOS Protocol settlement network.  Fablo generates all Fabric artefacts
(crypto material, configtx.yaml, docker-compose.yml, channel scripts) from
this single JSON file.  Start the network with `fablo up fablo-config.json`.

Fablo config requirements:

  Global:
    fabricVersion: "2.5.0"
    tls: true

  Organisations (3, extensible to N):
    - name: OperatorA  mspId: OperatorAMSP  peers: 2  ca: true  db: CouchDB
    - name: OperatorB  mspId: OperatorBMSP  peers: 2  ca: true  db: CouchDB
    - name: OperatorC  mspId: OperatorCMSP  peers: 2  ca: true  db: CouchDB

  Orderers:
    - type: raft  instances: 3
      One orderer organisation per operator for decentralisation.

  Channel:
    name: isl-settlement
    orgs: [OperatorA, OperatorB, OperatorC]
    endorsementPolicy: "MAJORITY"   # majority of channel members must endorse

  Chaincode:
    name: isl-settlement
    version: "1.0"
    lang: golang
    path: ../chaincode/isl_settlement
    channel: isl-settlement
    endorsementPolicy: "AND('OperatorAMSP.peer','OperatorBMSP.peer','OperatorCMSP.peer')"
      # override per §8.4 for cross-operator settlement

Directory layout:
  lios/fablo/
    fablo-config.json     # complete network definition
    README.md             # `fablo up`, `fablo chaincode install`, `fablo down`

Workflow:
  1. cd lios/fablo && fablo up fablo-config.json
     → generates crypto material, starts peers/orderers/CAs/CouchDB via Docker Compose.
  2. fablo chaincode install isl-settlement
     → packages, installs, approves, and commits the Go chaincode on all peers.
  3. fablo down  → tears down all containers and volumes.

No manual cryptogen, configtx.yaml, or docker-compose.yml editing is required.
```

#### P3.2 — Chaincode: Channel Lifecycle (Go)

```
Write Hyperledger Fabric chaincode in Go using the fabric-contract-api-go SDK.

Files:
  lios/chaincode/isl_settlement/
    main.go          # fabric.Start(new(ISLSettlementChaincode))
    chaincode.go     # ISLSettlementChaincode struct, all SmartContract methods
    types.go         # All struct definitions
    utils.go         # ECDSA signature verification, JSON helpers

types.go — key structs (match the data model in §8.2 exactly):
  OperatorChannel  { ID, OperatorA, OperatorB, BalanceA, BalanceB,
                     PenaltyReserveA, PenaltyReserveB, Status, CreatedAt }
  SatChannel       { ID, OperatorChannelID, SatA, SatB,
                     BalanceA, BalanceB, SeqNum, Status, PausedAt, ResumedAt }
  BalanceProof     { ChannelID, SeqNum, BalanceA, BalanceB,
                     HashHeadA, HashHeadB, SigA, SigB, Timestamp }
  OutOfServiceEntry { ChannelID, PausedAt, ResumedAt,
                      SatAAckAt, SatBAckAt, InitiatedBy }
  SatKey           { SatelliteID, OperatorID, PubKeyPEM,
                     IsRevoked, RegisteredAt, RevokedAt }

chaincode.go — implement these contract functions:

func (c *ISLSettlementChaincode) OpenOperatorChannel(
    ctx contractapi.TransactionContextInterface,
    operatorA, operatorB string,
    balanceA, balanceB, reserveA, reserveB float64) error
  - Validate channel does not already exist between this pair (composite key).
  - Validate caller MSP is OperatorA or OperatorB (ctx.GetClientIdentity().GetMSPID()).
  - Require endorsement from BOTH operator MSPs.
  - Store OperatorChannel with status "OPEN".
  - Emit CHANNEL_OPENED event.

func (c *ISLSettlementChaincode) RegisterSatChannel(
    ctx contractapi.TransactionContextInterface,
    operatorChannelID, satA, satB string,
    balA, balB float64) error
  - Validate parent OperatorChannel exists and is OPEN.
  - Validate allocated balances ≤ remaining operator channel balance pool.
  - Deduct from operator channel balance pool.
  - Store SatChannel with status "ACTIVE".

func (c *ISLSettlementChaincode) InitiateSettlement(
    ctx contractapi.TransactionContextInterface,
    satChannelID string, proof BalanceProof) error
  - Recover satellite public keys from on-chain SatKey registry.
  - Verify ECDSA signatures SigA and SigB over the canonical JSON of the proof.
  - Validate proof.SeqNum > current channel SeqNum.
  - Set status "PENDING_SETTLEMENT"; record submission timestamp (T_challenge starts).
  - Emit SETTLEMENT_INITIATED event.

func (c *ISLSettlementChaincode) ChallengeSettlement(
    ctx contractapi.TransactionContextInterface,
    satChannelID string, counter BalanceProof) error
  - Must be called within T_challenge window.
  - Validate counter.SeqNum > submitted proof SeqNum.
  - If valid: open dispute, record both proofs, slash penalty reserve of initiating party.
  - Emit DISPUTE_OPENED, PENALTY_APPLIED events.

func (c *ISLSettlementChaincode) FinalizeSettlement(
    ctx contractapi.TransactionContextInterface,
    satChannelID string) error
  - Must be called after T_challenge has expired (check block timestamp).
  - Use latest valid proof (submitted or counter-proof).
  - Update OperatorChannel balance totals.
  - Set SatChannel status "SETTLED".
  - Emit SETTLEMENT_FINALIZED event.

go.mod:
  module github.com/lios-protocol/chaincode/isl_settlement
  require github.com/hyperledger/fabric-contract-api-go v1.2.1
```

#### P3.3 — Chaincode: Top-Up, Key Management, and OOS Logging (Go)

```
Continue `lios/chaincode/isl_settlement/chaincode.go` with additional functions:

func (c *ISLSettlementChaincode) RequestTopUp(
    ctx contractapi.TransactionContextInterface,
    operatorChannelID string, amountA, amountB float64) error
  - Create TopUpRequest{ID, OperatorChannelID, AmountA, AmountB,
                         RequestedBy, Status: "PENDING",
                         ExpiresAt: now + T_TOPUP_CONFIRM (default 86400s)}.
  - Emit TOPUP_REQUESTED event.

func (c *ISLSettlementChaincode) ConfirmTopUp(
    ctx contractapi.TransactionContextInterface, topUpID string) error
  - Validate caller MSP is the counterpart operator.
  - Validate request not expired.
  - Update OperatorChannel balances (+AmountA, +AmountB).
  - Set TopUpRequest status "CONFIRMED".
  - Emit TOPUP_CONFIRMED event.

func (c *ISLSettlementChaincode) RegisterSatelliteKey(
    ctx contractapi.TransactionContextInterface,
    operatorID, satelliteID, pubKeyPEM, operatorCertSig string) error
  - Verify operatorCertSig (ECDSA P-256) over SHA-256(satelliteID || pubKeyPEM)
    using the operator's on-chain root public key.
  - Store SatKey{SatelliteID, OperatorID, PubKeyPEM, IsRevoked: false, RegisteredAt}.
  - Emit KEY_REGISTERED event.

func (c *ISLSettlementChaincode) RevokeSatelliteKey(
    ctx contractapi.TransactionContextInterface,
    operatorID, satelliteID string) error
  - Validate caller MSP owns the satellite (ctx MSP == SatKey.OperatorID).
  - Set IsRevoked = true, RevokedAt = now.
  - Emit KEY_REVOKED event (all subscribers update their local CRL).

func (c *ISLSettlementChaincode) GetSatelliteKey(
    ctx contractapi.TransactionContextInterface,
    satelliteID string) (*SatKey, error)

func (c *ISLSettlementChaincode) RecordISLPause(
    ctx contractapi.TransactionContextInterface,
    satChannelID, reason string) error
  - Append OutOfServiceEntry{ChannelID, PausedAt: now, ResumedAt: nil,
                              InitiatedBy: caller MSP}.
  - Set SatChannel status "PAUSED_PENDING_SETTLEMENT".
  - Emit ISL_PAUSED event.

func (c *ISLSettlementChaincode) RecordISLResume(
    ctx contractapi.TransactionContextInterface,
    satChannelID, satelliteID string) error
  - Find the latest OutOfServiceEntry for this channel with ResumedAt == nil.
  - Set the ack timestamp for the calling satellite (SatAAckAt or SatBAckAt).
  - Only set ResumedAt = max(SatAAckAt, SatBAckAt) when BOTH are non-nil.
  - Emit ISL_RESUMED event when ResumedAt is set.

func (c *ISLSettlementChaincode) GetPendingNotifications(
    ctx contractapi.TransactionContextInterface,
    operatorID string) ([]*SettlementNotification, error)
  - Rich CouchDB query: selector {targetOperator: operatorID, acknowledged: false}.

func (c *ISLSettlementChaincode) AcknowledgeNotification(
    ctx contractapi.TransactionContextInterface,
    notifID string) error
  - Set Acknowledged = true, AcknowledgedAt = now.

utils.go — ECDSA helpers:
  VerifyECDSA(pubKeyPEM, messageJSON, sigHex string) bool
    Parse PEM → ecdsa.PublicKey; decode signature from DER hex; verify SHA-256 digest.
  CanonicalJSON(v any) ([]byte, error)
    Marshal with sorted keys for deterministic signature inputs.
```

---

### Phase 4 Prompts

#### P4.1 — Settlement Manager (Ground Station)

```
Implement `ground/settlement_manager.py`.

SettlementManager class (runs on each GroundStationNode):
  - fabric_client: FabricClient (Fabric Python SDK wrapper)
  - pending_settlements: Dict[str, SettlementPayload]  # satChannelId -> payload

Methods:
  on_satellite_contact(sat_node: SatelliteNode):
    1. Receive TX log (SettlementPayload) from satellite.
    2. For each channel in payload:
       a. Check settlement triggers.
       b. If any trigger fired: call initiate_settlement(channel_id, proof).
       c. If no trigger but new TXs: record for later.

  initiate_settlement(channel_id, proof: BalanceProof):
    1. Submit initiateSettlement() to Fabric.
    2. Subscribe to SETTLEMENT_CHALLENGED event for this channel.
    3. Set local timer for T_challenge.
    4. Call record_isl_pause(channel_id).

  on_fabric_event(event: ChainEvent):
    - SETTLEMENT_INITIATED (from counterpart): retrieve latest BalProof from satellite,
      compare seq_nums, submit challengeSettlement() if newer proof available.
    - SETTLEMENT_FINALIZED: call record_isl_resume(channel_id), 
      notify satellite, register new SatChannel if balance remains.
    - DISPUTE_RESOLVED: log outcome, notify satellite.
    - TOPUP_REQUESTED: evaluate if top-up is needed, call confirmTopUp() if so.
    - KEY_REVOKED: update local CRL, schedule transmission to satellites on next contact.

  transmit_notifications(sat_node: SatelliteNode):
    1. Query getPendingNotifications() from Fabric.
    2. Add CRL deltas.
    3. Add latest peer BalProofs (from Fabric state).
    4. Construct NotificationBundle, sign with GS key.
    5. Transmit to satellite.
    6. Record bundle_id; expect ACK on next contact.

  evaluate_top_up_need(channel_id: str) -> bool:
    - Check if balance < T_LOW_THRESHOLD.
    - Check if both operators have ground contacts within next T_TOPUP_CONFIRM seconds.
    - Return true only if both conditions met (otherwise top-up will expire before confirmation).
```

#### P4.2 — ISL Pause/Resume State Machine

```
Implement `protocol/isl_state_machine.py`.

ISLChannelState enum: 
  ACTIVE, PAUSED_PENDING_SETTLEMENT, PAUSED_PENDING_RESUME, CLOSED

ISLStateMachine class:
  - Tracks state per (satellite_a_id, satellite_b_id) pair.
  - out_of_service_log: List[OutOfServiceEntry]

Transitions (enforce guard conditions):
  ACTIVE → PAUSED_PENDING_SETTLEMENT: 
    trigger: settlement_triggers_fired() AND contact_ended
    action: satellite locally records pausedAt = contact_end_time; GS logs OOS on Fabric on next ground contact
    guard: ISL contact must have fully ended — pause is always at contact-end boundary, never mid-contact

  PAUSED_PENDING_SETTLEMENT → PAUSED_PENDING_RESUME:
    trigger: finalizeSettlement() called on Fabric (or T_challenge expired with no dispute)
    action: both GSs receive SETTLEMENT_FINALIZED event; each GS schedules resume notification for its own satellite

  PAUSED_PENDING_RESUME → ACTIVE:
    trigger: BOTH satellites have been contacted by their respective GSs AND both have ACKed the resume notification
    action: log resumedAt = max(satA_ack_time, satB_ack_time); re-register SatChannel with new balance; emit ISL_RESUMED on Fabric

  ACTIVE → CLOSED:
    trigger: operator channel depleted AND no top-up available
    action: final settlement, close SatChannel and OperatorChannel

Methods:
  get_oos_duration(channel_id) -> float  # total out-of-service seconds
  get_oos_log(channel_id) -> List[OutOfServiceEntry]
  can_forward(channel_id) -> bool  # True only in ACTIVE state
```

---

### Phase 5 Prompts

#### P5.1 — Metrics Collector and Fairness Analysis

```
Implement `evaluation/metrics.py`.

MetricsCollector class:
  Collects and computes all metrics defined in the research plan §16.2.

  Per-operator tracking:
    bytes_forwarded_by[op] : int   # bytes this op's sats forwarded for others
    bytes_received_by[op]  : int   # bytes others forwarded for this op's traffic
    settlement_events[op]  : List
    penalty_events[op]     : List
    oos_seconds[op]        : float

  Methods:
    record_forwarding(from_sat, to_sat, bytes_kb, sim_time)
    record_settlement(sat_channel_id, duration_sec, was_disputed)
    record_penalty(cheating_operator, honest_operator, penalty_amount)
    record_oos(sat_channel_id, pause_time, resume_time)

    compute_jain_fairness_index() -> float
      # J = (sum x_i)^2 / (n * sum(x_i^2))
      # where x_i = bytes_forwarded_by[op_i] / bytes_received_by[op_i]

    compute_free_rider_prevention_rate() -> float
      # penalty_events_resolved / rollback_attempts

    compute_settlement_latency_stats() -> {mean, p50, p95, p99, max}

    compute_oos_fraction() -> float
      # total oos seconds / total potential contact seconds

    generate_report() -> dict  # all metrics as JSON-serialisable dict

    plot_fairness_over_time(output_path: str)
      # matplotlib: Jain index vs simulation time (sliding window)
    
    plot_operator_balance_evolution(output_path: str)
      # matplotlib: balance per operator pair over time

    plot_settlement_timeline(output_path: str)
      # matplotlib: Gantt-style chart of settlement events and OOS periods
```

#### P5.2 — Adversarial Scenario: Balance Rollback Attack

```
Implement `evaluation/adversarial.py` — adversarial satellite operator.

MaliciousSatelliteNode(SatelliteNode):
  Inherits from SatelliteNode but overrides settlement behaviour.

  attack_mode: Literal['rollback', 'selective_forward', 'none']

  If attack_mode == 'rollback':
    override get_settlement_payload(channel_id):
      - With probability p_attack (default 0.5), return an OLD BalanceProof 
        (one where self has higher balance) instead of the latest one.
      - Log the attack attempt.

  If attack_mode == 'selective_forward':
    override forward_traffic(flow):
      - With probability p_drop (default 0.3), drop the flow without recording it.
      - But still claim credit in the hash chain as if forwarded.

Run scenario:
  run_adversarial_scenario(attack_mode, num_operators, sats_per_op, duration):
    1. Replace one operator's satellites with MaliciousSatelliteNode instances.
    2. Run full simulation.
    3. Verify: all rollback attacks detected and penalised within T_challenge.
    4. Verify: Jain fairness with adversary < without adversary (expected).
    5. Verify: honest operators' balances are not permanently harmed.
    6. Return metrics report.

Include assertions that validate correct protocol behaviour under attack.
```

---

### Phase 6 Prompts

#### P6.1 — Integration Test Suite

```
Write a comprehensive pytest integration test suite in `tests/test_integration.py`.

Fixture: full_simulation_setup()
  - 3 operators, 5 sats each, 90-minute simulation.
  - Local Hyperledger Fabric testnet (docker-compose).
  - Pre-generated contact plan with known ISL contacts.

Test cases:
  test_channel_establishment():
    Assert OperatorChannel and SatChannels are registered on Fabric.

  test_off_chain_forwarding():
    Assert BalProofs are correctly updated after each forwarding batch.
    Assert hash chain integrity (verify_chain() == True).
    Assert balance conservation: balance_a + balance_b == initial_capacity.

  test_settlement_trigger_t1_balance_low():
    Drain channel to below T_LOW. Assert settlement initiated on next GS contact.

  test_settlement_trigger_t2_hash_chain_full():
    Generate H_MAX forwarding events. Assert settlement initiated.

  test_mutual_settlement():
    Assert ISL paused after settlement initiation.
    Assert ISL resumed after both GSs confirm.
    Assert OOS log correctly records pause/resume times.

  test_unilateral_settlement_no_dispute():
    Simulate counterpart GS offline for T_challenge. Assert finalizeSettlement() called.

  test_rollback_attack_detected():
    Use MaliciousSatelliteNode with rollback mode.
    Assert challengeSettlement() called with newer proof.
    Assert penalty applied to malicious operator.
    Assert honest operator's balance restored.

  test_top_up_flow():
    Deplete balance, request top-up. Assert confirmed within T_TOPUP_CONFIRM.
    Assert channel balance updated on Fabric.

  test_key_revocation_propagation():
    Revoke a satellite key on Fabric.
    Assert satellite receives CRL update on next GS contact.
    Assert revoked satellite fails authentication on next ISL contact.

  test_multi_hop_accounting():
    Verify 3-hop A→B→C path results in correct bilateral balance updates
    on both A-B and B-C channels.

  test_fairness_index():
    Run 24-hour simulation. Assert Jain fairness index >= 0.95.
```

#### P6.2 — Experiment Runner and Report Generator

```
Create `evaluation/run_experiments.py` — automated experiment runner.

ExperimentConfig dataclass:
  name, num_operators, sats_per_operator, duration_sec,
  traffic_load_fraction, adversarial_mode, random_seed

Experiments (matching §16.1 table): define all 7 configs as a list.

run_experiment(config: ExperimentConfig) -> ExperimentResult:
  1. Set random seed.
  2. Generate contact plan.
  3. Initialise all nodes (satellites, GSs, Fabric network).
  4. Optionally inject adversarial nodes.
  5. Run DES until config.duration_sec.
  6. Collect all metrics.
  7. Save raw event log to JSON.
  8. Return ExperimentResult.

generate_paper_figures(results: List[ExperimentResult]):
  - Figure 1: Jain fairness index across experiment configs (bar chart).
  - Figure 2: Settlement latency CDF (all configs, overlay).
  - Figure 3: OOS fraction per config (bar chart with error bars).
  - Figure 4: Balance evolution over time (3-operator baseline, line chart).
  - Figure 5: Penalty detection time CDF (adversarial configs only).
  - Figure 6: Throughput vs fairness trade-off scatter plot.
  All figures: publication quality, matplotlib, saved as PDF + PNG.

generate_latex_table(results: List[ExperimentResult]) -> str:
  Produce a LaTeX table of all metrics for all configs.

if __name__ == '__main__':
  results = [run_experiment(c) for c in EXPERIMENT_CONFIGS]
  generate_paper_figures(results)
  print(generate_latex_table(results))
```

---

## 19. Directory Structure

```
lios/
├── README.md                              # This file
├── requirements.txt                       # Python deps: sgp4, skyfield, cryptography,
│                                          #   simpy, networkx, matplotlib, requests
│
├── data/                                  # Real operator TLEs and ground station configs
│   ├── operatorA.tle                      # 3-line TLE entries for Operator A satellites
│   ├── operatorA.json                     # [{name, lat_deg, lon_deg, alt_m,
│   ├── operatorB.tle                      #    min_elevation_deg}, ...]
│   ├── operatorB.json
│   ├── operatorC.tle
│   └── operatorC.json
│                                          # Operator name = filename stem (no extension)
│
├── contact_plan/
│   ├── tle_loader.py                      # P1.1 — load *.tle from data/
│   ├── gs_loader.py                       # P1.1 — load *.json from data/
│   ├── window_calculator.py               # P1.1 — SGP4 propagation → contact windows
│   ├── compute_windows.py                 # P1.1 — CLI entry point
│   ├── contact_plan.csv                   # Generated output (git-ignored)
│   └── visualise_contact_plan.py          # Plot contact windows (matplotlib)
│
├── simulator/
│   ├── simulator.py                       # P1.2 — DES core (priority queue, event loop)
│   ├── satellite_node.py                  # P2.3 + P4.2 — satellite node
│   ├── ground_station_node.py             # P4.1 — ground station node
│   └── traffic_generator.py              # P1.4 — Poisson traffic flows
│
├── routing/
│   └── cgr.py                             # P1.3 — Contact Graph Routing (Yen's K-paths)
│
├── crypto/
│   ├── key_hierarchy.py                   # P2.1 — operator CA, satellite certs
│   └── hash_chain.py                      # P2.2 — SHA-256 hash chain logger
│
├── protocol/
│   ├── offchain.py                        # P2.3 — BalProof, SatChannelState
│   ├── auth.py                            # P2.4 — on-contact ECDH authentication
│   └── isl_state_machine.py               # P4.2 — ISL pause/resume FSM
│
├── ground/
│   └── settlement_manager.py              # P4.1 — GS settlement manager
│
├── fablo/
│   ├── fablo-config.json                  # P3.1 — Fablo network definition
│   └── README.md                          # fablo up / fablo down instructions
│
├── chaincode/
│   └── isl_settlement/
│       ├── go.mod                         # module github.com/lios-protocol/chaincode/...
│       ├── go.sum
│       ├── main.go                        # fabric.Start entry point
│       ├── chaincode.go                   # P3.2 + P3.3 — all SmartContract methods
│       ├── types.go                       # OperatorChannel, SatChannel, BalanceProof, ...
│       └── utils.go                       # VerifyECDSA, CanonicalJSON helpers
│
├── evaluation/
│   ├── metrics.py                         # P5.1 — Jain index, OOS fraction, etc.
│   ├── adversarial.py                     # P5.2 — rollback and selective-forward attacks
│   └── run_experiments.py                 # P6.2 — experiment runner and report generator
│
├── tests/
│   ├── test_integration.py                # P6.1 — end-to-end integration tests
│   ├── test_cgr.py
│   ├── test_crypto.py
│   ├── test_offchain.py
│   └── chaincode/                         # Go unit tests
│       └── chaincode_test.go
│
└── results/
    ├── figures/                           # Generated experiment figures
    └── logs/                             # Raw simulation logs
```

---

## 20. References

1. Poon, J., & Dryja, T. (2016). *The Bitcoin Lightning Network: Scalable Off-Chain Instant Payments*. USENIX.
2. Miller, A., Bentov, I., Kumaresan, R., & McCorry, P. (2017). *Sprites and State Channels: Payment Networks that Go Faster than Lightning*. arXiv.
3. Burleigh, S. C. (2011). *Contact Graph Routing*. IETF RFC 6693.
4. Bera, B., Saha, S., Das, A. K., & Vasilakos, A. V. (2021). *Designing Blockchain-Based Access Control Protocol in IoT-Enabled Smart-Grid System*. IEEE IoT Journal.
5. Hyperledger Foundation (2023). *Hyperledger Fabric v2.5 Documentation*. https://hyperledger-fabric.readthedocs.io/
6. CCSDS (2019). *Space Data Link Security Protocol — Summary of Concept and Rationale*. CCSDS 350.0-G-3.
7. Jain, R., Chiu, D., & Hawe, W. (1984). *A Quantitative Measure of Fairness and Discrimination for Resource Allocation in Shared Systems*. DEC Technical Report.
8. Wood, A., et al. (2023). *LEO Satellite Constellation Routing: Survey and Open Challenges*. IEEE Communications Surveys & Tutorials.
9. Qian, Y., et al. (2022). *Blockchain-Based Distributed Authentication for Satellite Networks*. IEEE Wireless Communications.
10. Li, Z., et al. (2023). *Delay-Tolerant Blockchain for Satellite IoT Data Integrity*. IEEE Transactions on Vehicular Technology.
11. ESA (2022). *Multi-Operator Constellation Interoperability Framework*. ESA Technical Memorandum.
12. ITU-R (2023). *Radio Regulations and Non-GSO Constellation Coordination Procedures*. ITU-R S.1503.

---

*LIOS Protocol Research Plan | Version 1.0 | Generated for PhD/Research Programme Use*
*All protocol designs, smart contract specifications, and implementation prompts are original contributions.*

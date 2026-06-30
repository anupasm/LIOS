# Current Experiment Settings

This document describes the effective settings for the experiments enabled in
`run_experiments.py`. It reflects the repository state as of 2026-06-29.

## Configuration precedence

1. Values set directly in `EXPERIMENT_CONFIGS` override `config.toml`.
2. An experiment with `operator_load_weights = None` derives weights from the
   satellite counts in the TLE files loaded through `--data`.
3. Remaining settings come from `lios/config.toml`, or from the file selected by
   the `LIOS_CONFIG` environment variable.
4. `--data` defaults to `lios/data`; `--out` defaults to `results`.

The configured TOML traffic rate is 0.6 flows/s, but both current experiments
override it with an effective rate of 0.3 flows/s. The TOML `op1`-`op4` weights
are also not used by these experiments.

## Enabled experiments

| Setting | LIOS | Ground-reset baseline |
|---|---:|---:|
| Name | `lios_constellation_weighted` | `ground_reset_constellation_weighted` |
| Protocol selector | `lios` | `ground_reset` |
| Simulated duration | 86,400 s (24 h) | 86,400 s (24 h) |
| Traffic arrival rate | 0.3 flows/s | 0.3 flows/s |
| Random seed | 42 | 42 |
| Adversarial mode | `none` | `none` |
| ISL search range | 4,000 km | 4,000 km |
| Orbital time step | 30 s | 30 s |
| Operator weighting | TLE constellation size | TLE constellation size |
| Ground ledger | In-memory `FabricMock` | In-memory `GroundResetFabricMock` |

Both runs use the same contact plan, random seed, arrival process, traffic
schedule, operator weights, satellite data, and ground-station data. The intended
experimental variable is the protocol behavior.

Greedy, tit-for-tat, central-authority, adversarial, depletion, top-up, fairness,
and high-density configurations are disabled.

## Constellations and operator weights

The default dataset contains 12,345 satellites across 18 operators. For operator
`i`, the effective source weight is:

```text
weight_i = satellites_i / 12,345
```

| Operator | Satellites | Effective weight |
|---|---:|---:|
| `spacex_starlink` | 10,655 | 0.863102471 |
| `eutelsat_oneweb` | 651 | 0.052733900 |
| `amazon_project_kuiper` | 364 | 0.029485622 |
| `g60_starlink` | 200 | 0.016200891 |
| `planet_labs` | 118 | 0.009558526 |
| `spire_global` | 83 | 0.006723370 |
| `iridium_communications` | 80 | 0.006480356 |
| `ses` | 60 | 0.004860267 |
| `intelsat` | 42 | 0.003402187 |
| `globalstar` | 28 | 0.002268125 |
| `orbcomm` | 14 | 0.001134062 |
| `china_satnet_guowang` | 13 | 0.001053058 |
| `echostar` | 13 | 0.001053058 |
| `inmarsat` | 13 | 0.001053058 |
| `viasat` | 5 | 0.000405022 |
| `lynk_global` | 4 | 0.000324018 |
| `ast_spacemobile` | 1 | 0.000081004 |
| `kepler_communications` | 1 | 0.000081004 |
| **Total** | **12,345** | **1.000000000** |

These values multiply directed active-pair sampling weights. They are not fixed
traffic quotas: the realized source distribution also depends on which
cross-operator contacts are active at each arrival time.

## Traffic model

| Setting | Effective value |
|---|---:|
| Arrival process | Global Poisson process |
| Global rate | 0.3 flows/s during periods with an eligible active ISL |
| Allocation | `uniform_active_pair`, modified by source-operator weights |
| Eligible traffic | Direct cross-operator active ISL pairs only |
| Route length | One hop; no end-to-end route calculation |
| Canonical direction bias | 0.5 |
| Flow-size distribution | Log-normal |
| Log-normal median | 1,024 KB |
| Log-normal sigma | 1.0 |
| Minimum flow size | 1,024 KB |
| Maximum flow size | 10,240 KB |
| Default flow priority | 2 |

At each arrival, the generator enumerates both directions for every active
cross-operator pair. Direction weight is the source operator's constellation
weight multiplied by the 0.5 direction prior. The global arrival rate does not
scale with satellite, ground-station, or active-pair count.

## Orbit and contact-plan settings

| Setting | Effective value |
|---|---:|
| Contact-plan epoch | 2026-06-14 00:00:00 UTC |
| Contact-plan end | Epoch + 86,400 s |
| Propagation model | SGP4 with WGS-84 |
| Sampling step | 30 s |
| Broad ISL search ceiling | 4,000 km |
| Maximum retained ISLs per satellite | 4 |
| Ground-station minimum elevation | 5 degrees |
| Speed of light | 299,792.458 km/s |
| Peak ISL capacity | 10,000 kbps |
| Peak GS capacity | 50,000 kbps |

Only cross-operator satellite pairs are emitted as ISL contacts. Each satellite
retains at most its four strongest candidate partners by in-range sample count.
Bilateral visibility uses the stricter endpoint criterion. ISL capacity decreases
linearly with mean range relative to the resolved maximum range. GS capacity
decreases linearly with mean range relative to 3,000 km.

### ISL visibility profiles

| Profile | Range km | Clearance km | Tangent depression | Pointing half-angle | Radial velocity km/s | Minimum duration s | Carrier GHz | EIRP dBW | RX gain dB | Sensitivity dBW |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| Default | 1,500 | 80 | 45 deg | 45 deg | 4.0 | 2.0 | 193,000 | 145 | 145 | -42 |
| Starlink | 1,750 | 80 | 26 deg | 26 deg | 4.0 | 2.0 | 193,000 | 145 | 145 | -42 |
| OneWeb | 2,500 | 20 | 60 deg | 60 deg | 1.5 | 0.5 | 60 | 65 | 45 | -38 |
| Telesat | 3,500 | 80 | 45 deg | 45 deg | 4.0 | 5.0 | 193,000 | 151 | 151 | -42 |
| Kuiper | 3,000 | 80 | 45 deg | 45 deg | 4.0 | 2.0 | 193,000 | 150 | 150 | -42 |

Profile aliases used by the loaded satellite operators are:

| Operators | Profile |
|---|---|
| `spacex_starlink`, `g60_starlink` | Starlink |
| `eutelsat_oneweb` | OneWeb |
| `amazon_project_kuiper` | Kuiper |
| All other loaded operators | Default |

The Telesat profile is configured but no Telesat satellite TLE is present in the
current dataset.

## Ground infrastructure

The default dataset contains 277 stations from 21 ground-station operators. A
single shared pending-notification registry is used, so any authorized ground
station can deliver a pending notification to any satellite it contacts. Uplink
and downlink propagation delay is computed as range divided by the configured
speed of light.

## Shared channel and security settings

| Setting | Effective value |
|---|---:|
| Initial bilateral balance per satellite side | 1,000,000,000 KB (1 TB) |
| Total initial bilateral channel pool | 2,000,000,000 KB (2 TB) |
| Low-balance trigger fraction | 0.05 of total pool |
| Effective T1 threshold | 100,000,000 KB (100 GB) on either side |
| Session-volume T7 threshold | 100,000,000 KB (100 GB) |
| Challenge window | 172,800 s (48 h) |
| Top-up confirmation deadline | 86,400 s |
| Authentication timestamp tolerance | 30 s |
| ECDH nonce length | 32 bytes |
| Satellite certificate validity | 90 days |

Each operator receives an operator CA. Satellite certificates and operational
keys are created before simulation. LIOS settlement proofs are signed by both
endpoint satellites. Ground-reset reports are signed independently by each
endpoint and reconciled by the ground ledger.

Adversarial behavior is disabled. The dormant defaults are rollback attack
probability `0.5` and selective-forward drop probability `0.3`.

## LIOS behavior

The LIOS run uses normal `SatelliteNode`, `GroundStationNode`, and `FabricMock`
implementations.

1. Traffic updates the bilateral off-chain balance and monotonic sequence number.
2. At cross-operator contact end, LIOS evaluates T1 and T7.
3. T1 fires if either side falls below 100,000,000 KB (100 GB).
4. T7 fires when cumulative session forwarding reaches 100,000,000 KB (100 GB).
5. If a trigger fires, both endpoints co-sign the latest proof and pause the
   channel while settlement is uploaded at a subsequent ground contact.
6. T1 requests a balance reset. T7 performs settlement without requesting the
   T1 reset path.
7. The channel resumes through ground-delivered notification and returns to an
   equal balance when `resume_channel` executes.

No settlement is performed between satellites belonging to the same operator.

## Ground-reset baseline behavior

The baseline uses `GroundResetSatelliteNode`, `GroundResetGroundStationNode`, and
`GroundResetFabricMock`.

1. Traffic is forwarded without per-flow off-chain balance or proof updates.
2. Every established cross-operator ISL contact end applies one aggregate balance
   update per endpoint, including a zero-byte update for contacts with no traffic.
3. Both endpoint channels pause and each endpoint independently uploads its
   contact-end report during a ground-station contact; there is no ISL proof
   exchange.
4. The first upload remains pending. After both endpoint reports arrive, the
   ledger nets their forwarding totals and commits the resulting balance reset.
5. Until commit and ground delivery of resume notifications to both endpoints,
   subsequent ISL opens for that satellite pair are rejected.
6. On resume, both channel balances return to 1,000,000,000 KB and cumulative forwarded
   volume returns to zero.

The in-memory ledger applies the configured 2 s commit latency to finalization
logging. Resume delivery still depends on later physical ground contacts.

## Ledger settings

The default experiments do not use a deployed Hyperledger Fabric network. Their
in-memory ledger settings are:

| Setting | Value |
|---|---:|
| Simulated commit latency | 2.0 s |
| Operator channel balance per side | 1,000,000,000,000 KB (1 PB) |
| Operator penalty reserve | 10,000,000 KB |
| Real Fabric network config | `lios/blockchain/network_config.json` |

The `--blockchain` CLI flag selects a real `FabricClient`. In the current code,
that mode is intended for LIOS and is not compatible with the ground-reset
baseline's required `GroundResetFabricMock` type.

## Cache and outputs

Contact plans are cached by duration, time step, and broad ISL range, with the
epoch validated in cache metadata. Traffic schedule filenames are additionally
keyed by epoch, random seed, arrival rate, and the full operator-weight mapping.

With the default `--out results`, each experiment writes:

- `results/logs/<experiment>_metrics.json`
- `results/logs/<experiment>_contact_plan.csv`
- `results/logs/<experiment>_contact_traffic.json`
- `results/logs/<experiment>_propagation_log.json` when the plan is computed
- `results/logs/<experiment>_balance_events.json`
- comparison figures under `results/figures/`

## Commands

Run both enabled experiments:

```bash
.env/bin/python lios/evaluation/run_experiments.py
```

Run one experiment:

```bash
.env/bin/python lios/evaluation/run_experiments.py \
  --config lios_constellation_weighted

.env/bin/python lios/evaluation/run_experiments.py \
  --config ground_reset_constellation_weighted
```

Use alternate data or output directories:

```bash
.env/bin/python lios/evaluation/run_experiments.py \
  --data /path/to/data \
  --out /path/to/results
```

## Authoritative source files

- `lios/evaluation/run_experiments.py`: active experiment matrix and overrides
- `lios/config.toml`: shared protocol, link, simulation, crypto, and ledger values
- `lios/evaluation/baselines.py`: ground-reset baseline behavior
- `lios/simulator/traffic_generator.py`: traffic distribution and allocation
- `lios/contact_plan/window_calculator.py`: contact geometry and capacity
- `lios/data/tles/`: satellite populations used to derive operator weights
- `lios/data/gss/`: ground-station dataset

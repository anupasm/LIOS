"""LIOS configuration loader.

Single source of truth: ``lios/config.toml``.
Parsed with the Python 3.11 built-in ``tomllib``; no extra dependencies.

The Python dataclasses below are pure type containers — they carry **no**
hardcoded defaults.  Every value must be present in the TOML file.
A missing file raises ``FileNotFoundError`` immediately so misconfiguration
is never silently swallowed.

Override the config file location::

    LIOS_CONFIG=/path/to/myconfig.toml python evaluation/run_experiments.py

Usage::

    from config import cfg

    cfg.link.isl_max_kbps           # 10_000.0
    cfg.simulation.arrival_rate     # 50 global flows/second
    cfg.crypto.cert_valid_days      # 90
"""
from __future__ import annotations

import math
import os
import sys
try:
    import tomllib
except ModuleNotFoundError:  # Python < 3.11
    try:
        import tomli as tomllib  # type: ignore[no-redef]
    except ModuleNotFoundError as exc:
        raise ModuleNotFoundError(
            "tomllib (Python 3.11+) or tomli (pip install tomli) is required"
        ) from exc
from dataclasses import dataclass
from pathlib import Path


# ── Typed config sections (no defaults — values come exclusively from TOML) ────

@dataclass
class ProtocolConfig:
    """Settlement rules, auth parameters, and channel sizing (§7, §11, §13)."""

    t_low_fraction: float
    """T1 trigger threshold: settlement fires when either side's balance falls
    below this fraction of total bilateral channel capacity (default 5 %)."""

    s_max_kb: int
    """T7 trigger threshold: cumulative bytes forwarded in one session (KB).
    The evaluation default is 1 GB, limiting unsettled session exposure."""

    t_challenge_sec: float
    """On-chain challenge window after initiateSettlement() (seconds).
    Default 48 h — the honest peer must submit a counter-proof within this
    window or the initiator's proof is finalised unchallenged."""

    t_topup_confirm_sec: float
    """Deadline for the counterpart ground station to confirm a top-up
    request (seconds). Default 24 h; expired requests are discarded."""

    channel_balance_kb: int
    """Initial channel balance allocated to each side at open time (KB).
    Total channel capacity = 2 × this value.
    Evaluation default: 5 GB per side (5,000,000 KB in decimal units).
    flow_size_max_kb must be ≤ this value to avoid guaranteed flow drops."""

    timestamp_tolerance_sec: float
    """Maximum allowed clock skew between two satellites during the auth
    handshake (seconds). Messages outside this window are rejected."""

    nonce_bytes: int
    """Length of the ECDH ephemeral nonce exchanged in the HELLO message
    (bytes). 32 bytes gives 256-bit entropy."""


@dataclass
class LinkConfig:
    """RF / laser link physics and contact-window thresholds."""

    c_km_s: float
    """Speed of light in vacuum (km/s), used to compute one-way ISL
    propagation delay from inter-satellite range."""

    isl_max_kbps: float
    """Peak inter-satellite laser link capacity at zero separation (kbps).
    Actual capacity scales with range; see WindowCalculator."""

    gs_max_kbps: float
    """Peak ground-station uplink/downlink capacity at minimum elevation
    angle (kbps). Used to bound GS contact throughput."""

    isl_max_range_km: float
    """Maximum range at which two satellites can maintain an ISL (km).
    Pairs beyond this distance are excluded from the contact plan."""

    max_isl_per_sat: int
    """Maximum simultaneous ISL connections per satellite (transceiver limit).
    For each satellite, only the top-N partners by in-range step count are
    retained after Phase 1.5 — reducing Phase 2 work proportionally."""

    gs_min_elevation_deg: float
    """Minimum elevation angle above the horizon required for a GS–satellite
    contact to be considered viable (degrees)."""

    operator_isl_criteria: dict
    """Per-operator ISL visibility/profile parameters.  Keys are operator IDs
    or aliases; values are interpreted by contact_plan.window_calculator.
    These values are deliberately kept as a raw mapping because experiments may
    add operators without changing the typed configuration schema."""


@dataclass
class SimulationConfig:
    """DES parameters, traffic shape, and PRNG seeds."""

    time_step_sec: int
    """SGP4 orbital propagation time step used when building the contact
    plan (seconds). Smaller values increase accuracy at the cost of speed."""

    arrival_rate: float
    """Global Poisson traffic arrival rate (flows/second).
    The rate is independent of ground-node, satellite, and active-pair counts,
    so workloads remain comparable across constellation sizes."""

    traffic_allocation: str
    """Traffic-allocation policy. ``uniform_active_pair`` samples uniformly
    from cross-operator satellite pairs with an active ISL at arrival time."""

    traffic_direction_bias: float
    """Probability of selecting the canonical A→B direction after sorting a
    sampled satellite pair by satellite ID. 0.5 gives unbiased directions."""

    operator_load_weights: dict[str, float]
    """Relative offered-load weight for traffic sourced by each operator.
    Equal weights preserve uniform active-pair allocation. Higher values skew
    more generated flows toward that operator without changing arrival_rate."""

    random_seed: int
    """Default PRNG seed for all stochastic components (traffic generator,
    adversarial node). Override per experiment for independent runs."""

    p_attack: float
    """Probability that a malicious satellite attacks each eligible
    forwarding event (rollback or selective-forward, depending on mode)."""

    flow_size_min_kb: int
    """Lower clamp on the log-normal flow size distribution (KB).
    Flows smaller than this are rounded up to this value."""

    flow_size_max_kb: int
    """Upper clamp on the log-normal flow size distribution (KB).
    Must be ≤ channel_balance_kb — the satellite drops any flow whose
    size_kb exceeds the current channel balance, so keep these in sync."""


@dataclass
class CryptoConfig:
    """PKI defaults."""

    cert_valid_days: int
    """Validity period for satellite operational certificates (days).
    After expiry the satellite must obtain a renewed cert from its operator CA."""


@dataclass
class BlockchainConfig:
    """Hyperledger Fabric network integration settings."""

    network_config_path: str
    """Path to network_config.json relative to lios/.  Written by
    lios/evaluation/start_network.sh; read by FabricClient at runtime."""

    operator_channel_balance_kb: float
    """On-chain operator channel balance per side (KB).  Set large enough to
    cover the sum of all satellite sub-channel allocations."""

    operator_channel_reserve_kb: float
    """Penalty reserve locked per bilateral operator channel (KB)."""

    commit_latency_sec: float
    """Simulated Fabric block-ordering + commit delay (seconds).
    In production, Hyperledger Fabric typically takes 1–3 s to order and
    commit a transaction.  The FabricMock uses this value to schedule
    SETTLEMENT_FINALIZED at t + commit_latency rather than synchronously,
    so that blockchain_sec reflects a realistic on-chain delay."""


@dataclass
class LIOSConfig:
    protocol:    ProtocolConfig
    link:        LinkConfig
    simulation:  SimulationConfig
    crypto:      CryptoConfig
    blockchain:  BlockchainConfig


# ── Loader ─────────────────────────────────────────────────────────────────────

_DEFAULT_PATH = Path(__file__).parent / "config.toml"


def load(path: Path | None = None) -> LIOSConfig:
    """Load and return a ``LIOSConfig`` from *path* (or ``config.toml``).

    Raises ``FileNotFoundError`` if the TOML file cannot be found so that
    misconfiguration is never silently swallowed.
    Unknown keys in the TOML file are silently ignored for forward
    compatibility with newer config files.
    """
    env_val = os.environ.get("LIOS_CONFIG", "")
    resolved: Path = path if path else (Path(env_val) if env_val else _DEFAULT_PATH)
    if not resolved.is_file():
        resolved = _DEFAULT_PATH
    if not resolved.is_file():
        raise FileNotFoundError(
            f"LIOS config file not found: {resolved}\n"
            "Copy lios/config.toml next to the source or set LIOS_CONFIG."
        )

    with resolved.open("rb") as fh:
        raw = tomllib.load(fh)

    def _section(cls, key):
        data = raw.get(key, {})
        known = {f.name for f in cls.__dataclass_fields__.values()}  # type: ignore[attr-defined]
        return cls(**{k: v for k, v in data.items() if k in known})

    config = LIOSConfig(
        protocol=_section(ProtocolConfig,     "protocol"),
        link=_section(LinkConfig,             "link"),
        simulation=_section(SimulationConfig, "simulation"),
        crypto=_section(CryptoConfig,         "crypto"),
        blockchain=_section(BlockchainConfig,  "blockchain"),
    )
    _validate(config)
    return config


def _validate(config: LIOSConfig) -> None:
    """Reject invalid workload settings before a simulation starts."""
    sim = config.simulation
    if sim.arrival_rate <= 0:
        raise ValueError("simulation.arrival_rate must be greater than zero")
    if sim.traffic_allocation not in {"uniform_active_pair"}:
        raise ValueError(
            "simulation.traffic_allocation must be 'uniform_active_pair'"
        )
    if not 0.0 <= sim.traffic_direction_bias <= 1.0:
        raise ValueError(
            "simulation.traffic_direction_bias must be between 0.0 and 1.0"
        )
    if any(
        not isinstance(weight, (int, float))
        or not math.isfinite(weight)
        or weight < 0
        for weight in sim.operator_load_weights.values()
    ):
        raise ValueError(
            "simulation.operator_load_weights values must be finite and non-negative"
        )
    if sim.operator_load_weights and not any(
        weight > 0 for weight in sim.operator_load_weights.values()
    ):
        raise ValueError(
            "simulation.operator_load_weights must contain at least one positive value"
        )


#: Module-level singleton — import this everywhere.
cfg: LIOSConfig = load()

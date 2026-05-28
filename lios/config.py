"""LIOS configuration loader.

Reads ``lios/config.toml`` using the Python 3.11 built-in ``tomllib``.
Falls back silently to hardcoded defaults when no file is found, so the
codebase works out-of-the-box without a config file present.

Override the config file location::

    LIOS_CONFIG=/path/to/myconfig.toml python evaluation/run_experiments.py

Usage::

    from config import cfg

    cfg.protocol.h_max              # 10_000
    cfg.link.isl_max_kbps           # 10_000.0
    cfg.simulation.arrival_rate     # 0.01
    cfg.crypto.cert_valid_days      # 90
"""
from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path


# ── Typed config sections ──────────────────────────────────────────────────────

@dataclass
class ProtocolConfig:
    """Settlement rules, auth parameters, and channel sizing (§7, §11, §13)."""

    t_low_fraction: float = 0.05
    """T1 trigger threshold: settlement fires when either side's balance falls
    below this fraction of the initial channel capacity (default 5 %)."""

    h_max: int = 10_000
    """T2 trigger threshold: maximum hash-chain entries per session before a
    mandatory settlement is initiated (prevents unbounded chain growth)."""

    s_max_kb: int = 100_000 * 1024
    """T7 trigger threshold: cumulative bytes forwarded in one session (KB).
    Default is 100 GB (102 400 000 KB); settlement resets the counter."""

    t_challenge_sec: float = 172_800.0
    """On-chain challenge window after initiateSettlement() (seconds).
    Default 48 h — the honest peer must submit a counter-proof within this
    window or the initiator's proof is finalised unchallenged."""

    t_topup_confirm_sec: float = 86_400.0
    """Deadline for the counterpart ground station to confirm a top-up
    request (seconds). Default 24 h; expired requests are discarded."""

    channel_balance_kb: int = 1_024 * 1_024
    """Initial channel balance allocated to each side at open time (KB).
    Total channel capacity = 2 × this value. Default 1 TB per side."""

    timestamp_tolerance_sec: float = 30.0
    """Maximum allowed clock skew between two satellites during the auth
    handshake (seconds). Messages outside this window are rejected."""

    nonce_bytes: int = 32
    """Length of the ECDH ephemeral nonce exchanged in the HELLO message
    (bytes). 32 bytes gives 256-bit entropy."""


@dataclass
class LinkConfig:
    """RF / laser link physics and contact-window thresholds."""

    c_km_s: float = 299_792.458
    """Speed of light in vacuum (km/s), used to compute one-way ISL
    propagation delay from inter-satellite range."""

    isl_max_kbps: float = 10_000.0
    """Peak inter-satellite laser link capacity at zero separation (kbps).
    Actual capacity scales with range; see WindowCalculator."""

    gs_max_kbps: float = 50_000.0
    """Peak ground-station uplink/downlink capacity at minimum elevation
    angle (kbps). Used to bound GS contact throughput."""

    isl_max_range_km: float = 2_500.0
    """Maximum range at which two satellites can maintain an ISL (km).
    Pairs beyond this distance are excluded from the contact plan."""

    gs_min_elevation_deg: float = 5.0
    """Minimum elevation angle above the horizon required for a GS–satellite
    contact to be considered viable (degrees)."""


@dataclass
class SimulationConfig:
    """DES parameters, traffic shape, and PRNG seeds."""

    time_step_sec: int = 30
    """SGP4 orbital propagation time step used when building the contact
    plan (seconds). Smaller values increase accuracy at the cost of speed."""

    arrival_rate: float = 0.01
    """Poisson traffic arrival rate per ground-station node (flows/second).
    The effective network rate scales with the number of ground nodes."""

    lookahead_sec: float = 600.0
    """Contact-plan lookahead window used by the traffic generator to find
    a viable next-hop contact for each flow (seconds)."""

    cross_operator_bias: float = 0.7
    """Probability that a generated flow is destined for a satellite owned
    by a different operator (drives inter-operator forwarding demand)."""

    random_seed: int = 42
    """Default PRNG seed for all stochastic components (traffic generator,
    adversarial node). Override per experiment for independent runs."""

    p_attack: float = 0.5
    """Probability that a malicious satellite attacks each eligible
    forwarding event (rollback or selective-forward, depending on mode)."""


@dataclass
class CryptoConfig:
    """PKI defaults."""

    cert_valid_days: int = 90
    """Validity period for satellite operational certificates (days).
    After expiry the satellite must obtain a renewed cert from its operator CA."""


@dataclass
class LIOSConfig:
    protocol:   ProtocolConfig   = field(default_factory=ProtocolConfig)
    link:       LinkConfig       = field(default_factory=LinkConfig)
    simulation: SimulationConfig = field(default_factory=SimulationConfig)
    crypto:     CryptoConfig     = field(default_factory=CryptoConfig)


# ── Loader ─────────────────────────────────────────────────────────────────────

_DEFAULT_PATH = Path(__file__).parent / "config.toml"


def load(path: Path | None = None) -> LIOSConfig:
    """Load and return a ``LIOSConfig`` from *path* (or the default location).

    Returns a config with hardcoded defaults if the file does not exist.
    Unknown keys in the TOML file are silently ignored so that older config
    files remain compatible with newer code.
    """
    env_val = os.environ.get("LIOS_CONFIG", "")
    resolved: Path = path if path else (Path(env_val) if env_val else _DEFAULT_PATH)
    if not resolved.is_file():
        resolved = _DEFAULT_PATH
    if not resolved.is_file():
        return LIOSConfig()

    with resolved.open("rb") as fh:
        raw = tomllib.load(fh)

    def _section(cls, key):
        data = raw.get(key, {})
        known = {f.name for f in cls.__dataclass_fields__.values()}  # type: ignore[attr-defined]
        return cls(**{k: v for k, v in data.items() if k in known})

    return LIOSConfig(
        protocol=_section(ProtocolConfig,   "protocol"),
        link=_section(LinkConfig,           "link"),
        simulation=_section(SimulationConfig, "simulation"),
        crypto=_section(CryptoConfig,       "crypto"),
    )


#: Module-level singleton — import this everywhere.
cfg: LIOSConfig = load()

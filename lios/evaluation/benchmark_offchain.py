#!/usr/bin/env python3
"""Computational overhead benchmark for the LIOS off-chain protocol.

Methodology
-----------
* GC is disabled for the duration of each hot loop to eliminate collection
  jitter from the samples.
* Sub-microsecond operations use batched timing (BATCH calls per timed block)
  to stay above timer-call overhead (~0.1–0.5 μs on CPython).  Calibration
  runs automatically pick single vs. batched mode.
* measure_prebuilt() interleaves setup/measure so objects are not all resident
  in memory simultaneously (avoids artificial warm-cache bias).
* 95% bootstrap confidence intervals on the mean are reported alongside
  percentiles so reviewers can assess measurement stability.
* System metadata (CPU model, Python version, cryptography version) is written
  into the JSON so results are reproducible.

Sections
--------
1. Raw crypto primitives  — ECDSA sign/verify, X25519 ECDH, HKDF, AES-GCM
2. Key hierarchy          — cert issuance, cert verification, serialisation
3. Auth handshake         — initiate, handle_hello, complete_auth, encrypt/decrypt
4. Off-chain channel      — open_channel, record_forwarding, cosign_proof,
                            settlement triggers, log serialisation
5. Channel-count scaling  — record_forwarding latency vs. open-channel count
6. Throughput             — sustained forward+cosign rounds per second

Usage
-----
    cd lios && python3 evaluation/benchmark_offchain.py [--n 500] [--out results/]
"""
from __future__ import annotations

import argparse
import gc
import json
import os
import platform
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).parent.parent))

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from crypto.key_hierarchy import OperatorCA, SatelliteKeyStore, _sign, _verify, _canonical_json
from protocol.auth import ContactAuthSession
from protocol.offchain import OffChainProtocol, ForwardingLog, BalanceProof


# ── Constants ──────────────────────────────────────────────────────────────────

WARMUP = 50
# If a single calibration call is under this threshold (μs), use batched timing.
BATCH_THRESHOLD_US = 8.0
# Number of calls per timed block for fast operations.
BATCH = 500
# Number of bootstrap resamples for CI.
N_BOOT = 1000
# Typical LEO ISL contact window (seconds) — used for contextualisation.
ISL_CONTACT_SEC = 300.0


# ── System information ─────────────────────────────────────────────────────────

def capture_sysinfo() -> dict:
    info: dict = {
        "python_version": platform.python_version(),
        "platform": platform.platform(),
        "machine": platform.machine(),
        "processor": platform.processor() or "unknown",
    }
    try:
        with open("/proc/cpuinfo") as f:
            for line in f:
                if line.startswith("model name"):
                    info["cpu_model"] = line.split(":", 1)[1].strip()
                    break
    except OSError:
        pass
    try:
        import cryptography
        info["cryptography_version"] = cryptography.__version__
    except Exception:
        pass
    try:
        info["numpy_version"] = np.__version__
    except Exception:
        pass
    return info


# ── Statistical helpers ────────────────────────────────────────────────────────

def _bootstrap_ci(samples: List[float], ci: float = 0.95) -> Tuple[float, float]:
    rng = np.random.default_rng(42)
    boot_means = np.array([
        np.mean(rng.choice(samples, size=len(samples), replace=True))
        for _ in range(N_BOOT)
    ])
    lo = float(np.percentile(boot_means, (1 - ci) / 2 * 100))
    hi = float(np.percentile(boot_means, (1 + ci) / 2 * 100))
    return lo, hi


# ── Result dataclass ───────────────────────────────────────────────────────────

@dataclass
class BenchResult:
    name: str
    section: str
    n: int
    samples_us: List[float]
    batched: bool = False

    @property
    def minimum(self) -> float:
        return float(np.min(self.samples_us))

    @property
    def mean(self) -> float:
        return float(np.mean(self.samples_us))

    @property
    def std(self) -> float:
        return float(np.std(self.samples_us))

    @property
    def p50(self) -> float:
        return float(np.percentile(self.samples_us, 50))

    @property
    def p95(self) -> float:
        return float(np.percentile(self.samples_us, 95))

    @property
    def p99(self) -> float:
        return float(np.percentile(self.samples_us, 99))

    def ci95(self) -> Tuple[float, float]:
        return _bootstrap_ci(self.samples_us)

    def to_dict(self) -> dict:
        lo, hi = self.ci95()
        return {
            "name": self.name,
            "section": self.section,
            "n": self.n,
            "batched": self.batched,
            "min_us":     round(self.minimum, 3),
            "mean_us":    round(self.mean, 3),
            "std_us":     round(self.std, 3),
            "ci95_lo_us": round(lo, 3),
            "ci95_hi_us": round(hi, 3),
            "p50_us":     round(self.p50, 3),
            "p95_us":     round(self.p95, 3),
            "p99_us":     round(self.p99, 3),
        }


# ── Measurement primitives ─────────────────────────────────────────────────────

def _calibrate_us(fn: Callable) -> float:
    """Return a single-call latency estimate (μs) for calibration."""
    t0 = time.perf_counter()
    fn()
    return (time.perf_counter() - t0) * 1e6


def measure(fn: Callable, n: int, name: str, section: str) -> BenchResult:
    """Time fn(); auto-select single vs. batched mode based on calibration."""
    for _ in range(WARMUP):
        fn()

    cal = _calibrate_us(fn)
    use_batch = cal < BATCH_THRESHOLD_US

    gc.disable()
    try:
        samples: List[float] = []
        if use_batch:
            for _ in range(n):
                t0 = time.perf_counter()
                for _ in range(BATCH):
                    fn()
                samples.append((time.perf_counter() - t0) * 1e6 / BATCH)
        else:
            for _ in range(n):
                t0 = time.perf_counter()
                fn()
                samples.append((time.perf_counter() - t0) * 1e6)
    finally:
        gc.enable()

    return BenchResult(name=name, section=section, n=n,
                       samples_us=samples, batched=use_batch)


def measure_prebuilt(setup: Callable, fn: Callable,
                     n: int, name: str, section: str) -> BenchResult:
    """Time fn(obj) where obj is built fresh by setup() before each call.

    Interleaves setup and measure (build one → time one → repeat) so objects
    are not all simultaneously resident in cache, reflecting real runtime.
    Used for state-mutating operations (e.g., complete_auth).
    """
    for _ in range(WARMUP):
        fn(setup())

    gc.disable()
    try:
        samples: List[float] = []
        for _ in range(n):
            obj = setup()
            t0 = time.perf_counter()
            fn(obj)
            samples.append((time.perf_counter() - t0) * 1e6)
    finally:
        gc.enable()

    return BenchResult(name=name, section=section, n=n, samples_us=samples)


# ── Shared fixtures ────────────────────────────────────────────────────────────

def _make_operator_pair():
    ca_a = OperatorCA("opA")
    ca_b = OperatorCA("opB")
    cert_a, priv_a = ca_a.issue_for_new_key("opA-s1", permitted_operators=["opA", "opB"])
    cert_b, priv_b = ca_b.issue_for_new_key("opB-s2", permitted_operators=["opA", "opB"])
    ks_a = SatelliteKeyStore()
    ks_a.register_operator("opA", ca_a.public_key)
    ks_a.register_operator("opB", ca_b.public_key)
    ks_b = SatelliteKeyStore()
    ks_b.register_operator("opA", ca_a.public_key)
    ks_b.register_operator("opB", ca_b.public_key)
    return ca_a, ca_b, cert_a, cert_b, priv_a, priv_b, ks_a, ks_b


# ── Section 1: Raw crypto primitives ──────────────────────────────────────────

def bench_raw_crypto(n: int) -> List[BenchResult]:
    results: List[BenchResult] = []
    sec = "1. Raw crypto"

    results.append(measure(
        lambda: ec.generate_private_key(ec.SECP256R1()),
        n, "ECDSA P-256 keygen", sec,
    ))

    priv = ec.generate_private_key(ec.SECP256R1())
    payload = _canonical_json({"channel_id": "ch", "seq_num": 42,
                               "balance_a_kb": 512.0, "balance_b_kb": 512.0})
    results.append(measure(
        lambda: _sign(priv, payload),
        n, "ECDSA P-256 sign (BalProof payload ~70 B)", sec,
    ))

    sig = _sign(priv, payload)
    pub = priv.public_key()
    results.append(measure(
        lambda: _verify(pub, payload, sig),
        n, "ECDSA P-256 verify (BalProof payload ~70 B)", sec,
    ))

    results.append(measure(
        lambda: X25519PrivateKey.generate(),
        n, "X25519 keygen", sec,
    ))

    a = X25519PrivateKey.generate()
    b_pub = X25519PrivateKey.generate().public_key()
    results.append(measure(
        lambda: a.exchange(b_pub),
        n, "X25519 ECDH exchange", sec,
    ))

    shared = a.exchange(b_pub)
    salt = os.urandom(64)
    results.append(measure(
        lambda: HKDF(algorithm=hashes.SHA256(), length=32,
                     salt=salt, info=b"lios-session-key").derive(shared),
        n, "HKDF-SHA256 derive (32 B key)", sec,
    ))

    key_bytes = os.urandom(32)
    aesgcm = AESGCM(key_bytes)
    for label, size in [("64 B", 64), ("1 KB", 1024), ("64 KB", 65536)]:
        pt = os.urandom(size)
        nonce = os.urandom(12)
        results.append(measure(
            lambda p=pt, nc=nonce: aesgcm.encrypt(nc, p, None),
            n, f"AES-256-GCM encrypt ({label})", sec,
        ))
        ct = aesgcm.encrypt(nonce, pt, None)
        results.append(measure(
            lambda c=ct, nc=nonce: aesgcm.decrypt(nc, c, None),
            n, f"AES-256-GCM decrypt ({label})", sec,
        ))

    return results


# ── Section 2: Key hierarchy ───────────────────────────────────────────────────

def bench_key_hierarchy(n: int) -> List[BenchResult]:
    results: List[BenchResult] = []
    sec = "2. Key hierarchy"

    ca = OperatorCA("opA")

    results.append(measure(
        lambda: ca.issue_for_new_key("sat-x", permitted_operators=["opA", "opB"]),
        n, "OperatorCA.issue_for_new_key()", sec,
    ))

    cert, _ = ca.issue_for_new_key("sat-1", permitted_operators=["opA"])
    ks = SatelliteKeyStore()
    ks.register_operator("opA", ca.public_key)
    results.append(measure(
        lambda: ks.verify_peer_cert(cert),
        n, "SatelliteKeyStore.verify_peer_cert()", sec,
    ))

    results.append(measure(
        lambda: cert.serialise(),
        n, "SatelliteCert.serialise()", sec,
    ))

    from crypto.key_hierarchy import SatelliteCert
    serialised = cert.serialise()
    results.append(measure(
        lambda: SatelliteCert.deserialise(serialised),
        n, "SatelliteCert.deserialise()", sec,
    ))

    return results


# ── Section 3: Authentication handshake ───────────────────────────────────────

def bench_auth(n: int) -> List[BenchResult]:
    results: List[BenchResult] = []
    sec = "3. Auth handshake"

    _, _, cert_a, cert_b, priv_a, priv_b, ks_a, ks_b = _make_operator_pair()

    results.append(measure(
        lambda: ContactAuthSession(cert_a, priv_a, ks_a, 0.0).initiate(),
        n, "ContactAuthSession.initiate() [X25519 keygen]", sec,
    ))

    hello_a = ContactAuthSession(cert_a, priv_a, ks_a, 0.0).initiate()
    results.append(measure(
        lambda: ContactAuthSession(cert_b, priv_b, ks_b, 0.0).handle_hello(hello_a),
        n, "ContactAuthSession.handle_hello() [cert-verify+keygen]", sec,
    ))

    # complete_auth: build a fresh initiated session each time; time only the call.
    hello_b_ref = ContactAuthSession(cert_b, priv_b, ks_b, 0.0).handle_hello(hello_a)

    def _setup_initiated_a():
        s = ContactAuthSession(cert_a, priv_a, ks_a, 0.0)
        s.initiate()
        return s

    results.append(measure_prebuilt(
        _setup_initiated_a,
        lambda s: s.complete_auth(hello_b_ref),
        n, "ContactAuthSession.complete_auth() [cert-verify+ECDH+HKDF]", sec,
    ))

    # Full handshake — both sides, all messages (the real per-contact cost)
    def _full_handshake():
        s_a = ContactAuthSession(cert_a, priv_a, ks_a, 0.0)
        s_b = ContactAuthSession(cert_b, priv_b, ks_b, 0.0)
        h_a = s_a.initiate()
        h_b = s_b.handle_hello(h_a)
        s_a.complete_auth(h_b)
        s_b.complete_auth(h_a)

    results.append(measure(_full_handshake, n, "Full auth handshake (both sides)", sec))

    # Encrypt / decrypt with derived session key — representative message sizes
    s_a = ContactAuthSession(cert_a, priv_a, ks_a, 0.0)
    s_b = ContactAuthSession(cert_b, priv_b, ks_b, 0.0)
    h_a = s_a.initiate()
    h_b = s_b.handle_hello(h_a)
    s_a.complete_auth(h_b)
    s_b.complete_auth(h_a)

    for label, size in [("64 B", 64), ("1 KB", 1024), ("64 KB", 65536)]:
        pt = os.urandom(size)
        results.append(measure(
            lambda p=pt: s_a.encrypt(p),
            n, f"Session AES-GCM encrypt ({label})", sec,
        ))
        enc = s_a.encrypt(pt)
        results.append(measure(
            lambda e=enc: s_b.decrypt(e),
            n, f"Session AES-GCM decrypt ({label})", sec,
        ))

    return results


# ── Section 4: Off-chain payment channel ──────────────────────────────────────

def bench_offchain(n: int) -> List[BenchResult]:
    results: List[BenchResult] = []
    sec = "4. Off-chain channel"

    _, _, cert_a, cert_b, priv_a, priv_b, _, _ = _make_operator_pair()
    pub_a = priv_a.public_key()
    pub_b = priv_b.public_key()
    dummy_sig = b"\x00" * 72

    # open_channel() — unique channel IDs to avoid dict collision
    proto_pool = OffChainProtocol("opA-s1", priv_a, cert_a)
    _ctr = [0]

    def _open():
        _ctr[0] += 1
        proto_pool.open_channel(f"ch{_ctr[0]}", "op_ch", "opB-s2", 1_048_576.0, "A")

    results.append(measure(_open, n, "open_channel()", sec))

    # build_hello()
    pa_hello = OffChainProtocol("opA-s1", priv_a, cert_a)
    pa_hello.open_channel("ch", "op_ch", "opB-s2", 1_048_576.0, "A")
    results.append(measure(
        lambda: pa_hello.build_hello("ch"),
        n, "build_hello()", sec,
    ))

    # record_forwarding() — sign + balance deduction + log append
    # Re-create a fresh protocol per call so balance never exhausts.
    def _record_fwd():
        p = OffChainProtocol("opA-s1", priv_a, cert_a)
        p.open_channel("ch", "op_ch", "opB-s2", 1_048_576.0, "A")
        p.record_forwarding("ch", 1.0, pub_b, 0.0, dummy_sig)

    results.append(measure(_record_fwd, n, "record_forwarding() [ECDSA sign]", sec))

    # cosign_proof() — verify + sign + state update
    import copy
    pa_tmpl = OffChainProtocol("opA-s1", priv_a, cert_a)
    pa_tmpl.open_channel("ch", "op_ch", "opB-s2", 1_048_576.0, "A")
    proof_tmpl = pa_tmpl.record_forwarding("ch", 1.0, pub_b, 0.0, dummy_sig)

    def _cosign():
        p = copy.copy(proof_tmpl)
        pb = OffChainProtocol("opB-s2", priv_b, cert_b)
        pb.open_channel("ch", "op_ch", "opA-s1", 1_048_576.0, "B")
        pb.cosign_proof("ch", p, pub_a)

    results.append(measure(_cosign, n, "cosign_proof() [ECDSA verify+sign]", sec))

    # Full forward+cosign round-trip (both sats)
    def _round_trip():
        pa = OffChainProtocol("opA-s1", priv_a, cert_a)
        pb = OffChainProtocol("opB-s2", priv_b, cert_b)
        pa.open_channel("ch", "op_ch", "opB-s2", 1_048_576.0, "A")
        pb.open_channel("ch", "op_ch", "opA-s1", 1_048_576.0, "B")
        proof = pa.record_forwarding("ch", 1.0, pub_b, 0.0, dummy_sig)
        pb.cosign_proof("ch", proof, pub_a)

    results.append(measure(_round_trip, n, "Forward+cosign round-trip (both sats)", sec))

    # evaluate_settlement_triggers()
    pt = OffChainProtocol("opA-s1", priv_a, cert_a)
    pt.open_channel("ch", "op_ch", "opB-s2", 1_048_576.0, "A")
    results.append(measure(
        lambda: pt.evaluate_settlement_triggers("ch"),
        n, "evaluate_settlement_triggers()", sec,
    ))

    # ForwardingLog.serialise() at increasing log sizes
    for log_size in [10, 100, 1_000]:
        lg = ForwardingLog("ch")
        for i in range(log_size):
            lg.append(i + 1, 1.0, "A_to_B", float(i))
        results.append(measure(
            lambda l=lg: l.serialise(),
            n, f"ForwardingLog.serialise() ({log_size} entries)", sec,
        ))

    # BalanceProof serialisation (on-wire)
    bp = BalanceProof("ch", 100, 500.0, 500.0)
    results.append(measure(
        lambda: bp.signing_payload(),
        n, "BalanceProof.signing_payload() [canonical JSON]", sec,
    ))
    bp.sig_a = b"\xab" * 72
    bp.sig_b = b"\xcd" * 72
    results.append(measure(lambda: bp.to_dict(), n, "BalanceProof.to_dict()", sec))
    d = bp.to_dict()
    results.append(measure(lambda: BalanceProof.from_dict(d), n, "BalanceProof.from_dict()", sec))

    return results


# ── Section 5: Channel-count scaling ──────────────────────────────────────────

def bench_channel_scaling(n: int) -> List[BenchResult]:
    """Measure record_forwarding() latency as open-channel count increases.

    Covers the range from a single bilateral channel up to 10,000 simultaneous
    channels, stress-testing the O(1) dict-lookup claim at constellation scale.

    Setup (channel creation) is done outside the timed window via measure_prebuilt
    so we measure only the record_forwarding() call itself.

    n is automatically reduced for large channel counts so that setup time
    (O(n_ch) channel opens per iteration) stays reasonable.
    """
    results: List[BenchResult] = []
    sec = "5. Channel-count scaling"

    _, _, cert_a, cert_b, priv_a, priv_b, _, _ = _make_operator_pair()
    pub_b = priv_b.public_key()
    dummy_sig = b"\x00" * 72

    # (n_ch, iterations): reduce n for large n_ch so total setup time stays < ~60s
    channel_counts = [
        (1,      n),
        (10,     n),
        (50,     n),
        (100,    n),
        (500,    max(n // 2, 20)),
        (1_000,  max(n // 5, 20)),
        (5_000,  max(n // 20, 10)),
        (10_000, max(n // 50, 10)),
    ]

    for n_ch, n_iter in channel_counts:
        def _setup(nc=n_ch):
            p = OffChainProtocol("opA-s1", priv_a, cert_a)
            for i in range(nc):
                p.open_channel(f"ch{i}", "op_ch", "opB-s2", 1e12, "A")
            return p

        results.append(measure_prebuilt(
            _setup,
            lambda p: p.record_forwarding("ch0", 1.0, pub_b, 0.0, dummy_sig),
            n_iter,
            f"record_forwarding() with {n_ch:,} channels open", sec,
        ))

    return results


# ── Section 6: Sustained throughput ───────────────────────────────────────────

def bench_throughput(duration_sec: float = 5.0) -> dict:
    """Measure sustained forward+cosign rounds per second on persistent channels."""
    _, _, cert_a, cert_b, priv_a, priv_b, _, _ = _make_operator_pair()
    pub_a = priv_a.public_key()
    pub_b = priv_b.public_key()

    proto_a = OffChainProtocol("opA-s1", priv_a, cert_a)
    proto_b = OffChainProtocol("opB-s2", priv_b, cert_b)
    proto_a.open_channel("ch", "op_ch", "opB-s2", 1e15, "A")
    proto_b.open_channel("ch", "op_ch", "opA-s1", 1e15, "B")

    dummy_sig = b"\x00" * 72
    count = 0
    gc.disable()
    t_end = time.perf_counter() + duration_sec
    try:
        while time.perf_counter() < t_end:
            proof = proto_a.record_forwarding("ch", 0.001, pub_b, 0.0, dummy_sig)
            if proof is not None:
                proto_b.cosign_proof("ch", proof, pub_a)
            count += 1
    finally:
        gc.enable()

    rounds_per_sec = count / duration_sec
    us_per_round = 1e6 / rounds_per_sec
    rounds_per_contact = int(ISL_CONTACT_SEC / (us_per_round / 1e6))
    return {
        "name": "Sustained forward+cosign rounds/sec",
        "section": "6. Throughput",
        "rounds": count,
        "duration_sec": duration_sec,
        "rounds_per_sec": round(rounds_per_sec, 1),
        "us_per_round": round(us_per_round, 2),
        "rounds_per_isl_contact": rounds_per_contact,
        "isl_contact_sec": ISL_CONTACT_SEC,
    }


# ── ISL contact window context ─────────────────────────────────────────────────

def print_isl_context(results: List[BenchResult], throughput: dict) -> None:
    """Print a short paragraph contextualising latency numbers against ISL budgets."""
    def _find(name_fragment: str) -> Optional[BenchResult]:
        for r in results:
            if name_fragment.lower() in r.name.lower():
                return r
        return None

    handshake = _find("full auth handshake")
    roundtrip = _find("forward+cosign round-trip")
    auth_fraction = (handshake.mean / 1e6 / ISL_CONTACT_SEC * 100) if handshake else None

    print("\n  ISL Contact Window Analysis")
    print(f"  ─────────────────────────────────────────────────────────────")
    print(f"  Assumed contact window : {ISL_CONTACT_SEC:.0f} s (typical LEO ISL)")
    if handshake:
        print(f"  Auth handshake (both)  : {handshake.mean:>8.1f} μs "
              f"= {auth_fraction:.3f}% of contact window")
    if roundtrip:
        cap = int(ISL_CONTACT_SEC / (roundtrip.mean / 1e6))
        print(f"  Forward+cosign round   : {roundtrip.mean:>8.1f} μs  "
              f"→ {cap:,} rounds possible per contact")
    print(f"  Sustained throughput   : {throughput['rounds_per_sec']:>8,.0f} rounds/s "
          f"({throughput['rounds_per_isl_contact']:,} rounds/contact)")
    print()


# ── Output ─────────────────────────────────────────────────────────────────────

_COL_W = (52, 5, 9, 9, 11, 9, 9, 9)
_HDR   = ("Operation", "N", "min μs", "mean μs", "95% CI ±", "p50 μs", "p95 μs", "p99 μs")


def _row(vals: tuple) -> str:
    return "  ".join(str(v).ljust(w) for v, w in zip(vals, _COL_W))


def print_table(results: List[BenchResult], throughput: dict) -> None:
    W = sum(_COL_W) + 2 * len(_COL_W)
    print()
    print("=" * W)
    print("  LIOS Off-Chain Protocol — Computational Overhead Benchmark")
    print("=" * W)
    print(_row(_HDR))
    print("-" * W)

    current_section = ""
    for r in results:
        if r.section != current_section:
            current_section = r.section
            print(f"\n  [{current_section}]")
        lo, hi = r.ci95()
        ci_str = f"[{lo:.2f}, {hi:.2f}]"
        tag = " *" if r.batched else "  "
        print(_row((
            r.name + tag,
            r.n,
            f"{r.minimum:>7.2f}",
            f"{r.mean:>7.2f}",
            ci_str,
            f"{r.p50:>7.2f}",
            f"{r.p95:>7.2f}",
            f"{r.p99:>7.2f}",
        )))

    print()
    print(f"  [{throughput['section']}]")
    print(f"  {throughput['name']}")
    print(f"    Rounds: {throughput['rounds']:,}  |  "
          f"Rate: {throughput['rounds_per_sec']:,.1f} rounds/s  |  "
          f"Per-round: {throughput['us_per_round']:.2f} μs  |  "
          f"Rounds/contact: {throughput['rounds_per_isl_contact']:,}")
    print("-" * W)
    print("  * batched timing: BATCH calls per timed block to stay above timer overhead")
    print("  95% CI computed via bootstrap resampling (n_boot=1000)")
    print("=" * W)


def save_json(results: List[BenchResult], throughput: dict,
              sysinfo: dict, path: Path) -> None:
    data = {
        "benchmark": "LIOS off-chain protocol computational overhead",
        "sysinfo": sysinfo,
        "methodology": {
            "warmup_iterations": WARMUP,
            "batch_size_fast_ops": BATCH,
            "batch_threshold_us": BATCH_THRESHOLD_US,
            "bootstrap_resamples": N_BOOT,
            "isl_contact_window_sec": ISL_CONTACT_SEC,
        },
        "results": [r.to_dict() for r in results],
        "throughput": throughput,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as f:
        json.dump(data, f, indent=2)
    print(f"  JSON  → {path}")


def save_latex(results: List[BenchResult], throughput: dict, path: Path) -> None:
    """Write a LaTeX table with mean, 95% CI, and p50/p99 for publication."""
    lines = [
        r"\begin{table}[ht]",
        r"\centering",
        r"\caption{LIOS off-chain protocol per-operation computational overhead. "
        r"Values in \si{\micro\second}; 95\% bootstrap CI on the mean. "
        r"Measured on \textit{\sysinfo} with Python \textit{\pyver}.}",
        r"\label{tab:crypto-overhead}",
        r"\begin{tabular}{lS[table-format=5.1]"
        r"S[table-format=5.1]@{\,}l"
        r"S[table-format=5.1]S[table-format=5.1]}",
        r"\toprule",
        r"Operation & {Mean} & \multicolumn{2}{c}{95\% CI} & {p50} & {p99} \\",
        r"\midrule",
    ]
    current_section = ""
    for r in results:
        if r.section != current_section:
            current_section = r.section
            lines.append(
                r"\addlinespace[2pt]"
                r"\multicolumn{6}{l}{\textit{"
                + current_section.replace("&", r"\&")
                + r"}} \\"
            )
        lo, hi = r.ci95()
        ci_str = f"[{lo:.1f},\\,{hi:.1f}]"
        name = r.name.replace("_", r"\_").replace("&", r"\&")
        lines.append(
            f"\\quad {name} & {r.mean:.1f} & "
            f"\\multicolumn{{2}}{{c}}{{{ci_str}}} & "
            f"{r.p50:.1f} & {r.p99:.1f} \\\\"
        )
    lines += [
        r"\midrule",
        rf"\multicolumn{{6}}{{l}}{{\textit{{Throughput: "
        rf"{throughput['rounds_per_sec']:,.0f}\,rounds/s\;"
        rf"({throughput['us_per_round']:.0f}\,\si{{\micro\second}}/round)\;"
        rf"→\;{throughput['rounds_per_isl_contact']:,}\,rounds per {ISL_CONTACT_SEC:.0f}\,s contact}}}} \\",
        r"\bottomrule",
        r"\end{tabular}",
        r"\end{table}",
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n")
    print(f"  LaTeX → {path}")


def save_figures(results: List[BenchResult], throughput: dict,
                 scaling: List[BenchResult], out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)

    # ── Figure 1: per-section bar charts (mean ± 95% CI) ──────────────────────
    sections: Dict[str, List[BenchResult]] = {}
    for r in results:
        sections.setdefault(r.section, []).append(r)

    fig, axes = plt.subplots(1, len(sections),
                             figsize=(max(16, len(sections) * 4), 5),
                             sharey=False)
    if len(sections) == 1:
        axes = [axes]

    palette = plt.cm.tab10.colors
    for ax, (sec_name, sec_rs) in zip(axes, sections.items()):
        names = [r.name for r in sec_rs]
        means = [r.mean for r in sec_rs]
        cis   = [r.ci95() for r in sec_rs]
        lo_errs = [m - lo for m, (lo, _) in zip(means, cis)]
        hi_errs = [hi - m for m, (_, hi) in zip(means, cis)]

        x = np.arange(len(names))
        colors = [palette[i % 10] for i in range(len(names))]
        ax.bar(x, means, color=colors, alpha=0.85, zorder=2)
        ax.errorbar(x, means, yerr=[lo_errs, hi_errs],
                    fmt="none", color="black", capsize=3, linewidth=0.9, zorder=3)
        ax.set_xticks(x)
        ax.set_xticklabels(
            [n.split("(")[0].strip()[:28] for n in names],
            rotation=38, ha="right", fontsize=7,
        )
        ax.set_ylabel("Latency (μs)")
        ax.set_title(sec_name, fontsize=9)
        ax.grid(True, axis="y", alpha=0.3, zorder=0)
        ax.set_axisbelow(True)

    fig.suptitle(
        "LIOS Off-Chain Protocol — Computational Overhead\n"
        "(bars = mean, error bars = 95% bootstrap CI)",
        fontsize=10, y=1.02,
    )
    fig.tight_layout()
    p1 = out_dir / "benchmark_offchain_ops.png"
    fig.savefig(p1, dpi=150, bbox_inches="tight")
    fig.savefig(p1.with_suffix(".pdf"), bbox_inches="tight")
    plt.close(fig)
    print(f"  Figure (ops) → {p1}  (+PDF)")

    # ── Figure 2: channel-count scaling ───────────────────────────────────────
    if scaling:
        n_channels = []
        for r in scaling:
            import re
            m = re.search(r"(\d+) channels", r.name)
            if m:
                n_channels.append((int(m.group(1)), r))
        if n_channels:
            xs = [x for x, _ in n_channels]
            ys = [r.mean for _, r in n_channels]
            cis = [r.ci95() for _, r in n_channels]
            lo_errs = [m - lo for m, (lo, _) in zip(ys, cis)]
            hi_errs = [hi - m for m, (_, hi) in zip(ys, cis)]

            fig2, ax2 = plt.subplots(figsize=(6, 4))
            ax2.plot(xs, ys, "o-", color="steelblue", linewidth=1.8, markersize=6, zorder=3)
            ax2.errorbar(xs, ys, yerr=[lo_errs, hi_errs],
                         fmt="none", color="steelblue", capsize=4, linewidth=1.0)
            ax2.set_xlabel("Open channels per satellite")
            ax2.set_ylabel("record_forwarding() latency (μs)")
            ax2.set_title("Channel-count scaling: record_forwarding()", fontsize=10)
            ax2.grid(True, alpha=0.3)
            ax2.set_xticks(xs)
            fig2.tight_layout()
            p2 = out_dir / "benchmark_offchain_scaling.png"
            fig2.savefig(p2, dpi=150, bbox_inches="tight")
            fig2.savefig(p2.with_suffix(".pdf"), bbox_inches="tight")
            plt.close(fig2)
            print(f"  Figure (scaling) → {p2}  (+PDF)")

    # ── Figure 3: sample CDF for key operations ────────────────────────────────
    key_ops = ["ECDSA P-256 sign", "cosign_proof", "Forward+cosign round-trip",
               "Full auth handshake"]
    selected = [r for r in results
                if any(k.lower() in r.name.lower() for k in key_ops)]
    if selected:
        fig3, ax3 = plt.subplots(figsize=(7, 4))
        for r in selected:
            s = sorted(r.samples_us)
            cdf = np.arange(1, len(s) + 1) / len(s)
            ax3.plot(s, cdf, linewidth=1.5, label=r.name.split("(")[0].strip()[:35])
        ax3.axhline(0.95, linestyle="--", color="gray", linewidth=0.8, label="p95")
        ax3.axhline(0.50, linestyle=":",  color="gray", linewidth=0.8, label="p50")
        ax3.set_xlabel("Latency (μs)")
        ax3.set_ylabel("CDF")
        ax3.set_title("Latency CDF — key protocol operations", fontsize=10)
        ax3.legend(fontsize=7, loc="lower right")
        ax3.grid(True, alpha=0.3)
        fig3.tight_layout()
        p3 = out_dir / "benchmark_offchain_cdf.png"
        fig3.savefig(p3, dpi=150, bbox_inches="tight")
        fig3.savefig(p3.with_suffix(".pdf"), bbox_inches="tight")
        plt.close(fig3)
        print(f"  Figure (CDF)     → {p3}  (+PDF)")


# ── Main ───────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="LIOS off-chain protocol benchmark")
    parser.add_argument("--n", type=int, default=500,
                        help="Iterations per operation (default 500)")
    parser.add_argument("--throughput-sec", type=float, default=5.0,
                        help="Duration for sustained-throughput test (default 5 s)")
    parser.add_argument("--out", default="results",
                        help="Output directory (default: results/)")
    args = parser.parse_args()

    n = args.n
    out = Path(args.out)

    sysinfo = capture_sysinfo()
    cpu = sysinfo.get("cpu_model", sysinfo.get("processor", "unknown"))
    print(f"\nLIOS Off-Chain Benchmark")
    print(f"  n={n} iterations/op  warmup={WARMUP}  batch={BATCH} (for ops <{BATCH_THRESHOLD_US} μs)")
    print(f"  CPU: {cpu}")
    print(f"  Python {sysinfo.get('python_version')}  "
          f"cryptography {sysinfo.get('cryptography_version', '?')}")
    print()

    all_results: List[BenchResult] = []

    print("  [1/6] Raw crypto primitives ...", end=" ", flush=True)
    all_results += bench_raw_crypto(n)
    print("done")

    print("  [2/6] Key hierarchy ...", end=" ", flush=True)
    all_results += bench_key_hierarchy(n)
    print("done")

    print("  [3/6] Auth handshake ...", end=" ", flush=True)
    all_results += bench_auth(n)
    print("done")

    print("  [4/6] Off-chain channel ...", end=" ", flush=True)
    all_results += bench_offchain(n)
    print("done")

    print("  [5/6] Channel-count scaling ...", end=" ", flush=True)
    scaling = bench_channel_scaling(n)
    print("done")

    print(f"  [6/6] Throughput ({args.throughput_sec:.0f}s) ...", end=" ", flush=True)
    throughput = bench_throughput(args.throughput_sec)
    print("done")

    print_table(all_results + scaling, throughput)
    print_isl_context(all_results, throughput)

    save_json(all_results + scaling, throughput, sysinfo, out / "benchmark_offchain.json")
    save_latex(all_results, throughput, out / "benchmark_offchain_table.tex")
    save_figures(all_results, throughput, scaling, out / "figures")


if __name__ == "__main__":
    main()

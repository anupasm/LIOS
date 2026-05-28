"""Two-tier key hierarchy for LIOS satellite authentication.

Tier 1: Operator CA (root key, offline HSM in production).
Tier 2: Satellite operational keys (90-day rotation, signed by operator CA).

Uses ECDSA P-256 throughout (NIST FIPS 186-4 / CCSDS 350.0-G-3).
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Set

from config import cfg
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import (
    decode_dss_signature,
    encode_dss_signature,
)


# ── helpers ────────────────────────────────────────────────────────────────────

def _now() -> datetime:
    return datetime.now(timezone.utc)


def _privkey() -> ec.EllipticCurvePrivateKey:
    return ec.generate_private_key(ec.SECP256R1())


def _pub_pem(key: ec.EllipticCurvePrivateKey) -> str:
    return key.public_key().public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode()


def _priv_pem(key: ec.EllipticCurvePrivateKey) -> str:
    return key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()


def _load_pub(pem: str) -> ec.EllipticCurvePublicKey:
    return serialization.load_pem_public_key(pem.encode())


def _load_priv(pem: str) -> ec.EllipticCurvePrivateKey:
    return serialization.load_pem_private_key(pem.encode(), password=None)


def _sign(private_key: ec.EllipticCurvePrivateKey, data: bytes) -> bytes:
    """ECDSA P-256 / SHA-256 signature; returns DER-encoded bytes."""
    return private_key.sign(data, ec.ECDSA(hashes.SHA256()))


def _verify(public_key: ec.EllipticCurvePublicKey, data: bytes, sig_der: bytes) -> bool:
    try:
        public_key.verify(sig_der, data, ec.ECDSA(hashes.SHA256()))
        return True
    except Exception:
        return False


def _canonical_json(obj: dict) -> bytes:
    """Deterministic JSON serialisation (sorted keys, no whitespace)."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


# ── Satellite Certificate ──────────────────────────────────────────────────────

@dataclass
class SatelliteCert:
    version: int
    satellite_id: str
    operator_id: str
    public_key_pem: str
    valid_from: datetime
    valid_until: datetime
    permitted_operators: List[str]
    signature: bytes  # ECDSA DER, by operator CA over canonical JSON of other fields

    def is_valid(self, at: Optional[datetime] = None) -> bool:
        t = at or _now()
        return self.valid_from <= t <= self.valid_until

    def to_dict(self) -> dict:
        return {
            "version": self.version,
            "satellite_id": self.satellite_id,
            "operator_id": self.operator_id,
            "public_key_pem": self.public_key_pem,
            "valid_from": self.valid_from.isoformat(),
            "valid_until": self.valid_until.isoformat(),
            "permitted_operators": self.permitted_operators,
        }

    def serialise(self) -> dict:
        d = self.to_dict()
        d["signature"] = self.signature.hex()
        return d

    @classmethod
    def deserialise(cls, d: dict) -> "SatelliteCert":
        return cls(
            version=d["version"],
            satellite_id=d["satellite_id"],
            operator_id=d["operator_id"],
            public_key_pem=d["public_key_pem"],
            valid_from=datetime.fromisoformat(d["valid_from"]),
            valid_until=datetime.fromisoformat(d["valid_until"]),
            permitted_operators=d["permitted_operators"],
            signature=bytes.fromhex(d["signature"]),
        )


# ── Operator CA ────────────────────────────────────────────────────────────────

class OperatorCA:
    """Operator Certificate Authority (root of trust for one operator fleet)."""

    def __init__(self, operator_id: str) -> None:
        self.operator_id = operator_id
        self._root_key: ec.EllipticCurvePrivateKey = _privkey()
        self._revoked: Set[str] = set()
        self._revocation_timestamps: Dict[str, datetime] = {}

    @property
    def public_key_pem(self) -> str:
        return _pub_pem(self._root_key)

    @property
    def public_key(self) -> ec.EllipticCurvePublicKey:
        return self._root_key.public_key()

    def issue_satellite_cert(
        self,
        satellite_id: str,
        pub_key: ec.EllipticCurvePublicKey | str,
        valid_days: int = cfg.crypto.cert_valid_days,
        permitted_operators: Optional[List[str]] = None,
    ) -> tuple[SatelliteCert, ec.EllipticCurvePrivateKey]:
        """Issue a certificate for satellite_id.

        pub_key may be an ec.EllipticCurvePublicKey or a PEM string (spec-compatible).
        Returns (cert, satellite_private_key) pair.
        """
        sat_priv = _privkey()
        if isinstance(pub_key, str):
            pub_pem = pub_key
        else:
            pub_pem = pub_key.public_bytes(
                serialization.Encoding.PEM,
                serialization.PublicFormat.SubjectPublicKeyInfo,
            ).decode()
        now = _now()
        cert_dict = {
            "version": 1,
            "satellite_id": satellite_id,
            "operator_id": self.operator_id,
            "public_key_pem": pub_pem,
            "valid_from": now.isoformat(),
            "valid_until": (now + timedelta(days=valid_days)).isoformat(),
            "permitted_operators": permitted_operators or [self.operator_id],
        }
        sig = _sign(self._root_key, _canonical_json(cert_dict))
        cert = SatelliteCert(
            version=1,
            satellite_id=satellite_id,
            operator_id=self.operator_id,
            public_key_pem=pub_pem,
            valid_from=now,
            valid_until=now + timedelta(days=valid_days),
            permitted_operators=permitted_operators or [self.operator_id],
            signature=sig,
        )
        return cert, sat_priv

    def issue_for_new_key(
        self,
        satellite_id: str,
        valid_days: int = cfg.crypto.cert_valid_days,
        permitted_operators: Optional[List[str]] = None,
    ) -> tuple[SatelliteCert, ec.EllipticCurvePrivateKey]:
        """Generate a new satellite key pair and issue a cert for it."""
        sat_priv = _privkey()
        cert, _ = self.issue_satellite_cert(
            satellite_id, sat_priv.public_key(), valid_days, permitted_operators
        )
        return cert, sat_priv

    def revoke(self, satellite_id: str) -> None:
        self._revoked.add(satellite_id)
        self._revocation_timestamps[satellite_id] = _now()

    def get_crl(self) -> List[str]:
        return list(self._revoked)

    def get_crl_delta(self, since: datetime) -> List[str]:
        return [
            sid for sid, ts in self._revocation_timestamps.items()
            if ts >= since
        ]

    def sign_satellite_key(self, satellite_id: str, pub_key_pem: str) -> bytes:
        """Sign (satellite_id || pubKeyPEM) for on-chain key registration."""
        payload = (satellite_id + pub_key_pem).encode()
        return _sign(self._root_key, payload)


# ── Satellite Key Store ────────────────────────────────────────────────────────

class SatelliteKeyStore:
    """Runs on each satellite; stores operator root keys and the revocation cache.

    N ≤ 20 operator keys fit in ~8 KB — feasible for even small satellites.
    """

    def __init__(self) -> None:
        self._operator_keys: Dict[str, ec.EllipticCurvePublicKey] = {}
        self._revoked: Set[str] = set()

    def register_operator(self, operator_id: str, public_key: ec.EllipticCurvePublicKey) -> None:
        self._operator_keys[operator_id] = public_key

    def register_operator_pem(self, operator_id: str, pem: str) -> None:
        self._operator_keys[operator_id] = _load_pub(pem)

    def verify_peer_cert(self, cert: SatelliteCert, at: Optional[datetime] = None) -> bool:
        """Verify a peer satellite's certificate.

        Checks:
          1. Operator root key is known.
          2. ECDSA signature is valid.
          3. Certificate is within its validity window.
          4. Satellite is not in the local revocation cache.
        """
        op_key = self._operator_keys.get(cert.operator_id)
        if op_key is None:
            return False
        if not _verify(op_key, _canonical_json(cert.to_dict()), cert.signature):
            return False
        if not cert.is_valid(at):
            return False
        if cert.satellite_id in self._revoked:
            return False
        return True

    def update_revocation_cache(self, crl_delta: List[str]) -> None:
        self._revoked.update(crl_delta)

    def is_revoked(self, satellite_id: str) -> bool:
        return satellite_id in self._revoked

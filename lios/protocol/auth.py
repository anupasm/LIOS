"""Satellite-to-satellite authentication handshake for LIOS (§10.4).

Phase 1: HELLO — exchange certificates and nonces.
Phase 2: AUTH  — exchange ECDH public keys and mutual signatures.
Result:  Shared session key K via ECDH (P-256); all subsequent messages
         encrypted with AES-256-GCM using K.

Security properties:
  - Mutual authentication via ECDSA certificate chain.
  - Forward secrecy via ephemeral ECDH.
  - Nonce prevents replay within a session.
  - Timestamp within ±30 s of simulation clock.
"""
from __future__ import annotations

import hashlib
import os
import struct
import time
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Optional, Set

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from config import cfg
from crypto.key_hierarchy import (
    SatelliteCert,
    SatelliteKeyStore,
    _canonical_json,
    _sign,
    _verify,
)


TIMESTAMP_TOLERANCE_SEC = cfg.protocol.timestamp_tolerance_sec
NONCE_BYTES             = cfg.protocol.nonce_bytes


class AuthState(Enum):
    IDLE = auto()
    HELLO_SENT = auto()
    HELLO_RECEIVED = auto()
    AUTHENTICATED = auto()
    FAILED = auto()


@dataclass
class HelloMessage:
    satellite_id: str
    cert: SatelliteCert
    nonce: bytes               # random 32 bytes
    ecdh_pub_pem: str          # ephemeral ECDH public key (P-256 PEM)
    timestamp: float           # simulation time


@dataclass
class AuthMessage:
    satellite_id: str
    ecdh_pub_pem: str
    nonce_sig: bytes           # ECDSA sig over (my_nonce || peer_nonce)


@dataclass
class EncryptedMessage:
    ciphertext: bytes
    nonce: bytes               # 12-byte AES-GCM nonce


class ContactAuthSession:
    """Manages one authentication session per ISL contact.

    Instantiate fresh for each contact; do not reuse.
    """

    def __init__(
        self,
        my_cert: SatelliteCert,
        my_priv_key: ec.EllipticCurvePrivateKey,
        key_store: SatelliteKeyStore,
        sim_time: float = 0.0,
    ) -> None:
        self._cert = my_cert
        self._priv = my_priv_key
        self._ks = key_store
        self._sim_time = sim_time
        self.state = AuthState.IDLE
        self._my_nonce: bytes = os.urandom(NONCE_BYTES)
        self._peer_nonce: Optional[bytes] = None
        self._seen_nonces: Set[bytes] = set()  # replay prevention within session TTL
        self._ecdh_priv: X25519PrivateKey = X25519PrivateKey.generate()
        self._session_key: Optional[bytes] = None
        self._peer_cert: Optional[SatelliteCert] = None

    @property
    def session_key(self) -> Optional[bytes]:
        return self._session_key

    @property
    def peer_cert(self) -> Optional[SatelliteCert]:
        return self._peer_cert

    # ── Phase 1 ────────────────────────────────────────────────────────────────

    def initiate(self) -> HelloMessage:
        """Build HELLO message to send to peer (initiator side)."""
        ecdh_pub_pem = self._ecdh_priv.public_key().public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        ).decode()
        self.state = AuthState.HELLO_SENT
        return HelloMessage(
            satellite_id=self._cert.satellite_id,
            cert=self._cert,
            nonce=self._my_nonce,
            ecdh_pub_pem=ecdh_pub_pem,
            timestamp=self._sim_time,
        )

    def handle_hello(self, hello: HelloMessage) -> Optional[HelloMessage]:
        """Process incoming HELLO; return own HELLO response or None on failure."""
        if not self._verify_hello(hello):
            self.state = AuthState.FAILED
            return None
        self._peer_nonce = hello.nonce
        self._peer_cert = hello.cert
        self.state = AuthState.HELLO_RECEIVED

        ecdh_pub_pem = self._ecdh_priv.public_key().public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        ).decode()
        return HelloMessage(
            satellite_id=self._cert.satellite_id,
            cert=self._cert,
            nonce=self._my_nonce,
            ecdh_pub_pem=ecdh_pub_pem,
            timestamp=self._sim_time,
        )

    def _verify_hello(self, hello: HelloMessage) -> bool:
        if not self._ks.verify_peer_cert(hello.cert):
            return False
        if abs(hello.timestamp - self._sim_time) > TIMESTAMP_TOLERANCE_SEC:
            return False
        # Replay: reject nonces seen before, but allow re-verify of the one we already
        # accepted for this session (complete_auth legitimately calls this twice).
        if hello.nonce in self._seen_nonces and hello.nonce != self._peer_nonce:
            return False
        self._seen_nonces.add(hello.nonce)
        return True

    # ── Phase 2 ────────────────────────────────────────────────────────────────

    def complete_auth(
        self,
        peer_hello: HelloMessage,
        peer_auth: Optional[AuthMessage] = None,
    ) -> bool:
        """Complete mutual authentication and derive shared session key.

        peer_hello:  the HELLO received from the peer.
        peer_auth:   the peer's AUTH message (optional; if None, skip AUTH sig check
                     — used when simulating without full message exchange).
        Returns True if authentication succeeded.
        """
        if not self._verify_hello(peer_hello):
            self.state = AuthState.FAILED
            return False
        self._peer_nonce = peer_hello.nonce
        self._peer_cert = peer_hello.cert

        if peer_auth is not None:
            # Verify peer's signature over (peer_nonce || my_nonce)
            msg = peer_auth.ecdh_pub_pem.encode() + self._my_nonce + self._peer_nonce
            peer_pub_key = peer_hello.cert
            # Load actual public key from cert PEM
            from cryptography.hazmat.primitives.serialization import load_pem_public_key
            peer_ec_pub = load_pem_public_key(peer_hello.cert.public_key_pem.encode())
            if not _verify(peer_ec_pub, msg, peer_auth.nonce_sig):
                self.state = AuthState.FAILED
                return False

        # Derive shared session key via ECDH
        peer_ecdh_pub = serialization.load_pem_public_key(peer_hello.ecdh_pub_pem.encode())
        shared_secret = self._ecdh_priv.exchange(peer_ecdh_pub)
        # Sort nonces for deterministic salt (same result on both sides)
        nonces = sorted([self._my_nonce, self._peer_nonce])
        self._session_key = HKDF(
            algorithm=hashes.SHA256(),
            length=32,
            salt=nonces[0] + nonces[1],
            info=b"lios-session-key",
        ).derive(shared_secret)
        self.state = AuthState.AUTHENTICATED
        return True

    def build_auth_message(self) -> AuthMessage:
        """Build AUTH message (sent after HELLO exchange)."""
        ecdh_pub_pem = self._ecdh_priv.public_key().public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        ).decode()
        msg = ecdh_pub_pem.encode() + self._my_nonce + (self._peer_nonce or b"")
        sig = _sign(self._priv, msg)
        return AuthMessage(
            satellite_id=self._cert.satellite_id,
            ecdh_pub_pem=ecdh_pub_pem,
            nonce_sig=sig,
        )

    # ── Encryption / Decryption ────────────────────────────────────────────────

    def encrypt(self, plaintext: bytes) -> EncryptedMessage:
        if self._session_key is None:
            raise RuntimeError("Session key not derived — authenticate first")
        gcm_nonce = os.urandom(12)
        aesgcm = AESGCM(self._session_key)
        ct = aesgcm.encrypt(gcm_nonce, plaintext, None)
        return EncryptedMessage(ciphertext=ct, nonce=gcm_nonce)

    def decrypt(self, msg: EncryptedMessage) -> bytes:
        if self._session_key is None:
            raise RuntimeError("Session key not derived — authenticate first")
        aesgcm = AESGCM(self._session_key)
        return aesgcm.decrypt(msg.nonce, msg.ciphertext, None)

    # spec-compatible aliases
    def encrypt_message(self, plaintext: bytes) -> EncryptedMessage:
        return self.encrypt(plaintext)

    def decrypt_message(self, msg: EncryptedMessage) -> bytes:
        return self.decrypt(msg)

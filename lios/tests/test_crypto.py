"""Unit tests for the LIOS cryptographic modules."""
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest
from cryptography.hazmat.primitives.asymmetric import ec

from crypto.key_hierarchy import OperatorCA, SatelliteKeyStore, _sign, _verify, _canonical_json


class TestOperatorCA:
    def setup_method(self):
        self.ca = OperatorCA("opA")

    def test_ca_has_public_key(self):
        assert self.ca.public_key_pem.startswith("-----BEGIN PUBLIC KEY-----")

    def test_issue_cert(self):
        cert, priv = self.ca.issue_for_new_key("opA-s1")
        assert cert.satellite_id == "opA-s1"
        assert cert.operator_id == "opA"
        assert cert.is_valid()

    def test_cert_validity_window(self):
        cert, _ = self.ca.issue_for_new_key("opA-s1", valid_days=90)
        now = datetime.now(timezone.utc)
        assert cert.valid_from <= now <= cert.valid_until

    def test_cert_expired_fails(self):
        cert, _ = self.ca.issue_for_new_key("opA-s1", valid_days=1)
        past = cert.valid_until + timedelta(days=1)
        assert not cert.is_valid(at=past)

    def test_revocation(self):
        cert, _ = self.ca.issue_for_new_key("opA-s1")
        self.ca.revoke("opA-s1")
        assert "opA-s1" in self.ca.get_crl()

    def test_sign_satellite_key(self):
        cert, _ = self.ca.issue_for_new_key("opA-s1")
        sig = self.ca.sign_satellite_key("opA-s1", cert.public_key_pem)
        assert len(sig) > 40


class TestSatelliteKeyStore:
    def setup_method(self):
        self.ca_a = OperatorCA("opA")
        self.ca_b = OperatorCA("opB")

    def _make_ks(self) -> SatelliteKeyStore:
        ks = SatelliteKeyStore()
        ks.register_operator("opA", self.ca_a.public_key)
        ks.register_operator("opB", self.ca_b.public_key)
        return ks

    def test_valid_cert_verifies(self):
        ks = self._make_ks()
        cert, _ = self.ca_a.issue_for_new_key("opA-s1", permitted_operators=["opA", "opB"])
        assert ks.verify_peer_cert(cert)

    def test_cert_from_unknown_operator_fails(self):
        ks = self._make_ks()
        ca_c = OperatorCA("opC")
        cert, _ = ca_c.issue_for_new_key("opC-s1")
        assert not ks.verify_peer_cert(cert)

    def test_revoked_cert_fails(self):
        ks = self._make_ks()
        cert, _ = self.ca_a.issue_for_new_key("opA-s1")
        ks.update_revocation_cache(["opA-s1"])
        assert not ks.verify_peer_cert(cert)

    def test_crl_update(self):
        ks = self._make_ks()
        assert not ks.is_revoked("opA-s1")
        ks.update_revocation_cache(["opA-s1"])
        assert ks.is_revoked("opA-s1")


class TestECDSA:
    def test_sign_verify_roundtrip(self):
        priv = ec.generate_private_key(ec.SECP256R1())
        data = b"hello lios"
        sig = _sign(priv, data)
        assert _verify(priv.public_key(), data, sig)

    def test_wrong_data_fails(self):
        priv = ec.generate_private_key(ec.SECP256R1())
        sig = _sign(priv, b"data")
        assert not _verify(priv.public_key(), b"different_data", sig)

    def test_wrong_key_fails(self):
        priv1 = ec.generate_private_key(ec.SECP256R1())
        priv2 = ec.generate_private_key(ec.SECP256R1())
        sig = _sign(priv1, b"data")
        assert not _verify(priv2.public_key(), b"data", sig)

    def test_canonical_json_deterministic(self):
        d1 = {"z": 1, "a": 2, "m": 3}
        d2 = {"m": 3, "z": 1, "a": 2}
        assert _canonical_json(d1) == _canonical_json(d2)

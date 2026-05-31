"""Unit tests for the off-chain payment channel protocol."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest
from cryptography.hazmat.primitives.asymmetric import ec

from crypto.key_hierarchy import OperatorCA
from protocol.auth import ContactAuthSession
from protocol.isl_state_machine import ISLChannelStatus, ISLStateMachine
from protocol.offchain import BalanceProof, OffChainProtocol, T_LOW_FRACTION


CHANNEL_ID = "opA-s1__opB-s2"
OP_CH_ID = "opA_opB_ch"
BALANCE_KB = 1_024.0  # 1 MB per side


def _make_protocol_pair():
    """Return two OffChainProtocol instances (A and B) sharing a channel."""
    ca_a = OperatorCA("opA")
    ca_b = OperatorCA("opB")
    cert_a, priv_a = ca_a.issue_for_new_key("opA-s1", permitted_operators=["opA", "opB"])
    cert_b, priv_b = ca_b.issue_for_new_key("opB-s2", permitted_operators=["opA", "opB"])

    proto_a = OffChainProtocol("opA-s1", priv_a, cert_a)
    proto_b = OffChainProtocol("opB-s2", priv_b, cert_b)

    state_a = proto_a.open_channel(CHANNEL_ID, OP_CH_ID, "opB-s2", BALANCE_KB, "A")
    state_b = proto_b.open_channel(CHANNEL_ID, OP_CH_ID, "opA-s1", BALANCE_KB, "B")

    pub_a = priv_a.public_key()
    pub_b = priv_b.public_key()
    return proto_a, proto_b, pub_a, pub_b, cert_a, cert_b


class TestOffChainProtocol:
    def setup_method(self):
        self.proto_a, self.proto_b, self.pub_a, self.pub_b, _, _ = _make_protocol_pair()

    def test_channel_opened(self):
        ch = self.proto_a.get_channel(CHANNEL_ID)
        assert ch is not None
        assert ch.status == "ACTIVE"

    def test_initial_balances_symmetric(self):
        ch = self.proto_a.get_channel(CHANNEL_ID)
        assert ch.balance_a_kb == BALANCE_KB
        assert ch.balance_b_kb == BALANCE_KB

    def test_initial_capacity(self):
        ch = self.proto_a.get_channel(CHANNEL_ID)
        assert ch.initial_capacity_kb == BALANCE_KB * 2

    def test_forward_deducts_balance(self):
        fwd_kb = 100.0
        dummy_sig = b"a" * 72
        proof = self.proto_a.record_forwarding(CHANNEL_ID, fwd_kb, self.pub_b, 0.0, dummy_sig)
        assert proof is not None
        ch = self.proto_a.get_channel(CHANNEL_ID)
        assert ch.balance_a_kb == BALANCE_KB - fwd_kb
        assert ch.balance_b_kb == BALANCE_KB + fwd_kb

    def test_balance_conservation_invariant(self):
        for i in range(5):
            self.proto_a.record_forwarding(CHANNEL_ID, 50.0, self.pub_b, float(i), b"s" * 72)
        ch = self.proto_a.get_channel(CHANNEL_ID)
        total = ch.balance_a_kb + ch.balance_b_kb
        assert abs(total - BALANCE_KB * 2) < 1e-9

    def test_seq_num_monotonically_increases(self):
        for i in range(3):
            self.proto_a.record_forwarding(CHANNEL_ID, 10.0, self.pub_b, float(i), b"s" * 72)
        ch = self.proto_a.get_channel(CHANNEL_ID)
        assert ch.seq_num == 3

    def test_cannot_forward_with_insufficient_balance(self):
        # Drain almost all balance
        big = BALANCE_KB - 1.0
        self.proto_a.record_forwarding(CHANNEL_ID, big, self.pub_b, 0.0, b"s" * 72)
        # Now try to forward more than remaining
        result = self.proto_a.record_forwarding(CHANNEL_ID, 10.0, self.pub_b, 1.0, b"s" * 72)
        assert result is None

    def test_forwarding_log_grows(self):
        self.proto_a.record_forwarding(CHANNEL_ID, 10.0, self.pub_b, 0.0, b"s" * 72)
        ch = self.proto_a.get_channel(CHANNEL_ID)
        assert ch.forwarding_log.length() == 1

    def test_t1_trigger_fires_when_low(self):
        threshold = BALANCE_KB * T_LOW_FRACTION * 0.5
        amount = BALANCE_KB - threshold
        self.proto_a.record_forwarding(CHANNEL_ID, amount, self.pub_b, 0.0, b"s" * 72)
        triggers = self.proto_a.evaluate_settlement_triggers(CHANNEL_ID)
        assert "T1" in triggers

    def test_no_triggers_initially(self):
        triggers = self.proto_a.evaluate_settlement_triggers(CHANNEL_ID)
        assert len(triggers) == 0

    def test_can_forward_when_active(self):
        assert self.proto_a.can_forward(CHANNEL_ID)

    def test_cannot_forward_when_paused(self):
        self.proto_a.pause_channel(CHANNEL_ID)
        assert not self.proto_a.can_forward(CHANNEL_ID)

    def test_resume_channel(self):
        self.proto_a.pause_channel(CHANNEL_ID)
        self.proto_a.resume_channel(CHANNEL_ID)
        assert self.proto_a.can_forward(CHANNEL_ID)

    def test_settlement_payload_includes_proof(self):
        self.proto_a.record_forwarding(CHANNEL_ID, 10.0, self.pub_b, 0.0, b"s" * 72)
        payload = self.proto_a.get_settlement_payload(CHANNEL_ID)
        assert payload is not None
        assert payload.latest_proof.seq_num >= 1

    def test_balance_proof_has_sig_a(self):
        proof = self.proto_a.record_forwarding(CHANNEL_ID, 10.0, self.pub_b, 0.0, b"s" * 72)
        assert proof is not None
        assert len(proof.sig_a) > 0
        assert proof.sig_b == b""  # not yet cosigned

    def test_hello_build(self):
        hello = self.proto_a.build_hello(CHANNEL_ID)
        assert hello["satellite_id"] == "opA-s1"
        assert hello["last_seq_num"] == 0


class TestISLStateMachine:
    def setup_method(self):
        self.fsm = ISLStateMachine()
        self.fsm.register_channel(CHANNEL_ID, "opA-s1", "opB-s2")

    def test_initial_state_active(self):
        assert self.fsm.get_status(CHANNEL_ID) == ISLChannelStatus.ACTIVE

    def test_can_forward_when_active(self):
        assert self.fsm.can_forward(CHANNEL_ID)

    def test_pause_at_contact_end(self):
        self.fsm.record_contact_end_pause(CHANNEL_ID, 300.0, "opA")
        assert self.fsm.get_status(CHANNEL_ID) == ISLChannelStatus.PAUSED_PENDING_SETTLEMENT

    def test_cannot_forward_when_paused(self):
        self.fsm.record_contact_end_pause(CHANNEL_ID, 300.0, "opA")
        assert not self.fsm.can_forward(CHANNEL_ID)

    def test_settlement_finalized_advances_state(self):
        self.fsm.record_contact_end_pause(CHANNEL_ID, 300.0, "opA")
        self.fsm.on_settlement_finalized(CHANNEL_ID)
        assert self.fsm.get_status(CHANNEL_ID) == ISLChannelStatus.PAUSED_PENDING_RESUME

    def test_resume_requires_both_acks(self):
        self.fsm.record_contact_end_pause(CHANNEL_ID, 300.0, "opA")
        self.fsm.on_settlement_finalized(CHANNEL_ID)
        done = self.fsm.record_satellite_resume_ack(CHANNEL_ID, "opA-s1", 500.0)
        assert not done  # only one ack
        assert self.fsm.get_status(CHANNEL_ID) == ISLChannelStatus.PAUSED_PENDING_RESUME

    def test_resume_after_both_acks(self):
        self.fsm.record_contact_end_pause(CHANNEL_ID, 300.0, "opA")
        self.fsm.on_settlement_finalized(CHANNEL_ID)
        self.fsm.record_satellite_resume_ack(CHANNEL_ID, "opA-s1", 500.0)
        done = self.fsm.record_satellite_resume_ack(CHANNEL_ID, "opB-s2", 600.0)
        assert done
        assert self.fsm.get_status(CHANNEL_ID) == ISLChannelStatus.ACTIVE

    def test_oos_duration_computed(self):
        self.fsm.record_contact_end_pause(CHANNEL_ID, 300.0, "opA")
        self.fsm.on_settlement_finalized(CHANNEL_ID)
        self.fsm.record_satellite_resume_ack(CHANNEL_ID, "opA-s1", 500.0)
        self.fsm.record_satellite_resume_ack(CHANNEL_ID, "opB-s2", 600.0)
        duration = self.fsm.get_oos_duration(CHANNEL_ID)
        assert duration == 300.0  # 600 - 300

    def test_resumed_at_is_max_of_acks(self):
        self.fsm.record_contact_end_pause(CHANNEL_ID, 300.0, "opA")
        self.fsm.on_settlement_finalized(CHANNEL_ID)
        self.fsm.record_satellite_resume_ack(CHANNEL_ID, "opA-s1", 500.0)
        self.fsm.record_satellite_resume_ack(CHANNEL_ID, "opB-s2", 700.0)
        oos = self.fsm.get_oos_log(CHANNEL_ID)[0]
        assert oos.resumed_at == 700.0  # max(500, 700)

    def test_oos_log_populated(self):
        self.fsm.record_contact_end_pause(CHANNEL_ID, 300.0, "opA", "SETTLEMENT")
        log = self.fsm.get_oos_log(CHANNEL_ID)
        assert len(log) == 1
        assert log[0].reason == "SETTLEMENT"
        assert log[0].paused_at == 300.0

    def test_close_channel(self):
        self.fsm.close_channel(CHANNEL_ID)
        assert self.fsm.get_status(CHANNEL_ID) == ISLChannelStatus.CLOSED


class TestAuthentication:
    def setup_method(self):
        self.ca_a = OperatorCA("opA")
        self.ca_b = OperatorCA("opB")
        self.cert_a, self.priv_a = self.ca_a.issue_for_new_key("opA-s1", permitted_operators=["opA", "opB"])
        self.cert_b, self.priv_b = self.ca_b.issue_for_new_key("opB-s2", permitted_operators=["opA", "opB"])
        from crypto.key_hierarchy import SatelliteKeyStore
        self.ks_a = SatelliteKeyStore()
        self.ks_a.register_operator("opA", self.ca_a.public_key)
        self.ks_a.register_operator("opB", self.ca_b.public_key)
        self.ks_b = SatelliteKeyStore()
        self.ks_b.register_operator("opA", self.ca_a.public_key)
        self.ks_b.register_operator("opB", self.ca_b.public_key)

    def test_mutual_auth_succeeds(self):
        sess_a = ContactAuthSession(self.cert_a, self.priv_a, self.ks_a, sim_time=100.0)
        sess_b = ContactAuthSession(self.cert_b, self.priv_b, self.ks_b, sim_time=100.0)

        hello_a = sess_a.initiate()
        hello_b = sess_b.handle_hello(hello_a)
        assert hello_b is not None

        ok_a = sess_a.complete_auth(hello_b)
        assert ok_a
        ok_b = sess_b.complete_auth(hello_a)
        assert ok_b

    def test_session_key_derived(self):
        sess_a = ContactAuthSession(self.cert_a, self.priv_a, self.ks_a, sim_time=100.0)
        sess_b = ContactAuthSession(self.cert_b, self.priv_b, self.ks_b, sim_time=100.0)
        hello_a = sess_a.initiate()
        hello_b = sess_b.handle_hello(hello_a)
        sess_a.complete_auth(hello_b)
        sess_b.complete_auth(hello_a)
        assert sess_a.session_key is not None
        assert len(sess_a.session_key) == 32

    def test_encrypt_decrypt_roundtrip(self):
        sess_a = ContactAuthSession(self.cert_a, self.priv_a, self.ks_a, sim_time=0.0)
        sess_b = ContactAuthSession(self.cert_b, self.priv_b, self.ks_b, sim_time=0.0)
        hello_a = sess_a.initiate()
        hello_b = sess_b.handle_hello(hello_a)
        sess_a.complete_auth(hello_b)
        sess_b.complete_auth(hello_a)

        msg = b"LIOS balance proof: seq=42"
        enc = sess_a.encrypt(msg)
        dec = sess_b.decrypt(enc)
        assert dec == msg

    def test_stale_timestamp_fails(self):
        sess_a = ContactAuthSession(self.cert_a, self.priv_a, self.ks_a, sim_time=0.0)
        sess_b = ContactAuthSession(self.cert_b, self.priv_b, self.ks_b, sim_time=100_000.0)
        hello_a = sess_a.initiate()
        # B's clock is far ahead — should reject A's hello
        result = sess_b.handle_hello(hello_a)
        assert result is None

    def test_revoked_cert_fails_auth(self):
        self.ks_b.update_revocation_cache(["opA-s1"])
        sess_a = ContactAuthSession(self.cert_a, self.priv_a, self.ks_a, sim_time=0.0)
        sess_b = ContactAuthSession(self.cert_b, self.priv_b, self.ks_b, sim_time=0.0)
        hello_a = sess_a.initiate()
        result = sess_b.handle_hello(hello_a)
        assert result is None

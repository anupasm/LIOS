"""Off-chain payment-channel protocol for LIOS bilateral ISL traffic accounting.

Implements the cryptographic state machine described in §7 of the LIOS spec:
  HELLO → SYNC → FORWARD+COMMIT → settlement trigger evaluation

Key invariant: balance_a_kb + balance_b_kb == initial_capacity_kb (always).
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import List, Literal, Optional, Tuple

from cryptography.hazmat.primitives.asymmetric import ec

from crypto.hash_chain import HashChainLog
from crypto.key_hierarchy import SatelliteCert, _canonical_json, _sign, _verify, _load_pub


# ── Settlement triggers (§11.1) ────────────────────────────────────────────────

T_LOW_FRACTION = 0.05       # T_low = 5% of initial capacity
H_MAX = 10_000              # max hash chain entries before settlement
S_MAX_KB = 100_000 * 1024  # 100 GB per session


# ── Data models ────────────────────────────────────────────────────────────────

@dataclass
class BalanceProof:
    """Co-signed bilateral channel state assertion (BalProof)."""
    channel_id: str
    seq_num: int
    balance_a_kb: float
    balance_b_kb: float
    hash_chain_head_a: str    # hex SHA-256
    hash_chain_head_b: str
    sig_a: bytes = b""        # ECDSA DER by satellite A over canonical JSON
    sig_b: bytes = b""

    def signing_payload(self) -> bytes:
        """Deterministic serialisation for signing (excludes signatures)."""
        return _canonical_json({
            "channel_id": self.channel_id,
            "seq_num": self.seq_num,
            "balance_a_kb": self.balance_a_kb,
            "balance_b_kb": self.balance_b_kb,
            "hash_chain_head_a": self.hash_chain_head_a,
            "hash_chain_head_b": self.hash_chain_head_b,
        })

    def is_fully_signed(self) -> bool:
        return bool(self.sig_a) and bool(self.sig_b)

    def to_dict(self) -> dict:
        return {
            "channel_id": self.channel_id,
            "seq_num": self.seq_num,
            "balance_a_kb": self.balance_a_kb,
            "balance_b_kb": self.balance_b_kb,
            "hash_chain_head_a": self.hash_chain_head_a,
            "hash_chain_head_b": self.hash_chain_head_b,
            "sig_a": self.sig_a.hex(),
            "sig_b": self.sig_b.hex(),
        }

    @classmethod
    def from_dict(cls, d: dict) -> "BalanceProof":
        return cls(
            channel_id=d["channel_id"],
            seq_num=d["seq_num"],
            balance_a_kb=d["balance_a_kb"],
            balance_b_kb=d["balance_b_kb"],
            hash_chain_head_a=d["hash_chain_head_a"],
            hash_chain_head_b=d["hash_chain_head_b"],
            sig_a=bytes.fromhex(d.get("sig_a", "")),
            sig_b=bytes.fromhex(d.get("sig_b", "")),
        )


@dataclass
class SatChannelState:
    """Runtime state for one bilateral satellite channel."""
    channel_id: str
    operator_channel_id: str
    satellite_a_id: str
    satellite_b_id: str
    my_role: Literal["A", "B"]
    initial_capacity_kb: float
    balance_a_kb: float
    balance_b_kb: float
    seq_num: int = 0
    hash_chain: HashChainLog = field(init=False)
    latest_proof: Optional[BalanceProof] = None
    settled_proof: Optional[BalanceProof] = None  # last proof sent to GS for settlement
    status: str = "ACTIVE"  # ACTIVE|PAUSED|PENDING_SETTLEMENT|SETTLED
    cumulative_kb_forwarded: float = 0.0

    def __post_init__(self) -> None:
        self.hash_chain = HashChainLog(self.channel_id)

    @property
    def t_low_kb(self) -> float:
        return self.initial_capacity_kb * T_LOW_FRACTION

    def my_balance(self) -> float:
        return self.balance_a_kb if self.my_role == "A" else self.balance_b_kb

    def peer_balance(self) -> float:
        return self.balance_b_kb if self.my_role == "A" else self.balance_a_kb


@dataclass
class SettlementPayload:
    """Package sent from satellite to ground station for settlement submission."""
    channel_id: str
    latest_proof: BalanceProof
    hash_chain_bytes: bytes      # serialised entries since last settlement
    triggers_fired: List[str]    # T1..T7


# ── Off-chain protocol engine ──────────────────────────────────────────────────

class OffChainProtocol:
    """One instance per satellite; manages all its bilateral channel states."""

    def __init__(
        self,
        satellite_id: str,
        private_key: ec.EllipticCurvePrivateKey,
        cert: SatelliteCert,
    ) -> None:
        self.satellite_id = satellite_id
        self._priv_key = private_key
        self.cert = cert
        self._channels: dict[str, SatChannelState] = {}

    # ── channel management ─────────────────────────────────────────────────────

    def open_channel(
        self,
        channel_id: str,
        op_channel_id: str,
        peer_sat_id: str,
        balance_kb: float,
        my_role: Literal["A", "B"],
    ) -> SatChannelState:
        state = SatChannelState(
            channel_id=channel_id,
            operator_channel_id=op_channel_id,
            satellite_a_id=self.satellite_id if my_role == "A" else peer_sat_id,
            satellite_b_id=peer_sat_id if my_role == "A" else self.satellite_id,
            my_role=my_role,
            initial_capacity_kb=balance_kb * 2,  # total pool = 2 × one-side balance
            balance_a_kb=balance_kb,
            balance_b_kb=balance_kb,
        )
        # Genesis BalProof (seq=0, both sigs empty — initial agreed state)
        genesis = BalanceProof(
            channel_id=channel_id,
            seq_num=0,
            balance_a_kb=balance_kb,
            balance_b_kb=balance_kb,
            hash_chain_head_a=state.hash_chain.get_head(),
            hash_chain_head_b=state.hash_chain.get_head(),
        )
        state.latest_proof = genesis
        self._channels[channel_id] = state
        return state

    def get_channel(self, channel_id: str) -> Optional[SatChannelState]:
        return self._channels.get(channel_id)

    def list_channels(self) -> List[SatChannelState]:
        return list(self._channels.values())

    # ── HELLO / SYNC ──────────────────────────────────────────────────────────

    def build_hello(self, channel_id: str) -> dict:
        """Build HELLO message for peer (Phase 1 of contact protocol)."""
        state = self._channels.get(channel_id)
        return {
            "satellite_id": self.satellite_id,
            "cert": self.cert.serialise(),
            "last_seq_num": state.seq_num if state else -1,
            "last_proof": state.latest_proof.to_dict() if (state and state.latest_proof) else None,
        }

    def sync(
        self,
        channel_id: str,
        peer_hello: dict,
        peer_pub_key: ec.EllipticCurvePublicKey,
    ) -> bool:
        """Phase 2: synchronise channel state with peer.

        If peer has higher seq_num and a valid proof, adopt peer state.
        Returns True if sync succeeded.
        """
        state = self._channels.get(channel_id)
        if state is None:
            return False

        peer_seq = peer_hello.get("last_seq_num", -1)
        peer_proof_dict = peer_hello.get("last_proof")
        if peer_proof_dict is None:
            return True

        peer_proof = BalanceProof.from_dict(peer_proof_dict)

        if peer_seq > state.seq_num:
            # Verify peer proof before adopting
            payload = peer_proof.signing_payload()
            sig = peer_proof.sig_a if state.my_role == "B" else peer_proof.sig_b
            if sig and _verify(peer_pub_key, payload, sig):
                state.balance_a_kb = peer_proof.balance_a_kb
                state.balance_b_kb = peer_proof.balance_b_kb
                state.seq_num = peer_proof.seq_num
                state.latest_proof = peer_proof
        return True

    # ── FORWARD / COMMIT ──────────────────────────────────────────────────────

    def record_forwarding(
        self,
        channel_id: str,
        bytes_kb: float,
        peer_pub_key: ec.EllipticCurvePublicKey,
        timestamp: float,
        peer_ack_sig: bytes,
    ) -> Optional[BalanceProof]:
        """Deduct balance, update hash chain, produce signed BalProof proposal.

        This is called when OUR satellite is FORWARDING traffic for the peer
        (we spend our forwarding capacity).
        Returns a BalProof with sig_a XOR sig_b set (caller's side), awaiting cosign.
        """
        state = self._channels.get(channel_id)
        if state is None or state.status != "ACTIVE":
            return None

        my_balance = state.my_balance()
        if my_balance < bytes_kb:
            return None  # insufficient balance

        # Determine direction based on role
        direction = "A_to_B" if state.my_role == "A" else "B_to_A"

        # Update hash chain
        state.hash_chain.append(
            bytes_forwarded=int(bytes_kb * 1024),
            direction=direction,
            timestamp=timestamp,
            peer_sig=peer_ack_sig.hex() if peer_ack_sig else "",
        )

        # Update balances (conservation invariant enforced)
        if state.my_role == "A":
            state.balance_a_kb -= bytes_kb
            state.balance_b_kb += bytes_kb
        else:
            state.balance_b_kb -= bytes_kb
            state.balance_a_kb += bytes_kb

        assert abs(state.balance_a_kb + state.balance_b_kb - state.initial_capacity_kb) < 1e-6, \
            "Balance conservation violation!"

        state.seq_num += 1
        state.cumulative_kb_forwarded += bytes_kb

        proof = BalanceProof(
            channel_id=channel_id,
            seq_num=state.seq_num,
            balance_a_kb=state.balance_a_kb,
            balance_b_kb=state.balance_b_kb,
            hash_chain_head_a=state.hash_chain.get_head(),
            hash_chain_head_b=state.hash_chain.get_head(),
        )

        # Sign with our key
        sig = _sign(self._priv_key, proof.signing_payload())
        if state.my_role == "A":
            proof.sig_a = sig
        else:
            proof.sig_b = sig

        # Adopt as latest proof (provisional until cosigned by peer)
        state.latest_proof = proof
        return proof

    def cosign_proof(
        self,
        channel_id: str,
        proof: BalanceProof,
        peer_pub_key: ec.EllipticCurvePublicKey,
    ) -> Optional[BalanceProof]:
        """Verify peer's half-signed proof, add our signature, update local state."""
        state = self._channels.get(channel_id)
        if state is None:
            return None

        # Verify peer's signature
        payload = proof.signing_payload()
        peer_sig = proof.sig_a if state.my_role == "B" else proof.sig_b
        if peer_sig and not _verify(peer_pub_key, payload, peer_sig):
            return None  # bad signature

        # Check balance conservation
        if abs(proof.balance_a_kb + proof.balance_b_kb - state.initial_capacity_kb) > 1e-6:
            return None

        # Check monotonic seq_num
        if proof.seq_num <= state.seq_num:
            return None

        # Cosign
        my_sig = _sign(self._priv_key, payload)
        if state.my_role == "B":
            proof.sig_b = my_sig
        else:
            proof.sig_a = my_sig

        # Adopt new state
        state.balance_a_kb = proof.balance_a_kb
        state.balance_b_kb = proof.balance_b_kb
        state.seq_num = proof.seq_num
        state.latest_proof = proof
        return proof

    # ── settlement triggers ────────────────────────────────────────────────────

    def evaluate_settlement_triggers(self, channel_id: str) -> List[str]:
        """Return list of triggered condition IDs (T1..T7)."""
        state = self._channels.get(channel_id)
        if state is None:
            return []
        triggers: List[str] = []
        if state.balance_a_kb < state.t_low_kb or state.balance_b_kb < state.t_low_kb:
            triggers.append("T1")
        if state.hash_chain.length() >= H_MAX:
            triggers.append("T2")
        if state.cumulative_kb_forwarded * 1024 >= S_MAX_KB:
            triggers.append("T7")
        return triggers

    def get_settlement_payload(self, channel_id: str, last_settled_seq: int = 0) -> Optional[SettlementPayload]:
        state = self._channels.get(channel_id)
        if state is None or state.latest_proof is None:
            return None
        triggers = self.evaluate_settlement_triggers(channel_id)
        chain_bytes = state.hash_chain.serialise(since_seq=last_settled_seq)
        return SettlementPayload(
            channel_id=channel_id,
            latest_proof=state.latest_proof,
            hash_chain_bytes=chain_bytes,
            triggers_fired=triggers,
        )

    def pause_channel(self, channel_id: str) -> None:
        state = self._channels.get(channel_id)
        if state:
            state.status = "PAUSED"

    def resume_channel(self, channel_id: str) -> None:
        state = self._channels.get(channel_id)
        if state:
            state.status = "ACTIVE"

    def can_forward(self, channel_id: str) -> bool:
        state = self._channels.get(channel_id)
        return state is not None and state.status == "ACTIVE"

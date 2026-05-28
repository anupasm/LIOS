"""SHA-256 hash chain logger for LIOS per-channel forwarding history.

Each forwarding batch appends one entry to the chain:
  H_n = SHA256(H_{n-1} || seq_num || bytes_forwarded || direction || timestamp || peer_sig)

The chain head H_n is embedded in every BalanceProof for auditability.
Tampered historical records invalidate all subsequent entries.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import List, Optional


@dataclass
class HashChainEntry:
    seq_num: int
    prev_hash: str          # hex SHA-256 of previous entry
    channel_id: str
    bytes_forwarded: int
    direction: str          # 'A_to_B' or 'B_to_A'
    timestamp: float        # simulation time in seconds
    peer_sig: str           # hex ECDSA DER sig from forwarding peer
    entry_hash: str = ""    # computed on append; hex SHA-256

    def compute_hash(self) -> str:
        payload = {
            "seq_num": self.seq_num,
            "prev_hash": self.prev_hash,
            "channel_id": self.channel_id,
            "bytes_forwarded": self.bytes_forwarded,
            "direction": self.direction,
            "timestamp": self.timestamp,
            "peer_sig": self.peer_sig,
        }
        raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        return hashlib.sha256(raw).hexdigest()

    def to_dict(self) -> dict:
        return {
            "seq_num": self.seq_num,
            "prev_hash": self.prev_hash,
            "channel_id": self.channel_id,
            "bytes_forwarded": self.bytes_forwarded,
            "direction": self.direction,
            "timestamp": self.timestamp,
            "peer_sig": self.peer_sig,
            "entry_hash": self.entry_hash,
        }


_GENESIS_HASH = "0" * 64  # 256 zero bits as initial chain anchor


class HashChainLog:
    """Append-only hash chain for one bilateral channel."""

    def __init__(self, channel_id: str) -> None:
        self.channel_id = channel_id
        self._entries: List[HashChainEntry] = []
        self._head: str = _GENESIS_HASH

    # ── mutation ───────────────────────────────────────────────────────────────

    def append(
        self,
        bytes_forwarded: int,
        direction: str,
        timestamp: float,
        peer_sig: str,
    ) -> HashChainEntry:
        """Append a new forwarding event to the chain; return the new entry."""
        assert direction in ("A_to_B", "B_to_A"), f"Invalid direction: {direction}"
        seq = len(self._entries)
        entry = HashChainEntry(
            seq_num=seq,
            prev_hash=self._head,
            channel_id=self.channel_id,
            bytes_forwarded=bytes_forwarded,
            direction=direction,
            timestamp=timestamp,
            peer_sig=peer_sig,
        )
        entry.entry_hash = entry.compute_hash()
        self._entries.append(entry)
        self._head = entry.entry_hash
        return entry

    # ── queries ────────────────────────────────────────────────────────────────

    def get_head(self) -> str:
        return self._head

    def length(self) -> int:
        return len(self._entries)

    def get_entries_since(self, seq_num: int) -> List[HashChainEntry]:
        return self._entries[seq_num:]

    def verify_chain(self) -> bool:
        """Validate full chain integrity from genesis."""
        prev = _GENESIS_HASH
        for entry in self._entries:
            if entry.prev_hash != prev:
                return False
            expected = entry.compute_hash()
            if entry.entry_hash != expected:
                return False
            prev = entry.entry_hash
        return True

    def serialise(self, since_seq: int = 0) -> bytes:
        """Serialise entries from since_seq onwards (for GS offload)."""
        entries = [e.to_dict() for e in self._entries[since_seq:]]
        return json.dumps({"channel_id": self.channel_id, "entries": entries}).encode()

    @property
    def entries(self) -> List[HashChainEntry]:
        return list(self._entries)

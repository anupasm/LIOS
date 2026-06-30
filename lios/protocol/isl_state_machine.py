"""ISL channel pause/resume finite state machine (§12).

States:
  ACTIVE                    — forwarding permitted
  PAUSED_PENDING_SETTLEMENT — contact ended, settlement in flight
  PAUSED_PENDING_RESUME     — settlement finalised, waiting for both sats to ACK
  CLOSED                    — channel exhausted and not renewed

Transition guards enforce the key protocol invariant: a channel may only
resume after BOTH satellites have individually acknowledged the resume signal
from their respective ground stations.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Dict, List, Optional


class ISLChannelStatus(str, Enum):
    ACTIVE = "ACTIVE"
    PAUSED_PENDING_SETTLEMENT = "PAUSED_PENDING_SETTLEMENT"
    PAUSED_PENDING_RESUME = "PAUSED_PENDING_RESUME"
    CLOSED = "CLOSED"


@dataclass
class OutOfServiceEntry:
    channel_id: str
    paused_at: float               # contact-end timestamp (satellite-authoritative)
    resumed_at: Optional[float] = None
    sat_a_ack_at: Optional[float] = None
    sat_b_ack_at: Optional[float] = None
    reason: str = "SETTLEMENT"     # SETTLEMENT | BALANCE_DEPLETED | OPERATOR_REQUEST
    initiated_by: str = ""         # operator MSP ID

    @property
    def duration_sec(self) -> Optional[float]:
        if self.resumed_at is not None:
            return self.resumed_at - self.paused_at
        return None

    def both_acked(self) -> bool:
        return self.sat_a_ack_at is not None and self.sat_b_ack_at is not None

    def to_dict(self) -> dict:
        return {
            "channel_id": self.channel_id,
            "paused_at": self.paused_at,
            "resumed_at": self.resumed_at,
            "sat_a_ack_at": self.sat_a_ack_at,
            "sat_b_ack_at": self.sat_b_ack_at,
            "duration_sec": self.duration_sec,
            "reason": self.reason,
            "initiated_by": self.initiated_by,
        }


@dataclass
class ISLChannelRecord:
    channel_id: str
    satellite_a_id: str
    satellite_b_id: str
    status: ISLChannelStatus = ISLChannelStatus.ACTIVE
    oos_log: List[OutOfServiceEntry] = field(default_factory=list)

    def latest_oos(self) -> Optional[OutOfServiceEntry]:
        return self.oos_log[-1] if self.oos_log else None


class ISLStateMachine:
    """Tracks FSM state for all known bilateral ISL channels."""

    def __init__(self) -> None:
        self._channels: Dict[str, ISLChannelRecord] = {}
        self._proof_exchange_busy_until: Dict[str, float] = {}

    def register_channel(self, channel_id: str, sat_a: str, sat_b: str) -> None:
        self._channels[channel_id] = ISLChannelRecord(
            channel_id=channel_id,
            satellite_a_id=sat_a,
            satellite_b_id=sat_b,
        )

    def get_status(self, channel_id: str) -> Optional[ISLChannelStatus]:
        rec = self._channels.get(channel_id)
        return rec.status if rec else None

    def can_forward(self, channel_id: str) -> bool:
        return self.get_status(channel_id) == ISLChannelStatus.ACTIVE

    def proof_exchange_busy_until(self, channel_id: str) -> float:
        """Return the end of the channel's current stop-and-wait exchange."""
        return self._proof_exchange_busy_until.get(channel_id, 0.0)

    def reserve_proof_exchange(self, channel_id: str, until: float) -> None:
        """Serialize balance-proof updates shared by both channel endpoints."""
        self._proof_exchange_busy_until[channel_id] = until

    # ── ACTIVE → PAUSED_PENDING_SETTLEMENT ─────────────────────────────────────

    def record_contact_end_pause(
        self,
        channel_id: str,
        contact_end_time: float,
        initiated_by: str,
        reason: str = "SETTLEMENT",
    ) -> None:
        """Called at ISL contact end when a settlement trigger has fired.

        The pause timestamp is satellite-authoritative (contact-end time).
        Guard: channel must be ACTIVE (pause only at contact boundary).
        """
        rec = self._channels.get(channel_id)
        if rec is None or rec.status != ISLChannelStatus.ACTIVE:
            return
        entry = OutOfServiceEntry(
            channel_id=channel_id,
            paused_at=contact_end_time,
            reason=reason,
            initiated_by=initiated_by,
        )
        rec.oos_log.append(entry)
        rec.status = ISLChannelStatus.PAUSED_PENDING_SETTLEMENT

    # ── PAUSED_PENDING_SETTLEMENT → PAUSED_PENDING_RESUME ──────────────────────

    def on_settlement_finalized(self, channel_id: str) -> None:
        """Called when Fabric emits SETTLEMENT_FINALIZED for this channel."""
        rec = self._channels.get(channel_id)
        if rec is None:
            return
        if rec.status == ISLChannelStatus.PAUSED_PENDING_SETTLEMENT:
            rec.status = ISLChannelStatus.PAUSED_PENDING_RESUME

    # ── PAUSED_PENDING_RESUME → ACTIVE (once both sats ACK) ────────────────────

    def record_satellite_resume_ack(
        self,
        channel_id: str,
        satellite_id: str,
        ack_time: float,
    ) -> bool:
        """Record that a satellite has acknowledged the resume notification.

        Returns True if BOTH satellites have now ACKed (ISL is back ACTIVE).
        """
        rec = self._channels.get(channel_id)
        if rec is None or rec.status != ISLChannelStatus.PAUSED_PENDING_RESUME:
            return False
        oos = rec.latest_oos()
        if oos is None:
            return False

        if satellite_id == rec.satellite_a_id:
            oos.sat_a_ack_at = ack_time
        elif satellite_id == rec.satellite_b_id:
            oos.sat_b_ack_at = ack_time

        if oos.both_acked():
            oos.resumed_at = max(oos.sat_a_ack_at, oos.sat_b_ack_at)
            rec.status = ISLChannelStatus.ACTIVE
            return True
        return False

    # ── ACTIVE → CLOSED ────────────────────────────────────────────────────────

    def close_channel(self, channel_id: str) -> None:
        rec = self._channels.get(channel_id)
        if rec:
            rec.status = ISLChannelStatus.CLOSED

    # ── Analytics ──────────────────────────────────────────────────────────────

    def get_oos_duration(self, channel_id: str) -> float:
        """Total confirmed out-of-service seconds for this channel."""
        rec = self._channels.get(channel_id)
        if rec is None:
            return 0.0
        return sum(e.duration_sec or 0.0 for e in rec.oos_log)

    def get_oos_log(self, channel_id: str) -> List[OutOfServiceEntry]:
        rec = self._channels.get(channel_id)
        return list(rec.oos_log) if rec else []

    def all_channels_summary(self) -> List[dict]:
        rows = []
        for rec in self._channels.values():
            rows.append({
                "channel_id": rec.channel_id,
                "status": rec.status.value,
                "oos_entries": len(rec.oos_log),
                "total_oos_sec": self.get_oos_duration(rec.channel_id),
            })
        return rows

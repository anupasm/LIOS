"""Discrete-Event Simulation (DES) engine for the LIOS protocol emulator.

Provides:
  - Priority-queue event loop.
  - Observer-pattern dispatch to registered node handlers.
  - Reproducible runs via configurable random seed.
  - Statistics collection for post-analysis.
"""
from __future__ import annotations

import heapq
import random
import time
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any, Callable, Dict, List, Optional
from uuid import uuid4


class EventType(str, Enum):
    ISL_OPEN = "ISL_OPEN"
    ISL_CLOSE = "ISL_CLOSE"
    GS_CONTACT_START = "GS_CONTACT_START"
    GS_CONTACT_END = "GS_CONTACT_END"
    TRAFFIC_ARRIVE = "TRAFFIC_ARRIVE"
    TRAFFIC_RETRY = "TRAFFIC_RETRY"
    SETTLEMENT_TRIGGER = "SETTLEMENT_TRIGGER"
    SETTLEMENT_UPLOAD = "SETTLEMENT_UPLOAD"
    PROOF_PROP = "PROOF_PROP"   # half-signed proof sent from forwarder to peer for cosigning
    PROOF_ACK  = "PROOF_ACK"   # co-signed proof returned by peer
    NOTIFICATION_DELIVER = "NOTIFICATION_DELIVER"
    TOPUP_REQUEST = "TOPUP_REQUEST"
    TOPUP_CONFIRM = "TOPUP_CONFIRM"
    CHALLENGE_WINDOW_EXPIRE = "CHALLENGE_WINDOW_EXPIRE"
    KEY_REVOKED = "KEY_REVOKED"
    SIMULATION_END = "SIMULATION_END"


@dataclass
class SimEvent:
    time: float
    event_type: EventType
    from_node: str
    to_node: str
    payload: Any = None
    event_id: str = field(default_factory=lambda: str(uuid4()))

    def __lt__(self, other: "SimEvent") -> bool:
        return self.time < other.time

    def __le__(self, other: "SimEvent") -> bool:
        return self.time <= other.time


@dataclass
class SimStats:
    events_processed: int = 0
    isl_opens: int = 0
    isl_closes: int = 0
    gs_contacts: int = 0
    traffic_flows: int = 0
    settlement_triggers: int = 0
    wall_clock_start: float = field(default_factory=time.time)

    def elapsed_wall(self) -> float:
        return time.time() - self.wall_clock_start

    def to_dict(self) -> Dict:
        return {
            "events_processed": self.events_processed,
            "isl_opens": self.isl_opens,
            "isl_closes": self.isl_closes,
            "gs_contacts": self.gs_contacts,
            "traffic_flows": self.traffic_flows,
            "settlement_triggers": self.settlement_triggers,
            "wall_clock_elapsed_sec": self.elapsed_wall(),
        }


HandlerFn = Callable[[SimEvent], List[SimEvent]]


class SimulationClock:
    def __init__(self) -> None:
        self.current_time: float = 0.0

    def advance(self, dt: float) -> None:
        self.current_time += dt

    def set(self, t: float) -> None:
        self.current_time = t


class EventLoop:
    """Core discrete-event simulation engine."""

    def __init__(self, seed: int = 42) -> None:
        self.clock = SimulationClock()
        self.stats = SimStats()
        self._queue: List[SimEvent] = []
        self._handlers: Dict[str, HandlerFn] = {}
        self._event_log: List[SimEvent] = []
        random.seed(seed)

    # ── node registry ──────────────────────────────────────────────────────────

    def register_node(self, node_id: str, handler: HandlerFn) -> None:
        """Register a handler for events addressed to node_id."""
        self._handlers[node_id] = handler

    def unregister_node(self, node_id: str) -> None:
        self._handlers.pop(node_id, None)

    # ── event scheduling ───────────────────────────────────────────────────────

    def schedule(self, event: SimEvent) -> None:
        """Enqueue an event."""
        heapq.heappush(self._queue, event)

    def schedule_all(self, events: List[SimEvent]) -> None:
        for e in events:
            self.schedule(e)

    # ── main loop ──────────────────────────────────────────────────────────────

    def run(self, until: float) -> SimStats:
        """Process events in chronological order until simulation time `until`."""
        _report_interval = max(1.0, until / 10)
        _next_report = _report_interval
        _wall_start = time.time()

        while self._queue:
            event = heapq.heappop(self._queue)
            if event.time > until:
                break

            self.clock.set(event.time)
            self._event_log.append(event)
            self._update_stats(event)

            if event.time >= _next_report:
                pct = int(event.time * 100 / until)
                wall_el = time.time() - _wall_start
                rate = self.stats.events_processed / max(0.1, wall_el)
                print(
                    f"  Sim: {pct:3d}%  t={event.time:,.0f}/{until:,.0f}s  "
                    f"events={self.stats.events_processed:,}  {rate:,.0f} ev/s  "
                    f"wall={wall_el:.1f}s   ",
                    end="\r", flush=True,
                )
                _next_report += _report_interval

            # Dispatch to the destination node's handler
            handler = self._handlers.get(event.to_node)
            if handler is not None:
                try:
                    new_events = handler(event) or []
                    self.schedule_all(new_events)
                except Exception as exc:  # noqa: BLE001
                    print(f"[SIM ERROR] t={event.time:.1f} handler={event.to_node} "
                          f"event={event.event_type}: {exc}")

        wall_total = time.time() - _wall_start
        print(
            f"  Sim: done   t={until:,.0f}s  "
            f"events={self.stats.events_processed:,}  "
            f"wall={wall_total:.1f}s                       "
        )
        return self.stats

    def _update_stats(self, e: SimEvent) -> None:
        s = self.stats
        s.events_processed += 1
        if e.event_type == EventType.ISL_OPEN:
            s.isl_opens += 1
        elif e.event_type == EventType.ISL_CLOSE:
            s.isl_closes += 1
        elif e.event_type in (EventType.GS_CONTACT_START, EventType.GS_CONTACT_END):
            s.gs_contacts += 1
        elif e.event_type == EventType.TRAFFIC_ARRIVE:
            s.traffic_flows += 1
        elif e.event_type == EventType.SETTLEMENT_TRIGGER:
            s.settlement_triggers += 1

    # ── utilities ──────────────────────────────────────────────────────────────

    @property
    def event_log(self) -> List[SimEvent]:
        return list(self._event_log)

    def get_log_for_node(self, node_id: str) -> List[SimEvent]:
        return [e for e in self._event_log if e.to_node == node_id or e.from_node == node_id]

    def seed_contacts_from_plan(self, contact_plan: Any) -> None:
        """Pre-populate the event queue from a ContactPlan object."""
        from contact_plan.window_calculator import ContactPlan  # avoid circular
        for c in contact_plan.contacts:
            if c.node_type_from == "SAT" and c.node_type_to == "SAT":
                # Send ISL_OPEN/ISL_CLOSE to BOTH ends so each satellite registers
                # its peer in _active_isls and can forward traffic in both directions.
                for src, dst in [(c.from_node, c.to_node), (c.to_node, c.from_node)]:
                    self.schedule(SimEvent(
                        time=c.start_time_sec,
                        event_type=EventType.ISL_OPEN,
                        from_node=src,
                        to_node=dst,
                        payload=c,
                    ))
                    self.schedule(SimEvent(
                        time=c.end_time_sec,
                        event_type=EventType.ISL_CLOSE,
                        from_node=src,
                        to_node=dst,
                        payload=c,
                    ))
            elif c.node_type_from == "GS" or c.node_type_to == "GS":
                gs = c.from_node if c.node_type_from == "GS" else c.to_node
                sat = c.to_node if c.node_type_from == "GS" else c.from_node
                # To satellite: satellite handles upload and active-GS tracking.
                self.schedule(SimEvent(
                    time=c.start_time_sec,
                    event_type=EventType.GS_CONTACT_START,
                    from_node=gs,
                    to_node=sat,
                    payload=c,
                ))
                self.schedule(SimEvent(
                    time=c.end_time_sec,
                    event_type=EventType.GS_CONTACT_END,
                    from_node=gs,
                    to_node=sat,
                    payload=c,
                ))
                # To GS (from_node=sat so the GS knows which satellite it's in contact with).
                # GS uses these to deliver queued notifications and poll Fabric.
                self.schedule(SimEvent(
                    time=c.start_time_sec,
                    event_type=EventType.GS_CONTACT_START,
                    from_node=sat,
                    to_node=gs,
                    payload=c,
                ))
                self.schedule(SimEvent(
                    time=c.end_time_sec,
                    event_type=EventType.GS_CONTACT_END,
                    from_node=sat,
                    to_node=gs,
                    payload=c,
                ))

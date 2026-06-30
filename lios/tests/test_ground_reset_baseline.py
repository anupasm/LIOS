"""Tests for contact-end-only accounting in the ground-reset baseline."""

from contact_plan.window_calculator import Contact
from crypto.key_hierarchy import OperatorCA
from evaluation.baselines import GroundResetFabricMock, GroundResetSatelliteNode
from protocol.isl_state_machine import ISLStateMachine
from simulator.satellite_node import create_satellite
from simulator.simulator import EventType, SimEvent
from simulator.traffic_generator import TrafficFlow


SAT_A = "opA-s1"
SAT_B = "opB-s1"
CHANNEL_ID = f"{SAT_A}__{SAT_B}"


def _event(event_type, time, from_node, to_node, payload):
    return SimEvent(time, event_type, from_node, to_node, payload)


def test_ground_reset_updates_once_at_contact_end_and_nets_reports_on_ground():
    cas = {operator: OperatorCA(operator) for operator in ("opA", "opB")}
    fsm = ISLStateMachine()
    sat_a = create_satellite(
        SAT_A, "opA", cas["opA"], cas, fsm, list(cas),
        node_class=GroundResetSatelliteNode,
    )
    sat_b = create_satellite(
        SAT_B, "opB", cas["opB"], cas, fsm, list(cas),
        node_class=GroundResetSatelliteNode,
    )
    contact = Contact(
        "ISL-AB", SAT_A, SAT_B, 0.0, 10.0, 10_000.0, 1_000.0,
        "SAT", "SAT", "opA", "opB",
    )

    sat_a.handle_event(_event(EventType.ISL_OPEN, 0.0, SAT_B, SAT_A, contact))
    sat_b.handle_event(_event(EventType.ISL_OPEN, 0.0, SAT_A, SAT_B, contact))
    state_a = sat_a.get_channel_state(SAT_B)
    state_b = sat_b.get_channel_state(SAT_A)
    initial = state_a.balance_a_kb

    sat_a.handle_event(_event(
        EventType.TRAFFIC_ARRIVE, 2.0, SAT_A, SAT_A,
        TrafficFlow("flow-a", SAT_A, SAT_B, "ISL-AB", CHANNEL_ID, 100.0, 2.0),
    ))
    sat_b.handle_event(_event(
        EventType.TRAFFIC_ARRIVE, 3.0, SAT_B, SAT_B,
        TrafficFlow("flow-b", SAT_B, SAT_A, "ISL-AB", CHANNEL_ID, 40.0, 3.0),
    ))

    # Forwarding does not mutate channel balances or produce off-chain updates.
    assert state_a.balance_a_kb == initial
    assert state_b.balance_b_kb == initial
    assert not any(e["event"] == "OFFCHAIN_PROOF_UPDATE" for e in sat_a.event_log)
    assert not any(e["event"] == "OFFCHAIN_PROOF_UPDATE" for e in sat_b.event_log)

    events_a = sat_a.handle_event(
        _event(EventType.ISL_CLOSE, 10.0, SAT_B, SAT_A, contact)
    )
    events_b = sat_b.handle_event(
        _event(EventType.ISL_CLOSE, 10.0, SAT_A, SAT_B, contact)
    )

    # Each endpoint creates one ground report; there is no peer proof exchange.
    assert events_a == []
    assert events_b == []
    assert state_a.balance_a_kb == initial - 100.0
    assert state_b.balance_b_kb == initial - 40.0
    assert sum(
        e["event"] == "GROUND_RESET_CONTACT_BALANCE_UPDATE"
        for e in sat_a.event_log + sat_b.event_log
    ) == 2
    assert all(
        e["isl_prop_delay_sec"] == 0.0
        for e in sat_a.event_log + sat_b.event_log
        if e["event"] == "SETTLEMENT_QUEUED"
    )
    assert all(
        e["offchain_latency_sec"] == 0.0
        for e in sat_a.event_log + sat_b.event_log
        if e["event"] == "SETTLEMENT_QUEUED"
    )

    proof_a = sat_a.get_pending_settlement_payloads()[0].latest_proof
    proof_b = sat_b.get_pending_settlement_payloads()[0].latest_proof
    fabric = GroundResetFabricMock()
    status, seen = fabric.submit_ground_reset_upload(
        CHANNEL_ID, SAT_A, proof_a, "opA", 20.0
    )
    assert status == "WAITING_FOR_PEER"
    assert seen == {SAT_A}

    status, seen = fabric.submit_ground_reset_upload(
        CHANNEL_ID, SAT_B, proof_b, "opB", 21.0
    )
    assert status == "RESET_COMMITTED"
    assert seen == {SAT_A, SAT_B}
    final = fabric._settlements[CHANNEL_ID]["proof"]
    assert final["balance_a_kb"] == initial - 60.0
    assert final["balance_b_kb"] == initial + 60.0

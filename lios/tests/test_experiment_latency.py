"""Regression tests for settlement-cycle latency aggregation."""

from contact_plan.gs_loader import GroundStation
from contact_plan.window_calculator import Contact
from config import cfg
from crypto.key_hierarchy import OperatorCA
from evaluation.run_experiments import (
    _OFFCHAIN_SAT_EVENTS,
    _compute_offchain_proof_exchanges,
    _compute_latency_stats,
    _compute_latency_summary,
)
from protocol.isl_state_machine import ISLStateMachine
from protocol.offchain import BalanceProof, SettlementPayload
from simulator.ground_station_node import FabricMock, GroundStationNode
from simulator.satellite_node import create_satellite
from simulator.simulator import EventType, SimEvent
from simulator.traffic_generator import TrafficFlow


def test_repeated_settlements_on_one_channel_are_counted_separately():
    channel_id = "opA-s1__opB-s1"
    offchain = [
        {"event": "OFFCHAIN_PROOF_UPDATE", "channel_id": channel_id,
         "seq_num": 1, "t": 1.0},
        {"event": "PROOF_PROP_SENT", "channel_id": channel_id,
         "seq_num": 1, "t": 9.994},
        {"event": "PROOF_PROP_COSIGNED", "channel_id": channel_id,
         "seq_num": 1, "t": 9.997},
        {"event": "PROOF_ACK_SENT", "channel_id": channel_id,
         "seq_num": 1, "t": 9.997},
        {"event": "PROOF_ACK_RECEIVED", "channel_id": channel_id,
         "seq_num": 1, "t": 10.0},
        {"event": "SETTLEMENT_QUEUED", "channel_id": channel_id,
         "seq_num": 1, "t": 10.0, "isl_prop_delay_sec": 0.003,
         "offchain_latency_sec": 999.0,
         "isl_range_km": 900.0},
        {"event": "ISL_RESUMED", "channel_id": channel_id,
         "seq_num": 1, "satellite": "opA-s1", "t": 15.0,
         "gs_sent_at": 14.0, "downlink_prop_delay_sec": 0.001},
        {"event": "OFFCHAIN_PROOF_UPDATE", "channel_id": channel_id,
         "seq_num": 2, "t": 20.0},
        {"event": "PROOF_PROP_SENT", "channel_id": channel_id,
         "seq_num": 2, "t": 29.992},
        {"event": "PROOF_PROP_COSIGNED", "channel_id": channel_id,
         "seq_num": 2, "t": 29.996},
        {"event": "PROOF_ACK_SENT", "channel_id": channel_id,
         "seq_num": 2, "t": 29.996},
        {"event": "PROOF_ACK_RECEIVED", "channel_id": channel_id,
         "seq_num": 2, "t": 30.0},
        {"event": "SETTLEMENT_QUEUED", "channel_id": channel_id,
         "seq_num": 2, "t": 30.0, "isl_prop_delay_sec": 0.004,
         "offchain_latency_sec": 999.0,
         "isl_range_km": 1_200.0},
    ]
    ground = [
        {"event": "SETTLEMENT_RECEIVED", "channel_id": channel_id,
         "seq_num": 1, "t": 12.0, "triggers": ["T7"],
         "uplink_prop_delay_sec": 0.001, "wait_for_gs_sec": 2.0},
        # The peer upload belongs to the same cycle and must not become a third sample.
        {"event": "SETTLEMENT_RECEIVED", "channel_id": channel_id,
         "seq_num": 1, "t": 12.1, "triggers": [],
         "uplink_prop_delay_sec": 0.002, "wait_for_gs_sec": 2.1},
        {"event": "SETTLEMENT_FINALIZED", "channel_id": channel_id,
         "seq_num": 1, "t": 14.0},
        {"event": "SETTLEMENT_RECEIVED", "channel_id": channel_id,
         "seq_num": 2, "t": 35.0, "triggers": ["T7"],
         "uplink_prop_delay_sec": 0.002, "wait_for_gs_sec": 5.0},
        {"event": "SETTLEMENT_FINALIZED", "channel_id": channel_id,
         "seq_num": 2, "t": 37.0},
    ]

    summary = _compute_latency_summary(offchain, ground)
    exchanges = _compute_offchain_proof_exchanges(offchain)
    stats = _compute_latency_stats(summary, exchanges)

    assert [entry["seq_num"] for entry in summary] == [1, 2]
    assert [entry["settlement_id"] for entry in summary] == [
        f"{channel_id}:1", f"{channel_id}:2",
    ]
    assert [entry["offchain"]["contact_duration_sec"] for entry in summary] == [9.0, 10.0]
    assert stats["offchain"]["count"] == 2
    assert stats["offchain"]["mean"] == 0.007
    assert stats["onchain"]["count"] == 2
    assert stats["protocol"]["count"] == 2
    assert [exchange["latency_sec"] for exchange in exchanges] == [0.006, 0.008]
    assert {
        "PROOF_PROP_SENT", "PROOF_PROP_COSIGNED", "PROOF_ACK_SENT",
        "PROOF_ACK_RECEIVED",
    }.issubset(_OFFCHAIN_SAT_EVENTS)


def test_incomplete_cycle_is_not_counted_as_onchain_or_protocol_latency():
    channel_id = "opA-s1__opB-s1"
    offchain = [{
        "event": "SETTLEMENT_QUEUED", "channel_id": channel_id,
        "seq_num": 3, "t": 40.0, "isl_prop_delay_sec": 0.003,
        "offchain_latency_sec": 0.006,
        "isl_range_km": 900.0,
    }]

    stats = _compute_latency_stats(_compute_latency_summary(offchain, []))

    assert stats["offchain"]["count"] == 1
    assert stats["onchain"]["count"] == 0
    assert stats["protocol"]["count"] == 0


def test_newer_proof_starts_another_fabric_settlement_cycle():
    channel_id = "opA-s1__opB-s1"
    fabric = FabricMock()
    first = BalanceProof(channel_id, 4, 9_000.0, 11_000.0)
    second = BalanceProof(channel_id, 8, 8_000.0, 12_000.0)

    assert fabric.initiate_settlement(channel_id, first, "opA", 1.0)["status"] == "NEW_PENDING"
    assert fabric.initiate_settlement(channel_id, first, "opB", 2.0)["status"] == "MUTUAL_FINALIZED"
    assert fabric.initiate_settlement(channel_id, second, "opA", 10.0)["status"] == "NEW_PENDING"
    assert fabric._settlements[channel_id]["proof"]["seq_num"] == 8


def test_ground_station_does_not_suppress_later_cycle_on_same_channel():
    channel_id = "opA-s1__opB-s1"
    gs = GroundStationNode(
        GroundStation("opA-gs1", "opA", 0.0, 0.0),
        FabricMock(),
        ISLStateMachine(),
    )

    for seq_num in (4, 8):
        proof = BalanceProof(channel_id, seq_num, 9_000.0, 11_000.0)
        payload = SettlementPayload(
            channel_id, proof, b"", ["T1"], reset_requested=True,
        )
        gs.receive_settlement_payload("opA-s1", payload, float(seq_num))

    finalized_sequences = [
        event["seq_num"] for event in gs.event_log
        if event["event"] == "SETTLEMENT_FINALIZED"
    ]
    assert finalized_sequences == [4, 8]


def test_offchain_latency_measures_each_balance_update_proposal_and_ack():
    sat_a_id, sat_b_id = "opA-s1", "opB-s1"
    cas = {operator: OperatorCA(operator) for operator in ("opA", "opB")}
    fsm = ISLStateMachine()
    sat_a = create_satellite(sat_a_id, "opA", cas["opA"], cas, fsm, list(cas))
    sat_b = create_satellite(sat_b_id, "opB", cas["opB"], cas, fsm, list(cas))
    contact = Contact(
        "ISL-AB", sat_a_id, sat_b_id, 0.0, 10.0, 10_000.0, 1_200.0,
        "SAT", "SAT", "opA", "opB",
    )

    sat_a.handle_event(SimEvent(0.0, EventType.ISL_OPEN, sat_b_id, sat_a_id, contact))
    sat_b.handle_event(SimEvent(0.0, EventType.ISL_OPEN, sat_a_id, sat_b_id, contact))
    proposal = sat_a.handle_event(SimEvent(
        1.0, EventType.TRAFFIC_ARRIVE, sat_a_id, sat_a_id,
        TrafficFlow("flow-1", sat_a_id, sat_b_id, "ISL-AB", "pair", 1_024.0, 1.0),
    ))[0]
    acknowledgement = sat_b.handle_event(proposal)[0]
    sat_a.handle_event(acknowledgement)

    exchange_log = [
        e for e in sat_a.event_log + sat_b.event_log
        if e["event"].startswith("PROOF_")
    ]
    one_way = contact.range_km / cfg.link.c_km_s
    assert {e["event"] for e in exchange_log} == {
        "PROOF_PROP_SENT", "PROOF_PROP_COSIGNED",
        "PROOF_ACK_SENT", "PROOF_ACK_RECEIVED",
    }
    sent_at = next(e["t"] for e in exchange_log if e["event"] == "PROOF_PROP_SENT")
    received_at = next(
        e["t"] for e in exchange_log if e["event"] == "PROOF_ACK_RECEIVED"
    )
    assert round(received_at - sent_at, 6) == round(2.0 * one_way, 6)
    exchanges = _compute_offchain_proof_exchanges(exchange_log)
    assert _compute_latency_stats([], exchanges)["offchain"]["count"] == 1

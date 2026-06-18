"""Tests for direct active-pair traffic allocation."""
from __future__ import annotations

from contact_plan.window_calculator import Contact, ContactPlan
from simulator.traffic_generator import TrafficGenerator


def _contact(
    contact_id: str,
    sat_a: str,
    sat_b: str,
    start: float,
    end: float,
    op_a: str,
    op_b: str,
) -> Contact:
    return Contact(
        contact_id=contact_id,
        from_node=sat_a,
        to_node=sat_b,
        start_time_sec=start,
        end_time_sec=end,
        capacity_kbps=10_000.0,
        range_km=500.0,
        node_type_from="SAT",
        node_type_to="SAT",
        operator_from=op_a,
        operator_to=op_b,
    )


def _plan() -> tuple[ContactPlan, dict[str, str]]:
    contacts = [
        _contact("ab", "opA-s1", "opB-s1", 0.0, 100.0, "opA", "opB"),
        _contact("ac", "opA-s2", "opC-s1", 20.0, 80.0, "opA", "opC"),
        _contact("aa", "opA-s1", "opA-s2", 0.0, 100.0, "opA", "opA"),
    ]
    operators = {
        "opA-s1": "opA",
        "opA-s2": "opA",
        "opB-s1": "opB",
        "opC-s1": "opC",
    }
    return ContactPlan(contacts=contacts), operators


def test_allocates_only_active_cross_operator_pairs() -> None:
    cp, operators = _plan()
    generator = TrafficGenerator(cp, operators, arrival_rate=1.0, seed=42)

    before_overlap = [generator.generate_flow(10.0) for _ in range(30)]
    assert {flow.contact_id for flow in before_overlap if flow} == {"ab"}

    during_overlap = [generator.generate_flow(50.0) for _ in range(100)]
    assert {flow.contact_id for flow in during_overlap if flow} == {"ab", "ac"}

    assert generator.generate_flow(100.0) is None
    assert generator.stats.flows_no_active_pair == 1


def test_seed_reproduces_pair_and_direction_sequence() -> None:
    cp, operators = _plan()
    first = TrafficGenerator(cp, operators, arrival_rate=1.0, seed=7)
    second = TrafficGenerator(cp, operators, arrival_rate=1.0, seed=7)

    first_seq = [
        (flow.flow_id, flow.src_satellite, flow.dst_satellite, flow.contact_id)
        for flow in (first.generate_flow(50.0) for _ in range(50))
        if flow
    ]
    second_seq = [
        (flow.flow_id, flow.src_satellite, flow.dst_satellite, flow.contact_id)
        for flow in (second.generate_flow(50.0) for _ in range(50))
        if flow
    ]
    assert first_seq == second_seq


def test_direction_bias_controls_canonical_direction() -> None:
    cp, operators = _plan()
    forward = TrafficGenerator(
        cp, operators, arrival_rate=1.0, seed=1, direction_bias=1.0
    )
    reverse = TrafficGenerator(
        cp, operators, arrival_rate=1.0, seed=1, direction_bias=0.0
    )

    flow_forward = forward.generate_flow(10.0)
    flow_reverse = reverse.generate_flow(10.0)
    assert flow_forward is not None and flow_reverse is not None
    assert (flow_forward.src_satellite, flow_forward.dst_satellite) == (
        "opA-s1",
        "opB-s1",
    )
    assert (flow_reverse.src_satellite, flow_reverse.dst_satellite) == (
        "opB-s1",
        "opA-s1",
    )


def test_arrival_rate_is_global_not_node_scaled() -> None:
    cp, operators = _plan()
    first = TrafficGenerator(cp, operators, arrival_rate=2.0, seed=11)
    second = TrafficGenerator(cp, operators, arrival_rate=2.0, seed=11)
    assert first.next_arrival_time(0.0) == second.next_arrival_time(0.0)

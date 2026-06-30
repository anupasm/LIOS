"""Tests for contact-window selection in the channel lifecycle plot."""

from pathlib import Path

from scripts.visualize_channel_lifecycle import (
    build_html_figure,
    default_output_path,
    select_contacts,
)


def _contact(from_node, to_node, from_type, to_type, start):
    return {
        "contact_id": f"C-{start}",
        "from_node": from_node,
        "to_node": to_node,
        "start_time_sec": str(start),
        "end_time_sec": str(start + 60),
        "node_type_from": from_type,
        "node_type_to": to_type,
    }


def test_selects_direct_isl_and_ground_contacts_for_both_satellites():
    contacts = [
        _contact("opA-s1", "opB-s1", "SAT", "SAT", 100),
        _contact("gs-a", "opA-s1", "GS", "SAT", 200),
        _contact("opB-s1", "gs-b", "SAT", "GS", 300),
        _contact("opA-s1", "opC-s1", "SAT", "SAT", 400),
        _contact("gs-c", "opC-s1", "GS", "SAT", 500),
    ]

    isl, ground = select_contacts(contacts, "opA-s1", "opB-s1")

    assert [contact["contact_id"] for contact in isl] == ["C-100"]
    assert [contact["contact_id"] for contact in ground["opA-s1"]] == ["C-200"]
    assert [contact["contact_id"] for contact in ground["opB-s1"]] == ["C-300"]


def test_default_output_is_interactive_html():
    output = default_output_path(Path("results/logs/exp_settlement_log.json"), "a__b")
    assert output.suffix == ".html"

    figure = build_html_figure(
        "a__b", "a", "b", [], [], [], [], {"a": [], "b": []}, 0.05
    )
    html = figure.to_html(include_plotlyjs=False, full_html=True)
    assert "LIOS Channel Lifecycle" in html
    assert "plotly-graph-div" in html

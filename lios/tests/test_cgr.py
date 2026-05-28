"""Unit tests for Contact Graph Routing (CGR)."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest
from contact_plan.window_calculator import Contact, ContactPlan
from routing.cgr import CGR


def _make_plan() -> ContactPlan:
    """3-operator, 6-satellite toy contact plan:
      opA-s1 → opB-s2  [0, 300] 5000 kbps
      opB-s2 → opC-s3  [60, 360] 5000 kbps
      opC-s3 → opA-s4  [120, 420] 5000 kbps
      opA-s1 → opA-s4  [0, 500] 3000 kbps  (intra-operator shortcut)
    """
    contacts = [
        Contact("C001", "opA-s1", "opB-s2", 0,   300, 5000, 1200, "SAT", "SAT", "opA", "opB"),
        Contact("C002", "opB-s2", "opC-s3", 60,  360, 5000, 1300, "SAT", "SAT", "opB", "opC"),
        Contact("C003", "opC-s3", "opA-s4", 120, 420, 5000, 1100, "SAT", "SAT", "opC", "opA"),
        Contact("C004", "opA-s1", "opA-s4", 0,   500, 3000, 1000, "SAT", "SAT", "opA", "opA"),
    ]
    return ContactPlan(contacts=contacts)


def _op_map():
    return {
        "opA-s1": "opA", "opA-s4": "opA",
        "opB-s2": "opB", "opC-s3": "opC",
    }


class TestCGR:
    def setup_method(self):
        self.cp = _make_plan()
        self.router = CGR(self.cp, operator_map=_op_map())

    def test_direct_path_found(self):
        paths = self.router.route("opA-s1", "opB-s2", t_start=0.0, k=1)
        assert len(paths) == 1
        assert paths[0].hops[0] == "opA-s1"
        assert paths[0].hops[-1] == "opB-s2"

    def test_multi_hop_path_found(self):
        paths = self.router.route("opA-s1", "opC-s3", t_start=0.0, k=3)
        assert len(paths) >= 1
        assert paths[0].hops[0] == "opA-s1"
        assert paths[0].hops[-1] == "opC-s3"

    def test_no_path_before_contact(self):
        # Contact C001 starts at t=0, but request at t=400 when C001 is over
        paths = self.router.route("opA-s1", "opB-s2", t_start=400.0, k=1)
        assert len(paths) == 0

    def test_latency_is_positive(self):
        paths = self.router.route("opA-s1", "opB-s2", t_start=0.0, k=1)
        assert paths[0].latency_sec > 0

    def test_intra_operator_path(self):
        paths = self.router.route("opA-s1", "opA-s4", t_start=0.0, k=2)
        assert len(paths) >= 1

    def test_channel_filter_excludes_hop(self):
        # Filter that blocks ALL hops involving opB-s2 (so no path can reach it)
        def filt(a, b):
            return "opB-s2" not in (a, b)

        router = CGR(self.cp, channel_filter=filt, operator_map=_op_map())
        paths = router.route("opA-s1", "opB-s2", t_start=0.0, k=1)
        assert len(paths) == 0

    def test_k_paths_count(self):
        paths = self.router.route("opA-s1", "opA-s4", t_start=0.0, k=3)
        assert 1 <= len(paths) <= 3

    def test_paths_sorted_by_latency(self):
        paths = self.router.route("opA-s1", "opA-s4", t_start=0.0, k=3)
        lats = [p.latency_sec for p in paths]
        assert lats == sorted(lats)

    def test_operator_sequence_populated(self):
        paths = self.router.route("opA-s1", "opB-s2", t_start=0.0, k=1)
        assert len(paths[0].operator_sequence) == len(paths[0].hops)

    def test_inter_operator_hops_counted(self):
        paths = self.router.route("opA-s1", "opC-s3", t_start=0.0, k=1)
        # A→B→C path has 2 inter-operator hops
        assert paths[0].inter_operator_hops >= 1

    def test_all_satellites_discovered(self):
        sats = self.cp.get_all_satellites()
        assert "opA-s1" in sats
        assert "opB-s2" in sats

    def test_contacts_at_time(self):
        contacts = self.cp.get_contacts_at(100.0)
        assert len(contacts) >= 2  # C001, C004 are active at t=100

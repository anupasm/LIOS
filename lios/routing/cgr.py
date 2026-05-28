"""Contact Graph Routing (CGR) for the LIOS protocol emulator.

Implements:
  - Earliest-arrival Dijkstra over the time-expanded contact graph.
  - Yen's K-shortest paths for path diversity.
  - Policy filtering: exclude hops where sat_channel status != ACTIVE.
  - Fairness tie-breaking: prefer paths that balance load across operator pairs.

Reference: Burleigh, S.C. (2011). Contact Graph Routing. IETF RFC 6693.
"""
from __future__ import annotations

import heapq
import math
from dataclasses import dataclass, field
from typing import Callable, Dict, FrozenSet, List, Optional, Set, Tuple

from contact_plan.window_calculator import Contact, ContactPlan


@dataclass
class Path:
    hops: List[str]               # sequence of node IDs (src_sat → ... → dst_sat)
    contacts: List[str]           # contact_ids traversed
    latency_sec: float            # end-to-end minimum latency
    arrival_time_sec: float       # absolute arrival time
    operator_sequence: List[str]  # operator of each hop node
    inter_operator_hops: int      # count of operator-boundary crossings

    def __lt__(self, other: "Path") -> bool:
        return self.latency_sec < other.latency_sec


# ── internal graph types ────────────────────────────────────────────────────────

@dataclass(order=True)
class _State:
    """Priority queue entry: (arrival_time, departure_node, contact_id_path, node_path)."""
    arrival: float
    node: str = field(compare=False)
    contacts: List[str] = field(compare=False, default_factory=list)
    nodes: List[str] = field(compare=False, default_factory=list)


def _propagation_delay_sec(range_km: float) -> float:
    """One-way propagation delay in seconds."""
    C_KM_S = 299_792.458
    return range_km / C_KM_S


class CGR:
    """Contact Graph Router.

    Parameters
    ----------
    contact_plan:       The computed contact plan.
    channel_filter:     Optional callable (from_node, to_node) → bool.
                        Return False to exclude a hop (e.g. channel not ACTIVE).
    operator_map:       Mapping node_id → operator_id (for fairness weighting).
    """

    def __init__(
        self,
        contact_plan: ContactPlan,
        channel_filter: Optional[Callable[[str, str], bool]] = None,
        operator_map: Optional[Dict[str, str]] = None,
    ):
        self._cp = contact_plan
        self._filter = channel_filter or (lambda a, b: True)
        self._op_map = operator_map or {}
        self._adjacency = self._build_adjacency()

    def _build_adjacency(self) -> Dict[str, List[Contact]]:
        adj: Dict[str, List[Contact]] = {}
        for c in self._cp.contacts:
            if c.node_type_from == "GS" or c.node_type_to == "GS":
                # GS contacts used only for ground-to-satellite first/last hop
                continue
            adj.setdefault(c.from_node, []).append(c)
            adj.setdefault(c.to_node, []).append(c)  # bidirectional ISL
        return adj

    # ── public API ──────────────────────────────────────────────────────────────

    def route(
        self,
        src: str,
        dst: str,
        t_start: float,
        k: int = 3,
    ) -> List[Path]:
        """Return up to k shortest (min-latency) paths from src to dst starting at t_start."""
        paths = self._yen_k_shortest(src, dst, t_start, k)
        # Sort: primarily by latency, break ties by fewest inter-operator hops (fairness)
        paths.sort(key=lambda p: (p.latency_sec, p.inter_operator_hops))
        return paths

    def route_with_gs(
        self,
        src_ground: str,
        dst_ground: str,
        t_start: float,
        k: int = 3,
    ) -> List[Path]:
        """Route from a ground node via satellite path to a destination ground node."""
        src_reachable = self._cp.get_reachable_satellites(src_ground, t_start, t_start + 600)
        if not src_reachable:
            return []
        best: List[Path] = []
        for src_sat in src_reachable:
            for dst_sat in self._cp.get_all_satellites():
                if dst_sat == src_sat:
                    continue
                paths = self.route(src_sat, dst_sat, t_start, k=1)
                best.extend(paths)
        best.sort(key=lambda p: p.latency_sec)
        return best[:k]

    # ── Earliest-arrival Dijkstra ───────────────────────────────────────────────

    def _dijkstra(
        self,
        src: str,
        dst: str,
        t_start: float,
        excluded_edges: Optional[Set[Tuple[str, str, str]]] = None,
    ) -> Optional[Path]:
        """Modified Dijkstra: minimise arrival time (= earliest-arrival algorithm).

        excluded_edges: set of (from_node, to_node, contact_id) triples to skip
                        (used by Yen's algorithm).
        """
        excluded = excluded_edges or set()
        # best_arrival[node] = earliest arrival time seen
        best: Dict[str, float] = {src: t_start}
        # pq: (arrival_time, node_id, contact_path, node_path)
        pq: List[Tuple[float, str, List[str], List[str]]] = [
            (t_start, src, [], [src])
        ]

        while pq:
            arr, node, c_path, n_path = heapq.heappop(pq)

            if arr > best.get(node, math.inf):
                continue  # stale entry

            if node == dst:
                return self._make_path(n_path, c_path, t_start, arr)

            # Enumerate adjacent contacts (ISL and same-operator links)
            contacts_from: List[Contact] = []
            for c in self._cp.contacts:
                if c.node_type_from == "SAT" and c.node_type_to == "SAT":
                    if c.from_node == node:
                        contacts_from.append((c, c.to_node))
                    elif c.to_node == node:
                        contacts_from.append((c, c.from_node))  # bidirectional

            for c, neighbor in contacts_from:
                edge = (c.from_node, c.to_node, c.contact_id)
                if edge in excluded:
                    continue
                if not self._filter(node, neighbor):
                    continue
                # Can only depart after arrival at current node AND contact start
                depart = max(arr, c.start_time_sec)
                if depart >= c.end_time_sec:
                    continue  # contact already over
                prop_delay = _propagation_delay_sec(c.range_km)
                arrive_neighbor = depart + prop_delay
                if arrive_neighbor < best.get(neighbor, math.inf):
                    best[neighbor] = arrive_neighbor
                    heapq.heappush(pq, (
                        arrive_neighbor,
                        neighbor,
                        c_path + [c.contact_id],
                        n_path + [neighbor],
                    ))

        return None  # no path found

    def _make_path(
        self, nodes: List[str], contacts: List[str], t_start: float, arrival: float
    ) -> Path:
        ops = [self._op_map.get(n, "unknown") for n in nodes]
        inter_op = sum(
            1 for i in range(1, len(ops)) if ops[i] != ops[i - 1]
        )
        return Path(
            hops=nodes,
            contacts=contacts,
            latency_sec=arrival - t_start,
            arrival_time_sec=arrival,
            operator_sequence=ops,
            inter_operator_hops=inter_op,
        )

    # ── Yen's K-Shortest Paths ─────────────────────────────────────────────────

    def _yen_k_shortest(
        self, src: str, dst: str, t_start: float, k: int
    ) -> List[Path]:
        """Yen's algorithm over the time-expanded contact graph."""
        first = self._dijkstra(src, dst, t_start)
        if first is None:
            return []
        A: List[Path] = [first]
        B: List[Path] = []  # candidate heap

        for ki in range(1, k):
            prev_path = A[ki - 1]
            for spur_idx in range(len(prev_path.hops) - 1):
                spur_node = prev_path.hops[spur_idx]
                root_nodes = prev_path.hops[:spur_idx + 1]
                root_contacts = prev_path.contacts[:spur_idx]

                excluded: Set[Tuple[str, str, str]] = set()

                # Exclude edges used by all previous A-paths sharing the same root
                for prev in A:
                    if (len(prev.hops) > spur_idx and
                            prev.hops[:spur_idx + 1] == root_nodes and
                            len(prev.contacts) > spur_idx):
                        c_id = prev.contacts[spur_idx]
                        # Find the contact to get from/to nodes
                        c_obj = next((c for c in self._cp.contacts if c.contact_id == c_id), None)
                        if c_obj:
                            excluded.add((c_obj.from_node, c_obj.to_node, c_id))
                            excluded.add((c_obj.to_node, c_obj.from_node, c_id))

                # Compute arrival time at spur node along the root path
                if spur_idx == 0:
                    t_spur = t_start
                else:
                    # Accumulate travel time along root
                    t_acc = t_start
                    for ci in range(spur_idx):
                        cid = root_contacts[ci]
                        c_obj = next((c for c in self._cp.contacts if c.contact_id == cid), None)
                        if c_obj:
                            t_acc = max(t_acc, c_obj.start_time_sec) + _propagation_delay_sec(c_obj.range_km)
                    t_spur = t_acc

                spur_path = self._dijkstra(spur_node, dst, t_spur, excluded)
                if spur_path is None:
                    continue

                # Splice root + spur
                total_nodes = root_nodes[:-1] + spur_path.hops
                total_contacts = root_contacts + spur_path.contacts
                if len(total_contacts) < len(total_nodes) - 1:
                    continue
                candidate = self._make_path(total_nodes, total_contacts, t_start, spur_path.arrival_time_sec)

                # Avoid duplicates (B is a heap of (latency, Path) tuples)
                b_paths = [item[1] for item in B]
                if not any(c.hops == candidate.hops for c in A + b_paths):
                    heapq.heappush(B, (candidate.latency_sec, candidate))

            if not B:
                break
            _, best_candidate = heapq.heappop(B)
            A.append(best_candidate)

        return A

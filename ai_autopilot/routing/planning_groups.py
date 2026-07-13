"""Group a set of selected work items by their ADO link graph (PURE).

Used by the Planning workbench's Analyze action to show, for the items a human
picked, which must run in order (Predecessor chains), which conflict and should be
serialised (Related clusters), and which are independent (parallel-OK). No I/O.
"""

from __future__ import annotations

from collections.abc import Mapping, Set

from ai_autopilot.models import WorkItemInfo


def _serial_components(edges: list[tuple[int, int]]) -> list[list[int]]:
    """Weakly-connected components of the predecessor edges, each topologically
    ordered (predecessor before successor). ``edges`` are ``(pred, succ)`` pairs."""
    adj: dict[int, set[int]] = {}
    succ: dict[int, set[int]] = {}
    nodes: set[int] = set()
    for a, b in edges:
        nodes.update((a, b))
        adj.setdefault(a, set()).add(b)
        adj.setdefault(b, set()).add(a)
        succ.setdefault(a, set()).add(b)

    seen: set[int] = set()
    out: list[list[int]] = []
    for start in sorted(nodes):
        if start in seen:
            continue
        comp: set[int] = set()
        stack = [start]
        while stack:
            x = stack.pop()
            if x in seen:
                continue
            seen.add(x)
            comp.add(x)
            stack.extend(adj.get(x, ()))
        # Kahn topological sort within the component.
        indeg = {x: sum(1 for a, b in edges if b == x and a in comp) for x in comp}
        queue = sorted(x for x in comp if indeg[x] == 0)
        order: list[int] = []
        while queue:
            x = queue.pop(0)
            order.append(x)
            for y in sorted(succ.get(x, ())):
                if y in comp:
                    indeg[y] -= 1
                    if indeg[y] == 0:
                        queue.append(y)
        out.append(order if len(order) == len(comp) else sorted(comp))
    return out


def _undirected_components(edges: set[frozenset[int]]) -> list[list[int]]:
    """Connected components (size ≥ 2) over undirected Related edges."""
    adj: dict[int, set[int]] = {}
    for e in edges:
        a, b = tuple(e)
        adj.setdefault(a, set()).add(b)
        adj.setdefault(b, set()).add(a)
    seen: set[int] = set()
    out: list[list[int]] = []
    for start in sorted(adj):
        if start in seen:
            continue
        comp: set[int] = set()
        stack = [start]
        while stack:
            x = stack.pop()
            if x in seen:
                continue
            seen.add(x)
            comp.add(x)
            stack.extend(adj.get(x, ()))
        if len(comp) >= 2:
            out.append(sorted(comp))
    return out


def group_by_links(
    items: list[WorkItemInfo],
    predecessors: Mapping[int, Set[int]] | None = None,
    related: Mapping[int, Set[int]] | None = None,
) -> dict:
    """Partition ``items`` into serial chains, conflict clusters, and parallel-OK.

    Only edges whose BOTH endpoints are in the selection count.

    Returns ``{"serial": [[ordered ids]…], "conflicts": [[ids]…], "parallel": [ids]}``.
    """
    predecessors = predecessors or {}
    related = related or {}
    idset = {i.id for i in items}

    serial_edges = [
        (p, b) for b in idset for p in predecessors.get(b, set()) if p in idset and p != b
    ]
    serial = [c for c in _serial_components(serial_edges) if len(c) >= 2]

    rel_edges: set[frozenset[int]] = set()
    for a in idset:
        for b in related.get(a, set()):
            if b in idset and a != b:
                rel_edges.add(frozenset((a, b)))
    conflicts = _undirected_components(rel_edges)

    grouped = {x for grp in serial for x in grp} | {x for c in conflicts for x in c}
    parallel = sorted(idset - grouped)
    return {"serial": serial, "conflicts": conflicts, "parallel": parallel}

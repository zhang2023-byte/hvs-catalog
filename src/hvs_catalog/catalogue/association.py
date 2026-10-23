"""Deterministic association with component-wide identity conflict checks."""

from dataclasses import dataclass
from itertools import combinations

from ..records.catalog import Association, AssociationOrigin
from .identity import coordinate, gaia_conflicts, keys, names, separation


@dataclass
class Node:
    key: str
    star: object
    catalog_id: str | None = None
    anchor: bool = False
    previous_id: str | None = None
    origin: AssociationOrigin | None = None


class Groups:
    def __init__(self, nodes):
        self.nodes = {n.key: n for n in nodes}
        self.parent = {key: key for key in self.nodes}
        self.members = {key: {key} for key in self.nodes}
        self.edges = []

    def find(self, key):
        while self.parent[key] != key:
            self.parent[key] = self.parent[self.parent[key]]
            key = self.parent[key]
        return key

    def join(
        self, left, right, method, details, *, external_names=(), rejection_reason=None
    ):
        a, b = self.find(left), self.find(right)
        members = self.members[a] | self.members[b]
        group_names = [name for key in members for name in names(self.nodes[key].star)]
        ids = {self.nodes[key].catalog_id for key in members} - {None}
        reason = rejection_reason
        if gaia_conflicts([*group_names, *external_names]):
            reason = "Conflicting Gaia identifiers within one release."
        elif len(ids) > 1:
            reason = "Distinct catalog IDs require an explicit identity revision."
        self.edges.append(
            Association(
                left=left,
                right=right,
                method=method,
                accepted=reason is None,
                origins={
                    k: self.nodes[k].origin
                    for k in sorted({left, right})
                    if self.nodes[k].origin is not None
                },
                details={**details, **({"reason": reason} if reason else {})},
            )
        )
        if reason is None and a != b:
            a, b = sorted((a, b))
            self.parent[b] = a
            self.members[a] = members
            del self.members[b]
        return reason is None

    def groups(self):
        return [sorted(v) for _, v in sorted(self.members.items())]

    def scientific(self, members):
        return [
            (key, self.nodes[key].star) for key in members if not self.nodes[key].anchor
        ]


def associate(nodes, explicit=(), previous_links=()):
    groups = Groups(nodes)
    for left, right, details in previous_links:
        groups.join(
            left,
            right,
            "previous",
            details,
        )
    by_identifier = {}
    for node in nodes:
        for key in sorted(keys(node.star)):
            by_identifier.setdefault(key, []).append(node.key)
    for identifier, members in sorted(by_identifier.items()):
        for left, right in combinations(sorted(members), 2):
            groups.join(left, right, "identifier", {"identifier": identifier})
    for link in explicit:
        if link.left not in groups.nodes or link.right not in groups.nodes:
            raise ValueError("Explicit association names an unknown source node.")
        if link.source not in {link.left.split("#")[0], link.right.split("#")[0]}:
            raise ValueError(
                "Correspondence evidence must belong to an endpoint paper."
            )
        groups.join(
            link.left,
            link.right,
            "paper",
            {
                "source": link.source,
                "evidence": [e.model_dump() for e in link.evidence],
            },
        )
    positions = {node.key: pos for node in nodes if (pos := coordinate(node.star))}
    pairs = []
    for (left, lp), (right, rp) in combinations(sorted(positions.items()), 2):
        distance = separation(lp, rp)
        if distance <= 5.0 + 1e-9:
            pairs.append((distance, left, right))
    for distance, left, right in sorted(pairs):
        # Keep every coordinate-only edge used to connect components.
        if groups.find(left) != groups.find(right):
            groups.join(
                left,
                right,
                "coordinates",
                {
                    "separation_arcsec": distance,
                    "threshold_arcsec": 5.0,
                    "left_position": positions[left].model_dump(mode="json"),
                    "right_position": positions[right].model_dump(mode="json"),
                    "epoch_propagation": False,
                },
            )
    return groups

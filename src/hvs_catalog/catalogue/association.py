"""Deterministic association with component-wide identity conflict checks."""

from dataclasses import dataclass
from itertools import combinations

from astropy.coordinates import SkyCoord

from ..records.catalog import Association, AssociationOrigin
from .identity import (
    coordinate,
    coordinate_interpretation,
    gaia_conflicts,
    keys,
    names,
    sky,
)


@dataclass
class Node:
    key: str
    star: object
    origin: AssociationOrigin | None = None


class Groups:
    def __init__(self, nodes, conflicts=(), separations=None):
        self.nodes = {n.key: n for n in nodes}
        self.parent = {key: key for key in self.nodes}
        self.members = {key: {key} for key in self.nodes}
        self.edges = []
        self.conflicts = conflicts
        self.separations = separations or {}

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
        reason = rejection_reason
        excluded = [
            d
            for pair, decisions in self.separations.items()
            if set(pair) <= members
            for d in decisions
        ]
        if excluded:
            reason = "Separated by approved review: " + ", ".join(
                sorted({d.id for d in excluded})
            )
            details = {**details, "review_decisions": sorted({d.id for d in excluded})}
        source_nodes = {
            key.split(":", 1)[1] if key.startswith("@") else key for key in members
        }
        conflicts = [c for c in self.conflicts if {c.left, c.right} <= source_nodes]
        if conflicts:
            reason = "Source identity evidence conflicts: " + "; ".join(
                c.reason for c in conflicts
            )
            details = {
                **details,
                "identity_conflicts": [c.model_dump(mode="json") for c in conflicts],
            }
        if gaia_conflicts([*group_names, *external_names]):
            reason = "Conflicting Gaia identifiers within one release."
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
        return [(key, self.nodes[key].star) for key in members]


def associate(nodes, explicit=(), *, conflicts=(), separations=None):
    groups = Groups(nodes, conflicts, separations)
    for conflict in conflicts:
        if conflict.left not in groups.nodes or conflict.right not in groups.nodes:
            raise ValueError("Identity conflict names an unknown source node.")
        if conflict.left == conflict.right or conflict.source not in {
            conflict.left.split("#")[0],
            conflict.right.split("#")[0],
        }:
            raise ValueError(
                "Identity conflict requires distinct endpoints and their paper evidence."
            )
        groups.join(
            conflict.left,
            conflict.right,
            "paper",
            {
                "identity_conflict": True,
                "source": conflict.source,
                "evidence": [e.model_dump(mode="json") for e in conflict.evidence],
            },
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
    ordered = sorted(positions)
    if ordered:
        # Transform each position once; array separation uses the same spherical rule.
        converted = [sky(positions[key]).icrs for key in ordered]
        icrs = SkyCoord(
            [p.ra.deg for p in converted],
            [p.dec.deg for p in converted],
            unit="deg",
            frame="icrs",
        )
        for index, left in enumerate(ordered[:-1]):
            distances = icrs[index].separation(icrs[index + 1 :]).arcsec
            for right, distance in zip(ordered[index + 1 :], distances, strict=True):
                if distance <= 5.0 + 1e-9:
                    pairs.append((float(distance), left, right))
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
                    "coordinate_interpretations": {
                        "left": coordinate_interpretation(positions[left]),
                        "right": coordinate_interpretation(positions[right]),
                    },
                    "epoch_propagation": False,
                },
            )
    return groups

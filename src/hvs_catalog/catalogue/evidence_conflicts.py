"""Detect contradictory row-bound identity evidence without correcting source values."""

import re
from html import unescape
from itertools import combinations

from astropy.coordinates import Angle

from ..records.catalog import AssociationConflict, SkyPosition
from .identity import _frame, coordinate, separation

# Deliberately require explicit sexagesimal units or colon notation and a signed Dec.
_PAIR = re.compile(
    r"(?<![\d.])(?P<ra>\d{1,2}(?:h\s*\d{1,2}m\s*\d{1,2}(?:\.\d+)?s|:\d{1,2}:\d{1,2}(?:\.\d+)?))"
    r"\s+(?P<dec>[+\-]\d{1,2}(?:d\s*\d{1,2}m\s*\d{1,2}(?:\.\d+)?s|:\d{1,2}:\d{1,2}(?:\.\d+)?))(?![\d.])",
    re.IGNORECASE,
)


def identity_position(star, position):
    """Accept only a single coordinate pair immediately following this row's ID."""
    found = []
    for identifier in star.identifiers:
        label = identifier.value.strip()
        local = re.fullmatch(r"(?:ID|No\.?|Star|Object)\s+(\d+)", label, re.IGNORECASE)
        labels = {label, local[1]} if local else {label}
        for evidence in identifier.evidence:
            text = unescape(
                re.sub(r"<[^>]*>", " ", getattr(evidence, "quote", "") or "")
            )
            text = " ".join(text.replace("−", "-").split())
            pairs = list(_PAIR.finditer(text))
            if len(pairs) != 1:
                continue
            match = pairs[0]
            prefix = text[: match.start()].strip(" |,;:")
            if prefix not in labels:
                continue
            frame = _frame([{"condition": "", "evidence": [evidence.model_dump()]}])
            if frame is None:
                continue
            try:
                parsed = SkyPosition(
                    ra_deg=float(Angle(match["ra"], unit="hourangle").degree),
                    dec_deg=float(Angle(match["dec"], unit="deg").degree),
                    frame=position.frame if frame[3] else frame[0],
                    equinox=position.equinox if frame[3] else frame[1],
                )
            except ValueError:
                continue
            found.append((parsed, evidence))
    if not found or any(separation(found[0][0], p) > 1e-9 for p, _ in found):
        return None
    return found[0][0], [e for _, e in found]


def source_conflicts(nodes):
    """Separate close same-paper records whose own identifier rows disagree spatially."""
    by_paper = {}
    for node in nodes:
        position = coordinate(node.star)
        if (
            position is None
            or (identity := identity_position(node.star, position)) is None
        ):
            continue
        by_paper.setdefault(node.key.split("#")[0], []).append(
            (node, position, identity)
        )
    conflicts = []
    for paper, records in sorted(by_paper.items()):
        for (a, ap, (ai, ae)), (b, bp, (bi, be)) in combinations(records, 2):
            if separation(ap, bp) > 5.0 + 1e-9 or separation(ai, bi) <= 5.0 + 1e-9:
                continue
            # A reused position must agree with one row and disagree with the other.
            if not (
                separation(ap, ai) <= 5.0 + 1e-9 < separation(bp, bi)
                or separation(bp, bi) <= 5.0 + 1e-9 < separation(ap, ai)
            ):
                continue
            conflicts.append(
                AssociationConflict(
                    left=a.key,
                    right=b.key,
                    source=paper,
                    evidence=[*ae, *be],
                    reason=(
                        "Nearby extracted coordinates conflict with distinct coordinates in "
                        "the records' own identifier evidence rows; possible reused row coordinates. "
                        "Original values are retained; this is an evidence contradiction, not a corrected position."
                    ),
                )
            )
    return conflicts

"""Compare two catalogs by source records, independent of catalog numbering."""

import json
from collections import Counter
from pathlib import Path

from .readable import read_snapshot

ANNOTATIONS = {"attribution", "attribution_reason", "standard", "correction"}


def _members(star):
    return frozenset(
        f"{key}#/stars/{o.result_index}"
        for key, source in star.sources.items()
        for o in source.objects
    )


def _items(stars, *, attribution=False):
    result = Counter()
    for star in stars:
        for i in star.identifiers:
            content = i.model_dump(mode="json", exclude={"correction"})
            label = json.dumps(content, sort_keys=True)
            result[(i.source, "identifier", label, None)] += 1
        for q in star.quantities:
            for v in q.values:
                content = v.model_dump(mode="json", exclude=ANNOTATIONS)
                label = json.dumps(content, sort_keys=True)
                kind = v.attribution if attribution else None
                result[(v.source, q.quantity, label, kind)] += 1
    return result


def _simbad(star):
    selected = star.simbad.selected
    return [
        star.simbad.status,
        star.simbad.method,
        selected.main_id if selected else None,
    ]


def _difference(old, new, limit=20):
    removed, added = old - new, new - old
    by_paper = Counter()
    for key, count in removed.items():
        by_paper[key[0] + " " + key[1] + " removed"] += count
    for key, count in added.items():
        by_paper[key[0] + " " + key[1] + " added"] += count
    return {
        "removed": sum(removed.values()),
        "added": sum(added.values()),
        "by_paper_quantity": dict(sorted(by_paper.items())),
        "removed_sample": [list(k) for k in sorted(removed.elements())][:limit],
        "added_sample": [list(k) for k in sorted(added.elements())][:limit],
    }


def _attributions(old, new):
    """Count attribution changes of values whose content is in both catalogs."""
    by_content = {}
    for side, items in (("old", old), ("new", new)):
        for (*content, attribution), count in items.items():
            by_content.setdefault(tuple(content), {"old": Counter(), "new": Counter()})
            by_content[tuple(content)][side][attribution] += count
    changes = Counter()
    for (paper, quantity, _), sides in by_content.items():
        if sides["old"] and sides["new"] and sides["old"] != sides["new"]:
            removed, added = sides["old"] - sides["new"], sides["new"] - sides["old"]
            for a, b in zip(sorted(removed.elements()), sorted(added.elements())):
                changes[f"{paper} {quantity} {a}->{b}"] += 1
    return {
        "changed": sum(changes.values()),
        "by_paper_quantity": dict(sorted(changes.items())),
    }


def compare_catalogs(old_root, new_root):
    (old_manifest, old_stars), (new_manifest, new_stars) = (
        read_snapshot(Path(root).resolve()) for root in (old_root, new_root)
    )
    old, new = list(old_stars.values()), list(new_stars.values())
    old_by, new_by = {_members(s): s for s in old}, {_members(s): s for s in new}
    owner = {n: m for m in old_by for n in m}
    groups = []
    for members, star in sorted(new_by.items(), key=lambda x: x[1].catalog_id):
        if members in old_by:
            continue
        before = sorted({owner[n] for n in members if n in owner}, key=sorted)
        groups.append(
            {
                "new": [star.catalog_id, star.display_name],
                "old": [[old_by[m].catalog_id, old_by[m].display_name] for m in before],
                "added_records": sorted(n for n in members if n not in owner),
            }
        )
    kept = [(old_by[m], new_by[m]) for m in old_by if m in new_by]
    return {
        "counts": {
            "old": {"papers": len(old_manifest.papers), "stars": len(old)},
            "new": {"papers": len(new_manifest.papers), "stars": len(new)},
        },
        "unchanged_groups": len(kept),
        "regrouped": groups,
        "removed_records": sorted(
            n for m in old_by for n in m if n not in {x for k in new_by for x in k}
        ),
        "display_names": [
            [b.catalog_id, a.display_name, b.display_name]
            for a, b in kept
            if a.display_name != b.display_name
        ],
        "simbad": [
            [b.catalog_id, b.display_name, _simbad(a), _simbad(b)]
            for a, b in kept
            if _simbad(a) != _simbad(b)
        ],
        "content": _difference(_items(old), _items(new)),
        "attributions": _attributions(
            _items(old, attribution=True), _items(new, attribution=True)
        ),
        "corrections": sum(
            1
            for s in new
            for item in [*s.identifiers, *(v for q in s.quantities for v in q.values)]
            if item.correction
        ),
    }

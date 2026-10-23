"""Compare current Gold annotations without treating either answer as scientific truth."""

import hashlib
from collections import Counter

from ..records.common import canonical_json
from .gold import annotation_path, read_annotation


def compare_annotations(before, after):
    old, new = read_annotation(before), read_annotation(after)
    if old.metadata.arxiv_id != new.metadata.arxiv_id:
        raise ValueError("Gold comparison requires the same paper.")
    changes = []

    def change(path, left, right):
        if left != right:
            changes.append({"path": path, "before": left, "after": right})

    change("metadata", old.metadata.model_dump(), new.metadata.model_dump())
    change("notes", old.notes, new.notes)
    old_ids = [{i.value for i in s.identifiers} for s in old.stars]
    new_ids = [{i.value for i in s.identifiers} for s in new.stars]
    edges = {
        i: [j for j, ids in enumerate(new_ids) if ids & names]
        for i, names in enumerate(old_ids)
    }
    counts = Counter(j for js in edges.values() for j in js)
    matches = {
        i: js[0] for i, js in edges.items() if len(js) == 1 and counts[js[0]] == 1
    }
    for i, star in enumerate(old.stars):
        if i not in matches:
            change(f"before.stars[{i}]", star.model_dump(mode="json"), None)
            continue
        j = matches[i]
        other = new.stars[j]
        path = f"stars[{i}] -> stars[{j}]"
        change(
            path + ".identifiers",
            sorted((x.model_dump() for x in star.identifiers), key=canonical_json),
            sorted((x.model_dump() for x in other.identifiers), key=canonical_json),
        )
        change(
            path + ".contribution_type",
            star.contribution_type.model_dump(),
            other.contribution_type.model_dump(),
        )
        a = {q.quantity: q for q in star.quantities}
        b = {q.quantity: q for q in other.quantities}
        for quantity in sorted(a.keys() | b.keys()):
            left = (
                sorted((v.model_dump() for v in a[quantity].values), key=canonical_json)
                if quantity in a
                else []
            )
            right = (
                sorted((v.model_dump() for v in b[quantity].values), key=canonical_json)
                if quantity in b
                else []
            )
            # Include conditions and evidence; numerical scoring intentionally does not.
            change(path + ".quantities." + quantity, left, right)
    for j, star in enumerate(new.stars):
        if j not in matches.values():
            change(f"after.stars[{j}]", None, star.model_dump(mode="json"))
    return {
        "arxiv_id": old.metadata.arxiv_id,
        "before_annotation_sha256": hashlib.sha256(
            annotation_path(before).read_bytes()
        ).hexdigest(),
        "after_annotation_sha256": hashlib.sha256(
            annotation_path(after).read_bytes()
        ).hexdigest(),
        "matches": [{"before": i, "after": j} for i, j in matches.items()],
        "unmatched_before": [i for i in range(len(old_ids)) if i not in matches],
        "unmatched_after": [
            j for j in range(len(new_ids)) if j not in matches.values()
        ],
        "changes": changes,
        "note": "Only unique exact shared identifiers pair stars. Unmatched rows are not inferred to be additions/deletions. All changed content is retained; no scientific judgement is made.",
    }

"""Apply bound PDF transcription corrections to a disposable scientific view."""

from copy import deepcopy

from ..records.literature_contribution import LiteratureContribution


def correction_node(decision):
    return decision.paper + "#" + "/".join(decision.path.split("/")[:3])


def marker(decision):
    return {
        "decision": decision.id,
        "evidence": [e.model_dump(mode="json") for e in decision.evidence],
    }


def corrected_items(key, paper, index, decisions):
    """Yield identifiers and values of one original star at their original paths."""
    changes = {d.path: d for d in decisions if d.paper == key}
    star = paper.stars[index]

    def apply(path, item):
        d = changes.get(path)
        if d is None:
            return item.model_dump(mode="json"), None
        if d.replacement is None:
            return None, None
        return type(item).model_validate(d.replacement).model_dump(mode="json"), d

    for i, identifier in enumerate(star.identifiers):
        yield (
            "identifier",
            None,
            f"/stars/{index}/identifiers/{i}",
            *apply(f"/stars/{index}/identifiers/{i}", identifier),
        )
    for qi, quantity in enumerate(star.quantities):
        for vi, value in enumerate(quantity.values):
            path = f"/stars/{index}/quantities/{qi}/values/{vi}"
            yield ("value", quantity.quantity, path, *apply(path, value))


def at_path(document, path):
    current = document
    for part in path.strip("/").split("/"):
        current = current[int(part)] if isinstance(current, list) else current[part]
    return current


def corrected_papers(papers, decisions):
    """Validate against original indices before making replacements or withdrawals."""
    raw = {key: paper.model_dump(mode="json") for key, paper in papers.items()}
    result = deepcopy(raw)
    seen, withdrawals = set(), []
    for d in decisions:
        if d.type != "correct":
            continue
        target = (d.paper, d.path)
        if target in seen:
            raise ValueError("Conflicting corrections for one original path.")
        seen.add(target)
        try:
            original = at_path(raw[d.paper], d.path)
        except (KeyError, IndexError, TypeError) as exc:
            raise ValueError("Correction path does not exist.") from exc
        if original != d.original:
            raise ValueError("Correction original differs from the fixed extraction.")
        parent_path, index = d.path.rsplit("/", 1)
        parent = at_path(result[d.paper], parent_path)
        if d.replacement is None:
            withdrawals.append((d.paper, parent_path, int(index)))
        else:
            parent[int(index)] = deepcopy(d.replacement)
    for paper, path, index in sorted(withdrawals, reverse=True):
        del at_path(result[paper], path)[index]
    for paper in result.values():
        for star in paper["stars"]:
            star["quantities"] = [q for q in star["quantities"] if q["values"]]
    return {
        key: LiteratureContribution.model_validate(value)
        for key, value in result.items()
    }

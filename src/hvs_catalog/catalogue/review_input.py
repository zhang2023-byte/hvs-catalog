"""Read, expand and write the human-edited review directory."""

import json
import os
from pathlib import Path

from ..records.catalog_review import (
    CatalogReview,
    CorrectionReviews,
    IdentityReviews,
    PaperReviews,
    ReviewedPaper,
)
from ..records.common import sha256_of
from .corrections import at_path

FILES = {
    "papers.json": PaperReviews,
    "identity.json": IdentityReviews,
    "corrections.json": CorrectionReviews,
}


def load_review(root):
    root = Path(root)
    if not root.is_dir():
        raise ValueError(
            "Review input is a directory of papers.json, identity.json and corrections.json."
        )
    unexpected = {p.name for p in root.glob("*.json")} - set(FILES)
    if unexpected:
        raise ValueError("Unexpected review file: " + ", ".join(sorted(unexpected)))
    parts = {}
    for name, model in FILES.items():
        path = root / name
        parts[name] = (
            model.model_validate_json(path.read_bytes()) if path.exists() else None
        )
    return parts


def fingerprint(parts):
    return sha256_of(
        {k: v.model_dump(mode="json") if v else None for k, v in sorted(parts.items())}
    )


def _approval(text):
    approved_on, approved_by = text.split(" ", 1)
    return {"approved_on": approved_on, "approved_by": approved_by}


def _table(parts):
    papers = parts["papers.json"]
    return {p.paper: p for p in papers.papers} if papers else {}


def _science(table, key):
    entry = table.get(key)
    if entry is None or entry.science_sha256 is None:
        raise ValueError(
            f"Review paper {key} lacks a science binding; run catalog bind-review."
        )
    return entry.science_sha256


def expand(parts, papers):
    """Return the bound CatalogReview and corrections that no longer match their paper."""
    table = _table(parts)
    reviews = [
        {
            "paper": p.paper,
            "science_sha256": _science(table, p.paper),
            "reviewed": True,
            "reason": parts["papers.json"].method + (" " + p.note if p.note else ""),
            **_approval(p.approved or parts["papers.json"].approved),
            "exceptions": [e.model_dump(mode="json") for e in p.exceptions],
        }
        for p in table.values()
        if p.values_reviewed
    ]
    decisions, invalid = [], []
    for d in parts["identity.json"].decisions if parts["identity.json"] else []:
        if d.type == "simbad" and not d.responses:
            raise ValueError(
                f"SIMBAD decision {d.id} lacks response bindings; run catalog bind-review."
            )
        keys = sorted({n.split("#")[0] for g in d.groups for n in g})
        decisions.append(
            {
                "id": d.id,
                "type": d.type,
                "groups": d.groups,
                "papers": {k: _science(table, k) for k in keys},
                "responses": d.responses,
                "reason": d.reason,
                "evidence": [e.model_dump(mode="json") for e in d.evidence],
                **_approval(d.approved),
                "unresolved": d.unresolved,
                "display_name": d.display_name,
                "aliases": d.aliases,
                "main_id": d.main_id,
            }
        )
    for c in parts["corrections.json"].corrections if parts["corrections.json"] else []:
        key, path = c.path.split("#", 1)
        entry = table.get(key)
        if entry is None or entry.science_sha256 is None or entry.pdf_sha256 is None:
            raise ValueError(
                f"Correction {c.id} lacks science and PDF bindings; run catalog bind-review."
            )
        current = papers.get(key)
        if current is None or current.source.result_sha256 != entry.science_sha256:
            invalid.append(
                {
                    "kind": "decision",
                    "id": c.id,
                    "groups": [[key + "#" + "/".join(path.split("/")[:3])]],
                    "reason": "Paper version or science hash changed or is missing.",
                    "status": "pending",
                }
            )
            continue
        try:
            original = at_path(current.science.model_dump(mode="json"), path)
        except (KeyError, IndexError, TypeError) as exc:
            raise ValueError("Correction path does not exist: " + c.id) from exc
        replacement = None
        if not c.withdraw:
            replacement = dict(original)
            for field, (extracted, corrected) in c.change.items():
                if original.get(field) != extracted:
                    raise ValueError(
                        f"Correction {c.id} differs from the fixed extraction in {field}."
                    )
                if corrected is None:
                    replacement.pop(field, None)
                else:
                    replacement[field] = corrected
        decisions.append(
            {
                "id": c.id,
                "type": "correct",
                "paper": key,
                "science_sha256": entry.science_sha256,
                "pdf_sha256": entry.pdf_sha256,
                "path": path,
                "original": original,
                "replacement": replacement,
                "evidence": [e.model_dump(mode="json") for e in c.evidence],
                "reason": c.reason,
                **_approval(c.approved),
            }
        )
    review = CatalogReview.model_validate(
        {"schema_version": 2, "decisions": decisions, "papers": reviews}
    )
    return review, invalid


def format_json(value, indent=0, width=100):
    """Valid JSON with one record per line; scalar-only containers stay on one line."""
    inline = json.dumps(value, ensure_ascii=False)
    if not isinstance(value, (dict, list)) or not value:
        return inline
    children = value.values() if isinstance(value, dict) else value
    if len(inline) + indent <= width or all(
        not isinstance(v, (dict, list)) for v in children
    ):
        return inline
    pad = " " * (indent + 2)
    if isinstance(value, dict):
        lines = [
            pad + json.dumps(k, ensure_ascii=False) + ": " + format_json(v, indent + 2)
            for k, v in value.items()
        ]
        return "{\n" + ",\n".join(lines) + "\n" + " " * indent + "}"
    lines = [pad + format_json(v, indent + 2) for v in value]
    return "[\n" + ",\n".join(lines) + "\n" + " " * indent + "]"


def write_review(root, parts):
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    for name, model in parts.items():
        if model is None:
            continue
        target = root / name
        temporary = target.with_suffix(".json.tmp")
        temporary.write_text(format_json(model.model_dump(mode="json")) + "\n")
        os.replace(temporary, target)


def bind_review(root, selection, simbad=None):
    """Fill only missing hash and response bindings; existing bindings are never refreshed."""
    from .inputs import load_papers
    from .review import Review
    from .simbad import FrozenSimbad, queries_for, query_key

    parts = load_review(root)
    if parts["papers.json"] is None:
        raise ValueError("papers.json with method and approved is required.")
    papers = {p.key: p for p in load_papers(selection)}
    table = _table(parts)
    filled = {"papers": 0, "science": 0, "pdf": 0, "responses": 0}
    identity = parts["identity.json"].decisions if parts["identity.json"] else []
    corrections = (
        parts["corrections.json"].corrections if parts["corrections.json"] else []
    )
    referenced = {n.split("#")[0] for d in identity for g in d.groups for n in g}
    referenced |= {c.path.split("#")[0] for c in corrections}
    for key in sorted(referenced - set(table)):
        table[key] = ReviewedPaper(paper=key, values_reviewed=False)
        parts["papers.json"].papers.append(table[key])
        filled["papers"] += 1
    needs_pdf = {c.path.split("#")[0] for c in corrections}
    for key, entry in table.items():
        if key not in papers:
            if entry.science_sha256 is None:
                raise ValueError(f"Review paper {key} is not in the selection.")
            continue
        if entry.science_sha256 is None:
            entry.science_sha256 = papers[key].source.result_sha256
            filled["science"] += 1
        if key in needs_pdf and entry.pdf_sha256 is None:
            entry.pdf_sha256 = papers[key].source.pdf_sha256
            filled["pdf"] += 1
    pending = [d for d in identity if d.type == "simbad" and not d.responses]
    if pending:
        if simbad is None:
            raise ValueError("SIMBAD decisions require --simbad to bind responses.")
        only_corrections = {**parts, "identity.json": None}
        frozen = FrozenSimbad(simbad)
        view = Review(expand(only_corrections, papers)[0], papers, frozen)
        for d in pending:
            stars = [
                (n, view.effective[n.split("#")[0]].stars[int(n.split("#/stars/")[1])])
                for n in d.groups[0]
            ]
            for query in queries_for(stars):
                if frozen.read(query) is not None:
                    d.responses[query_key(query)] = frozen.used[query_key(query)]
            if not d.responses:
                raise ValueError(f"No frozen response exists for {d.id}.")
            d.responses = dict(sorted(d.responses.items()))
            filled["responses"] += 1
    write_review(root, parts)
    return filled

"""Fictional reference answers with independently specified scientific content."""

from hvs_catalog.evaluation.bundle import program_reference
from hvs_catalog.evaluation.scoring import (
    Fingerprint,
    GoldReference,
    MaterialBinding,
    Prediction,
    ScoringPlan,
)
from hvs_catalog.records.literature_contribution import LiteratureContribution
from hvs_catalog.records.rules import current_rules_ref

EVIDENCE = [{"location": "T:p1-b0", "quote": "Synthetic supporting text."}]


def star(name="A", groups=None, aliases=(), category="candidates_found"):
    return {
        "identifiers": [
            {"value": value, "evidence": EVIDENCE} for value in (name, *aliases)
        ],
        "contribution_type": {"value": category, "evidence": EVIDENCE},
        "quantities": groups or [],
    }


def group(quantity="distance", *values):
    return {
        "quantity": quantity,
        "values": [{**v, "evidence": EVIDENCE} for v in values],
    }


def document(stars=(), paper="2601.99999"):
    return LiteratureContribution.model_validate(
        {
            "metadata": {
                "arxiv_id": paper,
                "version": "v1",
                "title": "Synthetic paper",
                "authors": ["A. Author"],
            },
            "stars": list(stars),
            "notes": [],
        }
    )


def pdf_answer(answer):
    from hvs_catalog.records.pdf_contribution import PdfContribution

    def convert(value):
        if isinstance(value, list):
            return [convert(item) for item in value]
        if isinstance(value, dict):
            if "location" in value:
                return {
                    "page": 1,
                    "location": value["location"],
                }
            return {key: convert(item) for key, item in value.items()}
        return value

    return PdfContribution.model_validate(convert(answer.model_dump(mode="json")))


def reference(answer, materials=None):
    answer = pdf_answer(answer)
    rules = Fingerprint(**current_rules_ref())
    materials = materials or MaterialBinding(
        files=[{"role": "pdf", "sha256": "1" * 64}], material_sha256="2" * 64
    )
    raw = answer.model_dump(mode="json")
    pdfs = [f for f in materials.files if f.role == "pdf"]
    assert len(pdfs) == 1
    raw["metadata"].update(
        rules=rules.model_dump(),
        material={"path": "paper.pdf", "sha256": pdfs[0].sha256},
    )
    return GoldReference.model_validate(raw)


def prediction(content, **kwargs):
    ref = reference(content)
    return Prediction(
        content=content,
        rules=ref.metadata.rules,
        materials=MaterialBinding(
            files=[{"role": "pdf", "sha256": "1" * 64}], material_sha256="2" * 64
        ),
        scope=kwargs.pop("scope", "full"),
        delivered=kwargs.pop("delivered", True),
        format_valid=kwargs.pop("format_valid", True),
        completion=kwargs.pop("completion", "complete"),
        execution_state="finished",
        **kwargs,
    )


def plan(*papers, scope="full"):
    kwargs = {"quantities": []} if scope == "roster" else {}
    return ScoringPlan(
        papers=list(papers or ["2601.99999"]),
        scope=scope,
        program=program_reference(),
        **kwargs,
    )

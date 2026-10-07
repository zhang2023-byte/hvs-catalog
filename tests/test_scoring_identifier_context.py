"""Independent identity examples: explicit context, ambiguity and preserved digits."""

import copy
from types import SimpleNamespace

import pytest

from hvs_catalog.evaluation.adapt import _objects_from_contributions
from hvs_catalog.evaluation.scoring import GoldObjectEntry, PredictionObjectEntry
from hvs_catalog.evaluation.scoring.identifiers import qualified_identifiers
from hvs_catalog.evaluation.scoring.layers import match_objects

SOURCE_ID = "1234567890123456789"


@pytest.mark.parametrize(
    ("value", "evidence", "expected"),
    [
        (
            SOURCE_ID,
            [("Table 2, Gaia DR2 Source ID column", SOURCE_ID)],
            [SOURCE_ID, f"Gaia DR2 {SOURCE_ID}"],
        ),
        (
            SOURCE_ID,
            [("Table 2, Gaia DR3 ID column", SOURCE_ID)],
            [SOURCE_ID, f"Gaia DR3 {SOURCE_ID}"],
        ),
        (
            SOURCE_ID,
            [("Paragraph 2", f"Gaia DR3 {SOURCE_ID}, 9876543210123456789")],
            [SOURCE_ID, f"Gaia DR3 {SOURCE_ID}"],
        ),
        (SOURCE_ID, [("Table 2, source_id column", SOURCE_ID)], [SOURCE_ID]),
        (SOURCE_ID, [("Table 2, Gaia ID column", SOURCE_ID)], [SOURCE_ID]),
        (SOURCE_ID, [("Text", "Gaia DR3 9876543210123456789")], [SOURCE_ID]),
        (
            SOURCE_ID,
            [("Gaia DR2 ID column", ""), ("Gaia DR3 ID column", "")],
            [SOURCE_ID],
        ),
        ("03", [("Table 2, HVS column", "03")], ["03", "HVS 03"]),
        ("3", [("Table 2, row no. 3", "3")], ["3"]),
        ("3", [("Table 2, HVS row 3", "3")], ["3"]),
    ],
)
def test_only_own_explicit_identifier_evidence_qualifies(value, evidence, expected):
    assert qualified_identifiers(value, evidence) == expected


@pytest.mark.parametrize(
    ("gold", "prediction", "matches"),
    [
        ([f"Gaia DR2 {SOURCE_ID}"], f"Gaia DR2 Source ID {SOURCE_ID}", True),
        ([f"Gaia DR2 {SOURCE_ID}"], f"Gaia DR3 {SOURCE_ID}", False),
        ([f"Gaia DR2 {SOURCE_ID}"], SOURCE_ID, False),
        ([f"Gaia DR2 {SOURCE_ID}"], "Gaia DR2 1234567890123456788", False),
        (["HVS 03"], "HVS 3", False),
        (["HVS 3"], "hvs 3", False),
        (["J0123-4567"], "J0123–4567", True),
        (["J0123-4567"], "J01234567", False),
        (["A", "B"], "A (a.k.a. B)", True),
        (["B"], "A (a.k.a. B)", True),
        (["B"], "A (possibly B)", False),
    ],
)
def test_identifier_spelling_preserves_namespace_and_actual_identity(
    gold, prediction, matches
):
    g = GoldObjectEntry(label="g", identifiers=gold)
    p = PredictionObjectEntry(
        paper_key="synthetic", label="p", identifiers=[prediction]
    )
    before = copy.deepcopy((g.model_dump(), p.model_dump()))
    result = match_objects([g], [p])
    assert (result.tp, result.fp, result.fn) == ((1, 0, 0) if matches else (0, 1, 1))
    assert (g.model_dump(), p.model_dump()) == before


def test_context_alias_retention_is_in_memory_and_deduplicated():
    identifier = SimpleNamespace(
        value="3", evidence=[SimpleNamespace(location="Table 2, HVS column", quote="3")]
    )
    obj = SimpleNamespace(
        contribution_id="g",
        identifiers=[identifier, SimpleNamespace(value="HVS 3", evidence=[])],
        quantities=[],
        quantity_conflicts=[],
        contribution_type=None,
        paper_boundness=None,
    )
    assert _objects_from_contributions([obj])[0].identifiers == ["3", "HVS 3"]
    assert identifier.value == "3"


def test_explicit_alias_ambiguity_and_duplicates_do_not_create_extra_true_positives():
    gold = [
        GoldObjectEntry(label="g1", identifiers=["A"]),
        GoldObjectEntry(label="g2", identifiers=["B"]),
    ]
    pred = PredictionObjectEntry(
        paper_key="synthetic", label="p", identifiers=["A (a.k.a. B)"]
    )
    result = match_objects(gold, [pred])
    assert (result.tp, result.fp, result.fn, result.pending) == (0, 0, 0, 3)
    duplicate = pred.model_copy(update={"label": "duplicate"})
    result = match_objects(gold[:1], [pred, duplicate])
    assert (result.tp, result.fp, result.fn) == (1, 1, 0)

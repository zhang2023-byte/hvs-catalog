"""Readable names are deterministic labels, independent of scientific identity."""

import json

import pytest
import test_catalog
from test_catalog import GAIA_A, GAIA_B, candidate, paper, source_star

from hvs_catalog.catalogue.identity import display_name, identifier_key
from hvs_catalog.catalogue.publication import verify_catalog
from hvs_catalog.records.catalog import SimbadSnapshot

build = test_catalog.build
CATALOG_ID = "hvs-catalog-000001"


@pytest.mark.parametrize(
    "identifiers,expected",
    [
        ([GAIA_A, "HVS2", "US 708"], "HVS2"),
        ([GAIA_A, "2MASS J01020304+0506078"], "2MASS J01020304+0506078"),
        (["0012", "1234567890123456789", "12-34"], CATALOG_ID),
        (["  Star   12  ", GAIA_A], GAIA_A),
        (["ID 7"], "ID 7 (2601.00001)"),
        (["Object 3"], "Object 3 (2601.00001)"),
        (["No. 5"], "No. 5 (2601.00001)"),
        (["Row 4"], "Row 4 (2601.00001)"),
        ([GAIA_B, GAIA_A, "US 708"], "US 708"),
    ],
)
def test_lettered_name_uses_local_labels_only_as_fallback(identifiers, expected):
    p = paper(1, [source_star(identifiers)])
    original = p.science.model_dump()
    assert (
        display_name(
            [(p.key + "#/stars/0", p.science.stars[0])],
            SimbadSnapshot(status="not_queried"),
            CATALOG_ID,
        )
        == expected
    )
    assert p.science.model_dump() == original


def test_equal_length_names_count_distinct_papers_then_use_lexical_order():
    a = paper(1, [source_star(["HD 1"])] * 3)
    b = paper(2, [source_star(["HD 9"])])
    c = paper(3, [source_star(["HD 9"])])
    entries = [(a.key + f"#/stars/{i}", star) for i, star in enumerate(a.science.stars)]
    entries += [(b.key, b.science.stars[0])]
    snapshot = SimbadSnapshot(status="not_queried")
    assert display_name(entries, snapshot, CATALOG_ID) == "HD 1"
    entries += [(c.key, c.science.stars[0])]
    assert display_name(entries, snapshot, CATALOG_ID) == "HD 9"
    assert display_name(list(reversed(entries)), snapshot, CATALOG_ID) == "HD 9"


def test_local_name_qualifier_is_a_deterministic_source_paper():
    star = paper(1, [source_star(["ID 12"])]).science.stars[0]
    entries = [
        ("astro-ph/9901002v2#/stars/0", star),
        ("astro-ph/9901001v3#/stars/2", star),
    ]
    assert display_name(entries, SimbadSnapshot(status="not_queried"), CATALOG_ID) == (
        "ID 12 (astro-ph/9901001)"
    )


def test_conflicting_external_candidates_use_paper_name():
    p = paper(1, [source_star(["HD 1", "HIP 2"])])
    snapshot = SimbadSnapshot(
        status="conflict",
        candidates=[candidate(name).model_dump() for name in ("HD 100", "HIP 200")],
    )
    assert display_name([(p.key, p.science.stars[0])], snapshot, CATALOG_ID) == "HD 1"
    assert snapshot.selected is None


def test_duplicate_paper_labels_do_not_become_identity_keys(build):
    a = paper(1, [source_star(["HVS2"])])
    b = paper(2, [source_star(["HVS2"])])
    path, manifest, stars = build([a, b])
    assert identifier_key("HVS2") is None
    assert len(stars) == 2
    assert [s["display_name"] for s in stars] == ["HVS2", "HVS2"]
    assert [s.catalog_id for s in manifest.stars] == [CATALOG_ID, "hvs-catalog-000002"]
    assert [list(s["sources"]) for s in stars] == [[a.key], [b.key]]
    verify_catalog(path)


def test_display_name_must_still_agree_with_index(build):
    path, _, _ = build([paper(1, [source_star(["HVS2"])])])
    raw = json.loads((path / "stars" / f"{CATALOG_ID}.json").read_bytes())
    raw["display_name"] = "HVS3"
    test_catalog.replace_star(path, raw)
    with pytest.raises(ValueError, match="Star index differs"):
        verify_catalog(path)

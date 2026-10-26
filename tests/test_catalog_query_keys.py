"""Query spellings preserve scientific identifiers and conservative association."""

import json

import pytest
import test_catalog
from test_catalog import candidate, freeze, paper, source_star
from test_catalog_review import decision, paper_review, report, review

from hvs_catalog.catalogue.identity import identifier_key, sdss_query_identifier
from hvs_catalog.catalogue.publication import check_release, verify_catalog
from hvs_catalog.catalogue.simbad import query_key
from hvs_catalog.records.catalog import SimbadQuery

build = test_catalog.build
no_external_network = test_catalog.no_external_network


@pytest.mark.parametrize(
    "original,expected",
    [
        ("SDSSJ123456.789+123456.98", "SDSS J123456.78+123456.9"),
        ("SDSS J123456.78−123456.98", "SDSS J123456.78-123456.9"),
        ("SDSS J123456.78+123456.9", "SDSS J123456.78+123456.9"),
        ("SDSS J1234+1234", None),
        ("J123456.789+123456.98", None),
        ("SDSS J123456+123456", None),
    ],
)
def test_sdss_query_spelling_truncates_complete_names_only(original, expected):
    assert sdss_query_identifier(original) == expected


@pytest.mark.parametrize(
    "original,expected",
    [
        ("PSR B1234+56", "PSR B1234+56"),
        ("psr J1234-5678", "PSR J1234-5678"),
        ("RX J1234−5678", "RX J1234-5678"),
        ("B1234+56", None),
        ("J1234-5678", None),
        ("PSR 1234", None),
        ("RX B1234+5678", None),
    ],
)
def test_only_qualified_pulsar_and_xray_names_are_identifiers(original, expected):
    assert identifier_key(original) == expected


def test_derived_query_matches_without_merging_and_preserves_trace(build, tmp_path):
    original = "SDSS J123456.789+123456.98"
    derived = "SDSS J123456.78+123456.9"
    a, b = paper(1, [source_star([original])]), paper(2, [source_star([derived])])
    folder, _ = freeze(
        tmp_path / "frozen",
        [(p.key, p.science.stars[0]) for p in [a, b]],
        lambda q: [candidate("HD 100")] if q.identifier == derived else [],
    )
    assert len(list(folder.glob("*.json"))) == 2
    root, _, stars = build(
        [a, b], simbad=folder, review=review(papers=[paper_review(a), paper_review(b)])
    )
    assert len(stars) == 2
    assert stars[0]["identifiers"][0]["value"] == original
    assert stars[0]["simbad"]["selected"]["main_id"] == "HD 100"
    query = next(
        q for q in stars[0]["simbad"]["queries"] if q["query"]["identifier"] == derived
    )
    assert query["identifier_inputs"] == [
        {
            "source_record": a.key + "#/stars/0",
            "original_identifier": a.science.stars[0]
            .identifiers[0]
            .model_dump(mode="json"),
            "derivation": "sdss_standard_truncation",
        }
    ]
    items = [i for i in report(root)["items"] if i["kind"] == "identity"]
    assert len(items) == 1 and items[0]["status"] == "pending"
    assert {r["kind"] for r in items[0]["reasons"]} == {"same_simbad_object"}
    with pytest.raises(ValueError, match="pending"):
        check_release(report(root))
    verify_catalog(root)


def test_original_and_derived_identifier_conflict_is_retained(build, tmp_path):
    a = paper(1, [source_star(["SDSS J123456.789+123456.98"])])
    folder, _ = freeze(
        tmp_path / "frozen",
        [(a.key, a.science.stars[0])],
        lambda q: [candidate("HD 100" if q.identifier.endswith(".98") else "HD 200")],
    )
    root, _, stars = build([a], simbad=folder)
    assert stars[0]["simbad"]["status"] == "conflict"
    assert any(i["kind"] == "simbad" for i in report(root)["items"])


@pytest.mark.parametrize(
    "left_method,right_method",
    [
        ("identifier", "coordinates"),
        ("coordinates", "coordinates"),
        ("identifier", "identifier"),
    ],
)
def test_same_counterpart_queue_ignores_matching_method(
    build, tmp_path, left_method, right_method
):
    papers = [
        paper(
            i,
            [
                source_star(
                    [f"HD {i}" if method == "identifier" else f"Local {i}"],
                    10 + (i - 1) * 2 / 3600,
                )
            ],
        )
        for i, method in enumerate([left_method, right_method], 1)
    ]
    folder, _ = freeze(
        tmp_path / "frozen",
        [(p.key, p.science.stars[0]) for p in papers],
        lambda q: [candidate("HD 100", ra=10 + 1 / 3600)],
    )
    # Explicit separation prevents the otherwise permitted same-ID automatic merge.
    approved = review(
        [decision("separate", papers, unresolved=True)],
        [paper_review(p) for p in papers],
    )
    root, _, stars = build(papers, simbad=folder, review=approved)
    assert len(stars) == 2
    item = next(i for i in report(root)["items"] if i["kind"] == "identity")
    assert any(r["kind"] == "same_simbad_object" for r in item["reasons"])
    assert item["status"] == "unresolved"
    check_release(report(root))


@pytest.mark.parametrize("arcsec,expected", [(0.9, False), (1.0, False), (1.1, True)])
def test_coordinate_distance_review_boundary(build, tmp_path, arcsec, expected):
    a = paper(1, [source_star(["Local A"], 10)])
    folder, _ = freeze(
        tmp_path / "frozen",
        [(a.key, a.science.stars[0])],
        lambda q: [candidate("HD 100", ra=10 + arcsec / 3600)],
    )
    root, _, _ = build([a], simbad=folder, review=review(papers=[paper_review(a)]))
    items = [i for i in report(root)["items"] if i["kind"] == "simbad"]
    assert bool(items) == expected
    if expected:
        assert items[0]["status"] == "pending"
        with pytest.raises(ValueError, match="pending"):
            check_release(report(root))
    else:
        check_release(report(root))


@pytest.mark.parametrize("main_id", ["HD 100", None])
def test_approved_distance_selection_or_revocation_clears_pending(
    build, tmp_path, main_id
):
    a = paper(1, [source_star(["Local A"], 10)])
    folder, _ = freeze(
        tmp_path / "frozen",
        [(a.key, a.science.stars[0])],
        lambda q: [candidate("HD 100", ra=10 + 2 / 3600)],
    )
    query = SimbadQuery(
        method="coordinates", position={"ra_deg": 10, "dec_deg": 0, "frame": "icrs"}
    )
    payload = json.loads((folder / (query_key(query) + ".json")).read_text())
    approved = review(
        [
            decision(
                "simbad",
                [a],
                main_id=main_id,
                responses={query_key(query): payload["sha256"]},
            )
        ],
        [paper_review(a)],
    )
    root, _, stars = build([a], simbad=folder, review=approved)
    assert report(root)["counts"]["pending"] == 0
    assert stars[0]["simbad"]["status"] == ("matched" if main_id else "not_found")
    check_release(report(root))

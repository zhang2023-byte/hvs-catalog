"""Bound human decisions and release gates use fictional, independently fixed inputs."""

import json

import pytest
import test_catalog
from test_catalog import GAIA_A, GAIA_B, candidate, freeze, paper, source_star

from hvs_catalog.catalogue.publication import check_release, verify_catalog
from hvs_catalog.catalogue.simbad import query_key
from hvs_catalog.catalogue.units import standard_value
from hvs_catalog.records.catalog import SimbadQuery
from hvs_catalog.records.catalog_review import CatalogReview

build = test_catalog.build
no_external_network = test_catalog.no_external_network


def decision(kind, papers, *, groups=None, **fields):
    return {
        "id": kind + "-example",
        "type": kind,
        "groups": groups or [[p.key + "#/stars/0"] for p in papers],
        "papers": {p.key: p.source.result_sha256 for p in papers},
        "reason": "Explicit fictional correspondence.",
        "evidence": [
            {
                "paper": papers[0].key,
                "location": "T:p1-b0",
                "quote": "Fictional objects A and B refer to the same star.",
            }
        ],
        "approved_by": "Synthetic reviewer",
        "approved_on": "2026-10-25",
        **fields,
    }


def paper_review(p, exceptions=()):
    return {
        "paper": p.key,
        "science_sha256": p.source.result_sha256,
        "reviewed": True,
        "reason": "Fictional source checked.",
        "approved_by": "Synthetic reviewer",
        "approved_on": "2026-10-25",
        "exceptions": list(exceptions),
    }


def review(decisions=(), papers=()):
    return CatalogReview.model_validate(
        {"decisions": list(decisions), "papers": list(papers)}
    )


def report(root):
    """The review queue written beside a test catalog, outside the published files."""
    return json.loads(root.with_name(root.name + ".queue.json").read_bytes())


def test_empty_review_preserves_conservative_groups_and_exact_science(build):
    a, b = paper(1, [source_star(["HVS1"])]), paper(2, [source_star(["hvs-1"])])
    root, manifest, stars = build([a, b], review=review())
    assert len(stars) == 2
    assert [
        s["sources"][p.key]["objects"][0]["result_index"]
        for s, p in zip(stars, (a, b), strict=True)
    ] == [0, 0]
    for s, p in zip(stars, (a, b), strict=True):
        value = s["quantities"][0]["values"][0]
        assert {
            k: v
            for k, v in value.items()
            if k not in {"source", "attribution", "standard"}
        } == p.science.stars[0].quantities[0].values[0].model_dump(mode="json")
        assert value["attribution"] == "own"
        assert (
            root / manifest.papers[p.key].science.path
        ).read_bytes() == p.science_bytes
    assert report(root)["counts"]["pending"] == 3
    assert not (root / "review-report.json").exists()
    with pytest.raises(ValueError, match="pending"):
        check_release(report(root))


def test_release_build_refuses_pending_items_and_keeps_queue_outside(
    monkeypatch, tmp_path
):
    from hvs_catalog.catalogue.inputs import json_bytes
    from hvs_catalog.cli import main
    from hvs_catalog.records.catalog import CatalogSelection

    a, b = paper(1, [source_star(["HVS1"])]), paper(2, [source_star(["hvs-1"])])
    monkeypatch.setattr("hvs_catalog.catalogue.build.load_papers", lambda s: [a, b])
    spec = tmp_path / "selection.json"
    spec.write_bytes(json_bytes(CatalogSelection(data_version="synthetic", papers=[])))
    output = tmp_path / "catalog"
    command = ["catalog", "build", "--selection", str(spec), "--output", str(output)]
    assert main([*command, "--release"]) == 2
    assert not output.exists()
    queue = tmp_path / "catalog.queue.json"
    assert json.loads(queue.read_bytes())["counts"]["pending"] == 3
    assert main(command) == 0
    assert verify_catalog(output) and not (output / "review-report.json").exists()
    nested = tmp_path / "new" / "date" / "catalog"
    assert main([*command[:-1], str(nested)]) == 0
    assert (tmp_path / "new" / "date" / "catalog.queue.json").exists()
    other = tmp_path / "other"
    inside = ["--queue", str(other / "queue.json")]
    assert main([*command[:-1], str(other), *inside]) == 2
    assert not other.exists()


@pytest.mark.parametrize("attribution", ["cited", "mismatch"])
def test_merge_name_value_reviews_and_reproducible_release(build, attribution):
    a, b = paper(1, [source_star(["HVS1"])]), paper(2, [source_star(["hvs-1"])])
    nodes = [a.key + "#/stars/0", b.key + "#/stars/0"]
    approved = review(
        [
            decision("merge", [a, b]),
            decision("name", [a, b], groups=[nodes], display_name="Chosen name"),
        ],
        [
            paper_review(a),
            paper_review(
                b,
                [
                    {
                        "path": "/stars/0/quantities/0/values/0",
                        "attribution": attribution,
                        "reason": "Explicit quotation.",
                    }
                ],
            ),
        ],
    )
    root, _, stars = build([a, b], review=approved)
    assert len(stars) == 1 and stars[0]["display_name"] == "Chosen name"
    assert [v["attribution"] for v in stars[0]["quantities"][0]["values"]] == [
        "own",
        attribution,
    ]
    marked = stars[0]["quantities"][0]["values"][1]
    assert {
        k: v
        for k, v in marked.items()
        if k not in {"source", "attribution", "attribution_reason", "standard"}
    } == b.science.stars[0].quantities[0].values[0].model_dump(mode="json")
    assert {d["id"] for d in stars[0]["review_decisions"]} == {
        "merge-example",
        "name-example",
    }
    check_release(report(root))
    second, _, _ = build([b, a], "repeat", review=approved)
    assert {
        str(p.relative_to(root)): p.read_bytes() for p in root.rglob("*") if p.is_file()
    } == {
        str(p.relative_to(second)): p.read_bytes()
        for p in second.rglob("*")
        if p.is_file()
    }


def test_separate_blocks_automatic_and_transitive_merges_and_unresolved_can_release(
    build,
):
    a, b, c = [paper(i, [source_star([GAIA_A])]) for i in (1, 2, 3)]
    approved = review(
        [decision("separate", [a, b], unresolved=True)],
        [paper_review(p) for p in (a, b, c)],
    )
    root, _, stars = build([a, b, c], review=approved)
    assert len(stars) == 2
    assert not any(a.key in s["sources"] and b.key in s["sources"] for s in stars)
    assert report(root)["counts"]["unresolved"] == 1
    check_release(report(root))


@pytest.mark.parametrize("change", ["hash", "version"])
def test_changed_paper_invalidates_only_affected_decisions(build, change):
    a, b = paper(1, [source_star(["HVS1"])]), paper(2, [source_star(["HVS1"])])
    approved = review([decision("merge", [a, b])], [paper_review(a), paper_review(b)])
    changed = paper(
        1,
        [source_star(["HVS1"], velocity="521")],
        version="v2" if change == "version" else "v1",
    )
    root, _, stars = build([changed, b], review=approved)
    assert len(stars) == 2
    unreviewed = {
        i["id"] for i in report(root)["items"] if i["kind"] == "unreviewed_paper"
    }
    assert unreviewed == {changed.key}
    assert any(
        i["id"] == "merge-example" and i["status"] == "pending"
        for i in report(root)["items"]
    )


def test_conflicting_merge_and_name_decisions_are_rejected(build):
    a, b = paper(1, [source_star([GAIA_A])]), paper(2, [source_star([GAIA_B])])
    with pytest.raises(ValueError, match="merge conflicts"):
        build([a, b], review=review([decision("merge", [a, b])]))
    name = decision("name", [a], display_name="First")
    with pytest.raises(ValueError, match="Conflicting review"):
        build(
            [a],
            "names",
            review=review([name, {**name, "id": "another", "display_name": "Second"}]),
        )


def test_simbad_selection_revocation_and_response_invalidation(build, tmp_path):
    a = paper(1, [source_star(["HD 1"])])
    query = SimbadQuery(method="identifier", identifier="HD 1")
    folder = tmp_path / "frozen"
    folder, _ = freeze(
        folder,
        [(a.key, a.science.stars[0])],
        lambda q: [candidate("HD 100"), candidate("HD 200")],
    )
    payload = json.loads((folder / (query_key(query) + ".json")).read_bytes())
    d = decision(
        "simbad", [a], main_id="HD 100", responses={query_key(query): payload["sha256"]}
    )
    root, _, stars = build([a], simbad=folder, review=review([d], [paper_review(a)]))
    assert stars[0]["simbad"]["selected"]["main_id"] == "HD 100"
    assert stars[0]["simbad"]["method"] == "review"
    check_release(report(root))
    _, _, revoked = build(
        [a], "revoked", simbad=folder, review=review([{**d, "main_id": None}])
    )
    assert revoked[0]["simbad"]["selected"] is None
    unresolved, _, unresolved_stars = build(
        [a],
        "unresolved",
        simbad=folder,
        review=review([{**d, "main_id": None, "unresolved": True}], [paper_review(a)]),
    )
    assert unresolved_stars[0]["simbad"]["status"] == "ambiguous"
    assert report(unresolved)["counts"]["unresolved"] == 1
    check_release(report(unresolved))
    fresh = tmp_path / "fresh"
    fresh, _ = freeze(
        fresh, [(a.key, a.science.stars[0])], lambda q: [candidate("HD 200")]
    )
    changed, _, stars = build([a], "changed", simbad=fresh, review=review([d]))
    assert stars[0]["simbad"]["selected"]["main_id"] == "HD 200"
    assert any(i["id"] == "simbad-example" for i in report(changed)["items"])


@pytest.mark.parametrize(
    "quantity,source,expected",
    [
        (
            "ra",
            {"value": "12h30m00s", "unit": "hourangle"},
            {"value": "187.5", "unit": "deg"},
        ),
        (
            "dec",
            {"value": "-00d30m00s", "unit": "deg"},
            {"value": "-0.5", "unit": "deg"},
        ),
        (
            "ra",
            {"value": "120.5000", "error": "3600", "unit": "deg ± mas"},
            {"value": "120.5000", "error": "0.001", "unit": "deg"},
        ),
        (
            "radial_velocity",
            {"value": "500", "unit": "km s<sup>−1</sup>"},
            {"value": "500", "unit": "km/s"},
        ),
        (
            "radial_velocity",
            {"value": "~1200.0", "error": "20", "unit": "m s^{-1}"},
            {"value": "1.2000", "error": "0.020", "unit": "km/s", "approximate": True},
        ),
        (
            "distance",
            {"lower_bound": "1200", "lower_inclusive": False, "unit": "pc"},
            {"lower_bound": "1.200", "lower_inclusive": False, "unit": "kpc"},
        ),
        (
            "parallax",
            {"value": "0.0010", "unit": "arcsec"},
            {"value": "1.0000", "unit": "mas"},
        ),
        (
            "proper_motion_ra",
            {
                "value": "2",
                "lower_error": "-1",
                "upper_error": "+3",
                "unit": "mas yr^-1",
            },
            {"value": "2", "lower_error": "-1", "upper_error": "3", "unit": "mas/yr"},
        ),
        (
            "unbound_probability",
            {"value": "90", "unit": "%"},
            {"value": "0.90", "unit": "1"},
        ),
        ("ra", {"value": "12:00:00", "unit": "hms"}, {"value": "180", "unit": "deg"}),
        (
            "dec",
            {"value": "-00:30:00", "unit": "dms"},
            {"value": "-0.5", "unit": "deg"},
        ),
    ],
)
def test_standard_units_preserve_all_original_fields(quantity, source, expected):
    original = source.copy()
    result = standard_value(quantity, source).model_dump()
    assert all(result[k] == v for k, v in expected.items())
    assert source == original


@pytest.mark.parametrize(
    "quantity,source",
    [
        ("distance", {"value": "10", "unit": "mag"}),
        ("radial_velocity", {"value": "100 to 200", "unit": "km/s"}),
        ("distance", {"value": "10", "error": "unknown", "unit": "kpc"}),
        ("distance", {"value": "10"}),
        ("ra", {"value": "12:61:00", "unit": "hms"}),
    ],
)
def test_ambiguous_values_have_no_partial_derivation(quantity, source):
    assert standard_value(quantity, source) is None


def test_standard_value_tampering_is_detected(build):
    root, _, _ = build([paper(1, [source_star(["HVS1"])])])
    raw = json.loads((root / "stars/hvs-catalog-000001.json").read_bytes())
    raw["quantities"][0]["values"][0]["standard"]["value"] = "999"
    test_catalog.replace_star(root, raw)
    with pytest.raises(ValueError, match="Standard-unit"):
        verify_catalog(root)


def test_query_plan_uses_reviewed_groups_and_binds_review_input(build, tmp_path):
    from test_catalog_review_input import authoring, entry

    from hvs_catalog.catalogue.inputs import json_bytes
    from hvs_catalog.catalogue.review_input import fingerprint, load_review
    from hvs_catalog.cli import main
    from hvs_catalog.records.catalog import CatalogSelection

    a, b = paper(1, [source_star(["HD 1"])]), paper(2, [source_star(["HIP 2"])])
    build([a, b])
    spec, source, output = (
        tmp_path / n for n in ("selection.json", "review", "plan.json")
    )
    spec.write_bytes(json_bytes(CatalogSelection(data_version="synthetic", papers=[])))
    authoring(source, [a, b], identity=[entry("merge", [a, b])])
    assert (
        main(
            [
                "catalog",
                "query-plan",
                "--selection",
                str(spec),
                "--review",
                str(source),
                "--output",
                str(output),
            ]
        )
        == 0
    )
    plan = json.loads(output.read_bytes())
    assert plan["schema_version"] == 2
    assert [o["records"] for o in plan["objects"]] == [
        [a.key + "#/stars/0", b.key + "#/stars/0"]
    ]
    assert {q["identifier"] for q in plan["queries"]} == {"HD 1", "HIP 2"}
    assert plan["review_fingerprint"] == fingerprint(load_review(source))
    single = tmp_path / "review.json"
    single.write_bytes(json_bytes(review([decision("merge", [a, b])])))
    assert (
        main(
            ["catalog", "query-plan", "--selection", str(spec), "--review", str(single)]
            + ["--output", str(tmp_path / "other.json")]
        )
        == 2
    )


def correction(p, path, original, replacement, *, number=1):
    return {
        "id": f"correct-{number}",
        "type": "correct",
        "paper": p.key,
        "science_sha256": p.source.result_sha256,
        "pdf_sha256": p.source.pdf_sha256,
        "path": path,
        "original": original,
        "replacement": replacement,
        "evidence": [
            {
                "page": 2,
                "location": "Table 1, row A",
                "quote": "Fictional row: HD 2; 620 +/- 12 km/s.",
            }
        ],
        "reason": "Fictional transcription error confirmed in fixed PDF.",
        "approved_by": "Synthetic reviewer",
        "approved_on": "2026-10-25",
    }


def test_correction_precedes_association_and_preserves_original_paper(build):
    a, b = paper(1, [source_star(["HD 1"])]), paper(2, [source_star(["HD 2"])])
    original = a.science.stars[0].identifiers[0].model_dump(mode="json")
    d = correction(a, "/stars/0/identifiers/0", original, {**original, "value": "HD 2"})
    root, manifest, stars = build([a, b], review=review([d]))
    assert len(stars) == 1
    assert [v["value"] for v in stars[0]["identifiers"]] == ["HD 2", "HD 2"]
    assert stars[0]["review_decisions"][0]["original"] == original
    assert (root / manifest.papers[a.key].science.path).read_bytes() == a.science_bytes
    changed = paper(1, [source_star(["HD 1"], velocity="521")])
    root, _, stars = build([changed, b], "changed", review=review([d]))
    assert len(stars) == 2
    assert any(
        i["id"] == d["id"] and i["status"] == "pending" for i in report(root)["items"]
    )


def test_coordinate_correction_changes_matching_and_replaces_value_with_error(build):
    a, b = (
        paper(1, [source_star(["Object A"], 30)]),
        paper(2, [source_star(["Object B"], 60)]),
    )
    coordinate = b.science.stars[0].quantities[1].values[0].model_dump(mode="json")
    velocity = b.science.stars[0].quantities[0].values[0].model_dump(mode="json")
    ds = [
        correction(
            b,
            "/stars/0/quantities/1/values/0",
            coordinate,
            {**coordinate, "value": "30"},
        ),
        correction(
            b,
            "/stars/0/quantities/0/values/0",
            velocity,
            {**velocity, "value": "620", "error": "12"},
            number=2,
        ),
    ]
    root, _, stars = build([a, b], review=review(ds))
    assert len(stars) == 1
    assert [(v["value"], v["error"]) for v in stars[0]["quantities"][0]["values"]] == [
        ("520", "10"),
        ("620", "12"),
    ]
    assert len(stars[0]["review_decisions"]) == 2
    verify_catalog(root)
    with pytest.raises(ValueError, match="Conflicting corrections"):
        build([a, b], "duplicate", review=review([ds[0], {**ds[0], "id": "other"}]))
    with pytest.raises(ValueError, match="original differs"):
        build(
            [b],
            "wrong-original",
            review=review([{**ds[1], "original": {**velocity, "value": "999"}}]),
        )


def test_withdrawal_keeps_exception_paths_and_original_in_provenance(build):
    raw = source_star(["HD 1"])
    raw["quantities"][0]["values"].append(
        {**raw["quantities"][0]["values"][0], "value": "620"}
    )
    p = paper(1, [raw])
    original = p.science.stars[0].quantities[0].values[0].model_dump(mode="json")
    d = correction(p, "/stars/0/quantities/0/values/0", original, None)
    approved = review(
        [d],
        [
            paper_review(
                p,
                [
                    {
                        "path": "/stars/0/quantities/0/values/1",
                        "attribution": "cited",
                        "reason": "Other paper result.",
                    }
                ],
            )
        ],
    )
    root, _, stars = build([p], review=approved)
    assert [
        (v["value"], v["attribution"]) for v in stars[0]["quantities"][0]["values"]
    ] == [("620", "cited")]
    assert stars[0]["review_decisions"][0]["original"] == original
    assert stars[0]["review_decisions"][0]["replacement"] is None
    check_release(report(root))
    repeat, _, _ = build([p], "repeat", review=approved)
    assert {
        f.relative_to(root): f.read_bytes() for f in root.rglob("*") if f.is_file()
    } == {
        f.relative_to(repeat): f.read_bytes() for f in repeat.rglob("*") if f.is_file()
    }


def test_display_aliases_are_not_original_identifiers(build):
    p = paper(1, [source_star(["HD 1", "HVS1"])])
    d = decision("name", [p], display_name="HD 1", aliases=["HVS1 (2601.00001)"])
    root, _, stars = build([p], review=review([d]))
    assert stars[0]["aliases"] == ["HVS1 (2601.00001)"]
    assert len(stars[0]["identifiers"]) == 2
    verify_catalog(root)

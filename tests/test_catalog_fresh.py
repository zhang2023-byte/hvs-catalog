"""Fresh construction uses only selected source evidence and frozen responses."""

import copy
import json

import pytest
import test_catalog
from test_catalog import candidate, freeze, paper, source_star

from hvs_catalog.catalogue.association import Node
from hvs_catalog.catalogue.evidence_conflicts import source_conflicts
from hvs_catalog.catalogue.inputs import json_bytes
from hvs_catalog.records.catalog import CatalogSelection

build = test_catalog.build
no_external_network = test_catalog.no_external_network


def row(number, extracted_ra=30, *, evidence_ra="02h00m00s", html=True):
    star = source_star([f"ID {number}"], extracted_ra, -4)
    quote = (
        f"<td>{number}</td><td>{evidence_ra}</td><td>-04d00m00s</td>"
        if html
        else f"{number} {evidence_ra} -04d00m00s"
    )
    star["identifiers"][0]["evidence"][0]["quote"] = quote
    return star


@pytest.mark.parametrize("html", [True, False])
@pytest.mark.parametrize("bad_row", [0, 1])
def test_conflicts_follow_evidence_without_paper_or_row_configuration(
    build, html, bad_row
):
    rows = [row(7, html=html), row(91, html=html)]
    rows[bad_row]["identifiers"][0]["evidence"][0]["quote"] = rows[bad_row][
        "identifiers"
    ][0]["evidence"][0]["quote"].replace("02h", "03h")
    p = paper(404, rows)
    before = p.science.model_dump(mode="json")
    _, _, stars = build([p])
    assert len(stars) == 2
    for star in stars:
        edges = star["associations"]
        assert edges and all(not e["accepted"] for e in edges)
        conflict = next(e for e in edges if e["method"] == "paper")
        assert conflict["details"]["identity_conflict"]
        assert len(conflict["details"]["evidence"]) == 2
        assert any("rejected" in w for w in star["warnings"])
    assert p.science.model_dump(mode="json") == before
    assert [s["quantities"][0]["values"][0]["value"] for s in stars] == ["520", "520"]


@pytest.mark.parametrize(
    "bridge_method", ["coordinates", "identifier", "paper", "simbad"]
)
def test_automatic_conflicts_cannot_be_bypassed_transitively(
    build, tmp_path, bridge_method
):
    rows = [row(7), row(91, evidence_ra="03h00m00s")]
    bridge = source_star(["HD 501"], 30.0001, -4)
    if bridge_method in {"identifier", "simbad"}:
        for raw, identifier in zip(rows, ["HD 501", "HIP 502"], strict=True):
            raw["identifiers"].append(
                {"value": identifier, "evidence": copy.deepcopy(test_catalog.EVIDENCE)}
            )
    if bridge_method == "identifier":
        bridge["identifiers"].append(
            {"value": "HIP 502", "evidence": copy.deepcopy(test_catalog.EVIDENCE)}
        )
    p, b = paper(404, rows), paper(405, [bridge])
    options = {}
    if bridge_method == "paper":
        options["explicit_associations"] = [
            {
                "left": f"{p.key}#/stars/{i}",
                "right": b.key + "#/stars/0",
                "source": p.key,
                "evidence": test_catalog.EVIDENCE,
            }
            for i in range(2)
        ]
    if bridge_method == "simbad":
        folder, _ = freeze(
            tmp_path,
            [(f"{p.key}#/stars/{i}", s) for i, s in enumerate(p.science.stars)],
            lambda _: [candidate("HD 700", ra=30)],
        )
        options["simbad"] = folder
    _, _, stars = build([p, b], **options)
    assert len(stars) == 2
    assert all(len(s["sources"][p.key]["objects"]) == 1 for s in stars)
    assert any(
        not e["accepted"] and e["method"] == bridge_method
        for s in stars
        for e in s["associations"]
    )


@pytest.mark.parametrize(
    "case",
    [
        "other_row",
        "multiple_pairs",
        "image",
        "no_pair",
        "same_position",
        "far_extraction",
        "different_paper",
        "unbound_prefix",
        "invalid_ra",
        "invalid_dec",
        "unsupported_frame",
        "contradictory_frame",
    ],
)
def test_incomplete_or_unrelated_identity_evidence_does_not_invent_conflicts(case):
    a, b = row(7), row(91, evidence_ra="03h00m00s")
    evidence = b["identifiers"][0]["evidence"][0]
    if case == "other_row":
        evidence["quote"] = evidence["quote"].replace("91", "92")
    elif case == "multiple_pairs":
        evidence["quote"] += " 93 04h00m00s -05d00m00s"
    elif case == "image":
        b["identifiers"][0]["evidence"] = [{"location": "I:p1-b0"}]
    elif case == "no_pair":
        evidence["quote"] = "91 has a large radial velocity."
    elif case == "same_position":
        b = row(91)
    elif case == "far_extraction":
        b = row(91, 45, evidence_ra="03h00m00s")
    elif case == "unbound_prefix":
        evidence["quote"] = "Other objects nearby: " + evidence["quote"]
    elif case == "invalid_ra":
        evidence["quote"] = evidence["quote"].replace("03h", "99h")
    elif case == "invalid_dec":
        evidence["quote"] = evidence["quote"].replace("-04d", "-94d")
    elif case == "unsupported_frame":
        evidence["quote"] += " equinox B1900"
    elif case == "contradictory_frame":
        evidence["quote"] += " ICRS FK4 B1950"
    pa, pb = paper(404, [a]), paper(405 if case == "different_paper" else 404, [b])
    assert (
        source_conflicts(
            [
                Node(pa.key + "#/stars/0", pa.science.stars[0]),
                Node(pb.key + "#/stars/1", pb.science.stars[0]),
            ]
        )
        == []
    )


@pytest.mark.parametrize(
    "coordinate_quote",
    ["91 03:00:00 -04:00:00", "91 03h00m00s −04d00m00s", "ID 91 03h00m00s -04d00m00s"],
)
def test_identity_evidence_coordinate_typography(coordinate_quote):
    a, b = row(7), row(91)
    b["identifiers"][0]["evidence"][0]["quote"] = coordinate_quote
    p = paper(404, [a, b])
    assert (
        len(
            source_conflicts(
                [Node(f"{p.key}#/stars/{i}", s) for i, s in enumerate(p.science.stars)]
            )
        )
        == 1
    )


def test_missing_frozen_coordinate_response_is_not_not_found(build, tmp_path):
    identifier_only = paper(1, [source_star(["HD 1"])])
    folder, _ = freeze(
        tmp_path,
        [(identifier_only.key, identifier_only.science.stars[0])],
        lambda _: [],
    )
    _, _, first = build([identifier_only], simbad=folder)
    assert first[0]["simbad"]["status"] == "not_found"
    with_coordinates = paper(1, [source_star(["HD 1"], 30)])
    _, _, rebuilt = build([with_coordinates], "fresh", simbad=folder)
    assert rebuilt[0]["simbad"]["status"] == "not_queried"
    assert any(q["response_sha256"] is None for q in rebuilt[0]["simbad"]["queries"])
    assert (
        rebuilt[0]["quantities"][0]["values"][0]
        == first[0]["quantities"][0]["values"][0]
    )


def test_complete_selection_and_query_plan_are_independent_of_old_outputs(
    build, tmp_path
):
    from hvs_catalog.cli import main

    a, b = paper(1, [source_star(["HD 1"])]), paper(2, [source_star(["HIP 2"])])
    old, _, _ = build([a], "irrelevant-old")
    # An invalid old catalog in the workspace must never be read by a fresh build.
    (old / "manifest.json").write_text("not a catalog")
    new, _, _ = build([b, a], "fresh")
    repeat, _, _ = build([a, b], "repeat")
    assert {
        p.relative_to(new): p.read_bytes() for p in new.rglob("*") if p.is_file()
    } == {
        p.relative_to(repeat): p.read_bytes() for p in repeat.rglob("*") if p.is_file()
    }
    spec, plan = tmp_path / "selection.json", tmp_path / "plan.json"
    spec.write_bytes(json_bytes(CatalogSelection(data_version="synthetic", papers=[])))
    assert (
        main(["catalog", "query-plan", "--selection", str(spec), "--output", str(plan)])
        == 0
    )
    assert {q["identifier"] for q in json.loads(plan.read_bytes())["queries"]} == {
        "HD 1",
        "HIP 2",
    }


@pytest.mark.parametrize(
    "retired_option",
    ["association_conflicts", "identity_assignments", "retired_ids", "simbad_refresh"],
)
def test_retired_build_configuration_is_rejected_instead_of_silently_affecting_identity(
    retired_option,
):
    with pytest.raises(ValueError, match="Extra inputs"):
        CatalogSelection.model_validate(
            {"data_version": "fresh", "papers": [], retired_option: []}
        )

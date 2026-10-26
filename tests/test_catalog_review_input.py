"""The human-edited review directory, its bindings, correction markers and comparisons."""

import json

import pytest
import test_catalog
from test_catalog import candidate, freeze, paper, source_star
from test_catalog_review import correction, decision, paper_review, report, review

from hvs_catalog.catalogue.compare import compare_catalogs
from hvs_catalog.catalogue.inputs import json_bytes
from hvs_catalog.catalogue.publication import check_release, verify_catalog
from hvs_catalog.catalogue.review_input import format_json
from hvs_catalog.catalogue.simbad import query_key
from hvs_catalog.cli import main
from hvs_catalog.records.catalog import CatalogSelection, SimbadQuery

build = test_catalog.build
no_external_network = test_catalog.no_external_network
APPROVED = "2026-10-25 Synthetic reviewer"
EVIDENCE = [{"page": 2, "location": "Table 1, row A", "quote": "Fictional row."}]


def entry(kind, papers, *, groups=None, **fields):
    return {
        "id": kind + "-example",
        "type": kind,
        "groups": groups or [[p.key + "#/stars/0"] for p in papers],
        **fields,
        "reason": "Explicit fictional correspondence.",
        "evidence": [
            {
                "paper": papers[0].key,
                "location": "T:p1-b0",
                "quote": "Fictional objects A and B refer to the same star.",
            }
        ],
        "approved": APPROVED,
    }


def fix(p, path, change, *, number=1, **fields):
    return {
        "id": f"correct-{number}",
        "path": p.key + "#" + path,
        **({"change": change} if change else {}),
        **fields,
        "evidence": EVIDENCE,
        "reason": "Fictional transcription error confirmed in fixed PDF.",
        "approved": APPROVED,
    }


def authoring(root, papers, *, identity=(), corrections=(), exceptions=None, bind=True):
    root.mkdir(parents=True, exist_ok=True)
    corrected = {c["path"].split("#")[0] for c in corrections}
    table = [
        {
            "paper": p.key,
            **({"science_sha256": p.source.result_sha256} if bind else {}),
            **(
                {"pdf_sha256": p.source.pdf_sha256}
                if bind and p.key in corrected
                else {}
            ),
            **(
                {"exceptions": exceptions[p.key]}
                if exceptions and p.key in exceptions
                else {}
            ),
        }
        for p in papers
    ]
    files = {
        "papers.json": {
            "method": "Fictional source checked.",
            "approved": APPROVED,
            "papers": table,
        },
        "identity.json": {"decisions": list(identity)},
        "corrections.json": {"corrections": list(corrections)},
    }
    for name, value in files.items():
        (root / name).write_text(json.dumps({"schema_version": 3, **value}))
    return root


def corrected_case(tmp_path):
    a, b = paper(1, [source_star(["HD 1"])]), paper(2, [source_star(["HD 2"])])
    nodes = [a.key + "#/stars/0", b.key + "#/stars/0"]
    exception = {"path": "/stars/0/quantities/0/values/0", "attribution": "cited"}
    folder = authoring(
        tmp_path / "review",
        [a, b],
        identity=[entry("name", [a, b], groups=[nodes], display_name="Chosen")],
        corrections=[
            fix(b, "/stars/0/identifiers/0", {"value": ["HD 2", "HD 1"]}),
            fix(
                b,
                "/stars/0/quantities/0/values/0",
                {"value": ["520", "620"], "error": ["10", "12"]},
                number=2,
            ),
        ],
        exceptions={a.key: [{**exception, "reason": "Other paper result."}]},
    )
    return a, b, nodes, folder, exception


def test_review_directory_expands_to_the_bound_model_and_marks_corrections(
    build, tmp_path
):
    a, b, nodes, folder, exception = corrected_case(tmp_path)
    identifier = b.science.stars[0].identifiers[0].model_dump(mode="json")
    velocity = b.science.stars[0].quantities[0].values[0].model_dump(mode="json")
    fixes = [
        correction(
            b, "/stars/0/identifiers/0", identifier, {**identifier, "value": "HD 1"}
        ),
        correction(
            b,
            "/stars/0/quantities/0/values/0",
            velocity,
            {**velocity, "value": "620", "error": "12"},
            number=2,
        ),
    ]
    expected = review(
        [
            decision("name", [a, b], groups=[nodes], display_name="Chosen"),
            *({**d, "evidence": EVIDENCE} for d in fixes),
        ],
        [
            paper_review(a, [{**exception, "reason": "Other paper result."}]),
            paper_review(b),
        ],
    )
    root, _, stars = build([a, b], review=folder)
    _, _, model_stars = build([a, b], "model", review=expected)
    assert stars == model_stars
    assert len(stars) == 1 and stars[0]["display_name"] == "Chosen"
    marks = {
        (i["source"], i["value"]): i.get("correction") for i in stars[0]["identifiers"]
    }
    assert marks[(a.key, "HD 1")] is None
    assert marks[(b.key, "HD 1")] == {"decision": "correct-1", "evidence": EVIDENCE}
    values = stars[0]["quantities"][0]["values"]
    assert [(v["value"], v["attribution"], "correction" in v) for v in values] == [
        ("520", "cited", False),
        ("620", "own", True),
    ]
    check_release(report(root))
    raw = json.loads((root / "stars/hvs-catalog-000001.json").read_bytes())
    raw["quantities"][0]["values"][1].pop("correction")
    test_catalog.replace_star(root, raw)
    with pytest.raises(ValueError, match="Scientific values"):
        verify_catalog(root)


def test_stale_mismatched_and_unbound_corrections(build, tmp_path):
    raw = source_star(["HD 1"])
    raw["quantities"][0]["values"].append(
        {**raw["quantities"][0]["values"][0], "value": "620"}
    )
    p = paper(1, [raw])
    withdraw = fix(p, "/stars/0/quantities/0/values/0", None, withdraw=True)
    folder = authoring(tmp_path / "w", [p], corrections=[withdraw])
    _, _, stars = build([p], review=folder)
    assert [v["value"] for v in stars[0]["quantities"][0]["values"]] == ["620"]
    changed = paper(1, [source_star(["HD 1"], velocity="521")])
    stale, _, stars = build([changed], "stale", review=tmp_path / "w")
    assert stars[0]["quantities"][0]["values"][0]["value"] == "521"
    assert any(
        i["id"] == "correct-1" and i["status"] == "pending"
        for i in report(stale)["items"]
    )
    wrong = fix(p, "/stars/0/quantities/0/values/0", {"value": ["999", "1"]})
    with pytest.raises(ValueError, match="differs from the fixed extraction"):
        build([p], "wrong", review=authoring(tmp_path / "x", [p], corrections=[wrong]))
    unbound = authoring(tmp_path / "y", [p], corrections=[withdraw], bind=False)
    with pytest.raises(ValueError, match="bind-review"):
        build([p], "unbound", review=unbound)
    (tmp_path / "y/notes.json").write_text("{}")
    with pytest.raises(ValueError, match="Unexpected review file"):
        build([p], "unexpected", review=unbound)


def test_bind_review_fills_only_missing_bindings(build, tmp_path, monkeypatch):
    a, b = paper(1, [source_star(["HD 1"])]), paper(2, [source_star(["HIP 2"])])
    query = SimbadQuery(method="identifier", identifier="HD 1")
    frozen, _ = freeze(
        tmp_path,
        [(a.key, a.science.stars[0])],
        lambda q: [candidate("HD 100"), candidate("HD 200")],
    )
    folder = authoring(
        tmp_path / "review",
        [a],
        identity=[
            entry("simbad", [a], main_id="HD 100"),
            entry("separate", [a, b], unresolved=True),
        ],
        bind=False,
    )
    monkeypatch.setattr("hvs_catalog.catalogue.inputs.load_papers", lambda s: [a, b])
    selection = tmp_path / "selection.json"
    selection.write_bytes(
        json_bytes(CatalogSelection(data_version="synthetic", papers=[]))
    )
    command = ["catalog", "bind-review", str(folder), "--selection", str(selection)]
    assert main(command) == 2
    assert main([*command, "--simbad", str(frozen)]) == 0
    table = json.loads((folder / "papers.json").read_text())["papers"]
    assert table == [
        {"paper": a.key, "science_sha256": a.source.result_sha256},
        {
            "paper": b.key,
            "science_sha256": b.source.result_sha256,
            "values_reviewed": False,
        },
    ]
    identity = json.loads((folder / "identity.json").read_text())["decisions"]
    payload = json.loads((frozen / (query_key(query) + ".json")).read_bytes())
    assert identity[0]["responses"] == {query_key(query): payload["sha256"]}
    root, _, stars = build([a, b], simbad=frozen, review=folder)
    assert stars[0]["simbad"]["selected"]["main_id"] == "HD 100"
    assert any(i["kind"] == "unreviewed_paper" for i in report(root)["items"])
    table[1]["science_sha256"] = "0" * 64
    (folder / "papers.json").write_text(
        json.dumps(
            {**json.loads((folder / "papers.json").read_text()), "papers": table}
        )
    )
    assert main([*command, "--simbad", str(frozen)]) == 0
    assert (
        json.loads((folder / "papers.json").read_text())["papers"][1]["science_sha256"]
        == "0" * 64
    )
    stale, _, _ = build([a, b], "stale", simbad=frozen, review=folder)
    assert any(
        i["id"] == "separate-example" and i["status"] == "pending"
        for i in report(stale)["items"]
    )


def test_compare_reports_regrouping_names_values_and_attributions(build, tmp_path):
    a, b, _, folder, _ = corrected_case(tmp_path)
    old, _, _ = build([a, b], "old")
    new, _, _ = build([a, b], "new", review=folder)
    result = compare_catalogs(old, new)
    assert (
        result["counts"]["old"]["stars"] == 2 and result["counts"]["new"]["stars"] == 1
    )
    assert result["regrouped"] == [
        {
            "new": ["hvs-catalog-000001", "Chosen"],
            "old": [["hvs-catalog-000001", "HD 1"], ["hvs-catalog-000002", "HD 2"]],
            "added_records": [],
        }
    ]
    assert result["content"]["by_paper_quantity"] == {
        b.key + " identifier added": 1,
        b.key + " identifier removed": 1,
        b.key + " radial_velocity added": 1,
        b.key + " radial_velocity removed": 1,
    }
    assert result["attributions"]["by_paper_quantity"] == {
        a.key + " radial_velocity own->cited": 1
    }
    assert result["corrections"] == 2


def test_review_formatting_keeps_records_on_lines_and_round_trips():
    value = {
        "groups": [["a#/stars/0", "b#/stars/0"], ["c#/stars/0"]],
        "evidence": [{"paper": "a", "location": "x" * 120}],
        "approved": "2026-10-25 Reviewer",
    }
    text = format_json(value)
    assert json.loads(text) == value
    assert '  "evidence": [\n    {"paper": "a", "location": "' in text
    assert text.count("\n") == 6

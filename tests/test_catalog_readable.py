"""Readable publication tests inspect actual files, including legacy migration and tampering."""

import json
import shutil
from functools import partial

import pytest
import test_catalog
from test_catalog import EVIDENCE, GAIA_A, candidate, freeze, paper, source_star

from hvs_catalog.catalogue.inputs import json_bytes
from hvs_catalog.catalogue.publication import load_previous, verify_catalog

build = test_catalog.build
no_external_network = test_catalog.no_external_network


def document(path, number=1):
    return json.loads((path / "stars" / f"hvs-catalog-{number:06d}.json").read_bytes())


replace_star = test_catalog.replace_star


def test_public_files_remove_execution_payload_and_pairwise_growth(build):
    papers = [
        paper(i, [source_star([GAIA_A])], notes=["Whole-paper note." * 100])
        for i in range(1, 9)
    ]
    for p in papers:
        p.source.errors = [{"message": "Recovered old failure"}]
    path, _, _ = build(papers)
    raw = document(path)
    assert all(
        set(s) == {"title", "authors", "pdf_url", "records"}
        for s in raw["sources"].values()
    )
    trace = json.loads((path / raw["provenance"]["path"]).read_bytes())
    assert "associations" not in raw and "association_origins" not in raw
    assert trace["associations"] == [
        {
            "method": "identifier",
            "identifier": GAIA_A,
            "nodes": [p.key + "#/stars/0" for p in papers],
        }
    ]
    assert "Whole-paper note" not in json.dumps(raw)
    assert "Recovered old failure" not in json.dumps(raw)
    assert list(raw)[-1] == "warnings"
    release = json.loads((path / "manifest.json").read_bytes())
    assert release["schema_version"] == 4
    assert release["counts"] == {
        "papers": 8,
        "stars": 1,
        "source_records": 8,
        "values": 8,
    }
    assert len((path / "manifest.json").read_bytes()) < 1500
    index = json.loads((path / "papers/index.json").read_bytes())
    assert all(
        "source" not in p and "incomplete" not in p for p in index["papers"].values()
    )
    next_path, _, _ = build(papers, "next")
    assert document(next_path) == raw


def test_publication_versions_preserve_science(build, monkeypatch):
    from hvs_catalog.catalogue import readable

    def legacy(manifest, stars, papers, files):
        files["manifest.json"] = json_bytes(manifest)

    papers = [paper(1, [source_star([GAIA_A], velocity="520.00")]), paper(2, [])]
    with monkeypatch.context() as patch:
        patch.setattr(readable, "export_files", legacy)
        old, old_manifest, _ = build(papers)
    old_raw = document(old)
    assert old_raw["schema_version"] == 1
    new, new_manifest, _ = build(papers, "new")
    raw = document(new)
    for field in (
        "catalog_id",
        "display_name",
        "identifiers",
        "contribution_types",
        "quantities",
    ):
        assert raw[field] == old_raw[field]
    assert new_manifest.next_id == old_manifest.next_id
    assert new_manifest.retired_ids == old_manifest.retired_ids
    for key, entry in old_manifest.papers.items():
        assert (old / entry.science.path).read_bytes() == (
            new / entry.science.path
        ).read_bytes()
        assert raw["sources"].get(key, {}).get("records", []) == [
            f"/stars/{o['result_index']}"
            for o in old_raw["sources"].get(key, {}).get("objects", [])
        ]
    verify_catalog(old)
    verify_catalog(new)


def test_simbad_science_is_readable_and_full_binding_is_delivered(build, tmp_path):
    p = paper(1, [source_star(["Star 1"], 10)])
    folder, _ = freeze(
        tmp_path,
        [(p.key + "#/stars/0", p.science.stars[0])],
        lambda q: [candidate("HD 9", ra=10.0001), candidate("HD 10", ra=10.0002)],
    )
    path, _, _ = build([p], simbad=folder)
    raw = document(path)
    simbad = raw["simbad"]
    assert simbad["selected"]["main_id"] == "HD 9"
    trace = json.loads((path / raw["provenance"]["path"]).read_bytes())
    assert len(trace["simbad"]["bindings"]) == 1
    assert simbad["match"]["coordinates"][0]["candidate_count"] == 2
    assert not any(
        a.get("right", "").startswith("simbad:") for a in trace["associations"]
    )
    assert trace["simbad"]["matches"][0]["candidate"] == {"$ref": "#/simbad/selected"}
    assert len(trace["simbad"]["alternatives"]) == 1
    assert sum("coordinates only" in w for w in raw["warnings"]) == 1
    assert "warnings" not in simbad
    assert '"rvz_radvel"' not in json.dumps(raw)
    assert json.dumps(trace).count('"rvz_radvel"') == 2
    assert simbad["selected"]["velocity"]["radial_velocity"] == {
        "value": 530.0,
        "unit": "km/s",
    }
    verify_catalog(path)


@pytest.mark.parametrize(
    "change", ["missing", "cycle", "binding", "science", "record", "identifier"]
)
def test_semantic_tampering_is_rejected_after_hashes_are_updated(
    build, tmp_path, change
):
    papers = [paper(i, [source_star([GAIA_A], 10)]) for i in (1, 2)]
    folder, _ = freeze(
        tmp_path,
        [(p.key + "#/stars/0", p.science.stars[0]) for p in papers],
        lambda q: [] if q.method == "identifier" else [candidate("HD 9", ra=10.0001)],
    )
    path, _, _ = build(papers, simbad=folder)
    raw = document(path)
    trace = json.loads((path / raw["provenance"]["path"]).read_bytes())
    if change in {"missing", "cycle"}:
        trace["simbad"]["matches"][0]["candidate"] = {
            "$ref": "#/simbad/absent"
            if change == "missing"
            else "#/simbad/matches/0/candidate"
        }
    elif change == "binding":
        del trace["simbad"]["bindings"]
    elif change == "science":
        raw["quantities"][0]["values"][0]["value"] = "999"
    elif change == "record":
        raw["sources"][papers[0].key]["records"] = ["/stars/8"]
    else:
        trace["associations"][0]["identifier"] = "HD 999"
    test_catalog.replace_provenance(path, raw, trace)
    with pytest.raises(ValueError):
        verify_catalog(path)


def test_quote_warnings_are_scoped_and_whitespace_stays_in_audit(build):
    p = paper(1, [source_star([GAIA_A])])
    mismatches = [
        {
            "result_path": "/stars/0/identifiers/0/evidence/0",
            "location": "T:p1-b0",
            "quote": EVIDENCE[0]["quote"],
            "reason": "unknown",
        },
        {
            "result_path": "/stars/0/contribution_type/evidence/0",
            "location": "T:p1-b0",
            "quote": EVIDENCE[0]["quote"],
            "reason": "whitespace_difference",
        },
    ]
    from hvs_catalog.records.evidence_audit import QuoteMismatch

    p.audit.quote_mismatches = [QuoteMismatch.model_validate(a) for a in mismatches]
    p.audit_bytes = json_bytes(p.audit)
    p.source.objects[0].evidence_audit = p.audit.quote_mismatches
    path, _, _ = build([p])
    warnings = document(path)["warnings"]
    assert len(warnings) == 1
    assert p.key + "#/stars/0/identifiers/0/evidence/0" in warnings[0]
    assert "unknown" in warnings[0]
    assert (
        "whitespace_difference"
        in (
            path / f"papers/{p.source.arxiv_id}/{p.key}__evidence_audit.json"
        ).read_text()
    )


def test_unified_provenance_preserves_all_current_methods(build, tmp_path, monkeypatch):
    from hvs_catalog.catalogue import readable
    from hvs_catalog.catalogue.simbad_publication import restore_document

    papers = [
        paper(1, [source_star(["HD 1"], 10)]),
        paper(2, [source_star(["HIP 2"], 20)]),
        paper(3, [source_star([GAIA_A], 30)]),
        paper(4, [source_star([GAIA_A], 30)]),
        paper(5, [source_star(["Star 5"], 40)]),
        paper(6, [source_star(["Star 6"], 40.0001)]),
        paper(7, [source_star(["HD 7"], 50)]),
        paper(8, [source_star(["HIP 8"], 60)]),
        paper(9, [source_star([test_catalog.GAIA_B], 30.0001)]),
    ]
    responses, _ = freeze(
        tmp_path,
        [(p.key + "#/stars/0", p.science.stars[0]) for p in papers],
        lambda q: [candidate("HD 9")] if q.identifier in {"HD 1", "HIP 2"} else [],
    )
    with monkeypatch.context() as patch:
        patch.setattr(
            readable, "export_files", partial(readable.export_files, schema_version=3)
        )
        first, _, _ = build(
            papers,
            simbad=responses,
            explicit_associations=[
                {
                    "left": papers[6].key + "#/stars/0",
                    "right": papers[7].key + "#/stars/0",
                    "source": papers[6].key,
                    "evidence": EVIDENCE,
                }
            ],
        )
        old, old_manifest, before = first, *load_previous(first)[::2]
    # Same inputs independently produce the unified layout.
    current, manifest, after = build(
        papers,
        "unified",
        simbad=responses,
        explicit_associations=[
            {
                "left": papers[6].key + "#/stars/0",
                "right": papers[7].key + "#/stars/0",
                "source": papers[6].key,
                "evidence": EVIDENCE,
            }
        ],
    )
    before = [s.model_dump(mode="json") for s in before]
    assert manifest.next_id == old_manifest.next_id
    assert manifest.retired_ids == old_manifest.retired_ids
    methods, rejected, historical = set(), [], []
    for old_star, new_star in zip(before, after, strict=True):
        old_star.pop("schema_version")
        new_star.pop("schema_version")
        assert new_star == old_star
        filename = "stars/" + new_star["catalog_id"] + ".json"
        original = restore_document(json.loads((old / filename).read_bytes()), old)
        raw = json.loads((current / filename).read_bytes())
        trace = json.loads((current / raw["provenance"]["path"]).read_bytes())
        assert "associations" not in raw and "association_origins" not in raw
        assert "provenance" not in raw["simbad"]
        for field in ("associations", "simbad"):
            assert trace[field] == original[field]
        assert trace.get("association_origins") == original.get("association_origins")
        methods.update(a["method"] for a in trace["associations"])
        rejected.extend(a for a in trace["associations"] if not a.get("accepted", True))
        historical.extend(a for a in trace["associations"] if a.get("historical"))
    assert methods == {"identifier", "coordinates", "paper", "simbad"}
    assert rejected and not historical
    shutil.rmtree(old)
    shutil.rmtree(responses)
    moved = tmp_path / "moved"
    current.rename(moved)
    assert len(load_previous(moved)[2]) == len(after)

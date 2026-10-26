"""Offline catalog invariants use fictional papers and fixed external responses only."""

import copy
import json
import socket

import pytest

from hvs_catalog.catalogue.build import build_catalog
from hvs_catalog.catalogue.identity import coordinate, display_name, identifier_key
from hvs_catalog.catalogue.inputs import LoadedPaper, digest, json_bytes
from hvs_catalog.catalogue.publication import load_previous, verify_catalog
from hvs_catalog.catalogue.simbad import (
    FrozenSimbad,
    collect_queries,
    queries_for,
    resolve_simbad,
)
from hvs_catalog.records.catalog import (
    CatalogSelection,
    CatalogSource,
    SimbadCandidate,
    SimbadResponse,
    SimbadSnapshot,
)
from hvs_catalog.records.evidence_audit import EvidenceAudit
from hvs_catalog.records.literature_contribution import LiteratureContribution

GAIA_A = "Gaia DR3 1234567890123456789"
GAIA_B = "Gaia DR3 2234567890123456789"
EVIDENCE = [{"location": "T:p1-b0", "quote": "Fictional measurement."}]


@pytest.fixture(autouse=True)
def no_external_network(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Offline catalog tests must not open network connections.")

    monkeypatch.setattr(socket, "create_connection", forbidden)


def source_star(ids, ra=None, dec=0, *, velocity="520", condition="ICRS"):
    quantities = [
        {
            "quantity": "radial_velocity",
            "values": [
                {
                    "value": velocity,
                    "error": "10",
                    "unit": "km/s",
                    "condition": "Heliocentric",
                    "evidence": copy.deepcopy(EVIDENCE),
                }
            ],
        }
    ]
    if ra is not None:
        quantities.extend(
            {
                "quantity": q,
                "values": [
                    {
                        "value": str(v),
                        "unit": "deg",
                        "condition": condition,
                        "evidence": copy.deepcopy(EVIDENCE),
                    }
                ],
            }
            for q, v in (("ra", ra), ("dec", dec))
        )
    return {
        "identifiers": [{"value": i, "evidence": copy.deepcopy(EVIDENCE)} for i in ids],
        "contribution_type": {
            "value": "follow_up",
            "evidence": copy.deepcopy(EVIDENCE),
        },
        "quantities": quantities,
    }


def paper(number, stars, *, version="v1", partial=False, notes=None):
    metadata = {
        "arxiv_id": f"2601.{number:05d}",
        "version": version,
        "title": "Fictional study",
        "authors": ["A. Example"],
    }
    science = LiteratureContribution.model_validate(
        {"metadata": metadata, "stars": stars, "notes": notes or []}
    )
    raw = json_bytes(science)
    audit = EvidenceAudit.model_validate(
        {
            "metadata": {
                "arxiv_id": metadata["arxiv_id"],
                "arxiv_version": version,
                "result_ref": {
                    "path": "runs/fixed/literature_contribution.json",
                    "sha256": digest(raw),
                },
            },
            "quote_mismatches": [],
        }
    )
    source = CatalogSource(
        **metadata,
        paper_notes=science.notes,
        result_status="partial" if partial else "complete",
        roster_status="complete",
        roster_issues=[],
        roster_rejected={},
        errors=[],
        result_sha256=digest(raw),
        run_sha256="a" * 64,
        input_sha256="b" * 64,
        pdf_sha256="c" * 64,
        pdf_origin={"kind": "synthetic"},
        pdf_url="https://arxiv.org/pdf/" + metadata["arxiv_id"] + version,
        objects=[
            {
                "result_index": i,
                "source_index": i,
                "status": "failed" if partial and not s.quantities else "complete",
                "snapshot_ref": {"record_id": "synthetic-" + str(i), "revision": 1},
                "issues": [],
                "rejected": {},
                "source_paths": [],
                "evidence_audit": [],
            }
            for i, s in enumerate(science.stars)
        ],
    )
    return LoadedPaper(science, raw, audit, json_bytes(audit), source)


@pytest.fixture
def build(monkeypatch, tmp_path):
    def run(papers, name="catalog", *, simbad=None, review=None, **options):
        monkeypatch.setattr(
            "hvs_catalog.catalogue.build.load_papers", lambda selection: papers
        )
        selection = CatalogSelection(data_version="synthetic-1", papers=[], **options)
        path = tmp_path / name
        manifest = build_catalog(
            selection,
            path,
            simbad=simbad,
            review=review,
            queue=tmp_path / (name + ".queue.json"),
        )
        # Exercise identity algorithms through the supported publication reader.
        # Public layout is tested separately from normalized build records.
        stars = [s.model_dump(mode="json") for s in load_previous(path)[2]]
        return path, manifest, stars

    return run


def replace_star(path, raw):
    file = path / "stars" / (raw["catalog_id"] + ".json")
    file.write_bytes(json_bytes(raw))
    index_path = path / "stars/index.json"
    index = json.loads(index_path.read_bytes())
    entry = next(s for s in index["stars"] if s["catalog_id"] == raw["catalog_id"])
    entry["file"]["sha256"] = digest(file.read_bytes())
    index_path.write_bytes(json_bytes(index))
    manifest_path = path / "manifest.json"
    manifest = json.loads(manifest_path.read_bytes())
    manifest["stars"]["sha256"] = digest(index_path.read_bytes())
    manifest_path.write_bytes(json_bytes(manifest))


def replace_provenance(path, raw, trace):
    file = path / raw["provenance"]["path"]
    file.write_bytes(json_bytes(trace))
    raw["provenance"]["sha256"] = digest(file.read_bytes())
    replace_star(path, raw)


def candidate(name, *, ra=10, aliases=None):
    return SimbadCandidate(
        main_id=name,
        aliases=aliases or [],
        position={
            "ra_deg": ra,
            "dec_deg": 0.0,
            "frame": "icrs",
        },
        fields={
            "rvz_radvel": {"value": 530.0, "unit": "km/s"},
            "rvz_bibcode": {"value": "FictionalReference"},
        },
        photometry=[{"band": "V", "fields": {"flux": {"value": 12.0, "unit": "mag"}}}],
    )


class FixedService:
    def __init__(self, replies):
        self.replies = replies
        self.calls = []

    def query(self, query):
        self.calls.append(query)
        reply = self.replies(query)
        return SimbadResponse(
            query=query,
            queried_at="2026-01-01T00:00:00+00:00",
            client_version="synthetic",
            candidates=[] if isinstance(reply, str) else reply,
            raw_response="<fictional/>",
            error=reply if isinstance(reply, str) else None,
        )


def freeze(tmp_path, stars, reply):
    folder = tmp_path / "responses"
    service = FixedService(reply)
    collect_queries(queries_for(stars), folder, service)
    return folder, service


def test_flat_science_self_contained_and_reproducible(build):
    a = paper(1, [source_star([GAIA_A])], notes=["Paper-wide note."])
    b = paper(2, [source_star([GAIA_A], velocity="520.0")])
    path, manifest, stars = build([a, b])
    assert len(stars) == 1
    star = stars[0]
    assert list(star)[-1] == "warnings"
    assert "contributions" not in star
    assert star["display_name"] == GAIA_A
    assert {v["source"] for v in star["quantities"][0]["values"]} == {a.key, b.key}
    assert [v["value"] for v in star["quantities"][0]["values"]] == ["520", "520.0"]
    assert star["sources"][a.key]["paper_notes"] == ["Paper-wide note."]
    raw = json.loads((path / manifest.stars[0].file.path).read_bytes())
    assert set(raw["sources"][a.key]) == {"title", "authors", "pdf_url", "records"}
    assert raw["sources"][a.key]["records"] == ["/stars/0"]
    assert (path / manifest.papers[a.key].science.path).read_bytes() == a.science_bytes
    second, other, _ = build([a, b], "rebuild")
    assert manifest == other
    assert {str(p.relative_to(path)): p.read_bytes() for p in path.rglob("*.json")} == {
        str(p.relative_to(second)): p.read_bytes() for p in second.rglob("*.json")
    }


def test_empty_and_failed_quantities_are_explicit(build):
    empty = paper(1, [])
    obj = source_star(["Star 1"])
    obj["quantities"] = []
    partial = paper(2, [obj], partial=True)
    _, manifest, stars = build([empty, partial])
    assert len(manifest.papers) == 2 and len(stars) == 1
    assert stars[0]["display_name"] == "Star 1 (2601.00002)"
    assert stars[0]["sources"][partial.key]["objects"][0]["status"] == "failed"
    assert stars[0]["warnings"]


@pytest.mark.parametrize("arcsec,expected", [(4.999, 1), (5.0, 1), (5.001, 2)])
def test_coordinate_radius_and_warning(build, arcsec, expected):
    a = paper(1, [source_star(["Star 1"], 10)])
    b = paper(2, [source_star(["Star 2"], 10 + arcsec / 3600)])
    _, _, stars = build([a, b])
    assert len(stars) == expected
    if expected == 1:
        assert any("coordinates only" in w for w in stars[0]["warnings"])
        assert stars[0]["associations"][0]["details"]["threshold_arcsec"] == 5.0


def test_chain_cannot_bypass_group_gaia_conflict(build):
    a = paper(1, [source_star([GAIA_A], 10)])
    b = paper(2, [source_star(["Star 2"], 10 + 4 / 3600)])
    c = paper(3, [source_star([GAIA_B], 10 + 8 / 3600)])
    _, _, stars = build([a, b, c])
    assert len(stars) == 2
    assert any(not edge["accepted"] for s in stars for edge in s["associations"])


def test_unknown_release_and_local_names_do_not_merge(build):
    a = paper(1, [source_star(["Star 1", "Gaia 1234567890123456789"])])
    b = paper(2, [source_star(["Star 1", "Gaia DR2 1234567890123456789"])])
    _, _, stars = build([a, b])
    assert len(stars) == 2
    assert identifier_key("Gaia 1234567890123456789") is None
    assert identifier_key("HD 123 B") != identifier_key("HD 123 b")


def test_ambiguous_coordinate_pair_is_not_arbitrarily_selected():
    star = paper(1, [source_star(["Star 1"], 10)]).science.stars[0]
    assert coordinate(star) is not None
    star.quantities[1].values.append(star.quantities[1].values[0])
    star.quantities[1].values[-1] = (
        star.quantities[1].values[-1].model_copy(update={"value": "11"})
    )
    assert coordinate(star) is None
    assert (
        coordinate(
            paper(
                2, [source_star(["Star 2"], 10, condition="Unknown frame")]
            ).science.stars[0]
        )
        is None
    )


def test_complete_selection_adds_papers_and_replaces_version(build):
    a = paper(1, [source_star([GAIA_A])])
    build([a])
    b = paper(2, [source_star([GAIA_A]), source_star([GAIA_B])])
    _, result, stars = build([a, b], "new")
    assert len(result.papers) == 2
    assert [s["catalog_id"] for s in stars] == [
        "hvs-catalog-000001",
        "hvs-catalog-000002",
    ]
    assert len(stars[0]["quantities"][0]["values"]) == 2
    newer = paper(1, [source_star([GAIA_A], velocity="600")], version="v2")
    _, result, stars = build([newer, b], "newer")
    assert a.key not in result.papers and newer.key in result.papers
    assert len(stars[0]["quantities"][0]["values"]) == 2
    assert result.next_id == 3


def test_duplicate_names_do_not_merge_conflicting_ids(build):
    a = paper(1, [source_star([GAIA_A, "HD 1"])])
    b = paper(2, [source_star([GAIA_B, "HD 1"])])
    path, _, stars = build([a, b])
    assert len(stars) == 2
    assert [s["display_name"] for s in stars] == ["HD 1", "HD 1"]
    assert len({s["catalog_id"] for s in stars}) == 2
    verify_catalog(path)
    assert all(any(not e["accepted"] for e in s["associations"]) for s in stars)


def test_display_priority_frequency_and_stability():
    snapshot = SimbadSnapshot(status="not_queried")
    a = paper(1, [source_star(["HD 9", "HIP 8"])])
    b = paper(2, [source_star(["HIP 8"])])
    entries = [(a.key, a.science.stars[0]), (b.key, b.science.stars[0])]
    assert display_name(entries, snapshot, "hvs-catalog-000001") == "HIP 8"
    c = paper(3, [source_star(["Gaia DR2 1234567890123456789", GAIA_A])])
    assert (
        display_name(
            entries + [(c.key, c.science.stars[0])],
            snapshot,
            "hvs-catalog-000001",
        )
        == "HIP 8"
    )


@pytest.mark.parametrize(
    "mode,status",
    [
        ("nearest", "matched"),
        ("tie", "ambiguous"),
        ("none", "not_found"),
        ("error", "failed"),
    ],
)
def test_simbad_coordinate_selection(tmp_path, mode, status):
    a = paper(1, [source_star(["Star 1"], 10)])
    entries = [(a.key, a.science.stars[0])]
    replies = {
        "nearest": [candidate("HD 1", ra=10.0001), candidate("HD 2", ra=10.0002)],
        "tie": [candidate("HD 1", ra=10), candidate("HD 2", ra=10)],
        "none": [],
        "error": "Service unavailable",
    }
    folder, service = freeze(tmp_path, entries, lambda q: replies[mode])
    snapshot = resolve_simbad(entries, FrozenSimbad(folder))
    assert snapshot.status == status
    if status == "matched":
        assert snapshot.selected.main_id == "HD 1" and len(snapshot.warnings) == 2
        assert (
            display_name(entries, snapshot, "hvs-catalog-000001")
            == "Star 1 (2601.00001)"
        )
    else:
        assert (
            display_name(entries, snapshot, "hvs-catalog-000001")
            == "Star 1 (2601.00001)"
        )
    collect_queries(queries_for(entries), folder, service)
    assert len(service.calls) == 1


def test_simbad_reliable_ids_merge_and_external_values_stay_separate(build, tmp_path):
    a = paper(1, [source_star(["HD 1"])])
    b = paper(2, [source_star(["HIP 2"])])
    folder, _ = freeze(
        tmp_path,
        [(a.key, a.science.stars[0]), (b.key, b.science.stars[0])],
        lambda q: [candidate("HD 1", aliases=["HD 1", "HIP 2"])],
    )
    _, _, stars = build([a, b], simbad=folder)
    assert len(stars) == 1 and stars[0]["display_name"] == "HD 1"
    assert all(v["value"] == "520" for v in stars[0]["quantities"][0]["values"])
    assert stars[0]["simbad"]["selected"]["fields"]["rvz_radvel"]["value"] == 530.0


def test_simbad_conflicting_identifiers_and_corrupt_response(tmp_path):
    a = paper(1, [source_star(["HD 1", "HIP 2"])])
    entries = [(a.key, a.science.stars[0])]
    folder, _ = freeze(tmp_path, entries, lambda q: [candidate(q.identifier)])
    assert resolve_simbad(entries, FrozenSimbad(folder)).status == "conflict"
    response = next(folder.glob("*.json"))
    data = json.loads(response.read_bytes())
    data["response"]["queried_at"] = "changed"
    response.write_bytes(json_bytes(data))
    with pytest.raises(ValueError, match="binding"):
        resolve_simbad(entries, FrozenSimbad(folder))


def test_tampered_science_and_unlisted_files_fail_verification(build):
    a = paper(1, [source_star([GAIA_A])])
    path, manifest, _ = build([a])
    (path / "unlisted.json").write_text("{}")
    with pytest.raises(ValueError, match="Unexpected"):
        verify_catalog(path)
    (path / "unlisted.json").unlink()
    star = path / manifest.stars[0].file.path
    star.write_text(star.read_text().replace('"520"', '"521"'))
    with pytest.raises(ValueError, match="hash"):
        verify_catalog(path)


def test_failure_before_publication_leaves_old_snapshot(build, monkeypatch, tmp_path):
    a = paper(1, [source_star([GAIA_A])])
    old, _, _ = build([a])
    before = (old / "manifest.json").read_bytes()
    monkeypatch.setattr("hvs_catalog.catalogue.build.load_papers", lambda s: [a])

    def fail(*args):
        raise RuntimeError("Interrupted")

    with pytest.raises(RuntimeError):
        build_catalog(
            CatalogSelection(data_version="new", papers=[]),
            tmp_path / "new",
            hook=fail,
        )
    assert not (tmp_path / "new").exists()
    assert (old / "manifest.json").read_bytes() == before
    assert not list(tmp_path.glob(".new-*/manifest.json"))
    with pytest.raises(ValueError, match="already exists"):
        build([a])


def test_coordinate_warning_survives_fresh_rebuild(build):
    a = paper(1, [source_star(["Star 1"], 10)])
    b = paper(2, [source_star(["Star 2"], 10.0001)])
    build([a, b])
    _, _, stars = build([a, b], "continued")
    assert any("coordinates only" in w for w in stars[0]["warnings"])
    assert any(
        e["method"] == "coordinates" and e["accepted"] for e in stars[0]["associations"]
    )


def test_all_numerical_forms_and_schema_survive_build(build):
    from pathlib import Path

    from jsonschema import Draft202012Validator

    from hvs_catalog.records.catalog_public import StellarRecord

    fixture = json.loads(
        (Path(__file__).parent / "fixtures/literature_contribution.json").read_bytes()
    )
    p = paper(1, fixture["stars"])
    path, manifest, stars = build([p])
    raw = json.loads((path / manifest.stars[0].file.path).read_bytes())
    Draft202012Validator(StellarRecord.model_json_schema()).validate(raw)
    restored = [
        {
            "quantity": q["quantity"],
            "values": [
                {
                    k: v
                    for k, v in item.items()
                    if k
                    not in {"source", "attribution", "attribution_reason", "standard"}
                }
                for item in q["values"]
            ],
        }
        for q in stars[0]["quantities"]
    ]
    assert restored == fixture["stars"][0]["quantities"]


def test_paper_correspondence_preserves_all_sources(build):
    a = paper(1, [source_star(["HD 1"])])
    b = paper(2, [source_star(["HIP 2"])])
    build([a, b])
    _, manifest, stars = build(
        [a, b],
        "merged",
        explicit_associations=[
            {
                "left": a.key + "#/stars/0",
                "right": b.key + "#/stars/0",
                "source": a.key,
                "evidence": EVIDENCE,
            }
        ],
    )
    assert len(stars) == 1
    assert stars[0]["catalog_id"] == "hvs-catalog-000001"
    assert set(stars[0]["sources"]) == {a.key, b.key}
    assert manifest.next_id == 2


@pytest.mark.integration
def test_process_exit_and_concurrent_publication(build, tmp_path):
    import subprocess
    import sys

    a = paper(1, [source_star([GAIA_A])])
    old, _, _ = build([a])
    destination = tmp_path / "new"
    code = """
import sys
from pathlib import Path
from hvs_catalog.catalogue.publication import publish
source, destination, mode = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3]
files = {str(p.relative_to(source)): p.read_bytes() for p in source.rglob("*.json")}
def checkpoint(stage, path):
    print("READY", flush=True)
    sys.stdin.readline()
if mode == "kill":
    publish(destination, files, hook=checkpoint)
else:
    print("READY", flush=True)
    sys.stdin.readline()
    try:
        publish(destination, files)
    except ValueError:
        raise SystemExit(2)
"""
    first = subprocess.Popen(
        [sys.executable, "-c", code, str(old), str(destination), "kill"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert first.stdout.readline().strip() == "READY"
        first.kill()
        first.communicate(timeout=15)
        assert not destination.exists()
        verify_catalog(old)
    finally:
        if first.poll() is None:
            first.kill()
            first.communicate()
    workers = [
        subprocess.Popen(
            [sys.executable, "-c", code, str(old), str(destination), "race"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for _ in range(2)
    ]
    try:
        for process in workers:
            assert process.stdout.readline().strip() == "READY"
        for process in workers:
            process.stdin.write("go\n")
            process.stdin.flush()
        for process in workers:
            process.communicate(timeout=30)
        assert sorted(p.returncode for p in workers) == [0, 2]
        verify_catalog(destination)
        assert (destination / "manifest.json").read_bytes() == (
            old / "manifest.json"
        ).read_bytes()
    finally:
        for process in workers:
            if process.poll() is None:
                process.kill()
                process.communicate()


def test_complete_catalog_namespace_normalization_preserves_component():
    assert identifier_key("hd123 B") == identifier_key("HD 123 B") == "HD 123 B"
    assert identifier_key("HD 123 b") != identifier_key("HD 123 B")


@pytest.mark.parametrize("paper_count", [1, 2])
def test_coordinate_simbad_association_has_source_and_query_evidence(
    build, tmp_path, paper_count
):
    papers = [
        paper(i, [source_star(["Star " + str(i)], 10)])
        for i in range(1, paper_count + 1)
    ]
    entries = [(p.key + "#/stars/0", p.science.stars[0]) for p in papers]
    folder, _ = freeze(
        tmp_path,
        entries,
        lambda q: [
            candidate("HD 9", ra=10.0001),
            candidate("HD 10", ra=10.0002),
        ],
    )
    old, _, stars = build(papers, simbad=folder)
    star = stars[0]
    edges = [e for e in star["associations"] if e["right"] == "simbad:HD 9"]
    assert len(edges) == paper_count
    assert {e["left"] for e in edges} == {node for node, _ in entries}
    for edge in edges:
        assert edge["accepted"] and edge["method"] == "coordinates"
        d = edge["details"]
        assert d["separation_arcsec"] == pytest.approx(0.36)
        assert d["threshold_arcsec"] == 5.0 and not d["epoch_propagation"]
        assert d["selection"] == "nearest"
        assert len(d["candidates"]) == 2
        assert d["query"] in star["simbad"]["queries"]
        assert d["query"]["response_sha256"] and d["query"]["queried_at"]
        assert len(d["left_position"]["evidence"]) == 2
        assert edge["origins"][edge["left"]]["source"]["version"] == "v1"
    current = old
    verify_catalog(current)
    # The catalog validator must enforce external binding completeness.
    file = next((current / "stars").glob("hvs-catalog-*.json"))
    raw = json.loads(file.read_bytes())
    trace = json.loads((current / raw["provenance"]["path"]).read_bytes())
    del trace["simbad"]["bindings"]
    raw["simbad"].pop("match")
    # Rebind both files, so the association check must reject the missing proof.
    replace_provenance(current, raw, trace)
    with pytest.raises(ValueError, match="SIMBAD.*association"):
        verify_catalog(current)


def test_coordinate_associations_keep_each_response_position(build, tmp_path):
    a, b = (
        paper(1, [source_star(["Star 1"], 10)]),
        paper(2, [source_star(["Star 2"], 10.0001)]),
    )
    entries = [(p.key + "#/stars/0", p.science.stars[0]) for p in [a, b]]
    folder, _ = freeze(
        tmp_path, entries, lambda q: [candidate("HD 9", ra=q.position.ra_deg + 0.0001)]
    )
    _, _, stars = build([a, b], simbad=folder)
    edges = [e for e in stars[0]["associations"] if e["right"] == "simbad:HD 9"]
    assert len(edges) == 2
    for edge in edges:
        d = edge["details"]
        assert d["right_position"]["ra_deg"] == pytest.approx(
            d["left_position"]["ra_deg"] + 0.0001
        )
        assert d["separation_arcsec"] == pytest.approx(0.36)


@pytest.mark.integration
def test_association_provenance_hashes_survive_process_hash_seeds(tmp_path):
    import os
    import subprocess
    import sys
    from pathlib import Path

    code = """
import sys
from pathlib import Path
sys.path.insert(0, str(Path.cwd() / "tests"))
from test_catalog import paper, source_star
from hvs_catalog.catalogue import build as module
from hvs_catalog.records.catalog import CatalogSelection
module.load_papers = lambda selection: [
    paper(1, [source_star(["HD 1"])]),
    paper(2, [source_star(["HD 1"])]),
]
module.build_catalog(CatalogSelection(data_version="fixed", papers=[]), Path(sys.argv[1]))
"""
    repo = Path(__file__).resolve().parents[1]
    roots = [tmp_path / "first", tmp_path / "second"]
    for seed, root in zip(["1", "17"], roots):
        subprocess.run(
            [sys.executable, "-B", "-c", code, str(root)],
            cwd=repo,
            env={**os.environ, "PYTHONHASHSEED": seed, "PYTHONDONTWRITEBYTECODE": "1"},
            check=True,
            capture_output=True,
            text=True,
        )
    assert (roots[0] / "manifest.json").read_bytes() == (
        roots[1] / "manifest.json"
    ).read_bytes()
    assert (roots[0] / "stars/hvs-catalog-000001.json").read_bytes() == (
        roots[1] / "stars/hvs-catalog-000001.json"
    ).read_bytes()


@pytest.mark.parametrize("new_identifier", [GAIA_B, "HD 41"])
def test_simbad_merge_checks_all_component_external_identities(
    build, tmp_path, new_identifier
):
    a = paper(40, [source_star(["HD 40"])])
    folder, _ = freeze(
        tmp_path,
        [(a.key, a.science.stars[0])],
        lambda q: [candidate("HIP 40", aliases=[GAIA_A])],
    )
    b = paper(41, [source_star([new_identifier])])
    other = tmp_path / "new-responses"
    collect_queries(
        queries_for([(b.key, b.science.stars[0])]),
        other,
        FixedService(lambda q: [candidate("HIP 40", aliases=[GAIA_B])]),
    )
    import shutil

    for response in folder.glob("*.json"):
        shutil.copy2(response, other / response.name)
    current, manifest, stars = build([a, b], "conflicting-link", simbad=other)
    assert len(stars) == 2
    assert manifest.next_id == 3
    for star in stars:
        assert len(star["sources"]) == 1
        assert len(star["quantities"][0]["values"]) == 1
        assert star["display_name"] in {"HD 40", new_identifier}
        edge = next(e for e in star["associations"] if e["method"] == "simbad")
        assert not edge["accepted"] and "Gaia" in edge["details"]["reason"]
        evidence = edge["details"]["simbad_evidence"]
        assert {v["candidate"]["aliases"][0] for v in evidence} == {GAIA_A, GAIA_B}
        assert all(v["queries"][0]["response_sha256"] for v in evidence)
    verify_catalog(current)
    _, _, continued = build([a, b], "conflicting-link-continued", simbad=other)
    assert [s["catalog_id"] for s in continued] == [s["catalog_id"] for s in stars]
    assert [s["simbad"] for s in continued] == [s["simbad"] for s in stars]


def test_same_main_id_responses_do_not_hide_conflicting_aliases(tmp_path):
    a = paper(1, [source_star(["HD 1", "HIP 2"])])
    entries = [(a.key, a.science.stars[0])]
    folder, _ = freeze(
        tmp_path,
        entries,
        lambda q: [
            candidate("HD 1", aliases=[GAIA_A if q.identifier == "HD 1" else GAIA_B])
        ],
    )
    snapshot = resolve_simbad(entries, FrozenSimbad(folder))
    assert snapshot.status == "conflict" and snapshot.selected is None
    assert len(snapshot.queries) == 2
    assert {c["candidate"]["aliases"][0] for c in snapshot.candidates} == {
        GAIA_A,
        GAIA_B,
    }

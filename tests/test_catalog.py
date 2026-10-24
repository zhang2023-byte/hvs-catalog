"""Offline catalog invariants use fictional papers and fixed external responses only."""

import copy
import json
import socket

import pytest

from hvs_catalog.catalogue.build import build_catalog
from hvs_catalog.catalogue.identity import coordinate, display_name, identifier_key
from hvs_catalog.catalogue.inputs import LoadedPaper, digest, json_bytes
from hvs_catalog.catalogue.publication import verify_catalog
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
    def run(papers, name="catalog", *, previous=None, simbad=None, **options):
        monkeypatch.setattr(
            "hvs_catalog.catalogue.build.load_papers", lambda selection: papers
        )
        selection = CatalogSelection(data_version="synthetic-1", papers=[], **options)
        path = tmp_path / name
        manifest = build_catalog(selection, path, previous=previous, simbad=simbad)
        stars = [
            json.loads((path / item.file.path).read_bytes()) for item in manifest.stars
        ]
        return path, manifest, stars

    return run


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
    assert stars[0]["display_name"] == stars[0]["catalog_id"]
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
    assert coordinate(star) is None
    assert (
        coordinate(
            paper(
                2, [source_star(["Star 2"], 10, condition="Unknown frame")]
            ).science.stars[0]
        )
        is None
    )


def test_incremental_match_and_append_and_new_version(build):
    a = paper(1, [source_star([GAIA_A])])
    old, _, _ = build([a])
    b = paper(2, [source_star([GAIA_A]), source_star([GAIA_B])])
    new, result, stars = build([b], "new", previous=old)
    assert len(result.papers) == 2
    assert [s["catalog_id"] for s in stars] == [
        "hvs-catalog-000001",
        "hvs-catalog-000002",
    ]
    assert len(stars[0]["quantities"][0]["values"]) == 2
    newer = paper(1, [source_star([GAIA_A], velocity="600")], version="v2")
    _, result, stars = build([newer], "newer", previous=new)
    assert a.key not in result.papers and newer.key in result.papers
    assert len(stars[0]["quantities"][0]["values"]) == 2
    assert result.next_id == 3


def test_collision_falls_back_without_merging_conflicting_ids(build):
    a = paper(1, [source_star([GAIA_A, "HD 1"])])
    b = paper(2, [source_star([GAIA_B, "HD 1"])])
    # Gaia names win; conflicting shared HD name still records rejected evidence.
    _, _, stars = build([a, b])
    assert len(stars) == 2
    assert all(any(not e["accepted"] for e in s["associations"]) for s in stars)


def test_display_priority_frequency_and_stability():
    snapshot = SimbadSnapshot(status="not_queried")
    a = paper(1, [source_star(["HD 9", "HIP 8"])])
    b = paper(2, [source_star(["HIP 8"])])
    entries = [(a.key, a.science.stars[0]), (b.key, b.science.stars[0])]
    assert display_name(entries, snapshot, "hvs-catalog-000001") == "HIP 8"
    assert display_name(entries, snapshot, "hvs-catalog-000001", "HD 9") == "HD 9"
    c = paper(3, [source_star(["Gaia DR2 1234567890123456789", GAIA_A])])
    assert (
        display_name(
            entries + [(c.key, c.science.stars[0])],
            snapshot,
            "hvs-catalog-000001",
            "HD 9",
        )
        == GAIA_A
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
            == "hvs-catalog-000001"
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
            previous=old,
            hook=fail,
        )
    assert not (tmp_path / "new").exists()
    assert (old / "manifest.json").read_bytes() == before
    assert not list(tmp_path.glob(".new-*/manifest.json"))
    with pytest.raises(ValueError, match="already exists"):
        build([a])


def test_explicit_split_uses_reserved_id_mapping(build):
    a = paper(1, [source_star(["HD 1"])])
    b = paper(2, [source_star(["HD 1"])])
    old, _, _ = build([a, b])
    _, manifest, stars = build(
        [],
        "split",
        previous=old,
        identity_assignments={
            a.key + "#/stars/0": "hvs-catalog-000002",
            b.key + "#/stars/0": "hvs-catalog-000003",
        },
        retired_ids={
            "hvs-catalog-000001": ["hvs-catalog-000002", "hvs-catalog-000003"]
        },
    )
    assert len(stars) == 2 and manifest.next_id == 4
    assert all(s["display_name"] == s["catalog_id"] for s in stars)
    assert all(any("collision" in w for w in s["warnings"]) for s in stars)


def test_coordinate_warning_survives_incremental_build(build):
    a = paper(1, [source_star(["Star 1"], 10)])
    b = paper(2, [source_star(["Star 2"], 10.0001)])
    old, _, _ = build([a, b])
    _, _, stars = build([], "continued", previous=old)
    assert any("coordinates only" in w for w in stars[0]["warnings"])
    assert any(
        e["method"] == "coordinates" and e["accepted"] for e in stars[0]["associations"]
    )


def test_external_snapshot_refresh_is_explicit(build, tmp_path):
    a = paper(1, [source_star(["HD 1"])])
    folder, _ = freeze(
        tmp_path, [(a.key, a.science.stars[0])], lambda q: [candidate("HD 1")]
    )
    old, _, _ = build([a], simbad=folder)
    other = tmp_path / "new-responses"
    collect_queries(
        queries_for([(a.key, a.science.stars[0])]),
        other,
        FixedService(lambda q: [candidate("HIP 2")]),
    )
    _, _, unchanged = build([], "unchanged", previous=old, simbad=other)
    assert unchanged[0]["display_name"] == "HD 1"
    _, _, refreshed = build(
        [],
        "refreshed",
        previous=old,
        simbad=other,
        simbad_refresh=["hvs-catalog-000001"],
    )
    assert refreshed[0]["display_name"] == "HIP 2"


def test_all_numerical_forms_and_schema_survive_build(build):
    from pathlib import Path

    from jsonschema import Draft202012Validator

    from hvs_catalog.records.catalog import CatalogStar

    fixture = json.loads(
        (Path(__file__).parent / "fixtures/literature_contribution.json").read_bytes()
    )
    p = paper(1, fixture["stars"])
    _, _, stars = build([p])
    Draft202012Validator(CatalogStar.model_json_schema()).validate(stars[0])
    restored = [
        {
            "quantity": q["quantity"],
            "values": [
                {k: v for k, v in item.items() if k != "source"} for item in q["values"]
            ],
        }
        for q in stars[0]["quantities"]
    ]
    assert restored == fixture["stars"][0]["quantities"]


def test_explicit_merge_preserves_retired_id_and_all_sources(build):
    a = paper(1, [source_star(["HD 1"])])
    b = paper(2, [source_star(["HIP 2"])])
    old, _, _ = build([a, b])
    _, manifest, stars = build(
        [],
        "merged",
        previous=old,
        retired_ids={"hvs-catalog-000002": ["hvs-catalog-000001"]},
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
    assert manifest.next_id == 3


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


@pytest.mark.parametrize("conflict_in_main_id", [True, False])
def test_inherited_simbad_conflict_preserves_evidence(
    build, tmp_path, conflict_in_main_id
):
    a = paper(1, [source_star(["HD 1"])])
    selected = candidate(
        GAIA_A if conflict_in_main_id else "HD 1",
        aliases=["HD 1"] if conflict_in_main_id else ["HD 1", GAIA_A],
    )
    folder, _ = freeze(tmp_path, [(a.key, a.science.stars[0])], lambda q: [selected])
    old, _, before = build([a], simbad=folder)
    b = paper(2, [source_star(["HD 1", GAIA_B])])
    current, _, stars = build([b], "conflict", previous=old)
    s = stars[0]
    assert s["catalog_id"] == before[0]["catalog_id"]
    assert s["simbad"]["status"] == "conflict"
    assert s["simbad"]["selected"] is None
    assert s["display_name"] == s["catalog_id"]
    assert s["simbad"]["queries"] == before[0]["simbad"]["queries"]
    rejected = [c for c in s["simbad"]["candidates"] if c.get("rejected_candidate")]
    assert rejected[0]["rejected_candidate"] == selected.model_dump(mode="json")
    assert any("conflict" in w for w in s["warnings"])
    assert len(s["quantities"][0]["values"]) == 2
    _, _, continued = build([], "conflict-continued", previous=current)
    assert continued[0]["simbad"] == s["simbad"]


@pytest.mark.parametrize("version", ["v1", "v2"])
def test_coordinate_sources_replaced_together_keep_identity(build, version):
    a = paper(1, [source_star(["Star 1"], 10)])
    b = paper(2, [source_star(["Star 2"], 10.0001)])
    old, _, original = build([a, b])
    replacement = [
        paper(1, [source_star(["Star 1"], 10, velocity="521")], version=version),
        paper(2, [source_star(["Star 2"], 10.0001, velocity="531")], version=version),
    ]
    current, manifest, stars = build(replacement, "replaced", previous=old)
    assert len(stars) == 1
    assert stars[0]["catalog_id"] == original[0]["catalog_id"]
    assert manifest.next_id == 2
    assert set(stars[0]["sources"]) == {p.key for p in replacement}
    assert [v["value"] for v in stars[0]["quantities"][0]["values"]] == ["521", "531"]
    assert any("coordinates only" in w for w in stars[0]["warnings"])
    _, _, continued = build([], "continued", previous=current)
    assert continued[0]["catalog_id"] == original[0]["catalog_id"]
    assert continued[0]["quantities"] == stars[0]["quantities"]


def test_previous_ambiguous_coordinate_source_remains_unusable(build):
    a = source_star(["Star 1"], 10)
    a["quantities"][1]["values"].append(copy.deepcopy(a["quantities"][1]["values"][0]))
    old, _, _ = build([paper(1, [a])])
    with pytest.raises(ValueError, match="previous object disappeared"):
        build(
            [paper(1, [source_star(["Star 1"], 10)], version="v2")],
            "not-inferred",
            previous=old,
        )


def test_historical_paper_association_keeps_exact_source_binding(build):
    a, b = paper(1, [source_star(["HD 1"])]), paper(2, [source_star(["HIP 2"])])
    old, _, _ = build(
        [a, b],
        explicit_associations=[
            {
                "left": a.key + "#/stars/0",
                "right": b.key + "#/stars/0",
                "source": a.key,
                "evidence": EVIDENCE,
            }
        ],
    )
    current, _, stars = build(
        [paper(1, [source_star(["HD 1"], velocity="600")], version="v2")],
        "updated",
        previous=old,
    )
    edge = next(e for e in stars[0]["associations"] if e["method"] == "paper")
    assert edge["historical"]
    assert edge["details"]["source"] == a.key
    origin = edge["origins"][edge["left"]]
    assert origin["source"] == a.science.metadata.model_dump()
    assert origin["result_sha256"] == a.source.result_sha256
    assert origin["result_index"] == 0
    assert origin["identifiers"] == [
        i.model_dump() for i in a.science.stars[0].identifiers
    ]
    assert a.key not in stars[0]["sources"]
    assert len(stars[0]["quantities"][0]["values"]) == 2
    verify_catalog(current)
    later, _, again = build(
        [paper(1, [source_star(["HD 1"], velocity="610")], version="v3")],
        "updated-again",
        previous=current,
    )
    assert next(e for e in again[0]["associations"] if e["method"] == "paper") == edge
    # Updating file hashes must not hide a dangling historical association.
    file = next((later / "stars").iterdir())
    raw = json.loads(file.read_bytes())
    e = next(e for e in raw["associations"] if e["method"] == "paper")
    del e["origins"][e["left"]]
    file.write_bytes(json_bytes(raw))
    manifest = json.loads((later / "manifest.json").read_bytes())
    manifest["stars"][0]["file"]["sha256"] = digest(file.read_bytes())
    (later / "manifest.json").write_bytes(json_bytes(manifest))
    with pytest.raises(ValueError, match="origin"):
        verify_catalog(later)


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
    current, _, newer = build(
        [
            paper(
                int(p.source.arxiv_id.split(".")[1]),
                [source_star(["Star " + str(i)], 10)],
                version="v2",
            )
            for i, p in enumerate(papers, 1)
        ],
        "version-two",
        previous=old,
    )
    assert any(
        e["historical"] for e in newer[0]["associations"] if e["right"] == "simbad:HD 9"
    )
    verify_catalog(current)
    # The catalog validator must enforce external binding completeness.
    file = next((current / "stars").iterdir())
    raw = json.loads(file.read_bytes())
    raw["associations"] = [
        e for e in raw["associations"] if not e["right"].startswith("simbad:")
    ]
    file.write_bytes(json_bytes(raw))
    manifest = json.loads((current / "manifest.json").read_bytes())
    manifest["stars"][0]["file"]["sha256"] = digest(file.read_bytes())
    (current / "manifest.json").write_bytes(json_bytes(manifest))
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
    old, _, before = build([a], simbad=folder)
    b = paper(41, [source_star([new_identifier])])
    other = tmp_path / "new-responses"
    collect_queries(
        queries_for([(b.key, b.science.stars[0])]),
        other,
        FixedService(lambda q: [candidate("HIP 40", aliases=[GAIA_B])]),
    )
    current, manifest, stars = build(
        [b], "conflicting-link", previous=old, simbad=other
    )
    assert len(stars) == 2
    assert stars[0]["catalog_id"] == before[0]["catalog_id"]
    assert manifest.next_id == 3
    for star in stars:
        assert len(star["sources"]) == 1
        assert len(star["quantities"][0]["values"]) == 1
        assert star["display_name"] == star["catalog_id"]
        edge = next(e for e in star["associations"] if e["method"] == "simbad")
        assert not edge["accepted"] and "Gaia" in edge["details"]["reason"]
        evidence = edge["details"]["simbad_evidence"]
        assert {v["candidate"]["aliases"][0] for v in evidence} == {GAIA_A, GAIA_B}
        assert all(v["queries"][0]["response_sha256"] for v in evidence)
    verify_catalog(current)
    _, _, continued = build([], "conflicting-link-continued", previous=current)
    assert [s["catalog_id"] for s in continued] == [s["catalog_id"] for s in stars]
    assert [s["simbad"] for s in continued] == [s["simbad"] for s in stars]


def test_retired_merge_keeps_all_frozen_external_evidence(build, tmp_path):
    a, b = paper(50, [source_star(["HD 50"])]), paper(51, [source_star(["HD 51"])])
    folder, _ = freeze(
        tmp_path,
        [(p.key, p.science.stars[0]) for p in [a, b]],
        lambda q: [candidate("HIP 50" if q.identifier == "HD 50" else "HIP 51")],
    )
    old, _, before = build([a, b], simbad=folder)
    current, manifest, stars = build(
        [],
        "retired-external",
        previous=old,
        retired_ids={"hvs-catalog-000002": ["hvs-catalog-000001"]},
        explicit_associations=[
            {
                "left": a.key + "#/stars/0",
                "right": b.key + "#/stars/0",
                "source": a.key,
                "evidence": EVIDENCE,
            }
        ],
    )
    assert len(stars) == 1 and manifest.next_id == 3
    star = stars[0]
    assert star["catalog_id"] == "hvs-catalog-000001"
    assert star["display_name"] == star["catalog_id"]
    assert set(star["sources"]) == {a.key, b.key}
    assert len(star["quantities"][0]["values"]) == 2
    snapshot = star["simbad"]
    assert snapshot["status"] == "conflict" and snapshot["selected"] is None
    assert snapshot["queries"] == [q for s in before for q in s["simbad"]["queries"]]
    for original in before:
        assert all(
            c in snapshot["candidates"] for c in original["simbad"]["candidates"]
        )
        assert any(
            c.get("rejected_candidate") == original["simbad"]["selected"]
            for c in snapshot["candidates"]
        )
    verify_catalog(current)
    _, _, continued = build([], "retired-external-continued", previous=current)
    assert continued[0]["simbad"] == snapshot


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


@pytest.mark.parametrize("refresh", [False, True])
def test_retired_refresh_scope_agrees_with_query_plan(build, tmp_path, refresh):
    from hvs_catalog.cli import main

    a, b = paper(50, [source_star(["HD 50"])]), paper(51, [source_star(["HD 51"])])
    folder, _ = freeze(
        tmp_path,
        [(p.key, p.science.stars[0]) for p in [a, b]],
        lambda q: [candidate("HIP 50" if q.identifier == "HD 50" else "HIP 51")],
    )
    old, _, _ = build([a, b], simbad=folder)
    spec = CatalogSelection(
        data_version="synthetic",
        papers=[],
        retired_ids={"hvs-catalog-000002": ["hvs-catalog-000001"]},
        explicit_associations=[
            {
                "left": a.key + "#/stars/0",
                "right": b.key + "#/stars/0",
                "source": a.key,
                "evidence": EVIDENCE,
            }
        ],
        simbad_refresh=["hvs-catalog-000002"] if refresh else [],
    )
    spec_path, plan_path = tmp_path / "selection.json", tmp_path / "query-plan.json"
    spec_path.write_bytes(json_bytes(spec))
    assert (
        main(
            [
                "catalog",
                "query-plan",
                "--selection",
                str(spec_path),
                "--previous",
                str(old),
                "--output",
                str(plan_path),
            ]
        )
        == 0
    )
    plan = json.loads(plan_path.read_bytes())
    assert {q["identifier"] for q in plan["queries"]} == (
        {"HD 50", "HD 51"} if refresh else set()
    )
    other = tmp_path / "refreshed-responses"
    collect_queries(
        queries_for([(p.key, p.science.stars[0]) for p in [a, b]]),
        other,
        FixedService(lambda q: [candidate("HIP 50")]),
    )
    _, _, stars = build(
        [],
        "refreshed-merge",
        previous=old,
        simbad=other,
        **spec.model_dump(exclude={"data_version", "papers"}),
    )
    assert stars[0]["simbad"]["status"] == ("matched" if refresh else "conflict")


def test_incremental_simbad_merge_keeps_aliases_from_every_frozen_response(
    build, tmp_path
):
    a, b = paper(60, [source_star(["HD 60"])]), paper(61, [source_star(["HD 61"])])
    folder, _ = freeze(
        tmp_path,
        [(p.key, p.science.stars[0]) for p in [a, b]],
        lambda q: [
            candidate("HIP 60", aliases=[GAIA_A] if q.identifier == "HD 61" else [])
        ],
    )
    old, _, before = build([a, b], simbad=folder)
    assert len(before) == 1
    assert before[0]["simbad"]["selected"]["aliases"] == []
    c = paper(62, [source_star(["HD 62"])])
    other = tmp_path / "new-responses"
    collect_queries(
        queries_for([(c.key, c.science.stars[0])]),
        other,
        FixedService(lambda q: [candidate("HIP 60", aliases=[GAIA_B])]),
    )
    _, _, stars = build([c], "incremental-aliases", previous=old, simbad=other)
    assert len(stars) == 2
    assert set(stars[0]["sources"]) == {a.key, b.key}
    assert set(stars[1]["sources"]) == {c.key}
    assert all(
        not e["accepted"]
        for s in stars
        for e in s["associations"]
        if e["method"] == "simbad" and not e["historical"]
    )


@pytest.mark.parametrize(
    "replace,refresh", [(False, False), (True, False), (True, True)]
)
def test_frozen_reliable_alias_preserves_existing_identity(
    build, tmp_path, monkeypatch, replace, refresh
):
    from hvs_catalog.cli import main

    a = paper(1, [source_star(["HD 1"])])
    folder, _ = freeze(
        tmp_path,
        [(a.key, a.science.stars[0])],
        lambda q: [candidate("HD 1", aliases=["HIP 2"])],
    )
    old, _, before = build([a], simbad=folder)
    b = paper(
        1 if replace else 2,
        [source_star(["HIP 2"], velocity="610")],
        version="v2" if replace else "v1",
    )
    # Prepare a query plan with the same synthetic source selection.
    import hvs_catalog.catalogue.build as module

    monkeypatch.setattr(module, "load_papers", lambda selection: [b])
    spec = CatalogSelection(
        data_version="synthetic",
        papers=[],
        simbad_refresh=["hvs-catalog-000001"] if refresh else [],
    )
    spec_path, plan_path = (
        tmp_path / "alias-selection.json",
        tmp_path / "alias-plan.json",
    )
    spec_path.write_bytes(json_bytes(spec))
    assert (
        main(
            [
                "catalog",
                "query-plan",
                "--selection",
                str(spec_path),
                "--previous",
                str(old),
                "--output",
                str(plan_path),
            ]
        )
        == 0
    )
    queries = json.loads(plan_path.read_bytes())["queries"]
    assert [q["identifier"] for q in queries] == (["HIP 2"] if refresh else [])
    other = tmp_path / "alias-refresh"
    collect_queries(
        queries_for([(b.key, b.science.stars[0])]),
        other,
        FixedService(lambda q: [candidate("HD 1", aliases=["HIP 2"])]),
    )
    current, manifest, stars = build(
        [b],
        "alias-linked",
        previous=old,
        simbad=other if refresh else None,
        simbad_refresh=spec.simbad_refresh,
    )
    assert len(stars) == 1 and manifest.next_id == 2
    s = stars[0]
    assert s["catalog_id"] == before[0]["catalog_id"]
    assert s["display_name"] == "HD 1"
    assert set(s["sources"]) == ({b.key} if replace else {a.key, b.key})
    assert [v["value"] for v in s["quantities"][0]["values"]] == (
        ["610"] if replace else ["520", "610"]
    )
    edge = next(e for e in s["associations"] if e["method"] == "simbad")
    assert edge["accepted"] and edge["details"]["matched_identifiers"] == ["HIP 2"]
    assert (
        edge["details"]["simbad_evidence"][0]["queries"]
        == before[0]["simbad"]["queries"]
    )
    verify_catalog(current)
    _, _, continued = build([], "alias-linked-continued", previous=current)
    assert continued[0]["catalog_id"] == s["catalog_id"]


@pytest.mark.parametrize(
    "mode", ["coordinate", "conflict", "ambiguous", "gaia_conflict"]
)
def test_frozen_aliases_do_not_bypass_weak_or_conflicting_identity(
    build, tmp_path, mode
):
    a = paper(
        1,
        [
            source_star(
                ["Star 1"] if mode == "coordinate" else ["HD 1"],
                10 if mode == "coordinate" else None,
            )
        ],
    )
    papers = [a]
    if mode == "ambiguous":
        papers.append(paper(2, [source_star(["HD 2"])]))
    folder, _ = freeze(
        tmp_path,
        [(p.key, p.science.stars[0]) for p in papers],
        lambda q: (
            [candidate("HD 1", aliases=["HIP 3"]), candidate("HD 2", aliases=["HIP 3"])]
            if mode == "conflict"
            else [candidate(q.identifier or "HD 1", aliases=["HIP 3", GAIA_A])]
        ),
    )
    old, _, before = build(papers, simbad=folder)
    b = paper(
        3, [source_star(["HIP 3", GAIA_B] if mode == "gaia_conflict" else ["HIP 3"])]
    )
    _, _, stars = build([b], "alias-rejected", previous=old)
    assert len(stars) == len(before) + 1
    assert set(stars[-1]["sources"]) == {b.key}
    assert stars[-1]["simbad"]["status"] != "matched"
    if mode in {"ambiguous", "gaia_conflict"}:
        assert stars[-1]["simbad"]["status"] == "conflict"
        assert stars[-1]["display_name"] == stars[-1]["catalog_id"]
        edges = [e for e in stars[-1]["associations"] if e["method"] == "simbad"]
        assert edges and all(not e["accepted"] for e in edges)


def test_frozen_alias_in_nonselected_representation_links_new_paper(build, tmp_path):
    a, b = paper(1, [source_star(["HD 1"])]), paper(2, [source_star(["HD 2"])])
    folder, _ = freeze(
        tmp_path,
        [(p.key, p.science.stars[0]) for p in [a, b]],
        lambda q: [
            candidate("HD 1", aliases=["HIP 3"] if q.identifier == "HD 2" else [])
        ],
    )
    old, _, before = build([a, b], simbad=folder)
    assert before[0]["simbad"]["selected"]["aliases"] == []
    c = paper(3, [source_star(["HIP 3"])])
    _, manifest, stars = build([c], "other-alias", previous=old)
    assert len(stars) == 1 and manifest.next_id == 2
    assert set(stars[0]["sources"]) == {a.key, b.key, c.key}


@pytest.mark.parametrize("direct_old_id", [False, True])
def test_rejected_frozen_alias_conflict_controls_snapshot_and_name(
    build, tmp_path, direct_old_id
):
    a, b = paper(1, [source_star(["HD 1"])]), paper(2, [source_star(["HD 2"])])
    folder, _ = freeze(
        tmp_path,
        [(p.key, p.science.stars[0]) for p in [a, b]],
        lambda q: [
            candidate(
                q.identifier, aliases=["HIP 3" if q.identifier == "HD 1" else "HIP 4"]
            )
        ],
    )
    old, _, before = build([a, b], simbad=folder)
    c = paper(3, [source_star(["HD 1" if direct_old_id else "HIP 3", "HIP 4"])])
    current, _, stars = build([c], "conflict-state", previous=old)
    affected = next(s for s in stars if c.key in s["sources"])
    assert affected["simbad"]["status"] == "conflict"
    assert affected["simbad"]["selected"] is None
    assert affected["display_name"] == affected["catalog_id"]
    assert all(
        q in affected["simbad"]["queries"]
        for s in before
        for q in s["simbad"]["queries"]
    )
    assert {"HD 1", "HD 2"} <= {c["main_id"] for c in affected["simbad"]["candidates"]}
    for s in stars:
        if s is not affected:
            original = next(v for v in before if v["catalog_id"] == s["catalog_id"])
            assert s["simbad"] == original["simbad"]
            assert s["display_name"] == original["display_name"]
    verify_catalog(current)
    _, _, continued = build([], "conflict-state-continued", previous=current)
    later = next(s for s in continued if c.key in s["sources"])
    assert later["catalog_id"] == affected["catalog_id"]
    assert later["simbad"]["status"] == "conflict"
    assert later["display_name"] == later["catalog_id"]

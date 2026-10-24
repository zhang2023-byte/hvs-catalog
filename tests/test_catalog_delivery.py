"""Final deliveries are frozen inputs, not mutable execution workspaces."""

import json
import shutil

import pytest
from test_quantities import quantities, run_full, value
from test_roster import entry, submission

from hvs_catalog.catalogue import build_catalog, verify_catalog
from hvs_catalog.catalogue.delivery import inspect_results, selection_from_results
from hvs_catalog.catalogue.inputs import digest, json_bytes, load_paper, load_selection
from hvs_catalog.cli import main
from hvs_catalog.pipeline.results import current_result
from hvs_catalog.records.catalog import CatalogSelection, PaperSelection
from hvs_catalog.records.common import ArtifactRef


def file_ref(root, path):
    return ArtifactRef(
        path=str(path.relative_to(root)), sha256=digest(path.read_bytes())
    )


def publish_final(root, history, source_ref, *, run=None):
    """Create the documented delivery using only fabricated pipeline results."""
    loaded = load_paper(PaperSelection(workspace=str(history), run_ref=source_ref))
    folder = (
        root / "runs" / loaded.science.metadata.arxiv_id.replace("/", "_") / "results"
    )
    folder.mkdir(parents=True)
    (folder / "literature_contribution.json").write_bytes(loaded.science_bytes)
    science_ref = file_ref(root, folder / "literature_contribution.json")
    audit = loaded.audit.model_dump(mode="json")
    audit["metadata"]["result_ref"] = science_ref.model_dump()
    (folder / "evidence_audit.json").write_bytes(json_bytes(audit))
    final = {
        "format": "hvs.final-literature-result",
        "version": 1,
        "arxiv_id": loaded.science.metadata.arxiv_id,
        "arxiv_version": loaded.science.metadata.version,
        "completion": loaded.source.result_status,
        "result_ref": science_ref.model_dump(),
        "evidence_audit_ref": file_ref(
            root, folder / "evidence_audit.json"
        ).model_dump(),
        "source": {
            "root": str(history.relative_to(root)),
            "run_ref": source_ref.model_dump(),
        },
    }
    if run:
        final.update(run)
    (folder / "run.json").write_bytes(json_bytes(final))
    return folder


def migrate_native(prepared, tmp_path, *, empty=False, partial=False):
    space = prepared[0]
    payloads = (
        [submission([])]
        if empty
        else [
            submission([entry("Gaia DR3 1234567890123456789")]),
            quantities([value("520")]),
        ]
    )
    if partial:
        payloads[-1]["quantities"][0]["values"].append(
            {"value": "90", "evidence": [{"location": "T:p99-b0", "quote": "Absent"}]}
        )
    handle, _, _, _ = run_full(prepared, payloads, limit=0)
    run_ref, _ = current_result(space, handle.execution_id)
    root = tmp_path / "delivery"
    history = root / "history" / "extraction"
    history.parent.mkdir(parents=True)
    shutil.copytree(space.root, history)
    folder = publish_final(root, history, run_ref)
    return root, folder, history, run_ref


@pytest.mark.parametrize(
    "empty,partial", [(False, False), (True, False), (False, True)]
)
def test_final_cli_export_and_read_only_inspection(
    prepared, tmp_path, capsys, empty, partial
):
    root, folder, _, _ = migrate_native(
        prepared, tmp_path, empty=empty, partial=partial
    )
    before = {p: p.read_bytes() for p in folder.iterdir()}
    assert main(["catalog", "check-inputs", "--results", str(root)]) == 0
    counts = json.loads(capsys.readouterr().out)
    assert counts == {
        "valid": True,
        "papers": 1,
        "empty_papers": int(empty),
        "objects": int(not empty),
        "values": int(not empty),
        "audit_observations": 0,
    }
    selected = tmp_path / "selected.json"
    assert (
        main(
            [
                "catalog",
                "select",
                "--results",
                str(root),
                "--data-version",
                "synthetic",
                "--output",
                str(selected),
            ]
        )
        == 0
    )
    capsys.readouterr()
    output = tmp_path / "catalog"
    assert (
        main(
            ["catalog", "build", "--selection", str(selected), "--output", str(output)]
        )
        == 0
    )
    manifest = verify_catalog(output)
    assert len(manifest.papers) == 1
    paper = next(iter(manifest.papers.values()))
    assert (output / paper.science.path).read_bytes() == before[
        folder / "literature_contribution.json"
    ]
    assert len(manifest.stars) == int(not empty)
    if not empty:
        star = json.loads((output / manifest.stars[0].file.path).read_bytes())
        assert star["display_name"] == "Gaia DR3 1234567890123456789"
        assert star["quantities"][0]["values"][0]["value"] == "520"
        source = next(iter(star["sources"].values()))
        assert source["records"] == ["/stars/0"]
        assert "result_status" not in source and "objects" not in source
        assert paper.source.result_status == ("partial" if partial else "complete")
        assert paper.source.objects[0].status == paper.source.result_status
        assert star["simbad"]["status"] == "not_queried"
    assert before == {p: p.read_bytes() for p in folder.iterdir()}
    assert not (root / ".state").exists()


def test_final_and_original_inputs_produce_same_science(prepared, tmp_path):
    root, _, history, original_ref = migrate_native(prepared, tmp_path)
    old = CatalogSelection(
        data_version="old",
        papers=[PaperSelection(workspace=str(history), run_ref=original_ref)],
    )
    previous = tmp_path / "old-catalog"
    old_manifest = build_catalog(old, previous)
    selected = selection_from_results(root, "new")
    manifest = build_catalog(selected, tmp_path / "new-catalog")
    assert manifest.stars[0].catalog_id == old_manifest.stars[0].catalog_id
    assert manifest.stars[0].display_name == old_manifest.stars[0].display_name
    # A relocated delivery uses only references relative to its declared root.
    moved = tmp_path / "moved"
    shutil.move(root, moved)
    selected.papers[0].workspace = "moved"
    path = tmp_path / "selection.json"
    path.write_bytes(json_bytes(selected))
    paper = load_paper(load_selection(path).papers[0])
    assert paper.science.stars[0].quantities[0].values[0].value == "520"
    assert inspect_results(moved)["papers"] == 1


@pytest.mark.parametrize(
    "fault",
    [
        "version",
        "id",
        "completion",
        "science_hash",
        "audit_binding",
        "audit_content",
        "science_content",
        "source_hash",
        "source_escape",
        "source_symlink",
        "extra",
        "missing",
        "orphan",
    ],
)
def test_final_rejects_changed_or_misbound_inputs(prepared, tmp_path, fault):
    root, folder, history, _ = migrate_native(prepared, tmp_path)
    run = json.loads((folder / "run.json").read_bytes())
    if fault == "version":
        run["version"] = 2
    elif fault == "id":
        run["arxiv_id"] = "2601.99999"
    elif fault == "completion":
        run["completion"] = "partial"
    elif fault == "science_hash":
        (folder / "literature_contribution.json").write_bytes(b"{}")
    elif fault in {"audit_binding", "audit_content"}:
        audit = json.loads((folder / "evidence_audit.json").read_bytes())
        if fault == "audit_binding":
            audit["metadata"]["result_ref"]["path"] = "different.json"
        else:
            audit["quote_mismatches"] = [
                {
                    "result_path": "/stars/0/identifiers/0/evidence/0",
                    "location": "T:p1-b0",
                    "quote": "Forged quote",
                    "reason": "unknown",
                }
            ]
        (folder / "evidence_audit.json").write_bytes(json_bytes(audit))
        run["evidence_audit_ref"] = file_ref(
            root, folder / "evidence_audit.json"
        ).model_dump()
    elif fault == "science_content":
        science = json.loads((folder / "literature_contribution.json").read_bytes())
        science["stars"][0]["quantities"][0]["values"][0]["value"] = "999"
        (folder / "literature_contribution.json").write_bytes(json_bytes(science))
        run["result_ref"] = file_ref(
            root, folder / "literature_contribution.json"
        ).model_dump()
        audit = json.loads((folder / "evidence_audit.json").read_bytes())
        audit["metadata"]["result_ref"] = run["result_ref"]
        (folder / "evidence_audit.json").write_bytes(json_bytes(audit))
        run["evidence_audit_ref"] = file_ref(
            root, folder / "evidence_audit.json"
        ).model_dump()
    elif fault == "source_hash":
        run["source"]["run_ref"]["sha256"] = "a" * 64
    elif fault == "source_escape":
        run["source"]["root"] = "../elsewhere"
    elif fault == "source_symlink":
        alias = root / "history-link"
        alias.symlink_to(history, target_is_directory=True)
        run["source"]["root"] = "history-link"
    elif fault == "extra":
        (folder / "old-result.json").write_text("{}")
    elif fault == "missing":
        (folder / "evidence_audit.json").unlink()
    else:
        (root / "runs/orphan/results").mkdir(parents=True)
    (folder / "run.json").write_bytes(json_bytes(run))
    from hvs_catalog.workspace import WorkspaceError

    with pytest.raises((ValueError, OSError, WorkspaceError)):
        if fault == "orphan":
            selection_from_results(root, "bad")
        else:
            load_paper(
                PaperSelection(
                    workspace=str(root), run_ref=file_ref(root, folder / "run.json")
                )
            )


def test_frozen_final_run_and_cli_argument_contract(prepared, tmp_path, capsys):
    root, folder, _, _ = migrate_native(prepared, tmp_path)
    selected = selection_from_results(root, "fixed")
    run = json.loads((folder / "run.json").read_bytes())
    run["completion"] = "partial"
    (folder / "run.json").write_bytes(json_bytes(run))
    with pytest.raises(ValueError, match="hash mismatch"):
        load_paper(selected.papers[0])
    assert (
        main(
            [
                "catalog",
                "select",
                "--results",
                str(root),
                "--space",
                str(root),
                "--data-version",
                "v",
                "--output",
                str(tmp_path / "s.json"),
            ]
        )
        == 2
    )
    assert "--space" in capsys.readouterr().err
    assert (
        main(
            [
                "catalog",
                "select",
                "--index",
                "missing.json",
                "--data-version",
                "v",
                "--output",
                str(tmp_path / "s.json"),
            ]
        )
        == 2
    )


def test_old_style_arxiv_id_and_complete_final_selection(
    prepared, tmp_path, monkeypatch
):
    import materials_helpers
    from test_roster import prepared as prepare_fixture

    root, _, _, _ = migrate_native(prepared, tmp_path)
    previous = tmp_path / "initial-catalog"
    first = build_catalog(selection_from_results(root, "first"), previous)
    monkeypatch.setattr(materials_helpers, "PAPER", "astro-ph/9901001")
    later = tmp_path / "later"
    later.mkdir()
    fixture = prepare_fixture.__wrapped__(later)
    second_prepared = next(fixture)
    try:
        handle, report, _, _ = run_full(
            second_prepared,
            [
                submission(
                    [
                        entry("Gaia DR3 1234567890123456789"),
                        entry("Gaia DR3 9876543210987654321", 2, "follow_up"),
                    ]
                ),
                quantities([value("540")]),
                quantities(
                    [
                        {
                            "value": "550",
                            "unit": "km/s",
                            "evidence": [
                                {
                                    "location": "T:p1-b1",
                                    "quote": "B is a previous unbound candidate studied here.",
                                }
                            ],
                        }
                    ]
                ),
            ],
        )
        assert report.result_status == "complete"
        source_ref, _ = current_result(second_prepared[0], handle.execution_id)
        history = root / "history/later-extraction"
        shutil.copytree(second_prepared[0].root, history)
        folder = publish_final(root, history, source_ref)
        assert folder == root / "runs/astro-ph_9901001/results"
        selected = selection_from_results(root, "second")
        assert len(selected.papers) == 2
        output = tmp_path / "increment"
        current = build_catalog(selected, output)
        assert len(current.stars) == 2
        old = next(
            s for s in current.stars if s.catalog_id == first.stars[0].catalog_id
        )
        assert old.display_name == first.stars[0].display_name
        star = json.loads((output / old.file.path).read_bytes())
        assert {v["value"] for q in star["quantities"] for v in q["values"]} == {
            "520",
            "540",
        }
        assert len(star["sources"]) == 2
        new = next(s for s in current.stars if s.catalog_id != old.catalog_id)
        assert new.catalog_id == "hvs-catalog-000002"
        assert (
            "astro-ph_9901001v1__literature_contribution.json"
            in current.papers["astro-ph/9901001v1"].science.path
        )
        assert inspect_results(root)["objects"] == 3
        assert verify_catalog(output) == current
    finally:
        fixture.close()

"""Stage isolation, canonical material storage, and portable literature recovery."""

import json
import subprocess
import sys
from pathlib import Path

import pytest
from materials_helpers import FakeMinerU, make_pdf
from pydantic import SecretStr
from test_preparation_batch import END, START, SyntheticApi
from test_roster import ScriptedAdapter, submission

from hvs_catalog.cli import main
from hvs_catalog.llm import RuntimeConfig
from hvs_catalog.llm.managed import CallService
from hvs_catalog.materials import MinerUError
from hvs_catalog.pipeline import ExtractionConfig, resume_extraction, start_extraction
from hvs_catalog.pipeline.acquisition import AcquisitionService
from hvs_catalog.pipeline.preparation_batch import (
    PreparationBatchService,
    export_manifest,
)
from hvs_catalog.pipeline.results import current_result
from hvs_catalog.workspace import (
    WorkspaceConflictError,
    backup_workspace,
    initialize_workspace,
    open_workspace,
    restore_workspace,
)

TERMS = ("hypervelocity star", "high velocity star", "unbound star")


@pytest.mark.integration
def test_discard_round_preserves_preparations_and_allows_paper_named_restart(tmp_path):
    from hvs_catalog.workspace.retention import discard_literature_extractions

    space = initialize_workspace(tmp_path / "literature", literature=True)
    service = PreparationBatchService(space, api=api_fixture(), runner=FakeMinerU())
    try:
        create(service)
        service.discover("sample")
        service.download("sample", identifiers=["1804.00001"])
        service.parse("sample", identifiers=["1804.00001"])
        before = service.get_manifest("sample")
        records = space.store().list_records()
        executions = space.store().list_executions()
        material_bytes = {
            p.relative_to(space.root): p.read_bytes()
            for p in (space.root / "papers").rglob("*")
            if p.is_file()
        }
        prep = before["papers"][0]["preparation"]["execution_id"]
        config = ExtractionConfig(
            runtime=RuntimeConfig(quota_store=tmp_path / "quota.db")
        )
        completed = start_extraction(
            space,
            preparation_execution_id=prep,
            configuration=config,
            request_key="discard-complete",
        )
        report = resume_extraction(
            space,
            completed.execution_id,
            service=CallService(
                config.runtime,
                credential=SecretStr("offline-test-key-123"),
                adapter=ScriptedAdapter([submission([])]),
            ),
        )
        assert report.result_status == "complete"
        pending = start_extraction(
            space,
            preparation_execution_id=prep,
            configuration=config,
            request_key="discard-pending",
        )
        with pytest.raises(ValueError, match="match all current"):
            discard_literature_extractions(
                space, expected_execution_ids=[completed.execution_id]
            )
        with pytest.raises(ValueError, match="match all current"):
            discard_literature_extractions(
                space,
                expected_execution_ids=[
                    completed.execution_id,
                    pending.execution_id,
                    prep,
                ],
            )
        assert len(space.store().list_executions()) == len(executions) + 2
        result = discard_literature_extractions(
            space, expected_execution_ids=[completed.execution_id, pending.execution_id]
        )
        assert result["discarded_executions"] == 2
        assert space.store().list_records() == records
        assert space.store().list_executions() == executions
        assert service.get_manifest("sample")["papers"] == before["papers"]
        assert not any((space.root / "runs").iterdir())
        assert all(
            (space.root / p).read_bytes() == data for p, data in material_bytes.items()
        )
        backup_workspace(space, tmp_path / "clean-backup")
        restored = restore_workspace(
            tmp_path / "clean-backup", tmp_path / "clean-restored"
        )
        restored.close()
        with pytest.raises(ValueError, match="match its arXiv paper"):
            start_extraction(
                space,
                preparation_execution_id=prep,
                configuration=config,
                request_key="wrong-paper-name",
                execution_id="1804.00002",
            )
        handle = start_extraction(
            space,
            preparation_execution_id=prep,
            configuration=config,
            request_key="paper-named-restart",
            execution_id="1804.00001",
        )
        ref, _ = current_result(space, handle.execution_id)
        assert ref.path.startswith("runs/1804.00001/results/")
        assert not list((space.root / "runs").glob("exec-*"))
    finally:
        space.close()


def api_fixture():
    api = SyntheticApi()
    api.pages = {term.removesuffix("s"): papers for term, papers in api.pages.items()}
    return api


def create(service):
    service.create_batch("sample", submitted_from=START, submitted_to=END, terms=TERMS)


def test_offline_init_stores_scope_without_starting_any_stage(tmp_path, monkeypatch):
    import hvs_catalog.cli_preparation as cli

    api, parser = api_fixture(), FakeMinerU()
    monkeypatch.setattr(
        cli,
        "_build_service",
        lambda space, **kwargs: PreparationBatchService(space, api=api, runner=parser),
    )
    root = tmp_path / "literature"
    common = ["--space", str(root), "--batch", "sample"]
    scope = ["--from", "2018-04-25", "--to", "2026-04-30"]
    for term in TERMS:
        scope += ["--term", term]
    assert main(["prepare", "init", *common, *scope]) == 0
    assert main(["prepare", "init", *common, *scope]) == 0
    assert main(["prepare", "status", *common]) == 3
    assert main(["prepare", "download", *common]) == 1
    assert main(["prepare", "parse", *common]) == 1
    assert not api.search_calls and not api.metadata_calls and not api.download_calls
    assert parser.calls == 0
    assert {p.name for p in root.iterdir()} == {"papers", "runs", "reports", ".state"}
    report = json.loads((root / "reports/preparation-manifest.json").read_text())
    assert report["scope"]["terms"] == list(TERMS)
    assert report["summary"]["queries"] == 291
    assert (
        report["summary"]["completed_queries"]
        == report["summary"]["unique_papers"]
        == 0
    )
    assert report["rounds"][0]["query"] == (
        'all:"hypervelocity star" AND submittedDate:[201804250000 TO 201804302359]'
    )
    assert (
        main(
            [
                "prepare",
                "init",
                *common,
                "--from",
                "2019-01-01",
                "--to",
                "2026-04-30",
                "--term",
                TERMS[0],
            ]
        )
        == 1
    )
    space = open_workspace(root)
    try:
        backup_workspace(space, tmp_path / "backup")
        restored = restore_workspace(tmp_path / "backup", tmp_path / "restored")
        restored.close()
    finally:
        space.close()
    assert {p.name for p in (tmp_path / "restored").iterdir()} == {
        "papers",
        "runs",
        "reports",
        ".state",
    }


def test_canonical_files_and_saved_scope_are_used_by_each_stage(tmp_path, monkeypatch):
    import hvs_catalog.cli_preparation as cli

    root = tmp_path / "literature"
    api, parser = api_fixture(), FakeMinerU()
    space = initialize_workspace(root, literature=True)
    create(PreparationBatchService(space, api=api, runner=parser))
    space.close()
    monkeypatch.setattr(
        cli,
        "_build_service",
        lambda space, **kwargs: PreparationBatchService(space, api=api, runner=parser),
    )
    common = ["--space", str(root), "--batch", "sample"]
    assert main(["prepare", "discover", *common]) == 0
    assert api.search_calls == list(TERMS)
    assert not api.download_calls and parser.calls == 0
    assert main(["prepare", "download", *common]) == 0
    assert len(api.download_calls) == 3 and parser.calls == 0
    assert main(["prepare", "parse", *common]) == 0
    assert main(["prepare", "parse", *common]) == 0
    assert parser.calls == 3
    assert (
        main(["prepare", "parse", *common, "--literature", str(tmp_path / "duplicate")])
        == 1
    )
    assert not (tmp_path / "duplicate").exists()
    assert len(list(root.rglob("*.pdf"))) == len(list(root.rglob("*.zip"))) == 3
    assert not (root / "sources").exists()
    assert not any((root / "runs").iterdir())
    for paper in ("1804.00001", "1804.00002", "1804.00003"):
        folder = root / "papers" / paper
        assert (folder / "paper.pdf").is_file()
        assert (folder / "metadata.json").is_file()
        assert (folder / "mineru/result.zip").is_file()
        assert (folder / "mineru/raw/middle.json").is_file()
        assert (folder / "mineru/pdf-blocks.json").is_file()
    report_path = root / "reports/preparation-manifest.json"
    report_path.write_text('{"obsolete": true}')
    assert main(["prepare", "status", *common]) == 0
    report = json.loads(report_path.read_text())
    assert report["summary"]["prepared"] == 3
    assert all(
        p["preparation"]["prepared_input_ref"]["path"].startswith(".state/")
        for p in report["papers"]
    )
    space = open_workspace(root)
    try:
        local = tmp_path / "other.pdf"
        local.write_bytes(make_pdf("different PDF"))
        with pytest.raises(WorkspaceConflictError):
            AcquisitionService(space, api).import_local_pdf(
                local, arxiv_id="1804.00001v8", provenance="synthetic"
            )
    finally:
        space.close()


def test_failure_retry_keeps_successful_materials_and_refreshes_summary(tmp_path):
    class FailingParser(FakeMinerU):
        fail = True

        def parse(self, path, page_count):
            if self.fail and Path(path).parent.name == "1804.00002":
                raise MinerUError("synthetic parser failure")
            return super().parse(path, page_count)

    root = tmp_path / "literature"
    space = initialize_workspace(root, literature=True)
    api, parser = api_fixture(), FailingParser()
    service = PreparationBatchService(space, api=api, runner=parser)
    try:
        create(service)
        service.discover("sample")
        service.download("sample")
        service.parse("sample")
        before = service.get_manifest("sample")
        assert before["summary"]["prepared"] == 2
        assert not (root / "papers/1804.00002/mineru").exists()
        first = (root / "papers/1804.00001/mineru/result.zip").read_bytes()
        parser.fail = False
        service.parse("sample", identifiers=["1804.00002"], retry_failed=True)
        after = service.get_manifest("sample")
        export_manifest(after, root / "reports")
        assert after["summary"]["prepared"] == 3
        assert parser.calls == 3
        assert (root / "papers/1804.00001/mineru/result.zip").read_bytes() == first
        recovered = next(p for p in after["papers"] if p["arxiv_id"] == "1804.00002")
        assert len(recovered["preparation"]["previous_execution_ids"]) == 1
        assert recovered["issue"] is None
    finally:
        space.close()


@pytest.mark.integration
def test_extraction_and_backup_reopen_in_an_independent_process(tmp_path):
    space = initialize_workspace(tmp_path / "literature", literature=True)
    api, parser = api_fixture(), FakeMinerU()
    service = PreparationBatchService(space, api=api, runner=parser)
    try:
        create(service)
        service.discover("sample")
        service.download("sample", identifiers=["1804.00001"])
        service.parse("sample", identifiers=["1804.00001"])
        prep = service.get_manifest("sample")["papers"][0]["preparation"][
            "execution_id"
        ]
        config = ExtractionConfig(
            runtime=RuntimeConfig(quota_store=tmp_path / "quota.db")
        )
        handle = start_extraction(
            space,
            preparation_execution_id=prep,
            configuration=config,
            request_key="empty-synthetic",
        )
        report = resume_extraction(
            space,
            handle.execution_id,
            service=CallService(
                config.runtime,
                credential=SecretStr("offline-test-key-123"),
                adapter=ScriptedAdapter([submission([])]),
            ),
        )
        assert report.result_status == "complete", report.to_dict()
        ref, result = current_result(space, handle.execution_id)
        assert ref.path.startswith(f"runs/{handle.execution_id}/results/")
        assert result.result_ref and result.evidence_audit_ref
        backup_workspace(space, tmp_path / "backup")
        restored = restore_workspace(tmp_path / "backup", tmp_path / "restored")
        restored.close()
    finally:
        space.close()
    script = """
import sys
from hvs_catalog.workspace import open_workspace
from hvs_catalog.materials import MaterialsService
from hvs_catalog.pipeline.results import current_result
space = open_workspace(sys.argv[1], read_only=True)
try:
    assert MaterialsService(space).get_prepared_input(sys.argv[2])['summary']['pages'] == 1
    ref, result = current_result(space, sys.argv[3])
    assert result.result_ref and result.evidence_audit_ref
    space.read_file(result.result_ref)
    space.read_file(result.evidence_audit_ref)
finally:
    space.close()
"""
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            script,
            str(tmp_path / "restored"),
            prep,
            handle.execution_id,
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr


@pytest.mark.integration
def test_official_cloud_zip_is_shared_with_preparation(tmp_path, monkeypatch):
    from test_mineru_official import api_server

    from hvs_catalog.materials import OfficialMinerU

    monkeypatch.setenv("MINERU_TOKEN", "synthetic")
    space = initialize_workspace(tmp_path / "literature", literature=True)
    with api_server() as (url, calls, _):
        parser = OfficialMinerU(space)
        parser.api_url = url
        service = PreparationBatchService(space, api=api_fixture(), runner=parser)
        try:
            create(service)
            service.discover("sample")
            service.download("sample", identifiers=["1804.00001"])
            service.parse("sample", identifiers=["1804.00001"])
            paper = service.get_manifest("sample")["papers"][0]
            assert paper["preparation"]["state"] == "finished", paper
            assert list(space.root.rglob("*.zip")) == [
                space.root / "papers/1804.00001/mineru/result.zip"
            ]
            count = len(calls)
            service.parse("sample", identifiers=["1804.00001"])
            assert len(calls) == count
            assert (space.root / "papers/1804.00001/mineru/raw/layout.json").is_file()
        finally:
            space.close()

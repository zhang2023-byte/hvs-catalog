"""Independent synthetic scope, stage isolation, persistence, and recovery expectations."""

from __future__ import annotations

import csv
import json
import os
import subprocess
import sys
from datetime import date

import pytest
from materials_helpers import FakeMinerU, make_pdf

from hvs_catalog.cli import main
from hvs_catalog.pipeline.acquisition import (
    AcquisitionService,
    ArxivSearchPage,
    parse_arxiv_identifier,
)
from hvs_catalog.pipeline.acquisition.lifecycle import AcquisitionError
from hvs_catalog.pipeline.acquisition.protocol import (
    ArxivIdBatchResult,
    ArxivIdLookup,
    ArxivPaperMeta,
    ArxivSourceDownload,
    PaginationAnomaly,
)
from hvs_catalog.pipeline.preparation_batch import (
    PreparationBatchService,
    export_manifest,
    monthly_requests,
)
from hvs_catalog.workspace import (
    WorkspaceConflictError,
    backup_workspace,
    initialize_workspace,
    open_workspace,
    restore_workspace,
)

TERMS = ("hypervelocity stars", "high velocity stars", "unbound stars")
START, END = date(2018, 4, 25), date(2018, 4, 30)


def meta(identifier, version=1, published="2018-04-25T00:00:00Z"):
    return ArxivPaperMeta(
        canonical_id=identifier,
        version=version,
        api_id_url=f"http://arxiv.org/abs/{identifier}v{version}",
        title=f'Synthetic, "paper" {identifier}',
        authors=("A. Author",),
        categories=("physics.gen-ph",),
        primary_category="physics.gen-ph",
        published=published,
        updated="2026-10-11T00:00:00Z",
        abstract="Independent synthetic metadata.",
        doi=None,
        journal_ref=None,
    )


class SyntheticApi:
    def __init__(self):
        self.search_calls, self.metadata_calls, self.download_calls = [], [], []
        self.pages = {
            TERMS[0]: [
                meta("1804.00001"),
                meta("1804.00002", published="2018-04-30T23:59:59Z"),
            ],
            TERMS[1]: [
                meta("1804.00001", 2),
                meta("1804.00003", published="2018-04-27T12:00:00Z"),
                meta("1804.00004", published="2018-04-25T01:00:00+02:00"),
            ],
            TERMS[2]: [
                meta("1804.00002", published="2018-04-30T23:59:59Z"),
                meta("1804.00005", published="2018-04-26T00:00:00"),
                meta("1804.00006", published="2018-05-01T00:00:00Z"),
            ],
        }
        self.latest_version = 7
        self.fail_download = None
        self.fail_term = None
        self.anomalies = ()

    def search(self, request):
        assert not request.categories
        self.search_calls.append(request.terms)
        if request.terms == self.fail_term:
            raise OSError("synthetic service interruption")
        entries = tuple(self.pages[request.terms])
        return ArxivSearchPage(
            request=request,
            response_url=request.to_request_url(),
            fetched_at=1_791_676_800.0,
            total_results=len(entries),
            returned_start_index=request.start,
            entries=entries,
            anomalies=self.anomalies,
        )

    def metadata_by_ids(self, identifiers):
        self.metadata_calls.extend(identifiers)
        return ArxivIdBatchResult(
            response_url="synthetic metadata",
            fetched_at=1_791_676_800.0,
            lookups=tuple(
                ArxivIdLookup(
                    parse_arxiv_identifier(i), "found", meta(i, self.latest_version)
                )
                for i in identifiers
            ),
            anomalies=(),
        )

    def download_pdf(self, identifier):
        self.download_calls.append(identifier)
        if identifier == self.fail_download:
            raise OSError("synthetic PDF failure")
        payload = make_pdf("Independent synthetic PDF.")
        return ArxivSourceDownload(
            identifier=parse_arxiv_identifier(identifier),
            final_url="https://arxiv.org/pdf/" + identifier,
            content_type="application/pdf",
            payload=payload,
            byte_count=len(payload),
            fetched_at=1_791_676_800.0,
        )


def create(service, batch="sample"):
    return service.create_batch(
        batch, submitted_from=START, submitted_to=END, terms=TERMS
    )


def test_scope_month_boundaries_and_three_literal_queries(tmp_path):
    space = initialize_workspace(tmp_path / "space")
    service = PreparationBatchService(space)
    batch = create(service)
    assert [r.search_query() for r in monthly_requests(batch)] == [
        'all:"hypervelocity stars" AND submittedDate:[201804250000 TO 201804302359]',
        'all:"high velocity stars" AND submittedDate:[201804250000 TO 201804302359]',
        'all:"unbound stars" AND submittedDate:[201804250000 TO 201804302359]',
    ]
    multi = service.create_batch(
        "months",
        submitted_from=date(2018, 12, 25),
        submitted_to=date(2019, 2, 2),
        terms=[TERMS[0]],
    )
    assert [(r.submitted_from, r.submitted_to) for r in monthly_requests(multi)] == [
        (date(2018, 12, 25), date(2018, 12, 31)),
        (date(2019, 1, 1), date(2019, 1, 31)),
        (date(2019, 2, 1), date(2019, 2, 2)),
    ]
    assert create(service) == batch
    with pytest.raises(WorkspaceConflictError):
        service.create_batch(
            "sample", submitted_from=START, submitted_to=END, terms=[TERMS[0]]
        )
    with pytest.raises(ValueError):
        service.create_batch(
            "empty", submitted_from=START, submitted_to=END, terms=[""]
        )
    space.close()


def test_discovery_dedup_date_checks_resume_and_read_only_exports(tmp_path):
    api, runner = SyntheticApi(), FakeMinerU()
    space = initialize_workspace(tmp_path / "space")
    service = PreparationBatchService(space, api=api, runner=runner)
    create(service)
    service.discover("sample", max_queries=1)
    assert not service.get_manifest("sample")["discovery_complete"]
    space.close()
    space = open_workspace(tmp_path / "space")
    service = PreparationBatchService(space, api=api, runner=runner)
    service.discover("sample")
    manifest = service.get_manifest("sample")
    assert manifest["discovery_complete"]
    assert manifest["summary"] == {
        "queries": 3,
        "completed_queries": 3,
        "unique_papers": 3,
        "accepted_entries": 5,
        "duplicate_entries": 2,
        "excluded_entries": 3,
        "terms": {TERMS[0]: 2, TERMS[1]: 2, TERMS[2]: 1},
        "downloaded": 0,
        "prepared": 0,
    }
    by_id = {p["arxiv_id"]: p for p in manifest["papers"]}
    assert by_id["1804.00001"]["matched_terms"] == [TERMS[0], TERMS[1]]
    assert by_id["1804.00001"]["metadata"]["version"] == 2
    assert len(by_id["1804.00001"]["discovery_sources"]) == 2
    assert [p["reason"] for p in manifest["excluded"]] == [
        "outside_window",
        "invalid_date",
        "outside_window",
    ]
    assert all(p["download"]["versioned_id"] is None for p in manifest["papers"])
    assert not AcquisitionService(space).list_selections()
    service.discover("sample")
    paths = export_manifest(manifest, tmp_path / "reports")
    assert json.loads(paths[0].read_text())["summary"] == manifest["summary"]
    rows = list(csv.DictReader(paths[1].open()))
    assert len(rows) == 3 and rows[0]["title"] == 'Synthetic, "paper" 1804.00001'
    assert api.search_calls == list(TERMS)
    assert api.metadata_calls == api.download_calls == [] and runner.calls == 0
    backup_workspace(space, tmp_path / "backup")
    space.close()
    restore_workspace(tmp_path / "backup", tmp_path / "restored")
    space = open_workspace(tmp_path / "restored")
    assert (
        PreparationBatchService(space).get_manifest("sample")["summary"]
        == manifest["summary"]
    )
    space.close()


def test_failed_or_anomalous_discovery_blocks_download_and_keeps_partial_results(
    tmp_path,
):
    api = SyntheticApi()
    space = initialize_workspace(tmp_path / "space")
    service = PreparationBatchService(space, api=api)
    create(service)
    api.fail_term = TERMS[1]
    with pytest.raises(OSError):
        service.discover("sample")
    manifest = service.get_manifest("sample")
    assert [r["state"] for r in manifest["rounds"]] == ["complete", "failed", "pending"]
    assert manifest["summary"]["unique_papers"] == 2
    with pytest.raises(AcquisitionError, match="未完整"):
        service.download("sample")
    api.fail_term = None
    api.anomalies = (PaginationAnomaly.TOTAL_MISSING,)
    service.discover("sample")
    assert not service.get_manifest("sample")["discovery_complete"]
    assert not api.metadata_calls and not api.download_calls
    space.close()


def test_download_latest_at_acquisition_freezes_targets_and_keeps_other_successes(
    tmp_path,
):
    api, runner = SyntheticApi(), FakeMinerU()
    space = initialize_workspace(tmp_path / "space")
    service = PreparationBatchService(space, api=api, runner=runner)
    create(service)
    service.discover("sample")
    # Existing selections must not prevent selecting the latest version for this batch.
    old = tmp_path / "old.pdf"
    old.write_bytes(make_pdf("Independent old PDF."))
    AcquisitionService(space).import_local_pdf(
        old, arxiv_id="1804.00001v1", provenance="synthetic old version"
    )
    api.fail_download = "1804.00002v7"
    service.download("sample")
    manifest = service.get_manifest("sample")
    assert manifest["summary"]["downloaded"] == 2 and runner.calls == 0
    assert {p["download"]["versioned_id"] for p in manifest["papers"]} == {
        "1804.00001v7",
        "1804.00002v7",
        "1804.00003v7",
    }
    api.latest_version, api.fail_download = 9, None
    space.close()
    space = open_workspace(tmp_path / "space")
    service = PreparationBatchService(space, api=api, runner=runner)
    service.download("sample")
    assert service.get_manifest("sample")["summary"]["downloaded"] == 3
    assert api.metadata_calls == ["1804.00001", "1804.00003", "1804.00002"]
    assert api.download_calls.count("1804.00002v7") == 2
    assert api.download_calls.count("1804.00001v7") == 1
    assert runner.calls == 0
    # A changed global selection cannot change the fixed batch source.
    AcquisitionService(space).import_local_pdf(
        old, arxiv_id="1804.00001v9", provenance="independent synthetic selection"
    )
    service.parse("sample")
    prepared = service.get_manifest("sample")
    assert prepared["summary"]["prepared"] == 3 and runner.calls == 3
    service.parse("sample")
    assert runner.calls == 3
    first = next(p for p in prepared["papers"] if p["arxiv_id"] == "1804.00001")
    saved = service.materials.get_preparation(first["preparation"]["execution_id"])
    assert saved["config"]["request"]["source_ref"] == first["download"]["source_ref"]
    with pytest.raises(ValueError, match="不在"):
        service.download("sample", identifiers=["1804.99999"])
    space.close()


def test_cli_discover_status_and_parse_never_download_implicitly(
    tmp_path, monkeypatch, capsys
):
    import hvs_catalog.cli_preparation as cli

    api, runner = SyntheticApi(), FakeMinerU()
    monkeypatch.setattr(
        cli,
        "_build_service",
        lambda space, **kwargs: PreparationBatchService(space, api=api, runner=runner),
    )
    monkeypatch.chdir(tmp_path)
    common = [
        "--space",
        str(tmp_path / "space"),
        "--batch",
        "cli",
        "--output",
        str(tmp_path / "reports"),
    ]
    scope = ["--init", "--from", "2018-04-25", "--to", "2018-04-30"]
    for term in TERMS:
        scope += ["--term", term]
    assert main(["prepare", "discover", *common, *scope]) == 0
    assert main(["prepare", "status", *common]) == 0
    assert main(["prepare", "parse", *common]) == 3
    assert not api.metadata_calls and not api.download_calls and runner.calls == 0
    assert main(["prepare", "download", *common, "--id", "1804.00001"]) == 0
    assert api.download_calls == ["1804.00001v7"] and runner.calls == 0
    assert main(["prepare", "parse", *common, "--id", "1804.00001"]) == 0
    assert runner.calls == 1
    literature = tmp_path / "minimal_literature"
    assert (
        main(
            [
                "prepare",
                "parse",
                "--space",
                str(tmp_path / "space"),
                "--batch",
                "cli",
                "--literature",
                str(literature),
                "--id",
                "1804.00001",
            ]
        )
        == 0
    )
    assert sorted(p.name for p in literature.iterdir() if p.is_file()) == [
        "summary.json"
    ]
    assert (literature / "1804.00001" / "pdf-blocks.json").is_file()
    assert runner.calls == 1
    assert "reports" in capsys.readouterr().out


def test_parser_failure_isolated_and_retry_reuses_other_papers(tmp_path):
    from hvs_catalog.materials import MinerUError

    class BrokenOnce(FakeMinerU):
        broken = True

        def parse(self, path, page_count):
            if self.broken:
                self.broken = False
                raise MinerUError("Independent synthetic parser failure")
            return super().parse(path, page_count)

    api, runner = SyntheticApi(), BrokenOnce()
    space = initialize_workspace(tmp_path / "space")
    service = PreparationBatchService(space, api=api, runner=runner)
    create(service)
    service.discover("sample")
    service.download("sample")
    service.parse("sample")
    manifest = service.get_manifest("sample")
    assert manifest["summary"]["prepared"] == 2 and runner.calls == 2
    failed = [p for p in manifest["papers"] if p["preparation"]["state"] == "failed"]
    assert len(failed) == 1 and "parser failure" in failed[0]["issue"]["message"]
    assert all(
        p["preparation"]["prepared_source_ref"] is not None
        for p in manifest["papers"]
        if p not in failed
    )
    service.parse("sample")
    assert service.get_manifest("sample")["summary"]["prepared"] == 3
    assert runner.calls == 3 and len(api.download_calls) == 3
    space.close()


def test_explicit_failed_retry_uses_new_execution_fixed_pdf_and_keeps_success_and_pending(
    tmp_path,
):
    from hvs_catalog.materials import MinerUError

    class OldParser(FakeMinerU):
        def parse(self, *args):
            raise MinerUError(
                "Independent synthetic old failure",
                artifacts={"hvs-parser.log": b"old failure"},
            )

    class NewParser(FakeMinerU):
        def fingerprint(self):
            return {**super().fingerprint(), "adapter": "2"}

    api = SyntheticApi()
    space = initialize_workspace(tmp_path / "space")
    service = PreparationBatchService(space, api=api, runner=FakeMinerU())
    create(service)
    service.discover("sample")
    service.download("sample")
    service.parse("sample", identifiers=["1804.00001"])
    old_service = PreparationBatchService(space, api=api, runner=OldParser())
    old_service.parse("sample", identifiers=["1804.00002"])
    before = sorted(
        old_service.get_manifest("sample")["papers"], key=lambda p: p["arxiv_id"]
    )
    assert [p["preparation"]["state"] for p in before] == [
        "finished",
        "failed",
        "pending",
    ]
    old_execution = before[1]["preparation"]["execution_id"]
    runner = NewParser()
    service = PreparationBatchService(space, api=api, runner=runner)
    with pytest.raises(ValueError, match="显式指定"):
        service.parse("sample", retry_failed=True)
    with pytest.raises(ValueError, match="首次解析"):
        service.parse(
            "sample", identifiers=["1804.00002", "1804.00003"], retry_failed=True
        )
    assert (
        runner.calls == 0
        and sorted(
            service.get_manifest("sample")["papers"], key=lambda p: p["arxiv_id"]
        )
        == before
    )
    service.parse("sample", identifiers=["1804.00001", "1804.00002"], retry_failed=True)
    after = sorted(
        service.get_manifest("sample")["papers"], key=lambda p: p["arxiv_id"]
    )
    assert runner.calls == 1 and sorted(api.download_calls) == [
        "1804.00001v7",
        "1804.00002v7",
        "1804.00003v7",
    ]
    assert after[0] == before[0] and after[2] == before[2]
    assert after[1]["download"] == before[1]["download"]
    assert after[1]["preparation"]["state"] == "finished"
    assert after[1]["preparation"]["execution_id"] != old_execution
    assert after[1]["preparation"]["previous_execution_ids"] == [old_execution]
    assert service.materials.get_preparation(old_execution)["state"] == "failed"
    space.close()
    space = open_workspace(tmp_path / "space")
    service = PreparationBatchService(space, api=api, runner=runner)
    assert (
        sorted(service.get_manifest("sample")["papers"], key=lambda p: p["arxiv_id"])
        == after
    )
    service.parse("sample", identifiers=["1804.00002"], retry_failed=True)
    assert runner.calls == 1
    space.close()


@pytest.mark.integration
def test_actual_exit_during_failed_retry_keeps_saved_success_and_pending_scope(
    tmp_path,
):
    from hvs_catalog.materials import MinerUError

    class Broken(FakeMinerU):
        def parse(self, *args):
            raise MinerUError("synthetic original failure")

    api = SyntheticApi()
    space = initialize_workspace(tmp_path / "space")
    service = PreparationBatchService(space, api=api, runner=Broken())
    create(service)
    service.discover("sample")
    service.download("sample")
    ids = ["1804.00001", "1804.00002"]
    service.parse("sample", identifiers=ids)
    space.close()
    driver = """
import os, sys
from pathlib import Path
sys.path.insert(0, os.environ['RETRY_TEST_HELPERS'])
from materials_helpers import FakeMinerU
from hvs_catalog.pipeline.preparation_batch import PreparationBatchService
from hvs_catalog.workspace import open_workspace
class Parser(FakeMinerU):
    def fingerprint(self): return {**super().fingerprint(), 'adapter': '2'}
    def parse(self, path, pages):
        with Path(os.environ['RETRY_CALLS']).open('a') as stream:
            stream.write('parse\\n'); stream.flush(); os.fsync(stream.fileno())
        return super().parse(path, pages)
space = open_workspace(os.environ['RETRY_SPACE'])
def progress(event):
    if os.environ.get('EXIT_AFTER_SAVED') == '1': os._exit(94)
PreparationBatchService(space, runner=Parser()).parse('sample', identifiers=['1804.00001','1804.00002'], retry_failed=True, progress=progress)
space.close()
"""
    from pathlib import Path

    log = tmp_path / "retry-calls.txt"
    env = {
        **os.environ,
        "RETRY_SPACE": str(tmp_path / "space"),
        "RETRY_CALLS": str(log),
        "RETRY_TEST_HELPERS": str(Path(__file__).parent),
        "EXIT_AFTER_SAVED": "1",
    }
    result = subprocess.run(
        [sys.executable, "-c", driver],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 94, result.stderr
    space = open_workspace(tmp_path / "space")
    papers = {
        p["arxiv_id"]: p
        for p in PreparationBatchService(space).get_manifest("sample")["papers"]
    }
    assert papers[ids[0]]["preparation"]["state"] == "finished"
    assert papers[ids[1]]["preparation"]["state"] == "failed"
    assert papers["1804.00003"]["preparation"]["execution_id"] is None
    space.close()
    result = subprocess.run(
        [sys.executable, "-c", driver],
        env={**env, "EXIT_AFTER_SAVED": "0"},
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert log.read_text().splitlines() == ["parse", "parse"]
    space = open_workspace(tmp_path / "space")
    manifest = PreparationBatchService(space).get_manifest("sample")
    assert manifest["summary"]["prepared"] == 2
    assert (
        next(p for p in manifest["papers"] if p["arxiv_id"] == "1804.00003")[
            "preparation"
        ]["state"]
        == "pending"
    )
    space.close()


@pytest.mark.integration
def test_actual_process_exit_after_saved_query_resumes_without_repeating_it(tmp_path):
    driver = """
import os
from datetime import date
from pathlib import Path
from hvs_catalog.pipeline.preparation_batch import PreparationBatchService
from hvs_catalog.pipeline.acquisition import ArxivSearchPage
from hvs_catalog.workspace import initialize_workspace
class Api:
    def search(self, request):
        with Path(os.environ['BATCH_CALL_LOG']).open('a') as f:
            f.write(request.terms + '\\n')
            f.flush()
            os.fsync(f.fileno())
        return ArxivSearchPage(request, 'synthetic', 1791676800., 0, 0, (), ())
space = initialize_workspace(Path(os.environ['BATCH_SPACE']))
service = PreparationBatchService(space, api=Api())
service.create_batch('exit', submitted_from=date(2018,4,25), submitted_to=date(2018,4,30), terms=['hypervelocity stars','unbound stars'])
def checkpoint(event):
    if event['state'] == 'complete':
        os._exit(91)
service.discover('exit', progress=checkpoint)
"""
    log = tmp_path / "calls.txt"
    env = {
        **os.environ,
        "BATCH_SPACE": str(tmp_path / "space"),
        "BATCH_CALL_LOG": str(log),
    }
    result = subprocess.run(
        [sys.executable, "-c", driver],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 91, result.stderr
    api = SyntheticApi()
    api.pages["unbound stars"] = []
    space = open_workspace(tmp_path / "space")
    service = PreparationBatchService(space, api=api)
    service.discover("exit")
    assert log.read_text().splitlines() == ["hypervelocity stars"]
    assert api.search_calls == ["unbound stars"]
    assert service.get_manifest("exit")["discovery_complete"]
    space.close()

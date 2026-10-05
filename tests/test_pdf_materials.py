"""PDF acquisition, external parser boundary, durable preparation and evidence safety."""

from __future__ import annotations

import io
import json
import zipfile

import pytest
from materials_helpers import FakeMinerU, make_pdf, mineru_zip, prepared_space
from test_acquisition_lifecycle import FakeArxivService

from hvs_catalog.materials import MaterialsService
from hvs_catalog.materials.mineru import LocalMinerU, MinerUError, read_result
from hvs_catalog.pipeline.acquisition import AcquisitionService
from hvs_catalog.records.common import RecordRef
from hvs_catalog.records.pdf import pdf_evidence_block, pdf_page_count
from hvs_catalog.workspace import (
    WorkspaceConflictError,
    backup_workspace,
    initialize_workspace,
    open_workspace,
    restore_workspace,
)


def test_default_pdf_and_optional_latex_failure_are_independent(tmp_path):
    space = initialize_workspace(tmp_path / "space")
    api = FakeArxivService(downloads={"2501.12345v1": make_pdf()})
    calls = []

    def no_source(identifier):
        calls.append(identifier)
        raise ValueError("source absent")

    api.download_source = no_source
    service = AcquisitionService(space, api)
    first = service.acquire("2501.12345v1")
    assert not calls and first.material_ref
    second = service.acquire("2501.12345v1", include_latex=True)
    assert calls == ["2501.12345v1"] and second.material_ref == first.material_ref
    assert second.latex_error == "source absent" and second.replayed
    assert service.selected_material("2501.12345").material_ref == first.material_ref
    space.close()


def test_journal_pdf_identity_replay_conflicts_and_backup(tmp_path):
    space = initialize_workspace(tmp_path / "space")
    pdf = tmp_path / "journal.pdf"
    pdf.write_bytes(make_pdf())
    service = AcquisitionService(space)
    args = {
        "paper_key": "Journal:star-study",
        "source_version": "published-2026",
        "provenance": "Synthetic fixed journal PDF",
        "doi": "10.1234/synthetic",
    }
    ref = service.import_journal_pdf(pdf, **args)
    assert service.import_journal_pdf(pdf, **args) == ref
    source = space.store().read_record(ref)
    assert source.data.origin.kind == "local_pdf"
    assert source.data.origin.doi == "10.1234/synthetic"
    pdf.write_bytes(make_pdf("Changed"))
    with pytest.raises(WorkspaceConflictError):
        service.import_journal_pdf(pdf, **args)
    runner = FakeMinerU()
    handle, report = MaterialsService(space, runner=runner).prepare_materials(
        request_key="journal", source_ref=ref
    )
    assert report.outcome == "finished", report.errors
    assert runner.calls == 1
    backup = tmp_path / "backup"
    backup_workspace(space, backup)
    space.close()
    restore_workspace(backup, tmp_path / "restored")
    restored = open_workspace(tmp_path / "restored")
    assert MaterialsService(restored).consume_prepared_input(handle.execution_id)["ok"]
    restored.close()


def test_prepare_replay_reopen_no_reparse_and_pdf_page_binding(tmp_path):
    space, source = prepared_space(
        tmp_path, make_pdf("S5-HVS1 is one object.\nRV -430 +/- 12 km/s.")
    )
    runner = FakeMinerU()
    service = MaterialsService(space, runner=runner)
    handle, report = service.prepare_materials(
        request_key="pdf", paper=source.canonical_id
    )
    assert report.outcome == "finished", report.errors
    assert service.prepare_materials(request_key="pdf", paper=source.canonical_id)[
        0
    ].reused
    assert runner.calls == 1
    bundle = service.get_prepared_input(handle.execution_id)
    record = space.store().read_record(
        RecordRef.model_validate(bundle["prepared_source_ref"])
    )
    block = pdf_evidence_block(record, space.root, "raw", 1, "p1-b0")
    assert block["text"] == "S5-HVS1 is one object."
    with pytest.raises(ValueError):
        pdf_evidence_block(record, space.root, "raw", 2, "p1-b0")
    with pytest.raises(ValueError):
        pdf_evidence_block(record, space.root, "raw", 1, "p1-b99")
    space.close()
    space = open_workspace(tmp_path / "space")
    assert (
        MaterialsService(space, runner=runner)
        .resume_preparation(handle.execution_id)
        .outcome
        == "finished"
    )
    assert runner.calls == 1
    space.close()


def test_missing_page_retained_as_gap_not_successful_full_material(tmp_path):
    space, source = prepared_space(tmp_path)
    handle, report = MaterialsService(
        space, runner=FakeMinerU(gaps=["PDF page 2 unreadable"])
    ).prepare_materials(request_key="missing", paper=source.canonical_id)
    assert report.outcome == "finished"
    bundle = MaterialsService(space).get_prepared_input(handle.execution_id)
    assert not bundle["summary"]["materials_complete"] and bundle["gaps"]
    space.close()


def test_failed_parser_durable_no_empty_success(tmp_path):
    space, source = prepared_space(tmp_path)

    class Broken(FakeMinerU):
        def parse(self, *args):
            raise MinerUError("parser process failed")

    service = MaterialsService(space, runner=Broken())
    handle, report = service.prepare_materials(
        request_key="broken", paper=source.canonical_id
    )
    assert report.outcome == "failed" and "parser process failed" in " ".join(
        report.errors
    )
    assert service.get_preparation(handle.execution_id)["state"] == "failed"
    assert space.store().read_record(source.material_ref)
    space.close()


@pytest.mark.parametrize("payload", [b"", b"<html>error</html>", b"%PDF-1.7\nbroken"])
def test_pdf_validation_rejects_error_pages(payload):
    with pytest.raises(ValueError):
        pdf_page_count(payload)


def test_parser_full_document_and_table_symbols_preserved():
    table = "<table><tr><th>Object</th><th>RV (km/s)</th></tr><tr><td>S5-HVS1</td><td>−430 ± 12</td></tr></table>"
    raw = mineru_zip(table)
    result = read_result(raw, 2)
    assert result["blocks"][0]["text"] == table
    assert result["gaps"] == ["Parser omitted PDF pages: [2]."]
    assert read_result(mineru_zip("text", full=False), 1)["gaps"]


@pytest.mark.parametrize("change", ["page", "bbox", "duplicate", "schema"])
def test_invalid_parser_mapping_rejected(change):
    with zipfile.ZipFile(io.BytesIO(mineru_zip("S5-HVS1"))) as z:
        doc = json.loads(z.read("middle.json"))
    if change == "page":
        doc["pages"][0]["page_idx"] = 9
    if change == "bbox":
        doc["pages"][0]["blocks"][0]["bbox"] = [0, 0, 2, 1]
    if change == "duplicate":
        doc["pages"].append(doc["pages"][0])
    if change == "schema":
        doc["schema"] = "unknown"
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as z:
        z.writestr("middle.json", json.dumps(doc))
        z.writestr("paper.md", "text")
    with pytest.raises(MinerUError):
        read_result(buffer.getvalue(), 1)


@pytest.mark.integration
def test_parser_cli_runs_all_pages_in_real_subprocess(tmp_path):
    exe = tmp_path / "mineru-kit"
    raw = tmp_path / "result.zip"
    raw.write_bytes(mineru_zip("S5-HVS1"))
    exe.write_text(
        "#!/usr/bin/env python3\nimport sys,shutil\nfrom pathlib import Path\n"
        'if sys.argv[1:]==["--version"]:print("mineru-kit 4.0.0")\n'
        'elif sys.argv[1:4]==["models","verify","--tier"]:assert sys.argv[4]=="standard"\n'
        'else:\n assert "--pages" in sys.argv and sys.argv[sys.argv.index("--pages")+1]=="all"\n'
        ' assert Path(sys.argv[2]).suffix==".pdf"\n'
        ' assert sys.argv[sys.argv.index("--tier")+1]=="advanced"\n'
        f' shutil.copyfile({str(raw)!r},sys.argv[sys.argv.index("-o")+1])\n'
    )
    exe.chmod(0o755)
    pdf = tmp_path / "source.bin"
    pdf.write_bytes(make_pdf())
    parser = LocalMinerU(str(exe))
    assert parser.fingerprint()["tier"] == "advanced"
    payload, parsed = parser.parse(pdf, 1)
    assert payload == raw.read_bytes() and parsed["blocks"][0]["text"] == "S5-HVS1"

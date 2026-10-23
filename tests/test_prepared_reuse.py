"""Reused parser bytes feed independent extraction without historical predictions."""

import json

import pytest
from materials_helpers import PAPER
from test_acquisition_lifecycle import FakeArxivService, make_meta
from test_quantities import quantities
from test_roster import ScriptedAdapter, submission

from hvs_catalog.evaluation.adapt import prediction_from_execution
from hvs_catalog.llm.managed import CallService
from hvs_catalog.materials import MaterialsService, reuse_prepared_materials
from hvs_catalog.pipeline import resume_extraction, start_extraction
from hvs_catalog.pipeline.acquisition import AcquisitionService
from hvs_catalog.pipeline.roster import material_prompt
from hvs_catalog.records.common import RecordRef
from hvs_catalog.workspace import (
    WorkspaceIntegrityError,
    backup_workspace,
    initialize_workspace,
    restore_workspace,
)


def test_reused_material_is_independent_and_preserves_model_input(prepared, tmp_path):
    from pydantic import SecretStr

    original, prep, config = prepared
    target = initialize_workspace(tmp_path / "reused")
    restored = None
    try:
        bundle = MaterialsService(original).get_prepared_input(prep)
        source = original.store().read_record(
            RecordRef.model_validate(bundle["prepared_source_ref"])
        )
        ref = reuse_prepared_materials(original, prep, target)
        assert reuse_prepared_materials(original, prep, target) == ref
        copied = target.store().read_record(ref)
        assert [f.sha256 for f in copied.data.files[:-1]] == [
            f.sha256 for f in source.data.files
        ]
        before, after = (
            material_prompt(original, bundle, source),
            material_prompt(target, {}, copied),
        )
        assert before.manuscript == after.manuscript and before.images == after.images
        assert target.store().list_executions() == []
        with pytest.raises(ValueError, match="metadata is missing"):
            start_extraction(
                target,
                prepared_source_ref=ref,
                request_key="extract",
                configuration=config,
            )
        AcquisitionService(
            target, FakeArxivService(metadata={PAPER: make_meta(PAPER, 1)})
        ).acquire_metadata(PAPER + "v1")
        original.close()
        original.root.rename(tmp_path / "original-unavailable")
        backup_workspace(target, tmp_path / "backup")
        restored = restore_workspace(tmp_path / "backup", tmp_path / "restored")
        handle = start_extraction(
            restored,
            prepared_source_ref=ref,
            request_key="extract",
            configuration=config,
        )
        adapter = ScriptedAdapter([submission(), quantities()])
        report = resume_extraction(
            restored,
            handle.execution_id,
            service=CallService(
                config.runtime, credential=SecretStr("synthetic"), adapter=adapter
            ),
        )
        assert report.result_status == "complete" and len(adapter.seen) == 2
        assert (
            prediction_from_execution(restored, handle.execution_id)
            .content.stars[0]
            .quantities[0]
            .values[0]
            .value
            == "520"
        )
    finally:
        target.close()
        if restored:
            restored.close()


def test_corrupt_attachment_prevents_import(prepared, tmp_path):
    original, prep, _ = prepared
    bundle = MaterialsService(original).get_prepared_input(prep)
    source = original.store().read_record(
        RecordRef.model_validate(bundle["prepared_source_ref"])
    )
    (original.root / source.data.files[0].path).write_bytes(b"corrupt")
    target = initialize_workspace(tmp_path / "reused")
    try:
        with pytest.raises(WorkspaceIntegrityError):
            reuse_prepared_materials(original, prep, target)
        assert not target.store().find_receipts(kind="reuse-materials")
    finally:
        target.close()


def test_reuse_cli_returns_exact_source_reference(prepared, tmp_path):
    import subprocess
    import sys

    original, prep, _ = prepared
    target = initialize_workspace(tmp_path / "reused")
    target.close()
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "hvs_catalog",
            "materials",
            "reuse",
            "--space",
            str(target.root),
            "--source-space",
            str(original.root),
            "--preparation",
            prep,
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert RecordRef.model_validate(json.loads(result.stdout)).record_id.startswith(
        "reused-materials-"
    )


@pytest.mark.integration
@pytest.mark.parametrize("after_commit", [False, True])
def test_reuse_recovers_after_process_exit(prepared, tmp_path, after_commit):
    import subprocess
    import sys

    original, prep, _ = prepared
    target = initialize_workspace(tmp_path / "reused")
    target.close()
    code = """
import os, sys
from pathlib import Path
from hvs_catalog.workspace import open_workspace
from hvs_catalog.materials import reuse_prepared_materials
source=open_workspace(Path(sys.argv[1]), read_only=True)
target=open_workspace(Path(sys.argv[2]))
store=target.store(); commit=store.commit
def interrupted(request):
    if sys.argv[4]=='True': commit(request)
    os._exit(71)
store.commit=interrupted
reuse_prepared_materials(source,sys.argv[3],target)
"""
    child = subprocess.run(
        [
            sys.executable,
            "-c",
            code,
            str(original.root),
            str(target.root),
            prep,
            str(after_commit),
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert child.returncode == 71, child.stderr
    from hvs_catalog.workspace import open_workspace

    target = open_workspace(target.root)
    try:
        ref = reuse_prepared_materials(original, prep, target)
        assert len(target.store().find_receipts(kind="reuse-materials")) == 1
        assert material_prompt(target, {}, target.store().read_record(ref)).manuscript
    finally:
        target.close()

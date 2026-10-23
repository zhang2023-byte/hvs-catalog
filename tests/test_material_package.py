"""Portable parsing inputs remain usable after their original workspace is removed."""

import hashlib
import json
import shutil
import subprocess
import sys

import pytest
from pydantic import SecretStr
from test_quantities import quantities
from test_roster import ScriptedAdapter, submission

from hvs_catalog import cli
from hvs_catalog.llm.managed import CallService
from hvs_catalog.materials import MaterialsService
from hvs_catalog.materials.metadata import PREFIX, fixed_metadata
from hvs_catalog.materials.package import (
    export_material_package,
    import_material_package,
    read_material_package,
)
from hvs_catalog.pipeline import resume_extraction, start_extraction
from hvs_catalog.pipeline.roster import material_prompt
from hvs_catalog.records.common import RecordRef
from hvs_catalog.workspace import (
    WorkspaceIntegrityError,
    backup_workspace,
    initialize_workspace,
    open_workspace,
    restore_workspace,
)
from hvs_catalog.workspace.archives import publish_json, read_json


def prepared_source(space, preparation):
    bundle = MaterialsService(space).get_prepared_input(preparation)
    return space.store().read_record(
        RecordRef.model_validate(bundle["prepared_source_ref"])
    )


def test_portable_inputs_preserve_prompt_and_shared_metadata_without_original(
    prepared, tmp_path
):
    original, preparation, config = prepared
    source = prepared_source(original, preparation)
    before = material_prompt(original, {}, source)
    metadata_ref, _ = fixed_metadata(original, source)
    document = read_json(original, metadata_ref)
    document["parsed_source"]["abstract"] = "Independent shared metadata. " * 200
    with original.lock():
        shared = publish_json(original, "sources/metadata/expanded.json", document)
        original.store().set_meta(
            PREFIX + source.data.paper_key + source.data.source_version,
            shared.model_dump_json(),
        )
    directory = tmp_path / "material"
    package = export_material_package(original, source.ref(), directory)
    assert json.loads((directory / "metadata.json").read_text()) == document
    assert all(
        f.role not in {"prepared_bundle", "preparation_provenance"}
        for f in package.source.files
    )
    original.close()
    shutil.rmtree(original.root)
    moved = tmp_path / "moved-material"
    directory.rename(moved)
    target = initialize_workspace(tmp_path / "target")
    try:
        ref = import_material_package(moved, target)
        assert import_material_package(moved, target) == ref
        assert target.store().list_executions() == []
        copied = target.store().read_record(ref)
        after = material_prompt(target, {}, copied)
        assert (before.manuscript, before.images) == (after.manuscript, after.images)
        assert [f.sha256 for f in copied.data.files] == [
            f.sha256 for f in package.source.files
        ]
        loaded_ref, _ = fixed_metadata(target, copied)
        assert read_json(target, loaded_ref) == document
        shutil.rmtree(moved)
        backup_workspace(target, tmp_path / "backup")
        target.close()
        shutil.rmtree(target.root)
        target = restore_workspace(tmp_path / "backup", tmp_path / "restored")
        restored_ref, _ = fixed_metadata(target, target.store().read_record(ref))
        assert read_json(target, restored_ref) == document
        assert target.store().list_executions() == []
        handle = start_extraction(
            target,
            prepared_source_ref=ref,
            request_key="synthetic",
            configuration=config,
        )
        adapter = ScriptedAdapter([submission(), quantities()])
        report = resume_extraction(
            target,
            handle.execution_id,
            service=CallService(
                config.runtime, credential=SecretStr("synthetic"), adapter=adapter
            ),
        )
        assert report.result_status == "complete"
        assert len(adapter.seen) == 2
    finally:
        target.close()


@pytest.mark.parametrize(
    "damage", ["missing", "corrupt", "escape", "symlink", "wrong-version"]
)
def test_invalid_package_does_not_publish_import(prepared, tmp_path, damage):
    original, preparation, _ = prepared
    source = prepared_source(original, preparation)
    directory = tmp_path / "material"
    export_material_package(original, source.ref(), directory)
    manifest_path = directory / "material.json"
    manifest = json.loads(manifest_path.read_text())
    if damage == "missing":
        (directory / "paper.pdf").unlink()
    elif damage == "corrupt":
        (directory / "paper.pdf").write_bytes(b"changed")
    elif damage == "escape":
        manifest["source"]["files"][0]["path"] = "../paper.pdf"
    elif damage == "symlink":
        outside = tmp_path / "outside.pdf"
        (directory / "paper.pdf").rename(outside)
        (directory / "paper.pdf").symlink_to(outside)
    else:
        metadata_path = directory / "metadata.json"
        metadata = json.loads(metadata_path.read_text())
        metadata["metadata"]["version"] = "v2"
        data = json.dumps(metadata).encode()
        metadata_path.write_bytes(data)
        manifest["metadata"]["sha256"] = hashlib.sha256(data).hexdigest()
    manifest_path.write_text(json.dumps(manifest))
    target = initialize_workspace(tmp_path / "target")
    try:
        with pytest.raises((ValueError, OSError)):
            import_material_package(directory, target)
        assert not target.store().find_receipts(kind="import-materials")
        assert not list((target.root / "sources").rglob("*.pdf"))
        assert target.store().list_executions() == []
    finally:
        target.close()


def test_failed_export_is_not_published_and_cannot_replace_materials(
    prepared, tmp_path
):
    original, preparation, _ = prepared
    source = prepared_source(original, preparation)
    directory = tmp_path / "material"
    export_material_package(original, source.ref(), directory)
    with pytest.raises(FileExistsError):
        export_material_package(original, source.ref(), directory)
    pdf = next(f for f in source.data.files if f.role == "pdf")
    (original.root / pdf.path).write_bytes(b"broken PDF")
    with pytest.raises(WorkspaceIntegrityError):
        export_material_package(original, source.ref(), tmp_path / "broken")
    assert not (tmp_path / "broken").exists()
    assert not list(tmp_path.glob(".broken-*"))
    read_material_package(directory)


def test_material_package_cli_and_reimport_recheck_saved_bytes(
    prepared, tmp_path, capsys
):
    original, preparation, _ = prepared
    source = prepared_source(original, preparation)
    directory = tmp_path / "material"
    assert (
        cli.main(
            [
                "materials",
                "export",
                "--space",
                str(original.root),
                "--source-ref",
                source.record_id + "@1",
                "--output",
                str(directory),
            ]
        )
        == 0
    )
    capsys.readouterr()
    target = initialize_workspace(tmp_path / "target")
    try:
        assert (
            cli.main(
                [
                    "materials",
                    "import",
                    "--space",
                    str(target.root),
                    "--package",
                    str(directory),
                ]
            )
            == 0
        )
        ref = RecordRef.model_validate(json.loads(capsys.readouterr().out))
        saved = target.store().read_record(ref)
        pdf = next(f for f in saved.data.files if f.role == "pdf")
        (target.root / pdf.path).write_bytes(b"corrupted local input")
        with pytest.raises(ValueError):
            import_material_package(directory, target)
        (directory / "paper.pdf").unlink()
        assert (
            cli.main(
                [
                    "materials",
                    "import",
                    "--space",
                    str(target.root),
                    "--package",
                    str(directory),
                ]
            )
            == 1
        )
        assert "[拒绝]" in capsys.readouterr().err
    finally:
        target.close()


@pytest.mark.integration
@pytest.mark.parametrize("after_commit", [False, True])
def test_portable_import_recovers_after_process_exit(prepared, tmp_path, after_commit):
    original, preparation, _ = prepared
    directory = tmp_path / "material"
    export_material_package(
        original, prepared_source(original, preparation).ref(), directory
    )
    target = initialize_workspace(tmp_path / "target")
    target.close()
    code = """
import os, sys
from pathlib import Path
from hvs_catalog.workspace import open_workspace
from hvs_catalog.materials.package import import_material_package
space=open_workspace(Path(sys.argv[1]))
store=space.store(); commit=store.commit
def interrupted(request):
    if sys.argv[3]=='True': commit(request)
    os._exit(71)
store.commit=interrupted
import_material_package(Path(sys.argv[2]),space)
"""
    child = subprocess.run(
        [
            sys.executable,
            "-c",
            code,
            str(target.root),
            str(directory),
            str(after_commit),
        ],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert child.returncode == 71, child.stderr
    target = open_workspace(target.root)
    try:
        ref = import_material_package(directory, target)
        assert len(target.store().find_receipts(kind="import-materials")) == 1
        assert material_prompt(target, {}, target.store().read_record(ref)).manuscript
        assert (
            fixed_metadata(target, target.store().read_record(ref))[1].version == "v1"
        )
    finally:
        target.close()

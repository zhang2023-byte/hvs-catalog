"""Benchmark material choices and closed archives preserve independent science."""

import json
import shutil
import subprocess
import sys

import pytest
from pydantic import SecretStr
from test_material_package import prepared_source
from test_quantities import quantities, value
from test_roster import ScriptedAdapter, submission

from hvs_catalog.benchmark.archive import (
    archive_execution,
    open_archive,
    verify_archive,
)
from hvs_catalog.benchmark.experiments import (
    close_experiment,
    experiment_status,
    prepare_experiment,
    preview_materials,
    run_experiment,
    score_experiment,
)
from hvs_catalog.benchmark.materials import migrate_package, selection
from hvs_catalog.benchmark.migration import migrate_benchmark
from hvs_catalog.evaluation.adapt import prediction_from_execution
from hvs_catalog.llm.managed import CallService
from hvs_catalog.materials.package import (
    export_material_package,
    import_material_package,
)
from hvs_catalog.pipeline import resume_extraction, start_extraction
from hvs_catalog.pipeline.results import current_result
from hvs_catalog.records.rules import current_rules_ref
from hvs_catalog.workspace import (
    backup_workspace,
    initialize_workspace,
    restore_workspace,
)


def material_fixture(prepared, tmp_path):
    source, prep, config = prepared
    package = tmp_path / "package"
    record = prepared_source(source, prep)
    export_material_package(source, record.ref(), package)
    paper = record.data.paper_key
    return package, paper, config


def test_closed_archive_moves_without_legacy_workspaces(prepared, tmp_path):
    package, paper, config = material_fixture(prepared, tmp_path)
    legacy = initialize_workspace(tmp_path / "legacy")
    bench = initialize_workspace(tmp_path / "benchmark", benchmark=True)
    try:
        ref = import_material_package(package, legacy)
        migrate_package(package, bench, paper, "mineru_local")
        handle = start_extraction(
            legacy, prepared_source_ref=ref, request_key="test", configuration=config
        )
        adapter = ScriptedAdapter([submission(), quantities()])
        resume_extraction(
            legacy,
            handle.execution_id,
            service=CallService(
                config.runtime, credential=SecretStr("synthetic"), adapter=adapter
            ),
        )
        _, previous = current_result(legacy, handle.execution_id)
        expected = legacy.read_file(previous.result_ref)
        result = archive_execution(
            legacy, bench, handle.execution_id, "exec01", paper, "mineru_local"
        )
        assert not result.status.resumable
        assert bench.read_file(result.result_ref) == expected
        legacy.close()
        shutil.rmtree(legacy.root)
        moved = tmp_path / "moved"
        bench.close()
        bench.root.rename(moved)
        archived = open_archive(moved, "exec01", paper)
        prediction = prediction_from_execution(archived, "exec01/" + paper)
        assert prediction.content.stars[0].quantities[0].values[0].value == "520"
        assert prediction.completion == "complete"
        assert verify_archive(moved, "exec01", paper).usage.attempts == 2
        with pytest.raises(ValueError, match="cannot be resumed"):
            archived.checkpoint_connection()
        archived.close()
    finally:
        legacy.close()
        bench.close()


def test_shared_materials_named_execution_and_backup(prepared, tmp_path):
    package, paper, config = material_fixture(prepared, tmp_path)
    bench = initialize_workspace(tmp_path / "benchmark", benchmark=True)
    try:
        migrate_package(package, bench, paper, "mineru_local")
        with pytest.raises(FileNotFoundError):
            selection(bench.root, [paper], "mineru_cloud")
        directory = bench.root / "materials" / paper / "mineru_local"
        ref = import_material_package(
            directory, bench, root=bench.root, copy_files=False
        )
        handle = start_extraction(
            bench,
            prepared_source_ref=ref,
            request_key="test",
            configuration=config,
            execution_id="exec01/" + paper,
        )
        adapter = ScriptedAdapter([submission(), quantities()])
        report = resume_extraction(
            bench,
            handle.execution_id,
            service=CallService(
                config.runtime, credential=SecretStr("synthetic"), adapter=adapter
            ),
        )
        assert report.outcome == "finished", report.errors
        result = archive_execution(
            bench, bench, handle.execution_id, "exec01", paper, "mineru_local"
        )
        assert result.status.result == "complete"
        assert not (bench.root / "sources").exists()
        assert not (bench.root / "backups").exists()
        assert (bench.root / "runs/exec01" / paper / "run.json").is_file()
        bench.publish_file(".state/history/exec01/config.json", b'{"fixed": true}\n')
        backup_workspace(bench, tmp_path / "backup")
        restored = restore_workspace(tmp_path / "backup", tmp_path / "restored")
        try:
            assert (
                verify_archive(restored.root, "exec01", paper).status.result
                == "complete"
            )
            assert (
                restored.root / ".state/history/exec01/config.json"
            ).read_bytes() == b'{"fixed": true}\n'
        finally:
            restored.close()
    finally:
        bench.close()


def test_experiment_lifecycle_uses_explicit_variant_and_retires_state(
    prepared, tmp_path
):
    package, paper, config = material_fixture(prepared, tmp_path)
    bench = initialize_workspace(tmp_path / "benchmark", benchmark=True)
    try:
        migrate_package(package, bench, paper, "mineru_local")
        migrate_package(package, bench, paper, "mineru_cloud")
        # The two parser choices are explicit even for equal synthetic content.
        state = prepare_experiment(
            bench.root,
            papers=[paper],
            variant="mineru_cloud",
            configuration=config.model_dump(mode="json"),
            purpose="Synthetic test",
        )
        assert state["id"] == "exec01"
        assert state["material_variant"] == "mineru_cloud"
        adapter = ScriptedAdapter([submission(), quantities()])

        def service(configuration):
            return CallService(
                configuration.runtime,
                credential=SecretStr("synthetic"),
                adapter=adapter,
            )

        result = run_experiment(bench.root, "exec01", service_factory=service)
        assert result["state"] == "archived"
        assert len(adapter.seen) == 2
        assert bench.store().list_executions() == []
        assert run_experiment(bench.root, "exec01", service_factory=service) == result
        assert len(adapter.seen) == 2
        assert experiment_status(bench.root, "exec01") == result
        assert not (bench.root / f".state/executions/exec01/{paper}").exists()
        with pytest.raises(ValueError, match="different fixed conditions"):
            prepare_experiment(
                bench.root,
                papers=[paper],
                variant="mineru_local",
                configuration=config.model_dump(mode="json"),
                purpose="Synthetic test",
                name="exec01",
            )
    finally:
        bench.close()


def test_material_change_stops_before_service_creation(prepared, tmp_path):
    package, paper, config = material_fixture(prepared, tmp_path)
    bench = initialize_workspace(tmp_path / "benchmark", benchmark=True)
    try:
        migrated = migrate_package(package, bench, paper, "mineru_local")
        prepare_experiment(
            bench.root,
            papers=[paper],
            variant="mineru_local",
            configuration=config.model_dump(mode="json"),
            purpose="Synthetic test",
        )
        pdf = next(f for f in migrated.source.files if f.role == "pdf")
        (bench.root / pdf.path).write_bytes(b"changed")

        def forbidden(_):
            pytest.fail("Service must not be created for changed material.")

        with pytest.raises(ValueError):
            run_experiment(bench.root, "exec01", service_factory=forbidden)
    finally:
        bench.close()


def test_offline_preview_and_explicit_close_preserve_unfinished_status(
    prepared, tmp_path
):
    package, paper, config = material_fixture(prepared, tmp_path)
    bench = initialize_workspace(tmp_path / "benchmark", benchmark=True)
    try:
        migrate_package(package, bench, paper, "mineru_cloud")
        options = {
            "papers": [paper],
            "variant": "mineru_cloud",
            "configuration": config.model_dump(mode="json"),
        }
        preview = preview_materials(bench.root, name="cloud", **options)
        assert preview["model_calls"] == 0
        assert len(preview["previews"]) == 2
        assert bench.store().list_executions() == []
        assert list((bench.root / "runs").iterdir()) == []
        prepare_experiment(bench.root, purpose="Explicit closure", **options)
        state = close_experiment(bench.root, "exec01")
        assert state["state"] == "archived"
        assert state["papers"][0]["status"]["result"] is None
        assert not state["papers"][0]["status"]["resumable"]
        assert verify_archive(bench.root, "exec01", paper).usage.attempts == 0
        assert bench.store().list_executions() == []
        assert close_experiment(bench.root, "exec01") == state
    finally:
        bench.close()


@pytest.mark.parametrize("changed", ["configuration", "variant", "scope"])
def test_changed_experiment_cannot_override_frozen_inputs(prepared, tmp_path, changed):
    package, paper, config = material_fixture(prepared, tmp_path)
    bench = initialize_workspace(tmp_path / "benchmark", benchmark=True)
    try:
        for variant in ("mineru_cloud", "mineru_local"):
            migrate_package(package, bench, paper, variant)
        prepare_experiment(
            bench.root,
            papers=[paper],
            variant="mineru_cloud",
            configuration=config.model_dump(mode="json"),
            purpose="Binding",
        )
        path = bench.root / "runs/exec01/experiment.json"
        spec = json.loads(path.read_bytes())
        if changed == "configuration":
            spec["configuration"]["runtime"]["concurrency"] += 1
        elif changed == "variant":
            spec["materials"] = {
                "variant": "mineru_local",
                "papers": selection(bench.root, [paper], "mineru_local"),
            }
        else:
            spec["scope"] = "roster"
        path.write_text(json.dumps(spec))

        def forbidden(_):
            pytest.fail(
                "Service must not be created for altered experiment conditions."
            )

        with pytest.raises(ValueError, match="frozen execution"):
            run_experiment(bench.root, "exec01", service_factory=forbidden)
    finally:
        bench.close()


def test_archive_scoring_uses_existing_scorer_without_model_calls(prepared, tmp_path):
    package, paper, config = material_fixture(prepared, tmp_path)
    bench = initialize_workspace(tmp_path / "benchmark", benchmark=True)
    try:
        migrated = migrate_package(package, bench, paper, "mineru_local")
        prepare_experiment(
            bench.root,
            papers=[paper],
            variant="mineru_local",
            configuration=config.model_dump(mode="json"),
            purpose="Synthetic scoring",
        )
        adapter = ScriptedAdapter([submission(), quantities()])
        run_experiment(
            bench.root,
            "exec01",
            service_factory=lambda c: CallService(
                c.runtime, credential=SecretStr("synthetic"), adapter=adapter
            ),
        )
        evidence = [{"page": 1, "location": "Synthetic paragraph"}]
        answer = {
            "metadata": {
                "arxiv_id": paper,
                "version": "v1",
                "title": "Synthetic reference",
                "authors": ["A. Author"],
                "rules": current_rules_ref(),
                "material": {
                    "path": "paper.pdf",
                    "sha256": next(
                        f.sha256 for f in migrated.source.files if f.role == "pdf"
                    ),
                },
            },
            "stars": [
                {
                    "identifiers": [{"value": "A", "evidence": evidence}],
                    "contribution_type": {
                        "value": "candidates_found",
                        "evidence": evidence,
                    },
                    "quantities": [
                        {
                            "quantity": "radial_velocity",
                            "values": [
                                {"value": "520", "unit": "km/s", "evidence": evidence}
                            ],
                        }
                    ],
                }
            ],
            "notes": [],
        }
        reference = tmp_path / "reference.json"
        reference.write_text(json.dumps(answer))
        shutil.copyfile(
            bench.root / f"materials/{paper}/paper.pdf", tmp_path / "paper.pdf"
        )
        report = score_experiment(bench.root, "exec01", {paper: reference})
        assert report.micro.l1.tp == report.micro.l2.correct == 1
        assert report.micro.l2.f1 == 1
        assert len(adapter.seen) == 2
        with pytest.raises(FileExistsError):
            score_experiment(bench.root, "exec01", {paper: reference})
    finally:
        bench.close()


def test_legacy_migration_preserves_partial_result_and_frozen_bytes(prepared, tmp_path):
    package, paper, config = material_fixture(prepared, tmp_path)
    config = config.model_copy(update={"max_format_corrections": 0})
    old = tmp_path / "old"
    shutil.copytree(package, old / "materials" / paper)
    shutil.copytree(package, old / "materials" / paper / "mineru_cloud")
    folder = old / "runs/exec01"
    space = initialize_workspace(folder / paper)
    try:
        ref = import_material_package(package, space)
        handle = start_extraction(
            space, prepared_source_ref=ref, request_key="legacy", configuration=config
        )
        report = resume_extraction(
            space,
            handle.execution_id,
            service=CallService(
                config.runtime,
                credential=SecretStr("synthetic"),
                adapter=ScriptedAdapter([submission(), quantities([value(4)])]),
            ),
        )
        assert report.result_status == "partial"
        _, run = current_result(space, handle.execution_id)
        assert run.status.resumable
        science = space.read_file(run.result_ref)
        spec = {
            "id": "exec01",
            "status": "finished",
            "purpose": "Synthetic legacy",
            "created_at": "2026-01-01T00:00:00+00:00",
            "scope": "full",
            "configuration": config.model_dump(mode="json"),
            "source": {},
            "fixed_files": {},
            "execution_map": [
                {
                    "arxiv_id": paper,
                    "workspace": paper,
                    "execution_id": handle.execution_id,
                }
            ],
        }
        (folder / "experiment.json").write_text(json.dumps(spec))
        (folder / "scoring").mkdir()
        frozen = b'{"synthetic_frozen_evidence": true}\n'
        (folder / "scoring/scoring-inputs.json").write_bytes(frozen)
        migrated = tmp_path / "migrated"
        counts = migrate_benchmark(old, migrated)
        assert counts == {
            "material_packages": 2,
            "archived_papers": 1,
            "rounds": ["exec01"],
        }
        archived = verify_archive(migrated, "exec01", paper)
        assert archived.status.result == "partial" and not archived.status.resumable
        assert (migrated / archived.result_ref.path).read_bytes() == science
        assert (
            migrated / "runs/exec01/scoring/scoring-inputs.json"
        ).read_bytes() == frozen
        assert migrate_benchmark(old, migrated) == counts
        assert current_result(space, handle.execution_id)[1].status.resumable
    finally:
        space.close()


@pytest.mark.integration
@pytest.mark.parametrize("boundary", ["before", "after"])
def test_process_exit_during_archive_publication_is_recoverable(
    prepared, tmp_path, boundary
):
    package, paper, config = material_fixture(prepared, tmp_path)
    legacy = initialize_workspace(tmp_path / "legacy")
    bench = initialize_workspace(tmp_path / "benchmark", benchmark=True)
    try:
        ref = import_material_package(package, legacy)
        migrate_package(package, bench, paper, "mineru_local")
        handle = start_extraction(
            legacy, prepared_source_ref=ref, request_key="test", configuration=config
        )
        resume_extraction(
            legacy,
            handle.execution_id,
            service=CallService(
                config.runtime,
                credential=SecretStr("synthetic"),
                adapter=ScriptedAdapter([submission(), quantities()]),
            ),
        )
        program = """
import os, sys
from pathlib import Path
from hvs_catalog.workspace import open_workspace
from hvs_catalog.benchmark.archive import archive_execution
source = open_workspace(Path(sys.argv[1]), read_only=True)
target = open_workspace(Path(sys.argv[2]))
original = target.publish_file
def publish(path, data):
    if path.startswith('.state/archives/'):
        if sys.argv[5] == 'after':
            original(path, data)
        os._exit(19)
    return original(path, data)
target.publish_file = publish
archive_execution(source, target, sys.argv[3], 'exec01', sys.argv[4], 'mineru_local')
"""
        child = subprocess.run(
            [
                sys.executable,
                "-c",
                program,
                str(legacy.root),
                str(bench.root),
                handle.execution_id,
                paper,
                boundary,
            ],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        assert child.returncode == 19, child.stderr
        result = archive_execution(
            legacy, bench, handle.execution_id, "exec01", paper, "mineru_local"
        )
        assert result.status.result == "complete"
        assert verify_archive(bench.root, "exec01", paper).usage.attempts == 2
    finally:
        legacy.close()
        bench.close()

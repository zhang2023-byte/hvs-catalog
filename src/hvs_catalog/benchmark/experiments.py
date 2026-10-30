"""Freeze experiments offline and advance only their explicitly selected executions."""

import json
import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from pydantic import Field, model_validator

from ..materials.package import import_material_package
from ..pipeline import start_extraction
from ..pipeline.batch import ExtractionTask, resume_extractions
from ..pipeline.configuration import (
    ExtractionConfig,
    check_extraction_config,
    stored_configuration,
)
from ..pipeline.inputs import code_fingerprint
from ..pipeline.results import current_result
from ..records.common import ArtifactRef, Config
from ..records.rules import current_rules_ref
from ..runtime import get_execution
from ..workspace import open_workspace
from ..workspace.layout import _write_file_published, resolve_within
from .archive import archive_execution, archive_path, open_archive, verify_archive
from .materials import round_id, selected_package, selection


class MaterialChoice(Config):
    arxiv_id: str = Field(pattern=r"^\d{4}\.\d{4,5}$")
    source_version: str
    package: str
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    pdf_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")


class MaterialSelection(Config):
    variant: Literal["mineru_local", "mineru_cloud"]
    papers: list[MaterialChoice] = Field(min_length=1)

    @model_validator(mode="after")
    def distinct(self):
        if len({p.arxiv_id for p in self.papers}) != len(self.papers):
            raise ValueError("An experiment cannot select a paper twice.")
        return self


class Experiment(Config):
    format: Literal["hvs.benchmark-experiment"]
    version: Literal[2]
    id: str = Field(pattern=r"^exec\d{2,}$")
    purpose: str = Field(min_length=1)
    created_at: str
    scope: Literal["full", "roster"]
    materials: MaterialSelection
    configuration: dict
    source: dict
    archived_origin: ArtifactRef | None = None


def read_experiment(root, name):
    path = resolve_within(Path(root), f"runs/{round_id(name)}/experiment.json")
    value = Experiment.model_validate_json(path.read_bytes()).model_dump(
        mode="json", exclude_none=True
    )
    if value["id"] != name:
        raise ValueError("Unsupported benchmark experiment.")
    return value


def write_json(path, value):
    _write_file_published(
        Path(path), (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode()
    )


def verify_selection(root, experiment):
    expected = experiment["materials"]
    actual = selection(
        Path(root),
        [entry["arxiv_id"] for entry in expected["papers"]],
        expected["variant"],
    )
    if actual != expected["papers"]:
        raise ValueError(
            "The selected material manifest, PDF, or paper version changed."
        )


def prepare_experiment(
    root, *, papers, variant, configuration, purpose, scope="full", name=None
):
    root = Path(root).resolve()
    space = open_workspace(root)
    try:
        if space.identity.format_version != 2:
            raise ValueError("Benchmark preparation requires a benchmark workspace.")
        config = ExtractionConfig.model_validate(configuration)
        check_extraction_config(config)
        if scope not in {"full", "roster"} or not purpose.strip():
            raise ValueError("Specify a purpose and supported extraction scope.")
        materials = selection(root, papers, variant)
        with space.lock():
            if name is None:
                numbers = [
                    int(p.name[4:])
                    for p in (root / "runs").iterdir()
                    if p.is_dir() and p.name.startswith("exec") and p.name[4:].isdigit()
                ]
                name = f"exec{max(numbers, default=0) + 1:02d}"
            round_id(name)
            spec = {
                "format": "hvs.benchmark-experiment",
                "version": 2,
                "id": name,
                "purpose": purpose,
                "scope": scope,
                "materials": {"variant": variant, "papers": materials},
                "configuration": config.model_dump(mode="json"),
                "source": {
                    "extraction_code_sha256": code_fingerprint(),
                    "rules": current_rules_ref(),
                },
            }
            path = root / "runs" / name / "experiment.json"
            if path.exists():
                existing = read_experiment(root, name)
                if {k: v for k, v in existing.items() if k != "created_at"} != spec:
                    raise ValueError(
                        "An existing experiment has different fixed conditions."
                    )
                spec = existing
            else:
                if path.parent.exists():
                    raise ValueError(
                        "The experiment directory already exists without a manifest."
                    )
                spec["created_at"] = datetime.now(UTC).isoformat()
                write_json(path, spec)
            for item in materials:
                paper = item["arxiv_id"]
                if (root / archive_path(name, paper)).exists():
                    raise ValueError("A closed experiment cannot be prepared again.")
                directory, _ = selected_package(root, paper, variant)
                ref = import_material_package(
                    directory, space, root=root, copy_files=False
                )
                start_extraction(
                    space,
                    prepared_source_ref=ref,
                    request_key=f"benchmark:{name}:{paper}",
                    configuration=config,
                    scope=scope,
                    execution_id=f"{name}/{paper}",
                )
        return experiment_status(root, name)
    finally:
        space.close()


def experiment_status(root, name):
    root = Path(root)
    spec = read_experiment(root, name)
    space = open_workspace(root, read_only=True)
    papers = []
    try:
        for item in spec["materials"]["papers"]:
            paper = item["arxiv_id"]
            execution = f"{name}/{paper}"
            if (root / archive_path(name, paper)).exists():
                archived = open_archive(root, name, paper)
                try:
                    _, run = current_result(archived, execution)
                finally:
                    archived.close()
                state = "archived"
            elif space.store().get_execution(execution):
                _, run = current_result(space, execution)
                state = "active"
            else:
                run, state = None, "unprepared"
            papers.append(
                {
                    "arxiv_id": paper,
                    "state": state,
                    "status": run.status.model_dump() if run else None,
                    "usage": run.usage.model_dump() if run else None,
                }
            )
        return {
            "id": name,
            "state": "archived"
            if all(p["state"] == "archived" for p in papers)
            else "active",
            "material_variant": spec["materials"]["variant"],
            "papers": papers,
        }
    finally:
        space.close()


def retire_execution(space, execution_id):
    """Delete recovery state only after an independently verified archive is committed."""
    name, paper = execution_id.split("/")
    verify_archive(space.root, name, paper)
    from langgraph.checkpoint.sqlite import SqliteSaver

    with space.lock():
        conn = space.checkpoint_connection()
        try:
            saver = SqliteSaver(conn)
            saver.setup()
            saver.delete_thread(execution_id)
        finally:
            conn.close()
        with space.store().transaction() as tx:
            owned = [
                r[0]
                for r in tx.conn.execute(
                    "SELECT DISTINCT record_id FROM records WHERE execution_id=?",
                    (execution_id,),
                )
            ]
            for key in owned:
                tx.conn.execute(
                    "DELETE FROM current_revisions WHERE record_id=?", (key,)
                )
                tx.conn.execute("DELETE FROM records WHERE record_id=?", (key,))
            for table in ("receipts", "step_results", "executions"):
                tx.conn.execute(
                    f"DELETE FROM {table} WHERE execution_id=?", (execution_id,)
                )
            for prefix in (
                "extraction_result:",
                "retained_result:",
                "framework_final_checkpoint:",
            ):
                tx.conn.execute(
                    "DELETE FROM meta WHERE key=?", (prefix + execution_id,)
                )
        temporary = space.root / ".state/executions" / execution_id
        if temporary.exists():
            shutil.rmtree(temporary)
        for parent in (temporary.parent, temporary.parent.parent):
            if parent.is_dir() and not any(parent.iterdir()):
                parent.rmdir()


def run_experiment(
    root, name, *, concurrency=None, env_file=None, service_factory=None
):
    """Resume the fixed work; the service is created only after every preflight check."""
    from ..llm.managed import CallService
    from ..pipeline.inputs import read_input

    root = Path(root)
    spec = read_experiment(root, name)
    if spec.get("archived_origin"):
        raise ValueError("Historical experiments are archived; create a new round.")
    verify_selection(root, spec)
    if spec["source"]["extraction_code_sha256"] != code_fingerprint():
        raise ValueError("Extraction code differs from the frozen experiment.")
    space = open_workspace(root)
    try:
        with space.lock():
            tasks = []
            for item in spec["materials"]["papers"]:
                execution = f"{name}/{item['arxiv_id']}"
                if (root / archive_path(name, item["arxiv_id"])).exists():
                    if space.store().get_execution(execution):
                        retire_execution(space, execution)
                    continue
                handle = get_execution(space, execution)
                document, source, _ = read_input(space, handle.input_snapshot)
                _, package = selected_package(
                    root, item["arxiv_id"], spec["materials"]["variant"]
                )
                if (
                    document["metadata"]["arxiv_id"] != item["arxiv_id"]
                    or source.data.files != package.source.files
                    or stored_configuration(handle.input_snapshot).model_dump(
                        mode="json"
                    )
                    != spec["configuration"]
                    or handle.input_snapshot.options.get("extraction_scope")
                    != spec["scope"]
                ):
                    raise ValueError(
                        "The experiment differs from its frozen execution input."
                    )
                tasks.append(ExtractionTask(space, execution))
            config = ExtractionConfig.model_validate(spec["configuration"])
            for task in tasks:
                task.service = (
                    service_factory(config)
                    if service_factory
                    else CallService(config.runtime, env_file=env_file)
                )
            reports = (
                resume_extractions(tasks, concurrency=concurrency) if tasks else []
            )
            for task, report in zip(tasks, reports, strict=True):
                if report.outcome == "finished" and report.result_status is not None:
                    paper = task.execution_id.split("/")[1]
                    archive_execution(
                        space,
                        space,
                        task.execution_id,
                        name,
                        paper,
                        spec["materials"]["variant"],
                    )
                    retire_execution(space, task.execution_id)
        return experiment_status(root, name)
    finally:
        space.close()


def score_experiment(root, name, references):
    """Use the existing scoring implementation with explicit reference paths."""
    from ..evaluation.saved import score_saved_manifest

    root = Path(root).resolve()
    spec = read_experiment(root, name)
    expected = [p["arxiv_id"] for p in spec["materials"]["papers"]]
    if set(references) != set(expected):
        raise ValueError("Scoring references must match all planned papers exactly.")
    output = root / "runs" / name / "scoring"
    if output.exists():
        raise FileExistsError(
            "Scoring output already exists; replay its frozen inputs explicitly."
        )
    manifest = {
        "format": "hvs.scoring-manifest",
        "version": 1,
        "scope": spec["scope"],
        "papers": [
            {
                "arxiv_id": paper,
                "workspace": "../..",
                "execution_id": f"{name}/{paper}",
                "reference": str(Path(references[paper]).resolve()),
            }
            for paper in expected
        ],
    }
    path = root / ".state/scoring-plans" / f"{name}.json"
    if path.exists() and json.loads(path.read_bytes()) != manifest:
        raise ValueError("The saved scoring plan specifies different references.")
    write_json(path, manifest)
    return score_saved_manifest(path, output)


def close_experiment(root, name):
    """Explicitly end remaining executions without issuing a request."""
    from ..workspace.retention import close_extraction

    root = Path(root)
    spec = read_experiment(root, name)
    space = open_workspace(root)
    try:
        with space.lock():
            for entry in spec["materials"]["papers"]:
                paper = entry["arxiv_id"]
                execution = f"{name}/{paper}"
                if (root / archive_path(name, paper)).exists():
                    if space.store().get_execution(execution):
                        retire_execution(space, execution)
                    continue
                close_extraction(space, execution, end=True)
                archive_execution(
                    space, space, execution, name, paper, spec["materials"]["variant"]
                )
                retire_execution(space, execution)
        return experiment_status(root, name)
    finally:
        space.close()


def preview_materials(root, *, papers, variant, configuration, name):
    """Archive reproducible offline previews without creating an experiment."""
    from ..materials.package import _PackageFiles, _source_record
    from ..pipeline.configuration import preview_extraction_prompt
    from ..pipeline.roster import material_prompt
    from ..workspace.archives import publish_json

    root = Path(root).resolve()
    if not name or Path(name).name != name or name in {".", ".."}:
        raise ValueError("Preview name must be a directory name.")
    spec = {
        "configuration": configuration,
        "materials": {"variant": variant, "papers": selection(root, papers, variant)},
    }
    config = ExtractionConfig.model_validate(configuration)
    check_extraction_config(config)
    space = open_workspace(root)
    previews = []
    try:
        with space.lock():
            for paper in papers:
                directory, package = selected_package(root, paper, variant)
                import_material_package(directory, space, root=root, copy_files=False)
                inputs = material_prompt(
                    _PackageFiles(root), {}, _source_record(package)
                )
                for step in ("roster", "quantities"):
                    preview = preview_extraction_prompt(
                        config,
                        step,
                        inputs,
                        ["OFFLINE-TARGET"] if step == "quantities" else None,
                    )
                    ref = publish_json(
                        space, f"preparations/{name}/{paper}/{step}.json", preview
                    )
                    previews.append(
                        {
                            "arxiv_id": paper,
                            "step": step,
                            "preview": ref.model_dump(),
                            "request_sha256": preview["request_sha256"],
                            "images": preview["images"],
                        }
                    )
            result = {
                **spec,
                "previews": previews,
                "model_calls": 0,
                "numeric_target": "OFFLINE-TARGET is a preview placeholder.",
            }
            space.publish_file(
                f"preparations/{name}/preview.json",
                (json.dumps(result, ensure_ascii=False, indent=2) + "\n").encode(),
            )
        return result
    finally:
        space.close()

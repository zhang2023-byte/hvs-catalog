"""Score explicit saved executions against approved Gold without extraction calls.

Only manifest-selected targets are read. The portable bundle retains the complete
plan, original numerical records, Gold commit/bytes, fixed material fingerprints,
execution facts, and current program fingerprint. Replays need no private repository.
"""

from __future__ import annotations

import csv
import hashlib
import json
import subprocess
from importlib.metadata import version
from pathlib import Path

from pydantic import Field, model_validator

from ..pipeline import get_extraction
from ..records.common import Config, sha256_of
from ..records.rules import load_rules, quantity_paths
from ..runtime import get_execution
from ..workspace import open_workspace, resolve_within
from ..workspace.store import verify_artifact_refs
from .adapt import (
    GoldReadinessReview,
    adapt_compact_gold_pair,
    prediction_from_execution,
)
from .bundle import save_scoring_bundle, score_scoring_bundle
from .compact import check_compact_gold_pair
from .scoring import (
    InputReference,
    PaperEvaluationScope,
    ScoringPlan,
    ScoringReport,
    auxiliary_config_digest,
    gold_input_digest,
    numeric_scheme_reference,
    numeric_scoring_config,
    score_batch,
)
from .store import GoldTarget


class SavedPaperInput(Config):
    paper_key: str = Field(min_length=1)
    gold_target: Path
    material_root: Path
    workspace: Path
    execution_id: str = Field(min_length=1)
    scoring_review: GoldReadinessReview | None = None


class SavedScoringInput(Config):
    gold_repository: Path
    papers: list[SavedPaperInput] = Field(min_length=1)

    @model_validator(mode="after")
    def _unique(self):
        if len({p.paper_key for p in self.papers}) != len(self.papers):
            raise ValueError("评分输入论文重复")
        return self


def _file_digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _git(root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(root), *args],
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def program_reference() -> InputReference:
    """Fingerprint installed source and resources; no checkout path is embedded."""
    root = Path(__file__).resolve().parents[1]
    files = {
        p.relative_to(root).as_posix(): _file_digest(p)
        for p in sorted(root.rglob("*"))
        if p.is_file() and p.suffix in {".py", ".toml", ".json", ".yaml"}
    }
    return InputReference(
        name="hvs-catalog", version=version("hvs-catalog"), fingerprint=sha256_of(files)
    )


def _paths(manifest: SavedScoringInput, base: Path) -> SavedScoringInput:
    data = manifest.model_dump(mode="python")
    data["gold_repository"] = (base / manifest.gold_repository).resolve()
    for row in data["papers"]:
        for field in ("gold_target", "material_root", "workspace"):
            row[field] = (base / row[field]).resolve()
    return SavedScoringInput.model_validate(data)


def prepare_saved_scoring(
    manifest: SavedScoringInput,
) -> tuple[ScoringPlan, list, list]:
    """Validate exact Gold/PDF/execution bindings before any numerical comparison."""
    repository = manifest.gold_repository.resolve()
    commit = _git(repository, "rev-parse", "HEAD")
    selected_paths = []
    for item in manifest.papers:
        selected_paths.append(
            item.gold_target.resolve().relative_to(repository).as_posix()
        )
    worktree_status = _git(repository, "status", "--porcelain", "--", *selected_paths)
    gold, predictions, snapshots, gaps = [], [], [], {}
    materials = []
    for item in manifest.papers:
        target = GoldTarget(item.gold_target)
        current = target.read_current()
        if current is None:
            raise ValueError(f"{item.paper_key} 没有完整获准Gold")
        pair, approval = current
        if pair.answer.arxiv_id != item.paper_key:
            raise ValueError("Gold论文与预定评分范围不符")
        checked = check_compact_gold_pair(pair, material_root=item.material_root)
        adaptation = adapt_compact_gold_pair(pair, scoring_review=item.scoring_review)
        paper_gaps = list(adaptation.readiness_gaps)
        paper_gaps.extend(
            f"{p.code}:{p.subject}:{p.message}"
            for p in checked.problems
            if p.category in {"structure", "material"}
            and p.code not in {"material_differences"}
        )
        space = open_workspace(item.workspace, read_only=True)
        try:
            handle = get_execution(space, item.execution_id)
            status = get_extraction(space, item.execution_id)
            run_record = space.store().read_record(handle.run_ref)
            sources = [
                space.store().read_record(ref)
                for ref in handle.input_snapshot.source_refs
            ]
            source_files = []
            fixed = set()
            for source in sources:
                if (
                    source.data.paper_key != item.paper_key
                    or source.data.source_version != pair.metadata.paper.version
                ):
                    raise ValueError("提取与Gold固定论文版本不符")
                for file in source.data.files:
                    path = resolve_within(space.root, file.path)
                    actual = _file_digest(path)
                    if actual != file.sha256:
                        raise ValueError(f"提取固定材料指纹不符：{file.file_id}")
                    source_files.append(file.model_dump(mode="json"))
                    if file.role in {"pdf", "supplement"}:
                        fixed.add(actual)
            if {file.sha256 for file in pair.metadata.files} != fixed:
                raise ValueError("Gold与提取PDF／附表范围不一致")
            prediction = prediction_from_execution(space, item.execution_id)
            archive_problems = verify_artifact_refs([run_record, *sources], space.root)
            if archive_problems:
                paper_gaps.extend("extraction_archive:" + p for p in archive_problems)
            if prediction.paper_key != item.paper_key:
                raise ValueError("预测论文与预定范围不符")
            snapshots.append(
                {
                    "paper_key": item.paper_key,
                    "gold_target": str(target.root),
                    "gold_files": {
                        name: _file_digest(target.root / name)
                        for name in ("answer.json", "metadata.json", "approval.json")
                    },
                    "gold_binding": adaptation.binding,
                    "gold_answer": pair.answer.model_dump(mode="json"),
                    "gold_metadata": pair.metadata.model_dump(
                        mode="json", by_alias=True
                    ),
                    "save_approval_binding": approval.binding.model_dump(mode="json"),
                    "pdf_checks": checked.model_dump(mode="json"),
                    "workspace": str(space.root),
                    "execution_id": item.execution_id,
                    "extraction_input_digest": handle.input_digest,
                    "source_files": source_files,
                    "run_facts": status,
                    "run_record": run_record.model_dump(mode="json"),
                }
            )
        finally:
            space.store().close()
        if paper_gaps:
            gaps[item.paper_key] = sorted(set(paper_gaps))
        gold.extend(adaptation.entries)
        predictions.append(prediction)
        materials.append({"paper_key": item.paper_key, "files": source_files})
    rules = load_rules()
    config = numeric_scoring_config()
    plan = ScoringPlan(
        comparison_mode="numeric",
        paper_keys=[p.paper_key for p in manifest.papers],
        scopes=[
            PaperEvaluationScope(
                paper_key=p.paper_key, quantity_paths=list(quantity_paths())
            )
            for p in manifest.papers
        ],
        gold_ref=InputReference(
            name="approved-gold", version=commit, fingerprint=gold_input_digest(gold)
        ),
        materials_ref=InputReference(
            name="fixed-pdf-and-parser-output",
            version="1",
            fingerprint=sha256_of(materials),
        ),
        science_rules_ref=InputReference(
            name="science-rules", version=rules.version, fingerprint=rules.sha256
        ),
        scoring_scheme_ref=numeric_scheme_reference(),
        program_ref=program_reference(),
        auxiliary_ref=InputReference(
            name="no-auxiliary-model",
            version="1",
            fingerprint=auxiliary_config_digest(config),
        ),
        readiness_gaps=gaps,
        input_snapshot={
            "gold_repository": str(repository),
            "gold_commit": commit,
            "gold_selected_worktree_status": worktree_status,
            "papers": snapshots,
        },
    )
    return plan, gold, predictions


def export_scoring_report(report: ScoringReport, output: Path) -> None:
    """Export per-paper/quantity metrics, one-to-one pairings, and diagnostic failures."""
    output.mkdir(parents=True, exist_ok=True)
    (output / "scoring-report.json").write_text(
        report.model_dump_json(indent=2) + "\n", encoding="utf-8"
    )
    with (output / "delivery.csv").open("w", newline="", encoding="utf-8") as stream:
        writer = csv.writer(stream)
        writer.writerow(
            [
                "paper",
                "planned",
                "delivered",
                "format_valid",
                "delivered_over_planned",
                "valid_over_delivered",
                "valid_over_planned",
                "completion",
                "execution_state",
                "science_state",
                "pending_questions",
                "errors",
            ]
        )
        for p in report.papers:
            m = p.l0
            writer.writerow(
                [
                    p.paper_key,
                    m.planned,
                    m.delivered,
                    m.format_valid,
                    m.delivered_over_planned,
                    m.valid_over_delivered,
                    m.valid_over_planned,
                    json.dumps(m.completion_counts),
                    p.execution_state,
                    p.science_state,
                    p.pending_questions,
                    ";".join(p.prediction_errors),
                ]
            )
    with (output / "metrics.csv").open("w", newline="", encoding="utf-8") as stream:
        writer = csv.writer(stream)
        writer.writerow(
            [
                "paper",
                "layer",
                "quantity",
                "matched",
                "predicted",
                "gold",
                "precision",
                "recall",
                "f1",
                "readiness",
            ]
        )
        for paper in report.papers:
            if paper.l1:
                m = paper.l1
                writer.writerow(
                    [
                        paper.paper_key,
                        "L1",
                        "",
                        m.tp,
                        m.tp + m.fp,
                        m.tp + m.fn,
                        m.precision,
                        m.recall,
                        m.f1,
                        paper.readiness,
                    ]
                )
            rows = [("", paper.l2)] + [
                (row.quantity_path, row.metrics) for row in paper.quantity_breakdown
            ]
            for path, m in rows:
                if m:
                    writer.writerow(
                        [
                            paper.paper_key,
                            "L2",
                            path,
                            m.correct,
                            m.predicted,
                            m.gold,
                            m.precision,
                            m.recall,
                            m.f1,
                            paper.readiness,
                        ]
                    )
    for filename, fields, rows in (
        (
            "pairings.csv",
            ["paper", "layer", "status", "gold_ref", "prediction_ref", "fields"],
            [
                [
                    p.paper_key,
                    r.layer,
                    r.status,
                    r.gold_ref,
                    r.prediction_ref,
                    ";".join(r.fields),
                ]
                for p in report.papers
                for r in p.pairings
            ],
        ),
        (
            "diagnostics.csv",
            ["paper", "code", "message", "refs"],
            [
                [p.paper_key, r.code, r.message, ";".join(r.refs)]
                for p in report.papers
                for r in [*p.diagnostics, *p.unresolved]
            ],
        ),
    ):
        with (output / filename).open("w", newline="", encoding="utf-8") as stream:
            writer = csv.writer(stream)
            writer.writerow(fields)
            writer.writerows(rows)


def score_saved_manifest(path: Path, output: Path) -> ScoringReport:
    manifest = _paths(
        SavedScoringInput.model_validate_json(path.read_bytes()), path.parent
    )
    plan, gold, predictions = prepare_saved_scoring(manifest)
    report = score_batch(plan, gold, predictions)
    if output.exists() and any(output.iterdir()):
        raise ValueError("评分输出目录非空；请指定新目录，保留既有快照")
    bundle = output / "evaluation-inputs.json"
    save_scoring_bundle(
        bundle,
        plan=plan,
        gold=gold,
        predictions=predictions,
        auxiliary=numeric_scoring_config(),
    )
    if score_scoring_bundle(bundle) != report:
        raise ValueError("保存后重评分不一致")
    export_scoring_report(report, output)
    return report

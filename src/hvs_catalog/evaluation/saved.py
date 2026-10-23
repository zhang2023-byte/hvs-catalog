"""Score only explicitly selected references and saved executions."""

import csv
import hashlib
import json
import subprocess
from pathlib import Path
from typing import Literal

from pydantic import Field, model_validator

from ..records.common import Config
from ..records.literature_contribution import QuantityId
from ..records.roster_validation import strict_json
from ..records.rules import load_rules
from ..workspace import open_workspace
from .adapt import prediction_from_execution
from .bundle import program_reference, save_scoring_bundle, score_scoring_bundle
from .scoring import GoldReference, ScoringPlan, score_batch


class SavedPaperInput(Config):
    arxiv_id: str = Field(min_length=1)
    reference: Path
    workspace: Path | None = None
    execution_id: str | None = None

    @model_validator(mode="after")
    def _execution(self):
        if (self.workspace is None) != (self.execution_id is None):
            raise ValueError("Provide both workspace and execution_id, or neither.")
        return self


class SavedScoringInput(Config):
    format: Literal["hvs.scoring-manifest"] = "hvs.scoring-manifest"
    version: Literal[1] = 1
    scope: Literal["full", "roster"] = "full"
    quantities: list[QuantityId] = Field(
        default_factory=lambda: [q.id for q in load_rules().quantities]
    )
    papers: list[SavedPaperInput] = Field(min_length=1)

    @model_validator(mode="after")
    def _unique(self):
        if len({p.arxiv_id for p in self.papers}) != len(self.papers):
            raise ValueError("A paper appears more than once in the scoring manifest.")
        return self


def _git_snapshot(path):
    """Record observed source identity; a commit is not an approval declaration."""

    def git(*args):
        return subprocess.run(
            ["git", "-C", str(path.parent), *args],
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
        ).stdout.strip()

    try:
        root = Path(git("rev-parse", "--show-toplevel"))
        commit = git("rev-parse", "HEAD")
        relative = path.relative_to(root).as_posix()
        tracked = (
            subprocess.run(
                ["git", "-C", str(root), "ls-files", "--error-unmatch", "--", relative],
                capture_output=True,
                check=False,
                timeout=10,
            ).returncode
            == 0
        )
        dirty = (
            bool(git("status", "--porcelain", "--untracked-files=all", "--", str(path)))
            or not tracked
        )
        return {"commit": commit, "path": relative, "tracked": tracked, "dirty": dirty}
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def prepare_saved_scoring(path):
    path = Path(path)
    raw = path.read_bytes()
    manifest = SavedScoringInput.model_validate(strict_json(raw.decode()))
    gold, predictions, files = [], [], []
    for item in manifest.papers:
        reference_path = (path.parent / item.reference).resolve()
        reference_bytes = reference_path.read_bytes()
        reference = GoldReference.model_validate(strict_json(reference_bytes.decode()))
        if reference.metadata.arxiv_id != item.arxiv_id:
            raise ValueError("A reference identifies a different planned paper.")
        gold.append(reference)
        files.append(
            {
                "path": str(reference_path),
                "sha256": hashlib.sha256(reference_bytes).hexdigest(),
                "git": _git_snapshot(reference_path),
            }
        )
        if item.workspace is not None:
            space = open_workspace(
                (path.parent / item.workspace).resolve(), read_only=True
            )
            if space.identity.format_version == 2 and "/" in item.execution_id:
                from ..benchmark.archive import archive_path, open_archive

                round_name, paper = item.execution_id.split("/")
                if (space.root / archive_path(round_name, paper)).is_file():
                    root = space.root
                    space.close()
                    space = open_archive(root, round_name, paper)
            try:
                prediction = prediction_from_execution(space, item.execution_id)
            finally:
                space.close()
            if prediction.content.metadata.arxiv_id != item.arxiv_id:
                raise ValueError("An execution identifies a different planned paper.")
            predictions.append(prediction)
    plan = ScoringPlan(
        papers=[p.arxiv_id for p in manifest.papers],
        scope=manifest.scope,
        quantities=manifest.quantities,
        program=program_reference(),
        provenance={
            "manifest": {
                "path": str(path.resolve()),
                "sha256": hashlib.sha256(raw).hexdigest(),
            },
            "reference_files": files,
        },
    )
    return plan, gold, predictions


def _write_report(report, output):
    with (output / "scoring-report.json").open("x", encoding="utf-8") as stream:
        stream.write(report.model_dump_json(indent=2) + "\n")
    with (output / "papers.csv").open("x", encoding="utf-8", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(
            [
                "arxiv_id",
                "version",
                "execution_state",
                "completion",
                "delivered",
                "format_valid",
                "tp",
                "fp",
                "fn",
                "l1_precision",
                "l1_recall",
                "l1_f1",
                "correct",
                "predicted",
                "gold",
                "l2_precision",
                "l2_recall",
                "l2_f1",
                "contribution_correct",
                "matched_stars",
                "contribution_accuracy",
                "missing_identifiers",
                "extra_identifiers",
                "correct_empty",
            ]
        )
        for p in report.papers:
            d = p.roster_details
            writer.writerow(
                [
                    p.arxiv_id,
                    p.version,
                    p.execution_state,
                    next(iter(p.l0.completion_counts)),
                    p.l0.delivered,
                    p.l0.format_valid,
                    p.l1.tp,
                    p.l1.fp,
                    p.l1.fn,
                    p.l1.precision,
                    p.l1.recall,
                    p.l1.f1,
                    *(
                        [
                            p.l2.correct,
                            p.l2.predicted,
                            p.l2.gold,
                            p.l2.precision,
                            p.l2.recall,
                            p.l2.f1,
                        ]
                        if p.l2
                        else [None] * 6
                    ),
                    d.contribution_correct,
                    d.matched_stars,
                    d.contribution_accuracy,
                    d.missing_identifiers,
                    d.extra_identifiers,
                    p.correct_empty,
                ]
            )
    with (output / "quantities.csv").open("x", encoding="utf-8", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(
            [
                "arxiv_id",
                "quantity",
                "correct",
                "predicted",
                "gold",
                "precision",
                "recall",
                "f1",
            ]
        )
        for p in report.papers:
            for quantity, metrics in p.quantities.items():
                writer.writerow(
                    [
                        p.arxiv_id,
                        quantity,
                        metrics.correct,
                        metrics.predicted,
                        metrics.gold,
                        metrics.precision,
                        metrics.recall,
                        metrics.f1,
                    ]
                )
    with (output / "differences.csv").open("x", encoding="utf-8", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(
            [
                "arxiv_id",
                "layer",
                "status",
                "quantity",
                "gold_path",
                "prediction_path",
                "fields",
                "basis",
            ]
        )
        for p in report.papers:
            for row in p.pairings:
                writer.writerow(
                    [
                        p.arxiv_id,
                        row.layer,
                        row.status,
                        row.quantity,
                        row.gold_path,
                        row.prediction_path,
                        ";".join(row.fields),
                        json.dumps(row.basis, ensure_ascii=False),
                    ]
                )


def export_scoring_report(report, output):
    output = Path(output)
    output.mkdir(parents=True, exist_ok=False)
    _write_report(report, output)


def score_saved_manifest(path, output):
    plan, gold, predictions = prepare_saved_scoring(path)
    report = score_batch(plan, gold, predictions)
    output = Path(output)
    output.mkdir(parents=True, exist_ok=False)
    bundle = output / "scoring-inputs.json"
    save_scoring_bundle(bundle, plan=plan, gold=gold, predictions=predictions)
    if score_scoring_bundle(bundle) != report:
        raise ValueError("Saved scoring inputs did not reproduce the original report.")
    _write_report(report, output)
    return report

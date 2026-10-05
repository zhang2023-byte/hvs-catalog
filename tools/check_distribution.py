"""Build distributions and verify installation in fresh environments outside the
repository.

1. uv build creates wheel/sdist in external temporary directories.
2. Check package contents contain only build/install files, excluding official data,
   development materials, and run outputs.
3. Create external fresh uv environments and install wheels.
4. Independently check CLI help/version, python -m, and package imports with PYTHONPATH
   cleared.
5. Exercise installed persistence/recovery demos: retained pending items/results, single
   decision application, conflicting rejection, and actual-exit duplicate-call counts.
6. Exercise public workspace initialization, synthetic import/pause, fresh-process
   decision/completion, backup/restore, terminal checks, and duplicate-submission
   protection.
7. Exercise synthetic source import, body/table preparation, and model-free consumption
   through installed_pipeline_checks.MATERIALS_DRIVER.
8. Exercise Gold v4 migration, fresh drafts/conversion, simulated approvals, paired
   additions/revisions, CLI, stale-approval rejection, interrupted partial-read
   refusal/manual recovery, and lost-return retries without second versions.
9. Exercise package provenance/method queries, 3 papers x 2 review modes (6
   CLI cases), repeated scoring consistency, plan-tampering rejection, fresh-process
   pause/decision/recovery, and isolated comparison reports with unknown expert time.
10. Exercise installed model interfaces/CLI against local synthetic HTTP/SSE: four
    parameter conversions, tools, persistence, and recovery without duplicate calls or
    paid models.
11. Exercise extraction configuration, shared English prompt resources, CLI previews,
    four-model step overrides, and saved mock pause/decision/recovery across processes.
12. Join installed PDF preparation to roster CLI/model calls over local HTTP,
    inspect common records, and recover without a second service request.

All materials are synthetic and independent of repository source paths. Usage: uv run
python tools/check_distribution.py [Python version]. The optional version, such as 3.12,
selects environment Python; otherwise use uv's default.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import tarfile
import tempfile
import tomllib
import zipfile
from collections.abc import Iterable, Sequence
from pathlib import Path

from installed_extraction_checks import verify_installed_extraction
from installed_llm_checks import verify_installed_llm
from installed_pdf_fixture import PDF_FIXTURE
from installed_pipeline_checks import verify_pipeline_and_scoring

REPO_ROOT = Path(__file__).resolve().parents[1]

# Exclude official catalog data, development materials, and run outputs from every distribution.
FORBIDDEN_SEGMENTS = frozenset(
    {
        ".dev",
        ".git",
        ".github",
        ".venv",
        ".work",
        "__pycache__",
        "build",
        "catalog",
        "contracts",
        "dist",
        "docs",
        "tests",
        "tools",
    }
)

# Seven explicitly retired modules must never reappear; retain valid modules/package entry points.
RETIRED_MODULE_PATHS = frozenset(
    {
        "hvs_catalog/demo/graph.py",
        "hvs_catalog/demo/service.py",
        "hvs_catalog/demo/store.py",
        "hvs_catalog/synthetic/flow.py",
        "hvs_catalog/synthetic/runner.py",
        "hvs_catalog/synthetic/service.py",
        "hvs_catalog/evaluation/git_revision.py",
    }
)

# sdist roots allow build/install files only; hatchling adds .gitignore automatically.
SDIST_ALLOWED_ROOTS = frozenset(
    {".gitignore", "PKG-INFO", "pyproject.toml", "README.md", "src"}
)


def project_version() -> str:
    pyproject = tomllib.loads(
        (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    )
    return str(pyproject["project"]["version"])


def top_levels(names: Iterable[str]) -> set[str]:
    return {name.split("/", maxsplit=1)[0] for name in names}


def find_forbidden_paths(names: Iterable[str]) -> list[str]:
    forbidden = []
    for name in names:
        if any(segment in FORBIDDEN_SEGMENTS for segment in name.split("/")):
            forbidden.append(name)
    return forbidden


def check_wheel(names: Sequence[str]) -> list[str]:
    """Wheel top level allows only hvs_catalog/ and its dist-info metadata."""
    violations = find_forbidden_paths(names)
    violations.extend(name for name in names if name in RETIRED_MODULE_PATHS)
    for top in sorted(top_levels(names)):
        if top in FORBIDDEN_SEGMENTS:
            continue  # These paths were already reported by full member path.
        is_package = top == "hvs_catalog"
        is_dist_info = top.startswith("hvs_catalog-") and top.endswith(".dist-info")
        if not is_package and not is_dist_info:
            violations.append(f"wheel 顶层出现意外内容：{top}")
    return violations


def check_sdist(names: Sequence[str], version: str) -> list[str]:
    """Source archives allow only hvs_catalog-<version>/ with required build/install
    contents.
    """
    violations = find_forbidden_paths(names)
    root = f"hvs_catalog-{version}"
    for name in names:
        if name.removeprefix(f"{root}/src/") in RETIRED_MODULE_PATHS:
            violations.append(name)
        if not name.startswith(f"{root}/"):
            violations.append(f"源码包顶层出现意外内容：{name}")
            continue
        member = name.removeprefix(f"{root}/").split("/", maxsplit=1)[0]
        if member and member not in SDIST_ALLOWED_ROOTS:
            violations.append(f"源码包包含构建安装不需要的内容：{name}")
    return violations


def run(command: Sequence[object], cwd: Path) -> subprocess.CompletedProcess[str]:
    print(f"$ {' '.join(str(part) for part in command)}")
    return subprocess.run(
        [str(part) for part in command],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
    )


def fail(result: subprocess.CompletedProcess[str], step: str) -> int:
    print(f"[失败] {step}（退出码 {result.returncode}）")
    if result.stdout:
        print(result.stdout)
    if result.stderr:
        print(result.stderr)
    return 1


def verify_installation(venv_dir: Path, work_dir: Path, version: str) -> list[str]:
    """Verify installed commands/imports in independent external-directory subprocesses."""
    problems = []
    bin_dir = venv_dir / ("Scripts" if os.name == "nt" else "bin")
    python = bin_dir / "python"
    script = bin_dir / "hvs-catalog"
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    checks = [
        ([script, "--version"], f"hvs-catalog {version}"),
        ([script, "--help"], "usage: hvs-catalog"),
        ([python, "-m", "hvs_catalog", "--version"], f"hvs-catalog {version}"),
        (
            [
                python,
                "-c",
                "from importlib.metadata import version; print(version('hvs-catalog'))",
            ],
            version,
        ),
    ]
    for command, expected in checks:
        result = subprocess.run(
            [str(part) for part in command],
            cwd=work_dir,
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            problems.append(
                f"命令失败（退出码 {result.returncode}）：{' '.join(str(p) for p in command)}\n{result.stderr}"
            )
            continue
        if expected not in result.stdout:
            problems.append(
                f"输出不符合预期：{' '.join(str(p) for p in command)}\n"
                f"期望包含 {expected!r}，实际输出 {result.stdout!r}"
            )
    return problems


def verify_demo(script: Path, work_dir: Path) -> list[str]:
    """Shared CLI restart, saved-result reuse, decision replay and response death."""
    import json

    problems = []
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)

    def command(*args, code=0):
        result = subprocess.run(
            [str(script), "demo", *map(str, args)],
            cwd=work_dir,
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != code:
            raise AssertionError(
                f"demo {args}: {result.returncode}, expected {code}\n{result.stdout}{result.stderr}"
            )
        return json.loads(result.stdout) if code == 0 else result

    try:
        root = work_dir / "demo-space"
        first = command("run", "--space", root)
        assert first["report"]["outcome"] == "paused"
        assert first["external_calls"]["calls"] == 2
        status = command("status", "--space", root)
        assert status["input_digest"] == first["input_digest"]
        assert status["pending"][0]["status"] == "open"
        done = command("decide", "--space", root, "--choice", "no_overall_conclusion")
        assert done["report"]["outcome"] == "finished"
        assert done["external_calls"]["calls"] == 2
        again = command("decide", "--space", root, "--choice", "no_overall_conclusion")
        assert not again["first_save"]
        assert again["external_calls"]["calls"] == 2
        command("decide", "--space", root, "--choice", "keep_pending", code=2)
        crash = work_dir / "demo-crash-space"
        command("run", "--space", crash, "--crash-after-response", code=70)
        assert command("status", "--space", crash)["external_calls"]["calls"] == 1
        resumed = command("run", "--space", crash)
        assert resumed["report"]["outcome"] == "paused"
        assert resumed["external_calls"]["calls"] == 3
        command("decide", "--space", crash, "--choice", "no_overall_conclusion")
        command("reset", "--space", root, code=2)
    except (AssertionError, ValueError, KeyError) as exc:
        problems.append(f"Shared demo verification: {exc}")
    return problems


WORKSPACE_DRIVER = r'''
"""Installed shared extraction/runtime/review/backup public chain, new processes."""
import json
import sys
from pathlib import Path
from hvs_catalog.pipeline import (ExtractionRequest, StepModel, start_extraction,
                                 resume_extraction, pending_questions)
from hvs_catalog.pipeline.mock import (PAPER_A, PipelineMockService, import_mock_source,
                                       paper_record_id, submit_expert_decision)
from hvs_catalog.workspace import (initialize_workspace, open_workspace, backup_workspace,
                                   restore_workspace, WorkspaceConflictError,
                                   WorkspaceIntegrityError, RecoveryInconsistentError)
step, root = sys.argv[1], Path(sys.argv[2])
path, journal = root / "space", root / "workspace-calls.jsonl"
if step == "run":
    import subprocess
    space = initialize_workspace(path, config={"kept": True})
    assert initialize_workspace(path, config={"ignored": True}).read_config() == {"kept": True}
    unknown = root / "unknown"
    unknown.mkdir()
    (unknown / "note").write_bytes(b"keep")
    try:
        initialize_workspace(unknown)
    except WorkspaceConflictError:
        pass
    else:
        raise AssertionError("unknown directory adopted")
    assert (unknown / "note").read_bytes() == b"keep"
    with space.lock():
        probe = subprocess.run([sys.executable, "-c",
            "import sys\nfrom hvs_catalog.workspace import open_workspace, WorkspaceBusyError\n"
            "space=open_workspace(sys.argv[1])\ntry:\n space.lock().acquire()\n"
            "except WorkspaceBusyError:\n sys.exit(3)\nsys.exit(1)", str(path)],
            capture_output=True, text=True, check=False)
        assert probe.returncode == 3, probe.stderr
    handle = start_extraction(space, request=ExtractionRequest(
        source_refs=[import_mock_source(space, PAPER_A)], method="roster", mode="synthetic",
        models=[StepModel(model="synthetic-mock")], expert_adjudication=True), request_key="installed-workspace")
    report = resume_extraction(space, handle.execution_id, service=PipelineMockService(journal))
    assert report.outcome == "paused", report.errors
    print("WORKSPACE-PAUSED")
    print(json.dumps(report.pending_question["question_ref"]))
    space.close()
elif step in ("decide", "verify"):
    from hvs_catalog.records.common import RecordRef
    q = RecordRef.model_validate(json.loads(sys.argv[3]))
    space = open_workspace(path)
    if step == "decide":
        submit_expert_decision(space, execution_id="exec-000001", question_ref=q,
                              request_key="installed-workspace-decision", choice="no_overall_conclusion")
        report = resume_extraction(space, "exec-000001", service=PipelineMockService(journal))
        assert report.outcome == "finished", report.errors
        assert PipelineMockService(journal).summary()["calls"] == 2
        assert space.store().current_revision(paper_record_id(PAPER_A, "exec-000001")) == 2
        print("WORKSPACE-FINISHED")
    else:
        backup_workspace(space, root / "backup")
        restored = restore_workspace(root / "backup", root / "restored")
        assert restored.identity.space_id == space.identity.space_id
        report = resume_extraction(restored, "exec-000001", service=None)
        assert (report.outcome, report.mode) == ("finished", "already-finished")
        replay = submit_expert_decision(restored, execution_id="exec-000001", question_ref=q,
                                       request_key="installed-workspace-decision", choice="no_overall_conclusion")
        assert not replay.first_save
        assert restored.store().current_revision(paper_record_id(PAPER_A, "exec-000001")) == 2
        # Client restore does not erase the service's extra call after its backup.
        service = PipelineMockService(journal)
        service.call(step="roster", request={"step": "roster"},
                     material={"paper_key": PAPER_A, "roster_entries": []})
        second = restore_workspace(root / "backup", root / "restored-again")
        assert PipelineMockService(journal).summary()["calls"] == 3
        assert not (second.root / journal.name).exists()
        second.close()
        from hvs_catalog.workspace.backup import framework_final_checkpoint, framework_final_key
        assert framework_final_checkpoint(restored, "exec-000001") is not None
        with restored.store().transaction() as tx:
            tx.conn.execute("DELETE FROM meta WHERE key = ?", (framework_final_key("exec-000001"),))
        try:
            resume_extraction(restored, "exec-000001", service=None)
        except RecoveryInconsistentError:
            pass
        else:
            raise AssertionError("missing completion credential accepted")
        assert framework_final_checkpoint(restored, "exec-000001") is None
        try:
            backup_workspace(restored, root / "invalid-backup")
        except WorkspaceIntegrityError:
            pass
        else:
            raise AssertionError("missing completion credential accepted by backup")
        restored.close()
        print("WORKSPACE-VERIFIED")
    space.close()
else:
    raise AssertionError(step)
'''


def verify_workspace(python: Path, work_dir: Path) -> list[str]:
    """Exercise installed workspace save/review/backup/restore outside the repository.

    Three independent processes cover pause, decisions/completion, and
    backup/restore/replay protection, with PYTHONPATH cleared and installed-package-only
    imports.
    """
    problems: list[str] = []
    driver = work_dir / "workspace_check.py"
    driver.write_text(WORKSPACE_DRIVER, encoding="utf-8")
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)

    def run_step(name: str, *args: object) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [str(python), str(driver), name, str(work_dir), *map(str, args)],
            cwd=work_dir,
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )

    run_result = run_step("run")
    if run_result.returncode != 0:
        problems.append(
            f"工作空间执行步骤失败（退出码 {run_result.returncode}）：\n"
            f"{run_result.stdout}{run_result.stderr}"
        )
        return problems
    lines = [line for line in run_result.stdout.splitlines() if line.strip()]
    if not lines or lines[0] != "WORKSPACE-PAUSED":
        problems.append(f"工作空间执行步骤输出异常：{run_result.stdout!r}")
        return problems
    question_ref = lines[1]

    decide_result = run_step("decide", question_ref)
    if (
        decide_result.returncode != 0
        or "WORKSPACE-FINISHED" not in decide_result.stdout
    ):
        problems.append(
            f"工作空间决定步骤失败（退出码 {decide_result.returncode}）：\n"
            f"{decide_result.stdout}{decide_result.stderr}"
        )
        return problems

    verify_result = run_step("verify", question_ref)
    if (
        verify_result.returncode != 0
        or "WORKSPACE-VERIFIED" not in verify_result.stdout
    ):
        problems.append(
            f"工作空间备份恢复核对步骤失败（退出码 {verify_result.returncode}）：\n"
            f"{verify_result.stdout}{verify_result.stderr}"
        )
    return problems


GOLD_DRIVER = '''
"""External installed Gold checks: migration, validation, simulated approval, saves, and manual recovery.

Use installed public APIs/CLI with synthetic materials only, without repository paths, legacy repositories, or real Gold. Steps: migrate legacy synthetic v4 to drafts; check; fresh preparation/approval/v1 save/replay; crash at after_files, then recover in a new subprocess and verify one version.
"""

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

root = Path(sys.argv[1])
step = sys.argv[2]
material = root / "materials"
material.mkdir(exist_ok=True)
# Synthetic paper PDF shared by every step.
PDF_SHA256 = None




def add_synthetic_metadata(document):
    """Explicit synthetic attribution/material facts, never production defaults."""
    import hashlib
    document["paper"]["version"] = "synthetic-v1"
    document["provenance"]["prepared_by"] = {
        "actor": {"actor_id": "synthetic-maker", "kind": "human", "model": None},
        "prepared_at": "2026-09-22T00:00:00+00:00",
        "action": "migration" if document["provenance"]["origin"] == "migration" else "initial_draft",
        "description": "Synthetic preparation for offline tests.",
    }
    document["material_correspondence"] = {
        "status": "checked", "pdf_file_ids": [x["file_id"] for x in document["files"]],
        "checked_by": {"actor_id": "synthetic-checker", "kind": "human"},
        "checked_at": "2026-09-22T00:00:00+00:00", "basis": "Synthetic matching materials.", "differences": [],
    }
    return document


def ensure_pdf():
    global PDF_SHA256
    if PDF_SHA256 is None:
        data = synthetic_pdf([
            ["Synthetic Hypervelocity Star Survey"],
            ["Table 2: candidate kinematics", "SYN-A 612.0 +- 8.0 km/s"],
            ["Section 5: conclusions"],
        ])
        (material / "paper.pdf").write_bytes(data)
        PDF_SHA256 = hashlib.sha256(data).hexdigest()
    return PDF_SHA256


def synthetic_pdf(pages):
    """Synthetic PDF with complete xref/font resources for parser page-tree reads."""
    header = b"%PDF-1.4\\n"
    objects = [b"", b"", b""]
    objects[0] = b"<< /Type /Catalog /Pages 2 0 R >>"
    page_numbers = list(range(4, 4 + 2 * len(pages), 2))
    kids = " ".join(f"{n} 0 R" for n in page_numbers)
    objects[1] = f"<< /Type /Pages /Kids [{kids}] /Count {len(pages)} >>".encode()
    objects[2] = b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"
    for number, lines in zip(page_numbers, pages):
        content = "BT /F1 12 Tf 72 720 Td\\n" + "\\n".join(
            f"({line}) Tj T*" for line in lines
        ) + "\\nET"
        objects.append(
            (
                f"<< /Type /Page /Parent 2 0 R /Contents {number + 1} 0 R"
                " /Resources << /Font << /F1 3 0 R >> >> >>"
            ).encode()
        )
        objects.append(
            f"<< /Length {len(content)} >>\\nstream\\n{content}\\nendstream".encode()
        )
    body = bytearray()
    offsets = []
    for i, payload in enumerate(objects, start=1):
        offsets.append(len(header) + len(body))
        body += f"{i} 0 obj\\n".encode() + payload + b"\\nendobj\\n"
    xref_offset = len(header) + len(body)
    xref = f"xref\\n0 {len(objects) + 1}\\n".encode() + b"0000000000 65535 f \\n"
    for offset in offsets:
        xref += f"{offset:010d} 00000 n \\n".encode()
    trailer = (
        f"trailer << /Size {len(objects) + 1} /Root 1 0 R >>\\nstartxref\\n"
        f"{xref_offset}\\n%%EOF\\n"
    ).encode()
    return header + bytes(body) + xref + trailer


def legacy_doc():
    return {
        "schema": {"name": "benchmark.hvs_contribution_annotation", "version": 4},
        "arxiv_id": "2501.00001",
        "annotator": "synthetic-annotator",
        "annotated_at": "2026-08-01T00:00:00+00:00",
        "guideline_version": "synthetic-guideline-0",
        "evidence_basis": "pdf",
        "annotation_process": {
            "protocol": "contribution_migration_ai_assisted_v1",
            "preannotation_agent": "synthetic-pre",
            "preannotation_model": "model-a",
            "reconciliation_agent": "synthetic-recon",
            "reconciliation_model": "model-b",
            "expert_review_scope": "paper_level",
            "process_note": "synthetic",
        },
        "canary": "",
        "status": "contributions_found",
        "contributions": [
            {
                "identifiers": [
                    {"value": "SYN-A", "evidence": [{"location": "Table 2, row SYN-A"}]}
                ],
                "contribution_type": "candidates_found",
                "contribution_summary": "Reports a synthetic candidate.",
                "contribution_evidence": [{"location": "Section 4.1"}],
                "paper_boundness": {
                    "status": "unbound",
                    "evidence": [{"location": "Section 5"}],
                },
                "quantities": [
                    {
                        "quantity": "observed_phase_space.radial_velocity",
                        "values": [
                            {
                                "value": "612.0",
                                "error": "8.0",
                                "unit": "km/s",
                                "limit_kind": "none",
                                "condition": "heliocentric",
                                "source": "this_paper",
                                "evidence": [{"location": "Table 2, row SYN-A"}],
                            }
                        ],
                    }
                ],
                "quantity_conflicts": [],
            }
        ],
        "reviewed_exclusions": [],
    }


def fresh_draft(pdf_sha256):
    from hvs_catalog.records.rules import load_rules

    rules = load_rules()
    evidence = {
        "file_id": "pdf-1",
        "location": "Table 2, row SYN-A",
        "page": 2,
        "quote": "SYN-A 612.0 +- 8.0 km/s",
    }
    return add_synthetic_metadata({
        "schema": {"name": "evaluation.hvs_contribution_gold", "version": 1},
        "kind": "draft",
        "document_id": "gold-synth-001",
        "paper": {"paper_key": "synth-001", "arxiv_id": "2501.00001"},
        "rules": {"version": rules.version, "sha256": rules.sha256},
        "files": [{"file_id": "pdf-1", "name": "paper.pdf", "sha256": pdf_sha256}],
        "status": "contributions_found",
        "contributions": [
            {
                "contribution_id": "c1",
                "legacy_index": None,
                "identifiers": [{"value": "SYN-A", "evidence": [dict(evidence)]}],
                "contribution_type": "candidates_found",
                "contribution_summary": "Reports a synthetic candidate.",
                "contribution_evidence": [
                    {"file_id": "pdf-1", "location": "Section 4.1", "page": 1}
                ],
                "paper_boundness": {
                    "status": "unbound",
                    "legacy_status": None,
                    "evidence": [
                        {"file_id": "pdf-1", "location": "Section 5", "page": 3}
                    ],
                },
                "quantities": [
                    {
                        "quantity": "observed_phase_space.radial_velocity",
                        "values": [
                            {
                                "value": "612.0",
                                "error": "8.0",
                                "lower_error": None,
                                "upper_error": None,
                                "unit": "km/s",
                                "limit_kind": "none",
                                "range_lower": None,
                                "range_upper": None,
                                "coordinate_format": None,
                                "condition": "heliocentric, adopted solution",
                                "interval_kind": None,
                                "coverage": None,
                                "lower_quantile": None,
                                "upper_quantile": None,
                                "error_components": [],
                                "representation": None,
                                "source": "this_paper",
                                "source_note": None,
                                "evidence": [dict(evidence)],
                                "context_evidence": [],
                            }
                        ],
                    }
                ],
                "quantity_conflicts": [],
            }
        ],
        "reviewed_exclusions": [],
        "provenance": {
            "origin": "new_annotation",
            "legacy": None,
            "note": "synthetic fresh draft",
        },
        "pending": [],
    })


def approval_payload(binding):
    return {
        "approved_by": "synthetic-expert", "approver_kind": "human",
        "approved_at": "2026-09-30T00:00:00+00:00",
        "binding": binding.model_dump(mode="json"), "binding_sha256": binding.sha256,
        "basis": "Synthetic distribution human approval", "scientific_review": True,
        "accepted_pending": binding.science_pending,
    }


if step == "migrate":
    from hvs_catalog.evaluation import (
        GoldFormatError,
        migrate_legacy_annotation,
    )

    document = legacy_doc()
    payload = dict(document)
    payload.pop("canary")
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    document["canary"] = (
        "stella-contribution-gold-canary-v0.1-"
        + hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:24]
    )
    legacy = root / "legacy.json"
    legacy.write_text(json.dumps(document, indent=2), encoding="utf-8")
    ensure_pdf()
    result = migrate_legacy_annotation(
        legacy_path=legacy,
        draft_dir=root / "draft",
        known_pdfs=[material / "paper.pdf"],
        material_root=material,
    )
    assert result.draft.contributions[0].quantities[0].values[0].value == "612.0"
    print("GOLD-MIGRATED")
elif step == "check":
    from hvs_catalog.evaluation import check_gold_annotation, parse_gold_annotation

    draft = json.loads((root / "draft" / "draft.json").read_text(encoding="utf-8"))
    report = check_gold_annotation(parse_gold_annotation(draft), material_root=material)
    assert report.blocking_count == 3
    draft = add_synthetic_metadata(draft)
    (root / "draft" / "draft.json").write_text(json.dumps(draft), encoding="utf-8")
    report = check_gold_annotation(parse_gold_annotation(draft), material_root=material)
    assert report.blocking_count == 0 and report.pending_ids()
    print("GOLD-CHECKED")
elif step == "fresh":
    from hvs_catalog.evaluation import (
        GoldTarget, SaveApproval, approved_save, check_save,
        compact_gold_annotation, parse_gold_annotation, write_compact_gold_pair,
    )
    digest = ensure_pdf()
    draft = parse_gold_annotation(fresh_draft(digest))
    pair = compact_gold_annotation(
        draft, title="Synthetic installed Gold", original_annotator="synthetic-expert",
        reviewer="synthetic-reviewer",
        material_review={"value": "checked", "basis": "Synthetic material review", "record_sha256": "a" * 64},
        scope_review={"value": "complete", "basis": "Synthetic scope review", "record_sha256": "a" * 64},
    )
    pair_path = root / "pair"
    write_compact_gold_pair(pair, pair_path)
    target = GoldTarget(root / "target")
    checked = check_save(pair_path=pair_path, material_root=material, target=target)
    assert not target.root.exists()
    approval = SaveApproval.model_validate(approval_payload(checked.binding))
    (root / "approval.json").write_text(json.dumps(approval.model_dump(mode="json")))
    result = approved_save(pair_path=pair_path, material_root=material, target=target, approval=approval)
    assert not result.already_present
    assert approved_save(pair_path=pair_path, material_root=material, target=target, approval=approval).already_present
    assert target.read_current()[0].answer.contributions[0].contribution_id == "2501.00001:cand01"
    print("GOLD-SAVED")
elif step == "cli":
    script = Path(sys.argv[3])
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)

    def run_cli(*args):
        return subprocess.run(
            [str(script), "gold", *[str(a) for a in args]],
            cwd=root,
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )

    legacy = root / "legacy.json"
    result = run_cli(
        "migrate", "--legacy", legacy,
        "--draft-dir", root / "cli-draft",
        "--known-pdf", material / "paper.pdf",
        "--material-root", material,
    )
    assert result.returncode == 0, result.stderr
    assert (root / "cli-draft" / "draft.json").is_file()
    check = run_cli(
        "check", "--draft", root / "cli-draft" / "draft.json",
        "--material-root", material,
    )
    assert check.returncode == 2, check.stderr
    cli_draft = root / "cli-draft" / "draft.json"
    completed = add_synthetic_metadata(json.loads(cli_draft.read_text(encoding="utf-8")))
    cli_draft.write_text(json.dumps(completed), encoding="utf-8")
    check = run_cli("check", "--draft", cli_draft, "--material-root", material)
    assert check.returncode == 0, check.stderr
    compare = run_cli(
        "compare", "--legacy", legacy,
        "--current", root / "cli-draft" / "draft.json",
    )
    assert compare.returncode == 0, compare.stderr
    assert "一致" in compare.stdout
    inspected = run_cli("inspect-save", "--pair", root / "pair" / "answer.json",
                        "--material-root", material, "--target", root / "cli-target")
    assert inspected.returncode == 0, inspected.stderr
    from hvs_catalog.evaluation import SaveBinding
    binding = SaveBinding.model_validate(json.loads(inspected.stdout)["binding"])
    cli_approval = root / "cli-approval.json"
    cli_approval.write_text(json.dumps(approval_payload(binding)))
    save = run_cli("save", "--pair", root / "pair" / "answer.json", "--material-root", material,
                   "--target", root / "cli-target", "--approval", cli_approval)
    assert save.returncode == 0, save.stderr
    read = run_cli("read", "--target", root / "cli-target")
    assert read.returncode == 0, read.stderr
    assert json.loads(read.stdout)["pair_sha256"] == binding.pair_sha256
    print("GOLD-CLI")
elif step == "revision":
    from hvs_catalog.evaluation import (
        GoldTarget, SaveApproval, CompactGoldAnswer, CompactGoldMetadata, CompactGoldPair,
        GoldTargetError, approved_save, check_save, compact_answer_to_json,
        read_compact_gold_pair, write_compact_gold_pair,
    )
    target = GoldTarget(root / "target")
    current, original_approval = target.read_current()
    before = target.fingerprint()
    data = current.answer.model_dump(mode="json")
    data["contributions"][0]["quantities"][0]["values"][0]["value"] = "613.0"
    answer = CompactGoldAnswer.model_validate(data)
    metadata = current.metadata.model_dump(mode="json", by_alias=True)
    metadata["review"]["answer_sha256"] = hashlib.sha256(compact_answer_to_json(answer).encode()).hexdigest()
    metadata["revision"] = dict(revised_by="synthetic-preparer", decided_by="synthetic-expert", revised_at="2026-09-30", basis="Synthetic correction", previous_answer_sha256=original_approval.binding.answer_sha256)
    revised = CompactGoldPair(answer=answer, metadata=CompactGoldMetadata.model_validate(metadata))
    write_compact_gold_pair(revised, root / "revised")
    binding = check_save(pair_path=root / "revised", material_root=material, target=target, expected_old=before).binding
    approval = SaveApproval.model_validate(approval_payload(binding))
    approved_save(pair_path=root / "revised", material_root=material, target=target, approval=approval)
    assert target.read_current()[0].metadata.annotation == current.metadata.annotation
    try:
        approved_save(pair_path=root / "pair", material_root=material, target=target, approval=original_approval)
    except GoldTargetError:
        pass
    else:
        raise AssertionError("old approval overwrote a newer revision")
    assert {p.name for p in target.root.iterdir()} == {"answer.json", "metadata.json", "approval.json", "gold-target.json", ".lock"}
    print("GOLD-REVISED")
elif step in {"crash", "receipt-lost"}:
    from hvs_catalog.evaluation import GoldTarget, SaveApproval, approved_save, check_save
    target = GoldTarget(root / ("crash-target" if step == "crash" else "lost-target"))
    binding = check_save(pair_path=root / "pair", material_root=material, target=target).binding
    approval = SaveApproval.model_validate(approval_payload(binding))
    (root / (step + "-approval.json")).write_text(json.dumps(approval.model_dump(mode="json")))
    approved_save(pair_path=root / "pair", material_root=material, target=target, approval=approval,
                  fault_hooks={"after_answer.json" if step == "crash" else "after_complete": lambda: os._exit(9)})
    raise AssertionError("fault window not reached")
elif step == "recover":
    import shutil
    from hvs_catalog.evaluation import GoldIntegrityError, GoldTarget, SaveApproval, approved_save
    from hvs_catalog.workspace import SpaceLock
    target = GoldTarget(root / "crash-target")
    approval = SaveApproval.model_validate_json((root / "crash-approval.json").read_bytes())
    try:
        target.read_current()
    except GoldIntegrityError:
        pass
    else:
        raise AssertionError("interrupted pair read as approved")
    backup = target.root / ".save-backup"
    with SpaceLock(target.root):
        for name in ("answer.json", "metadata.json", "approval.json"):
            shutil.copyfile(backup / "new" / name, target.root / name)
        shutil.rmtree(backup)  # explicit synthetic manual recovery, no product replay
    assert target.read_current()[1] == approval
    assert approved_save(pair_path=root / "pair", material_root=material, target=target, approval=approval).already_present
    print("GOLD-RECOVERED")
elif step == "retry-lost":
    from hvs_catalog.evaluation import GoldTarget, SaveApproval, approved_save
    target = GoldTarget(root / "lost-target")
    approval = SaveApproval.model_validate_json((root / "receipt-lost-approval.json").read_bytes())
    assert approved_save(pair_path=root / "pair", material_root=material, target=target, approval=approval).already_present
    print("GOLD-RETRY-LOST")
elif step == "verify-import":
    import hvs_catalog.evaluation as evaluation

    assert evaluation.GOLD_SCHEMA_VERSION == 1
    print("GOLD-IMPORT")
else:
    raise SystemExit(f"未知步骤：{step}")
'''


ACQUISITION_DRIVER = (
    PDF_FIXTURE
    + "\nSYNTHETIC_PDF_HELPERS = "
    + repr(PDF_FIXTURE)
    + "\n"
    + '''
"""External installed acquisition CLI checks using offline synthetic materials.

Initialize via documented public Python commands, build synthetic tar.gz source packages, then use installed console scripts in independent subprocesses for import/status (human and JSON)/material/error codes. netflow verifies discovery/fetch/revision success and fresh-process recovery using installed Python, cleared PYTHONPATH, and test-side injected fake protocol services. The product exposes no rate-limit/offline bypass. Two subprocesses cover page-boundary interruption, complete recovery, fetch, pending revisions, and material output without real arXiv access.
"""

import io
import json
import os
import subprocess
import sys
import tarfile
from pathlib import Path

root = Path(sys.argv[1])
step = sys.argv[2]
script = Path(sys.argv[3])
space = root / "acquisition-space"
# Subprocess step cwd; initialize the workspace itself through the documented public command.
root.mkdir(parents=True, exist_ok=True)

# Same public initialization command as README, the sole initialization entry point.
DOCUMENTED_INIT = (
    "from hvs_catalog.workspace import initialize_workspace; "
    "initialize_workspace(r'%s')" % space
)

NETCHILD_SOURCE = SYNTHETIC_PDF_HELPERS + """import sys
from pathlib import Path

# Check acquisition network paths in subprocesses after installation (offline synthetic).
# Use the installed interpreter to import hvs_catalog and replace the CLI service factory
# with a synthetic protocol service (test injection; no offline/rate-limit bypass in the
# product CLI). Call CLI main in separate interruption and resume/fetch/revision/material processes.

import io
import tarfile

from hvs_catalog.cli import main
import hvs_catalog.cli_acquisition as cli_acq
from hvs_catalog.pipeline.acquisition.lifecycle import AcquisitionService
from hvs_catalog.pipeline.acquisition.protocol import (
    ArxivIdBatchResult,
    ArxivIdLookup,
    ArxivPaperMeta,
    ArxivSearchPage,
    ArxivSearchRequest,
    ArxivSourceDownload,
    parse_arxiv_identifier,
)
from hvs_catalog.workspace import open_workspace


class FakeClock:
    def __init__(self):
        self._now = 1700000000.0

    def now(self):
        return self._now

    def sleep(self, seconds):
        self._now += max(0.0, seconds)


def make_meta(cid, version):
    return ArxivPaperMeta(
        canonical_id=cid,
        version=version,
        api_id_url="http://arxiv.org/abs/%sv%d" % (cid, version),
        title="Paper " + cid,
        authors=("Author One",),
        categories=("astro-ph.CO",),
        primary_category="astro-ph.CO",
        published="2024-01-01T00:00:00Z",
        updated="2024-01-02T00:00:00Z",
        abstract="installed smoke abstract",
        doi=None,
        journal_ref=None,
    )


def make_page(request, entries, total, fetched_at):
    return ArxivSearchPage(
        request=request,
        response_url="https://export.arxiv.org/api/query synthetic",
        fetched_at=fetched_at,
        total_results=total,
        returned_start_index=request.start,
        entries=entries,
        anomalies=(),
    )


PACKAGE_BYTES = None


def package_bytes():
    global PACKAGE_BYTES
    if PACKAGE_BYTES is None:
        PACKAGE_BYTES = make_pdf("Synthetic netflow smoke")
    return PACKAGE_BYTES


class FakeApi:
    def __init__(self):
        self.metadata = {"2401.05555": make_meta("2401.05555", 2)}

    def search(self, request):
        if request.start == 0:
            return make_page(
                request, (make_meta("2401.06666", 1),), 2, 1700000100.0
            )
        if request.start == 1:
            return make_page(
                request, (make_meta("2401.06667", 1),), 2, 1700000200.0
            )
        raise AssertionError("unscripted page %d" % request.start)

    def metadata_by_ids(self, identifiers):
        lookups = []
        for item in identifiers:
            requested = parse_arxiv_identifier(
                item if isinstance(item, str) else item.canonical_id
            )
            meta = self.metadata.get(requested.canonical_id)
            lookups.append(
                ArxivIdLookup(
                    requested=requested,
                    status="found" if meta is not None else "not_found",
                    meta=meta,
                )
            )
        return ArxivIdBatchResult(
            response_url="synthetic",
            fetched_at=1700000000.0,
            lookups=tuple(lookups),
            anomalies=(),
        )

    def download_pdf(self, identifier):
        versioned = (
            identifier if isinstance(identifier, str) else identifier.versioned_id
        )
        payload = package_bytes()
        return ArxivSourceDownload(
            identifier=parse_arxiv_identifier(versioned),
            final_url="https://arxiv.org/e-print/" + versioned,
            content_type="application/gzip",
            payload=payload,
            byte_count=len(payload),
            fetched_at=1700000000.0,
        )


phase = sys.argv[1]
space_path = sys.argv[2]
workspace = open_workspace(Path(space_path))
service = AcquisitionService(workspace, FakeApi(), clock=FakeClock())


def factory(space_arg):
    return service


cli_acq._build_acquisition_service = factory

if phase == "partial":
    code = main([
        "acquisition", "discover", "--space", space_path,
        "--from", "2024-01-01", "--to", "2024-01-31",
        "--terms", "dark matter", "--category", "astro-ph.CO",
        "--max-pages", "1",
    ])
    print("NETFLOW-PARTIAL-CODE=%d" % code)
    sys.exit(0 if code == 3 else 1)

if phase == "resume":
    checks = []
    code_discover = main([
        "acquisition", "discover", "--space", space_path,
        "--from", "2024-01-01", "--to", "2024-01-31",
        "--terms", "dark matter", "--category", "astro-ph.CO",
    ])
    checks.append(("discover-resume", code_discover, 0))
    code_fetch = main([
        "acquisition", "fetch", "--space", space_path, "--id", "2401.05555v1",
    ])
    checks.append(("fetch", code_fetch, 0))
    code_rev = main([
        "acquisition", "revisions", "--space", space_path,
        "--id", "2401.05555",
    ])
    checks.append(("revisions", code_rev, 0))
    code_mat = main([
        "acquisition", "material", "--space", space_path, "--id", "2401.05555",
    ])
    checks.append(("material", code_mat, 0))
    bad = []
    for name, code, want_code in checks:
        if code != want_code:
            bad.append("%s 退出码 %d（期望 %d）" % (name, code, want_code))
    if bad:
        for item in bad:
            print("[问题] " + item)
        sys.exit(1)
    print("NETFLOW-RESUME-OK")
    sys.exit(0)

sys.exit(2)
"""

def run_cli(*args: object) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    return subprocess.run(
        [str(script), "acquisition", *map(str, args)],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


def run_child(*command: object) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    return subprocess.run(
        list(map(str, command)),
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


def make_package(path: Path) -> None:
    path.write_bytes(make_pdf("Synthetic installed PDF"))


if step == "setup":
    # Use README's exact public initialization command in a subprocess with installed Python.
    result = run_child(sys.executable, "-c", DOCUMENTED_INIT)
    if result.returncode != 0:
        print("[问题] 文档化初始化失败：%s%s" % (result.stdout, result.stderr))
        raise SystemExit(1)
    make_package(root / "smoke.tar.gz")
    print("ACQUISITION-READY")
    raise SystemExit(0)

if step == "flow":
    problems = []
    imported = run_cli(
        "import", "--space", space, "--file", root / "smoke.tar.gz",
        "--id", "2401.01234v1", "--provenance", "distribution smoke",
    )
    if imported.returncode != 0 or "state=imported" not in imported.stdout:
        problems.append("import 失败：%d %s %s" % (
            imported.returncode, imported.stdout, imported.stderr))
    again = run_cli(
        "import", "--space", space, "--file", root / "smoke.tar.gz",
        "--id", "2401.01234v1", "--provenance", "second declaration",
    )
    if again.returncode != 0 or "重放" not in again.stdout:
        problems.append("重复导入未按重放处理：%d %s %s" % (
            again.returncode, again.stdout, again.stderr))
    missing_provenance = run_cli(
        "import", "--space", space, "--file", root / "smoke.tar.gz",
        "--id", "2401.01234v1",
    )
    if missing_provenance.returncode != 2:
        problems.append("缺来源声明应退出 2，实际 %d" % missing_provenance.returncode)
    status_text = run_cli("status", "--space", space)
    if status_text.returncode != 0 or "state=imported" not in status_text.stdout:
        problems.append("status 失败：%d %s %s" % (
            status_text.returncode, status_text.stdout, status_text.stderr))
    status_json = run_cli("status", "--space", space, "--json")
    try:
        payload = json.loads(status_json.stdout)
        logged = [
            item["provenance"]
            for item in payload["selections"][0]["provenance_log"]
        ]
    except (ValueError, KeyError, IndexError):
        logged = []
        problems.append("status --json 输出不可解析：%r" % status_json.stdout)
    if logged != ["distribution smoke", "second declaration"]:
        problems.append("溯源日志异常：%s" % logged)
    material = run_cli("material", "--space", space, "--id", "2401.01234")
    if material.returncode != 0 or "arxiv-source-2401.01234" not in material.stdout:
        problems.append("material 失败：%d %s %s" % (
            material.returncode, material.stdout, material.stderr))
    # Acquisition v2 separates selection from declaration reuse; old-version lookup leaves current unchanged.
    newer = run_cli(
        "import", "--space", space, "--file", root / "smoke.tar.gz",
        "--id", "2401.01234v2", "--provenance", "distribution v2",
    )
    old = run_cli("status", "--space", space, "--id", "2401.01234v1", "--json")
    try:
        history = json.loads(old.stdout)
        current = history["selections"][0]
        previous = history["versions"][0]
        if (newer.returncode != 0 or old.returncode != 0
            or current["version"] != 2 or previous["version"] != 1
            or previous["raw_path"] != current["raw_path"]
            or previous["provenance_log"][0]["provenance"] != "distribution smoke"):
            problems.append("版本回查/内容复用异常")
    except (ValueError, KeyError, IndexError):
        problems.append("版本回查输出不可解析：%r" % old.stdout)
    not_ready = run_cli("material", "--space", space, "--id", "2401.09999")
    if not_ready.returncode != 6:
        problems.append("未就绪材料退出码应为 6，实际 %d" % not_ready.returncode)
    uninitialized = run_cli("status", "--space", root / "not-a-space")
    if uninitialized.returncode != 1:
        problems.append("未初始化空间退出码应为 1，实际 %d" % uninitialized.returncode)
    bad_discover = run_cli(
        "discover", "--space", space, "--from", "2024-01-01", "--to", "2024-01-02"
    )
    if bad_discover.returncode != 1 or "至少提供其一" not in bad_discover.stderr:
        problems.append("缺检索条件应拒绝（退出码 1）：%d %s" % (
            bad_discover.returncode, bad_discover.stderr))
    if problems:
        for problem in problems:
            print("[问题] %s" % problem)
        raise SystemExit(1)
    print("ACQUISITION-CLI-OK")
    raise SystemExit(0)

if step == "netflow":
    # Installed offline synthetic discover/fetch/revisions success and cross-process recovery:
    # two independent installed-Python subprocesses with cleared PYTHONPATH and test-side fake-service injection.
    netchild = root / "acquisition_network.py"
    netchild.write_text(NETCHILD_SOURCE, encoding="utf-8")
    partial = run_child(sys.executable, netchild, "partial", space)
    if partial.returncode != 0 or "NETFLOW-PARTIAL-CODE=3" not in partial.stdout:
        print("[问题] 页界中断步骤异常：%d %s %s" % (
            partial.returncode, partial.stdout, partial.stderr))
        raise SystemExit(1)
    resume = run_child(sys.executable, netchild, "resume", space)
    if resume.returncode != 0 or "NETFLOW-RESUME-OK" not in resume.stdout:
        print("[问题] 恢复步骤异常：%d %s %s" % (
            resume.returncode, resume.stdout, resume.stderr))
        raise SystemExit(1)
    # Independently verify restored stored facts with pure CLI status, without injection.
    verify = run_cli("status", "--space", space)
    if verify.returncode != 0 or "state=complete" not in verify.stdout:
        print("[问题] 恢复后状态查询异常：%d %s %s" % (
            verify.returncode, verify.stdout, verify.stderr))
        raise SystemExit(1)
    print("ACQUISITION-NETFLOW-OK")
    raise SystemExit(0)

raise SystemExit(2)
'''
)


def verify_gold(python: Path, work_dir: Path, script: Path) -> list[str]:
    """Exercise installed Gold APIs/CLI outside the repository.

    Independent subprocesses cover migration/checks/paired additions-revisions/full CLI,
    interruption rejection/manual recovery, and lost-return repeated checks with
    PYTHONPATH cleared.
    """
    problems: list[str] = []
    driver = work_dir / "gold_check.py"
    driver.write_text(GOLD_DRIVER, encoding="utf-8")
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)

    def run_step(name: str, *args: object) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [str(python), str(driver), str(work_dir), name, *map(str, args)],
            cwd=work_dir,
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )

    expectations = [
        ("migrate", "GOLD-MIGRATED", 0),
        ("check", "GOLD-CHECKED", 0),
        ("fresh", "GOLD-SAVED", 0),
        ("cli", "GOLD-CLI", 0),
        ("verify-import", "GOLD-IMPORT", 0),
        ("crash", None, 9),
        ("recover", "GOLD-RECOVERED", 0),
        ("revision", "GOLD-REVISED", 0),
        ("receipt-lost", None, 9),
        ("retry-lost", "GOLD-RETRY-LOST", 0),
    ]
    for step, marker, code in expectations:
        args: tuple[object, ...] = ()
        if step == "cli":
            args = (script,)
        result = run_step(step, *args)
        print(
            f"[Gold installed subprocess] {step}: exit={result.returncode}\n{result.stdout}{result.stderr}"
        )
        if result.returncode != code:
            problems.append(
                f"Gold 步骤 {step} 退出码 {result.returncode}（期望 {code}）：\n"
                f"{result.stdout}{result.stderr}"
            )
            continue
        if marker is not None and marker not in result.stdout:
            problems.append(f"Gold 步骤 {step} 输出缺少 {marker!r}：{result.stdout!r}")
    return problems


def verify_acquisition(python: Path, work_dir: Path, script: Path) -> list[str]:
    """Exercise installed acquisition CLI offline outside the repository.

    Initialize workspaces/build synthetic packages in subprocesses, then independently
    test imports/replay, human/JSON status, materials, and error codes using installed
    console scripts, cleared PYTHONPATH, and no networks.
    """
    problems: list[str] = []
    driver = work_dir / "acquisition_check.py"
    driver.write_text(ACQUISITION_DRIVER, encoding="utf-8")
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)

    def run_step(name: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                str(python),
                str(driver),
                str(work_dir / "acquisition"),
                name,
                str(script),
            ],
            cwd=work_dir,
            env=env,
            capture_output=True,
            text=True,
            timeout=300,
            check=False,
        )

    for step, marker, code in (
        ("setup", "ACQUISITION-READY", 0),
        ("flow", "ACQUISITION-CLI-OK", 0),
        ("netflow", "ACQUISITION-NETFLOW-OK", 0),
    ):
        result = run_step(step)
        if result.returncode != code:
            problems.append(
                f"获取步骤 {step} 退出码 {result.returncode}（期望 {code}）：\n"
                f"{result.stdout}{result.stderr}"
            )
            continue
        if marker not in result.stdout:
            problems.append(f"获取步骤 {step} 输出缺少 {marker!r}：{result.stdout!r}")
    return problems


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="check_distribution",
        description="构建发行包，并在仓库外的全新环境安装 wheel、用独立子进程验证帮助与版本。",
    )
    parser.add_argument(
        "python",
        nargs="?",
        default=None,
        help="创建虚拟环境使用的 Python 版本，如 3.12；默认使用 uv 的默认解释器",
    )
    args = parser.parse_args(argv)

    version = project_version()
    with tempfile.TemporaryDirectory(prefix="hvs-catalog-check-") as tmp_name:
        tmp = Path(tmp_name)
        dist_dir = tmp / "dist"
        built = run(["uv", "build", "--out-dir", dist_dir], REPO_ROOT)
        if built.returncode != 0:
            return fail(built, "uv build")
        wheels = sorted(dist_dir.glob("*.whl"))
        sdists = sorted(dist_dir.glob("*.tar.gz"))
        if len(wheels) != 1 or len(sdists) != 1:
            print(
                f"[失败] 期望各生成一个 wheel 和源码包，实际 wheel={wheels}，sdist={sdists}"
            )
            return 1
        with zipfile.ZipFile(wheels[0]) as archive:
            wheel_names = archive.namelist()
        with tarfile.open(sdists[0]) as archive:
            sdist_names = archive.getnames()
        violations = [
            *check_wheel(wheel_names),
            *check_sdist(sdist_names, version),
        ]
        if violations:
            print("[失败] 发行内容检查未通过：")
            for violation in violations:
                print(f"  - {violation}")
            return 1
        print(f"[通过] 发行内容检查：{wheels[0].name} 与 {sdists[0].name}")
        print("[通过] 七个退役模块均不在 wheel/sdist 中")

        venv_dir = tmp / "venv"
        venv_command = ["uv", "venv", venv_dir]
        if args.python:
            venv_command += ["--python", args.python]
        created = run(venv_command, tmp)
        if created.returncode != 0:
            return fail(created, "uv venv")
        bin_python = venv_dir / ("Scripts" if os.name == "nt" else "bin") / "python"
        installed = run(
            ["uv", "pip", "install", "--python", bin_python, wheels[0]], tmp
        )
        if installed.returncode != 0:
            return fail(installed, "uv pip install")
        work_dir = tmp / "work"
        work_dir.mkdir()
        problems = verify_installation(venv_dir, work_dir, version)
        if problems:
            print("[失败] 仓库外子进程验证未通过：")
            for problem in problems:
                print(f"  - {problem}")
            return 1
        script = venv_dir / ("Scripts" if os.name == "nt" else "bin") / "hvs-catalog"
        problems = verify_demo(script, work_dir)
        if problems:
            print("[失败] 仓库外演示走查未通过：")
            for problem in problems:
                print(f"  - {problem}")
            return 1
        problems = verify_workspace(bin_python, work_dir)
        if problems:
            print("[失败] 仓库外工作空间公共接口走查未通过：")
            for problem in problems:
                print(f"  - {problem}")
            return 1
        problems = verify_gold(bin_python, work_dir, script)
        if problems:
            print("[失败] 仓库外 Gold 工具走查未通过：")
            for problem in problems:
                print(f"  - {problem}")
            return 1
        problems = verify_acquisition(bin_python, work_dir, script)
        if problems:
            print("[失败] 仓库外获取子系统 CLI 走查未通过：")
            for problem in problems:
                print(f"  - {problem}")
            return 1
        problems = verify_pipeline_and_scoring(venv_dir, work_dir, script)
        if problems:
            print("[失败] 仓库外合成提取与评分走查未通过：")
            for problem in problems:
                print(f"  - {problem}")
            return 1
        problems = verify_installed_llm(venv_dir, work_dir, script)
        if problems:
            print("[失败] 仓库外模型接口与 CLI 走查未通过：")
            for problem in problems:
                print(f"  - {problem}")
            return 1
        problems = verify_installed_extraction(venv_dir, work_dir, script)
        if problems:
            print("[失败] 仓库外提取配置、提示与恢复走查未通过：")
            for problem in problems:
                print(f"  - {problem}")
            return 1
    print(
        f"[通过] 仓库外全新环境安装 wheel，独立子进程查询帮助与版本均正常（{version}）；"
        "持久化恢复演示走查通过（重启恢复、结果复用、决定规则、重复调用）；"
        "工作空间公共接口走查通过（初始化、执行、裁决、备份、恢复、重放保护）；"
        "Gold 工具走查通过（旧 v4 迁移与检查、显式转换、当前配对统一新增／修订、"
        "内容绑定人类批准、CLI、旧批准拒绝覆盖新修订、独立进程中断拒读／人工恢复、"
        "完成后丢失返回的重复核对）；合成提取与评分走查通过"
        "（包来源与方法查询、3×2 提取与评分用例、重复评分一致与篡改"
        "拒绝、跨进程暂停决定恢复、两组对照实验隔离与专家用时边界）；"
        "材料准备走查通过（合成PDF导入、本地解析器替身、准备、无模型消费核对）；"
        "模型接口与 CLI 走查通过（四模型 HTTP/SSE、工具输出、持久保存、恢复复用及许可随包）；"
        "提取配置与提示走查通过（英文资源、完整预览、逐步模型与跨进程模拟恢复）；"
        "获取子系统 CLI 走查通过（文档化公开初始化、导入/重放、"
        "状态人读+JSON、材料输出、未就绪/未初始化/缺检索条件/缺来源"
        "声明的错误码边界；安装后 discover/fetch/revisions 的离线合成"
        "成功与跨进程恢复，全部离线合成、清 PYTHONPATH、无产品后门）"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

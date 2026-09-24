"""构建发行包，并在仓库外的全新环境中验证安装。

执行内容：

1. 调用 uv build 把 wheel 与源码包生成到仓库外的临时目录；
2. 检查两个包的内容只含构建安装所需文件，排除正式数据、开发材料与运行产物；
3. 用 uv 在仓库外创建全新虚拟环境并安装 wheel；
4. 以独立子进程验证 ``hvs-catalog --help``、``--version``、``python -m`` 与包导入，
   子进程在仓库外目录运行并清除 PYTHONPATH；
5. 用安装后的 ``hvs-catalog demo`` 在仓库外走查持久化恢复演示：重启找回待办
   与复用结果、同一决定只生效一次、冲突决定被拒绝、真实进程终止后的重复
   调用与计费；
6. 用安装后的 R05 公共接口在仓库外走查工作空间全链路：初始化空间、导入
   合成材料、执行暂停、新进程提交专家决定并完成、备份、恢复到新目录后
   在新进程核对已完成状态且重复提交保护仍然有效；
7. 用安装后的 Gold 公共接口与 CLI 在仓库外走查 R06 链路：旧 v4 合成文档
   迁移成草稿、全新草稿检查、准备与模拟批准保存、查询与重放保护，另用
   独立子进程在真实中断窗口后恢复保存；精简 Gold v2 另验两文件包的检查、
   科学审定批准、保存与中断恢复。

用法：uv run python tools/check_distribution.py [Python 版本]

可选参数为创建虚拟环境使用的 Python 版本（如 3.12），默认使用 uv 的默认解释器。
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

REPO_ROOT = Path(__file__).resolve().parents[1]

# 不允许出现在任何发行包中的内容：正式星表数据、开发材料与运行产物。
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

# 源码包根目录下只允许构建安装所需文件；.gitignore 由 hatchling 固定加入。
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
    """wheel 顶层只允许包目录 hvs_catalog/ 及其 dist-info 元数据。"""
    violations = find_forbidden_paths(names)
    for top in sorted(top_levels(names)):
        if top in FORBIDDEN_SEGMENTS:
            continue  # 这些路径已按完整成员路径报告
        is_package = top == "hvs_catalog"
        is_dist_info = top.startswith("hvs_catalog-") and top.endswith(".dist-info")
        if not is_package and not is_dist_info:
            violations.append(f"wheel 顶层出现意外内容：{top}")
    return violations


def check_sdist(names: Sequence[str], version: str) -> list[str]:
    """源码包顶层只允许 hvs_catalog-<version>/，其下只允许构建安装所需文件。"""
    violations = find_forbidden_paths(names)
    root = f"hvs_catalog-{version}"
    for name in names:
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
    """在仓库外目录用独立子进程验证已安装的命令与包。"""
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
    """在仓库外用独立子进程走查演示：重启恢复、结果复用、决定规则与重复计费。

    退出码 70 与 hvs_catalog.demo.runner.CRASH_EXIT_CODE 一致，代表
    “收到响应但尚未保存”的真实进程终止。
    """
    problems: list[str] = []
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)

    def run_demo(args: Sequence[object]) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [str(script), "demo", *[str(arg) for arg in args]],
            cwd=work_dir,
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )

    def expect(
        step: str,
        args: Sequence[object],
        *,
        code: int = 0,
        includes: Sequence[str] = (),
        stderr_includes: str | None = None,
    ) -> None:
        result = run_demo(args)
        if result.returncode != code:
            problems.append(
                f"演示步骤 {step} 退出码 {result.returncode}（期望 {code}）："
                f"demo {' '.join(str(a) for a in args)}\n{result.stdout}{result.stderr}"
            )
            return
        for text in includes:
            if text not in result.stdout:
                problems.append(
                    f"演示步骤 {step} 输出缺少 {text!r}，实际输出 {result.stdout!r}"
                )
        if stderr_includes is not None and stderr_includes not in result.stderr:
            problems.append(
                f"演示步骤 {step} 错误输出缺少 {stderr_includes!r}，"
                f"实际错误输出 {result.stderr!r}"
            )

    space = work_dir / "demo-space"
    crash_space = work_dir / "demo-crash-space"

    # 场景一：暂停 → 新进程找回待办与已保存结果 → 决定并复用结果
    expect(
        "run",
        ["run", "--space", space],
        includes=(
            "[暂停]",
            "是否识别为同一天体",
            "580±20 km/s（来源：当前论文）",
            "累计调用 2 次",
        ),
    )
    expect(
        "status",
        ["status", "--space", space],
        includes=("[固定输入]", "等待裁决", "已保存结果 2 项"),
    )
    expect(
        "decide",
        ["decide", "--space", space, "--choice", "merge"],
        includes=("[复用]", "[完成]", "对象分组：2 组", "累计调用 2 次"),
    )
    # 同一决定重复提交只生效一次；冲突决定被拒绝
    expect(
        "decide-again",
        ["decide", "--space", space, "--choice", "merge"],
        includes=("未重复生效", "累计调用 2 次"),
    )
    expect(
        "decide-conflict",
        ["decide", "--space", space, "--choice", "keep-separate"],
        code=2,
        stderr_includes="[拒绝]",
    )

    # 场景二：真实终止进程后恢复，展示重复调用与重复计费
    expect(
        "crash",
        ["run", "--space", crash_space, "--crash-after-response"],
        code=70,
        includes=("尚未保存",),
    )
    expect(
        "status-after-crash",
        ["status", "--space", crash_space],
        includes=("累计调用 1 次",),
    )
    expect(
        "resume",
        ["run", "--space", crash_space],
        includes=("重复计费", "累计调用 3 次"),
    )
    expect(
        "crash-decide",
        ["decide", "--space", crash_space, "--choice", "keep-separate"],
        includes=("[完成]", "累计调用 3 次"),
    )
    return problems


R05_DRIVER = '''
"""仓库外 R05 公共接口走查：初始化、执行、裁决、备份、恢复与重放保护。

只用安装后的包内公共入口，不依赖仓库源码路径。步骤：
run（初始化+执行暂停）、decide（提交决定+完成）、verify（备份+恢复+核对）。
"""

import json
import sys
from pathlib import Path

from hvs_catalog.records.common import RecordRef
from hvs_catalog.runtime import resume_execution
from hvs_catalog.synthetic import (
    open_service,
    start_execution,
    submit_expert_decision,
)
from hvs_catalog.synthetic.flow import SyntheticFlow
from hvs_catalog.workspace import backup_workspace, restore_workspace

step, root = sys.argv[1], Path(sys.argv[2])
space_path = root / "space"
ledger = root / "ledger.db"
if step == "run":
    from hvs_catalog.workspace import initialize_workspace

    space = initialize_workspace(space_path)
    execution, flow, _ = start_execution(space, paper="A", request_key="r05-dist")
    service = open_service(ledger)
    report = resume_execution(
        space, execution.execution_id, flow, service=service
    )
    service.close()
    assert report.outcome == "paused", report.outcome
    question = report.pending_question
    print("R05-PAUSED")
    print(
        json.dumps(
            question.question_ref.model_dump(mode="json"), ensure_ascii=False
        )
    )
    space.close()
elif step == "decide":
    from hvs_catalog.workspace import open_workspace

    question_ref = RecordRef.model_validate(json.loads(sys.argv[3]))
    space = open_workspace(space_path)
    submit_expert_decision(
        space,
        execution_id="exec-000001",
        question_ref=question_ref,
        request_key="r05-dist-dec",
    )
    service = open_service(ledger)
    report = resume_execution(
        space, "exec-000001", SyntheticFlow(paper="A"), service=service
    )
    service.close()
    assert report.outcome == "finished", report.outcome
    from hvs_catalog.synthetic import material as m

    revision = space.store().current_revision(m.paper_record_id("A", "exec-000001"))
    assert revision == 2, revision
    print("R05-FINISHED")
    space.close()
elif step == "verify":
    from hvs_catalog.workspace import open_workspace

    question_ref = RecordRef.model_validate(json.loads(sys.argv[3]))
    space = open_workspace(space_path)
    backup_workspace(space, root / "backup")
    restored = restore_workspace(root / "backup", root / "restored")
    assert restored.identity.space_id == space.identity.space_id
    report = resume_execution(
        restored, "exec-000001", SyntheticFlow(paper="A"), service=None
    )
    assert report.outcome == "finished", report.outcome
    assert report.mode == "already-finished", report.mode
    from hvs_catalog.synthetic import material as m

    paper_id = m.paper_record_id("A", "exec-000001")
    assert restored.store().current_revision(paper_id) == 2
    # 同一决定在恢复出的空间重复提交：回放原回执，不再生效
    replay = submit_expert_decision(
        restored,
        execution_id="exec-000001",
        question_ref=question_ref,
        request_key="r05-dist-dec",
    )
    assert not replay.first_save
    assert restored.store().current_revision(paper_id) == 2
    print("R05-VERIFIED")
    restored.close()
    space.close()
else:
    raise SystemExit(f"未知步骤：{step}")
'''


def verify_r05(python: Path, work_dir: Path) -> list[str]:
    """仓库外用安装后的 R05 公共接口走查全链路（A22 发行场景）。

    三个独立子进程分别完成执行暂停、决定与完成、备份恢复与重放保护，
    全部清除 PYTHONPATH，只依赖安装后的包。
    """
    problems: list[str] = []
    driver = work_dir / "r05_check.py"
    driver.write_text(R05_DRIVER, encoding="utf-8")
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
            f"R05 执行步骤失败（退出码 {run_result.returncode}）：\n"
            f"{run_result.stdout}{run_result.stderr}"
        )
        return problems
    lines = [line for line in run_result.stdout.splitlines() if line.strip()]
    if not lines or lines[0] != "R05-PAUSED":
        problems.append(f"R05 执行步骤输出异常：{run_result.stdout!r}")
        return problems
    question_ref = lines[1]

    decide_result = run_step("decide", question_ref)
    if decide_result.returncode != 0 or "R05-FINISHED" not in decide_result.stdout:
        problems.append(
            f"R05 决定步骤失败（退出码 {decide_result.returncode}）：\n"
            f"{decide_result.stdout}{decide_result.stderr}"
        )
        return problems

    verify_result = run_step("verify", question_ref)
    if verify_result.returncode != 0 or "R05-VERIFIED" not in verify_result.stdout:
        problems.append(
            f"R05 备份恢复核对步骤失败（退出码 {verify_result.returncode}）：\n"
            f"{verify_result.stdout}{verify_result.stderr}"
        )
    return problems


R06_DRIVER = '''
"""仓库外 R06 Gold 工具走查：迁移、检查、准备、模拟批准保存与查询恢复。

只用安装后的包内公共入口与 CLI，材料全部合成，不依赖仓库源码路径、
旧仓或真实 Gold。步骤：migrate（旧 v4 合成文档 → 草稿）、check、
fresh（全新草稿 → 准备 → 批准 → 保存 v1 → 查询重放）、crash（在
after_files 故障窗口硬退出，由下一个子进程恢复保存并核对单一版本）。
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
# 全部步骤共用的合成论文 PDF
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
        "latex": {"name": "paper.tex", "sha256": hashlib.sha256(b"Synthetic source material.").hexdigest(), "version": "synthetic-v1"},
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
        (material / "paper.tex").write_bytes(b"Synthetic source material.")
        PDF_SHA256 = hashlib.sha256(data).hexdigest()
    return PDF_SHA256


def synthetic_pdf(pages):
    """带完整 xref 与字体资源的合成 PDF（成熟解析器可按页树读取）。"""
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


def approval_payload(preparation):
    manifest = preparation.manifest
    from hvs_catalog.evaluation import manifest_sha256

    return {
        "approved_by": "synthetic-expert",
        "approver_kind": "human",
        "approved_at": "2026-09-22T00:00:00+00:00",
        "document_id": manifest.document_id,
        "target_path": manifest.target_path,
        "manifest_sha256": manifest_sha256(manifest),
        "expected_old": (
            None
            if manifest.expected_old is None
            else manifest.expected_old.model_dump(mode="json")
        ),
        "rules_version": manifest.rules_version,
        "rules_sha256": manifest.rules_sha256,
        "basis": "Synthetic distribution check approval.",
        "accepted_pending": list(manifest.science_pending),
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
    print("R06-MIGRATED")
elif step == "check":
    from hvs_catalog.evaluation import check_gold_annotation, parse_gold_annotation

    draft = json.loads((root / "draft" / "draft.json").read_text(encoding="utf-8"))
    report = check_gold_annotation(parse_gold_annotation(draft), material_root=material)
    assert report.blocking_count == 3
    draft = add_synthetic_metadata(draft)
    (root / "draft" / "draft.json").write_text(json.dumps(draft), encoding="utf-8")
    report = check_gold_annotation(parse_gold_annotation(draft), material_root=material)
    assert report.blocking_count == 0 and report.pending_ids()
    print("R06-CHECKED")
elif step == "fresh":
    from hvs_catalog.evaluation import (
        GoldTarget,
        approved_save,
        check_gold_annotation,
        parse_gold_annotation,
        prepare_save,
        query_operation,
    )

    digest = ensure_pdf()
    draft_path = root / "fresh.json"
    draft_path.write_text(
        json.dumps(fresh_draft(digest), indent=2), encoding="utf-8"
    )
    report = check_gold_annotation(
        parse_gold_annotation(draft_path), material_root=material
    )
    assert report.blocking_count == 0
    target = GoldTarget(root / "target")
    target.initialize()
    preparation = prepare_save(
        draft_path=draft_path,
        material_root=material,
        work_dir=root / "work",
        target=target,
    )
    approval_path = root / "approval.json"
    approval_path.write_text(
        json.dumps(approval_payload(preparation), indent=2), encoding="utf-8"
    )
    receipt = approved_save(
        draft_path=draft_path,
        material_root=material,
        target=target,
        preparation_dir=preparation.directory,
        approval=approval_path,
        operation_id="op-dist",
    )
    assert receipt.version == 1 and not receipt.replayed
    entry, annotation = target.read_current_answer("gold-synth-001")
    assert annotation.kind == "answer"
    status = query_operation(operation_id="op-dist", target=target)
    assert status.state == "saved"
    replay = approved_save(
        draft_path=draft_path,
        material_root=material,
        target=target,
        preparation_dir=preparation.directory,
        approval=approval_path,
        operation_id="op-dist",
    )
    assert replay.replayed and replay.version == 1
    print("R06-SAVED")
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
    prepare = run_cli(
        "prepare", "--draft", root / "cli-draft" / "draft.json",
        "--material-root", material, "--target", root / "cli-target",
        "--work-dir", root / "cli-work",
    )
    assert prepare.returncode == 0, prepare.stderr
    from hvs_catalog.evaluation import load_preparation

    prep_dir = next((root / "cli-work" / "preparations").iterdir())
    preparation = load_preparation(prep_dir)
    cli_approval = root / "cli-approval.json"
    cli_approval.write_text(
        json.dumps(approval_payload(preparation), indent=2), encoding="utf-8"
    )
    save = run_cli(
        "save", "--draft", root / "cli-draft" / "draft.json",
        "--material-root", material, "--target", root / "cli-target",
        "--preparation", prep_dir, "--approval", cli_approval,
        "--operation-id", "op-cli",
    )
    assert save.returncode == 0, save.stderr
    query = run_cli(
        "query", "--operation-id", "op-cli", "--target", root / "cli-target"
    )
    assert query.returncode == 0, query.stderr
    assert "saved" in query.stdout
    print("R06-CLI")
elif step == "crash":
    from hvs_catalog.evaluation import GoldTarget, approved_save, prepare_save

    ensure_pdf()
    draft_path = root / "fresh.json"
    target = GoldTarget(root / "crash-target")
    target.initialize()
    preparation = prepare_save(
        draft_path=draft_path,
        material_root=material,
        work_dir=root / "crash-work",
        target=target,
    )
    approval_path = root / "crash-approval.json"
    approval_path.write_text(
        json.dumps(approval_payload(preparation), indent=2), encoding="utf-8"
    )
    approved_save(
        draft_path=draft_path,
        material_root=material,
        target=target,
        preparation_dir=preparation.directory,
        approval=approval_path,
        operation_id="op-crash",
        fault_hooks={"after_files": lambda: os._exit(9)},
    )
    raise AssertionError("crash step should not reach here")
elif step == "recover":
    from hvs_catalog.evaluation import (
        GoldTarget,
        approved_save,
        load_preparation,
        query_operation,
    )

    target = GoldTarget(root / "crash-target")
    prep_dir = next((root / "crash-work" / "preparations").iterdir())
    preparation = load_preparation(prep_dir)
    status = query_operation(operation_id="op-crash", target=target)
    assert status.state == "recoverable", status.detail
    receipt = approved_save(
        draft_path=root / "fresh.json",
        material_root=material,
        target=target,
        preparation_dir=prep_dir,
        approval=root / "crash-approval.json",
        operation_id="op-crash",
    )
    assert receipt.recovered and receipt.version == 1
    versions = sorted(
        p.name
        for p in (target.root / "annotations" / "gold-synth-001").iterdir()
    )
    assert versions == ["v1"]
    entry, annotation = target.read_current_answer("gold-synth-001")
    assert annotation.contributions[0].quantities[0].values[0].value == "612.0"
    print("R06-RECOVERED")
elif step in {"compact", "compact-crash", "compact-recover"}:
    from hvs_catalog.evaluation import (
        GoldTarget, approved_save, compact_gold_annotation, load_preparation,
        prepare_save, query_operation, read_compact_gold_pair,
        read_gold_annotation, write_compact_gold_pair,
    )

    script = Path(sys.argv[3])
    pair_dir = root / "compact-pair"
    if step == "compact":
        old = read_gold_annotation(root / "fresh.json")
        pair = compact_gold_annotation(
            old,
            title="Synthetic distribution candidate paper",
            original_annotator="synthetic-expert",
            reviewer="synthetic-expert",
            material_review={
                "value": "checked", "basis": "Synthetic material check",
                "record_sha256": "a" * 64,
            },
            scope_review={
                "value": "complete", "basis": "Synthetic scope check",
                "record_sha256": "a" * 64,
            },
        )
        write_compact_gold_pair(pair, pair_dir)
        assert read_compact_gold_pair(pair_dir).answer == pair.answer
        check = subprocess.run(
            [str(script), "gold", "check", "--draft", str(pair_dir / "answer.json"),
             "--material-root", str(material)],
            cwd=root, env=os.environ.copy(), capture_output=True, text=True,
        )
        assert check.returncode == 0, check.stderr

    crashing = step != "compact"
    target = GoldTarget(root / ("compact-crash-target" if crashing else "compact-target"))
    work = root / ("compact-crash-work" if crashing else "compact-work")
    operation_id = "compact-crash" if crashing else "compact-save"
    approval_path = root / (operation_id + "-approval.json")
    if step == "compact-recover":
        preparation_dir = next((work / "preparations").iterdir())
        assert query_operation(operation_id=operation_id, target=target).state == "recoverable"
    else:
        preparation = prepare_save(
            draft_path=pair_dir / "answer.json", material_root=material,
            target=target, work_dir=work,
        )
        assert preparation.manifest.format_version == 2
        preparation_dir = preparation.directory
        approval = approval_payload(preparation)
        approval["scientific_review"] = True
        approval_path.write_text(json.dumps(approval), encoding="utf-8")
    receipt = approved_save(
        draft_path=pair_dir / "answer.json", material_root=material,
        target=target, preparation_dir=preparation_dir, approval=approval_path,
        operation_id=operation_id,
        fault_hooks={"after_files": lambda: os._exit(9)} if step == "compact-crash" else None,
    )
    if step == "compact-crash":
        raise AssertionError("Compact crash hook did not terminate the process")
    assert receipt.version == 1
    if step == "compact-recover":
        assert receipt.recovered
    entry, answer = target.read_current_answer("gold-2501.00001")
    assert entry.version == 1
    assert answer.contributions[0].contribution_id == "2501.00001:cand01"
    assert answer.contributions[0].quantities[0].values[0].value == "612.0"
    version_dir = target.root / "annotations" / "gold-2501.00001" / "v1"
    assert {p.name for p in version_dir.iterdir()} == {"answer.json", "metadata.json"}
    assert query_operation(operation_id=operation_id, target=target).state == "saved"
    print("COMPACT-RECOVERED" if crashing else "COMPACT-SAVED")
elif step == "verify-import":
    import hvs_catalog.evaluation as evaluation

    assert evaluation.GOLD_SCHEMA_VERSION == 1
    print("R06-IMPORT")
else:
    raise SystemExit(f"未知步骤：{step}")
'''


def verify_r06(python: Path, work_dir: Path, script: Path) -> list[str]:
    """仓库外用安装后的 Gold 公共接口与 CLI 走查 R06 链路。

    独立子进程完成迁移、检查、全新草稿保存与重放、CLI 全流程，以及 v1/v2
    真实中断窗口硬退出与恢复，全部清除 PYTHONPATH，只依赖安装后的包。
    """
    problems: list[str] = []
    driver = work_dir / "r06_check.py"
    driver.write_text(R06_DRIVER, encoding="utf-8")
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
        ("migrate", "R06-MIGRATED", 0),
        ("check", "R06-CHECKED", 0),
        ("fresh", "R06-SAVED", 0),
        ("cli", "R06-CLI", 0),
        ("verify-import", "R06-IMPORT", 0),
        ("crash", None, 9),
        ("recover", "R06-RECOVERED", 0),
        ("compact", "COMPACT-SAVED", 0),
        ("compact-crash", None, 9),
        ("compact-recover", "COMPACT-RECOVERED", 0),
    ]
    for step, marker, code in expectations:
        args: tuple[object, ...] = ()
        if step == "cli" or step.startswith("compact"):
            args = (script,)
        result = run_step(step, *args)
        if result.returncode != code:
            problems.append(
                f"R06 步骤 {step} 退出码 {result.returncode}（期望 {code}）：\n"
                f"{result.stdout}{result.stderr}"
            )
            continue
        if marker is not None and marker not in result.stdout:
            problems.append(f"R06 步骤 {step} 输出缺少 {marker!r}：{result.stdout!r}")
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
        problems = verify_r05(bin_python, work_dir)
        if problems:
            print("[失败] 仓库外 R05 公共接口走查未通过：")
            for problem in problems:
                print(f"  - {problem}")
            return 1
        problems = verify_r06(bin_python, work_dir, script)
        if problems:
            print("[失败] 仓库外 R06 Gold 工具走查未通过：")
            for problem in problems:
                print(f"  - {problem}")
            return 1
    print(
        f"[通过] 仓库外全新环境安装 wheel，独立子进程查询帮助与版本均正常（{version}）；"
        "持久化恢复演示走查通过（重启恢复、结果复用、决定规则、重复计费）；"
        "R05 公共接口走查通过（初始化、执行、裁决、备份、恢复、重放保护）；"
        "R06 Gold 工具走查通过（旧 v4 迁移、草稿检查、全新草稿保存、CLI、"
        "中断窗口恢复与重放保护）；精简 Gold v2 两文件包、CLI 检查、"
        "获准保存和独立进程中断恢复通过"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

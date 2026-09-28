"""安装级合成提取与评分的仓库外走查驱动与核对函数。

``tools/check_distribution.py`` 在仓库外全新虚拟环境安装 wheel 后调用
本模块：驱动脚本只用安装后的包内公共入口（不注入仓库源码路径），核
对函数把实际输出与期望逐项比较并返回问题清单，便于离线单元测试。

覆盖（全部合成材料）：包来源、方法查询、3 篇合成论文 × 2 方法 × 裁决
开/关共 12 个 CLI 用例、保存输入的重复评分一致性与计划篡改拒绝
（退出码 2）、独立进程暂停→决定→恢复与调用日志核对，以及两组对照
实验（歧义论文与候选论文）的隔离、专家用时未知为 null。
"""

from __future__ import annotations

import json
import os
import subprocess
from collections.abc import Sequence
from pathlib import Path
from subprocess import TimeoutExpired

#: 组合走查的合成论文、方法与裁决开关（与 CLI 支持集一致）。
CHECK_PAPERS = ("FIC-R05-A", "FIC-R05-B", "FIC-CANDIDATE-C")
CHECK_METHODS = ("direct", "candidate_review")
CHECK_ADJUDICATION = ("on", "off")

#: 子进程超时（秒）：任一步骤超时按失败报告，不无限等待。
SUBPROCESS_TIMEOUT = 180

PIPELINE_DRIVER = '''
"""仓库外安装包走查：共享提取入口的跨进程暂停、决定与恢复。

只用安装后的包内公共入口，不依赖仓库源码路径。步骤：run-to-pause
（执行到专家裁决暂停）、decide-and-resume（提交固定模拟决定并恢复）、
journal（核对调用日志的步骤计数）。
"""

import json
import sys
from pathlib import Path

from hvs_catalog.pipeline import (
    PAPER_A,
    ExtractionRequest,
    PipelineMockService,
    StepModel,
    get_extraction,
    import_mock_source,
    pending_questions,
    resume_extraction,
    start_extraction,
    submit_expert_decision,
)
from hvs_catalog.workspace import initialize_workspace, open_workspace


def emit(payload):
    print(json.dumps(payload, ensure_ascii=False))
    sys.stdout.flush()


command = sys.argv[1]
root = Path(sys.argv[2])
journal = sys.argv[3]

if command == "run-to-pause":
    space = initialize_workspace(root)
    source = import_mock_source(space, PAPER_A)
    request = ExtractionRequest(
        mode="daily", method="direct", source_refs=[source],
        models=[StepModel(model="mock-default")],
    )
    handle = start_extraction(space, request=request, request_key="installed-pause")
    report = resume_extraction(
        space, handle.execution_id, service=PipelineMockService(journal)
    )
    emit({"execution_id": handle.execution_id, "outcome": report.outcome})
elif command == "decide-and-resume":
    execution_id = sys.argv[4]
    space = open_workspace(root)
    question = pending_questions(space, execution_id)[0]
    submit_expert_decision(
        space, execution_id=execution_id, question_ref=question.question_ref,
        request_key="installed-decision", choice="no_overall_conclusion",
    )
    report = resume_extraction(
        space, execution_id, service=PipelineMockService(journal)
    )
    status = get_extraction(space, execution_id)
    emit({
        "execution_id": execution_id,
        "outcome": report.outcome,
        "errors": list(report.errors),
        "state": status["state"],
    })
elif command == "journal":
    lines = Path(journal).read_text(encoding="utf-8").splitlines()
    calls = [json.loads(line) for line in lines if line.strip()]
    steps = {}
    for call in calls:
        steps[call["step"]] = steps.get(call["step"], 0) + 1
    emit({"steps": steps})
else:
    raise SystemExit(f"未知步骤：{command}")
'''

EXPERIMENT_DRIVER = '''
"""仓库外安装包走查：裁决开关两组对照实验（歧义论文与候选论文）。

只用安装后的包内公共入口；模拟决定来自固定合成材料，不从 Gold、
评分或诊断读取。断言两组执行隔离、专家次数与科学完成状态、纯 AI
组不写专家决定、专家用时未知为 null。
"""

import json
from pathlib import Path

from hvs_catalog.evaluation.experiment import run_two_group_experiment
from hvs_catalog.workspace import initialize_workspace

reports = []
for paper, method in [
    ("FIC-R05-A", "direct"),
    ("FIC-CANDIDATE-C", "candidate_review"),
]:
    space = initialize_workspace(Path("experiment-" + paper.replace("-", "_")))
    try:
        report = run_two_group_experiment(space, paper, method=method)
    finally:
        space.close()
    reports.append(report)

Path("installed-experiment-reports.json").write_text(
    json.dumps(reports, ensure_ascii=False, indent=2), encoding="utf-8"
)
print("EXPERIMENTS-OK")
'''


def _clean_env() -> dict[str, str]:
    """仓库外子进程环境：清除源码路径注入，禁用用户 site。"""
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    env.pop("PYTHONHOME", None)
    env["PYTHONNOUSERSITE"] = "1"
    return env


def module_origin_problems(origins: Sequence[str], venv_dir: Path) -> list[str]:
    """核对模块来源都位于该虚拟环境的 site-packages 内。"""
    problems = []
    for origin in origins:
        path = Path(origin)
        if not path.is_relative_to(venv_dir) or "site-packages" not in origin:
            problems.append(
                f"模块来源不在安装环境 site-packages 内：{origin}（环境：{venv_dir}）"
            )
    return problems


def methods_payload_problems(payload: object) -> list[str]:
    """核对方法查询输出：两个 synthetic-mock 方法，且都不是真实提取。"""
    if not isinstance(payload, list) or len(payload) != 2:
        return [f"方法查询应返回 2 个方法，实际：{payload!r}"]
    problems = []
    expected = {("direct",), ("candidate_review",)}
    actual = {(method.get("method_id"),) for method in payload}
    if actual != expected:
        problems.append(f"方法集合不符：期望 {sorted(expected)}，实际 {sorted(actual)}")
    for method in payload:
        if method.get("implementation") != "synthetic-mock":
            problems.append(
                f"方法 {method.get('method_id')!r} 实现标记异常："
                f"{method.get('implementation')!r}"
            )
        if method.get("real_extraction") is not False:
            problems.append(
                f"方法 {method.get('method_id')!r} 冒称真实提取：{method!r}"
            )
    return problems


def scoring_repeat_problems(first: object, second: object) -> list[str]:
    """核对同一保存输入的两次评分报告一致（论文与摘要逐字相同）。"""
    problems = []
    for field in ("papers", "digests", "micro"):
        if first.get(field) != second.get(field):
            problems.append(f"重复评分的 {field} 不一致")
    return problems


def cross_process_problems(
    paused: object, resumed: object, journal: object
) -> list[str]:
    """核对跨进程暂停→决定→恢复与调用日志。"""
    problems = []
    if paused.get("outcome") != "paused":
        problems.append(f"首进程应暂停，实际 {paused.get('outcome')!r}")
    if resumed.get("outcome") != "finished":
        problems.append(f"决定并恢复后应完成，实际 {resumed.get('outcome')!r}")
    if resumed.get("errors") != []:
        problems.append(f"恢复后不应有错误，实际 {resumed.get('errors')!r}")
    if resumed.get("state") != "finished":
        problems.append(f"执行状态应为 finished，实际 {resumed.get('state')!r}")
    steps = journal.get("steps", {})
    if steps.get("direct_roster") != 1 or steps.get("quantities") != 1:
        problems.append(f"调用日志步骤计数异常：{steps!r}")
    return problems


def experiment_report_problems(reports: Sequence[object]) -> list[str]:
    """核对两组对照实验报告：隔离、专家次数、科学完成与专家用时。"""
    if len(reports) != 2:
        return [f"应有两份实验报告（歧义论文与候选论文），实际 {len(reports)} 份"]
    problems = []
    for report in reports:
        paper = report.get("paper_key")
        try:
            on = report["groups"]["expert_on"]
            off = report["groups"]["expert_off"]
        except (KeyError, TypeError):
            problems.append(f"{paper!r} 报告缺少两组结果")
            continue
        if on.get("execution_id") == off.get("execution_id"):
            problems.append(f"{paper!r} 两组必须使用不同执行")
        if on.get("decisions", {}).get("human") != 1:
            problems.append(f"{paper!r} 开启组应有一次专家决定")
        if off.get("decisions", {}).get("human") != 0:
            problems.append(f"{paper!r} 关闭组不应有专家决定")
        if on.get("science_completion") != "complete":
            problems.append(
                f"{paper!r} 开启组科学完成状态异常：{on.get('science_completion')!r}"
            )
        if off.get("science_completion") != "pending":
            problems.append(
                f"{paper!r} 关闭组应保持 pending，实际 "
                f"{off.get('science_completion')!r}"
            )
        if not off.get("quality", {}).get("l2", {}).get("gold"):
            problems.append(f"{paper!r} 关闭组 Gold 分母丢失")
        for name, group in (("开启", on), ("关闭", off)):
            if group.get("expert_time_seconds") is not None:
                problems.append(f"{paper!r} {name}组专家用时应为 null")
        if report.get("isolation", {}).get("expert_off_existing_decision_refs") != []:
            problems.append(f"{paper!r} 关闭组不应消费开启组的决定")
    return problems


def verify_pipeline_and_scoring(
    venv_dir: Path, work_dir: Path, script: Path
) -> list[str]:
    """仓库外用安装后的包走查合成提取、评分、跨进程恢复与两组实验。"""
    problems: list[str] = []
    bin_dir = venv_dir / ("Scripts" if os.name == "nt" else "bin")
    python = bin_dir / "python"
    env = _clean_env()

    def run(command: Sequence[object], *, cwd: Path | None = None):
        try:
            return subprocess.run(
                [str(part) for part in command],
                cwd=cwd or work_dir,
                env=env,
                capture_output=True,
                text=True,
                check=False,
                timeout=SUBPROCESS_TIMEOUT,
            )
        except TimeoutExpired:
            return subprocess.CompletedProcess(
                [str(part) for part in command],
                returncode=124,
                stdout="",
                stderr=f"步骤超过 {SUBPROCESS_TIMEOUT}s 超时："
                + " ".join(str(part) for part in command),
            )
        except FileNotFoundError as exc:
            return subprocess.CompletedProcess(
                [str(part) for part in command],
                returncode=127,
                stdout="",
                stderr=f"命令不存在：{exc}",
            )

    def note(step: str, result: subprocess.CompletedProcess[str]) -> None:
        problems.append(
            f"步骤 {step} 退出码 {result.returncode}：\n{result.stdout}{result.stderr}"
        )

    def run_json(command: Sequence[object], *, cwd: Path | None = None):
        result = run(command, cwd=cwd)
        if result.returncode != 0:
            note(" ".join(str(part) for part in command[:2]), result)
            return None
        try:
            return json.loads(result.stdout)
        except json.JSONDecodeError:
            problems.append(
                f"命令输出不是 JSON：{' '.join(str(p) for p in command[:3])}\n"
                f"{result.stdout!r}"
            )
            return None

    def read_artifact(path: Path, context: str, expected_type: type):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            problems.append(f"{context}：无法读取 JSON 产物 {path}：{exc}")
            return None
        if not isinstance(payload, expected_type):
            problems.append(f"{context}：JSON 产物类型错误 {path}")
            return None
        return payload

    # 1) 包来源：模块必须来自安装环境的 site-packages
    origin = run_json(
        [
            python,
            "-I",
            "-c",
            (
                "import json,hvs_catalog,hvs_catalog.pipeline,"
                "hvs_catalog.evaluation.scoring; print(json.dumps("
                "[m.__file__ for m in (hvs_catalog,hvs_catalog.pipeline,"
                "hvs_catalog.evaluation.scoring)]))"
            ),
        ]
    )
    if origin is None:
        return problems
    problems.extend(module_origin_problems(origin, venv_dir))

    # 2) 方法查询：两个 synthetic-mock 方法，不冒称真实提取
    payload = run_json([script, "methods", "list", "--json"])
    if payload is None:
        return problems
    problems.extend(methods_payload_problems(payload))

    # 3) 3 论文 × 2 方法 × 裁决开/关：提取 demo + 重复评分一致
    bundle_path = None
    for paper in CHECK_PAPERS:
        for method in CHECK_METHODS:
            for switch in CHECK_ADJUDICATION:
                case = work_dir / f"case-{paper}-{method}-{switch}"
                extracted = run(
                    [
                        script,
                        "extract",
                        "demo",
                        "--space",
                        case / "space",
                        "--paper",
                        paper,
                        "--method",
                        method,
                        "--mode",
                        "evaluation",
                        "--adjudication",
                        switch,
                        "--output",
                        case / "out",
                    ]
                )
                if extracted.returncode != 0:
                    note(f"extract {paper}/{method}/{switch}", extracted)
                    continue
                bundle_path = case / "out" / "evaluation-inputs.json"
                scores = []
                for number in (1, 2):
                    scored = run(
                        [
                            script,
                            "score",
                            "demo",
                            "--inputs",
                            bundle_path,
                            "--output",
                            case / f"score{number}.json",
                        ]
                    )
                    if scored.returncode != 0:
                        note(f"score {paper}/{method}/{switch} 第{number}次", scored)
                        scores = []
                        break
                    score = read_artifact(
                        case / f"score{number}.json",
                        f"score {paper}/{method}/{switch} 第{number}次",
                        dict,
                    )
                    if score is None:
                        break
                    scores.append(score)
                if len(scores) == 2:
                    problems.extend(
                        [
                            f"{paper}/{method}/{switch}：{problem}"
                            for problem in scoring_repeat_problems(*scores)
                        ]
                    )
    if bundle_path is None:
        problems.append("没有任何 extract demo 用例产出评分输入集")

    # 4) 计划篡改：任一内容被改动后必须拒绝按旧指纹重评（退出码 2）
    if bundle_path is not None:
        tampered = read_artifact(bundle_path, "计划篡改检查", dict)
        if tampered is not None:
            try:
                tampered["plan"]["program_ref"]["version"] = "TAMPERED"
            except (KeyError, TypeError):
                problems.append(f"评分输入集缺少有效的程序版本：{bundle_path}")
            else:
                bad = work_dir / "tampered-plan.json"
                bad.write_text(json.dumps(tampered), encoding="utf-8")
                rejected = run([script, "score", "demo", "--inputs", bad])
                if rejected.returncode != 2:
                    problems.append(
                        f"计划篡改后应以退出码 2 拒绝，实际 {rejected.returncode}：\n"
                        f"{rejected.stdout}{rejected.stderr}"
                    )

    # 5) 独立进程暂停→决定→恢复（无源码路径注入）与调用日志核对
    driver = work_dir / "installed_pipeline_check.py"
    driver.write_text(PIPELINE_DRIVER, encoding="utf-8")
    space = work_dir / "resume-space"
    journal = work_dir / "calls.jsonl"
    paused = run_json([python, "-I", driver, "run-to-pause", space, journal])
    if paused is None:
        return problems
    resumed = None
    if paused.get("outcome") == "paused":
        resumed = run_json(
            [
                python,
                "-I",
                driver,
                "decide-and-resume",
                space,
                journal,
                paused["execution_id"],
            ]
        )
        counts = run_json([python, "-I", driver, "journal", space, journal])
        if resumed is not None and counts is not None:
            problems.extend(cross_process_problems(paused, resumed, counts))
    else:
        problems.append(f"首进程应暂停，实际 {paused.get('outcome')!r}")

    # 6) 两组对照实验：隔离、专家次数、科学完成与专家用时未知
    experiment = work_dir / "installed_experiment_check.py"
    experiment.write_text(EXPERIMENT_DRIVER, encoding="utf-8")
    reports_path = work_dir / "installed-experiment-reports.json"
    result = run([python, "-I", experiment])
    if result.returncode != 0 or "EXPERIMENTS-OK" not in result.stdout:
        note("two-group experiments", result)
    else:
        reports = read_artifact(reports_path, "two-group experiments", list)
        if reports is not None:
            problems.extend(experiment_report_problems(reports))
    return problems

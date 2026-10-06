"""External installed-package synthetic extraction/scoring drivers and validators.

check_distribution installs fresh wheels then calls these public-API-only drivers
without repository path injection. Compare actual/expected outputs itemwise and return
issues for offline unit tests. Cover package provenance/method queries, 12
paper-method-review combinations, saved-input rescoring consistency, plan-tampering code
2, fresh-process pause/decision/recovery/call logs, and ambiguity/candidate comparisons
with isolated outputs and null expert time.
"""

from __future__ import annotations

import json
import os
import subprocess
from collections.abc import Sequence
from pathlib import Path
from subprocess import TimeoutExpired

from installed_pdf_fixture import PDF_FIXTURE

#: Synthetic papers/methods/review switches match CLI supported sets.
CHECK_PAPERS = ("FIC-R05-A", "FIC-R05-B", "FIC-CANDIDATE-C")
CHECK_METHODS = ("roster",)
CHECK_ADJUDICATION = ("on", "off")

#: Subprocess timeout in seconds; timed-out steps report failure rather than wait indefinitely.
SUBPROCESS_TIMEOUT = 180

MATERIALS_DRIVER = (
    PDF_FIXTURE
    + r"""
import sys
from hvs_catalog.materials import MaterialsService
from hvs_catalog.materials.flow import parser_fingerprint
from hvs_catalog.pipeline.acquisition import AcquisitionService
from hvs_catalog.workspace import initialize_workspace
work=Path(sys.argv[1]);space=initialize_workspace(work/"space")
pdf=work/"paper.pdf";pdf.write_bytes(make_pdf())
ref=AcquisitionService(space).import_journal_pdf(pdf,paper_key="SYN-JOURNAL",source_version="v1",provenance="synthetic installed PDF")
service=MaterialsService(space,runner=FakeMinerU())
handle,report=service.prepare_materials(request_key="installed-pdf",source_ref=ref)
consumed=service.consume_prepared_input(handle.execution_id)
print(json.dumps({"outcome":report.outcome,"errors":report.errors,"summary":report.summary,
                  "consume_ok":consumed["ok"],"consume_problems":consumed["problems"],
                  "parser_fingerprint":parser_fingerprint(),"flow_version":handle.flow_version}))
space.close()
"""
)

NUMERIC_DRIVER = r"""
import json,sys
from pathlib import Path
from hvs_catalog.evaluation.bundle import load_scoring_bundle,save_scoring_bundle,score_scoring_bundle
from hvs_catalog.evaluation.saved import program_reference
from hvs_catalog.evaluation.scoring import auxiliary_config_digest,numeric_scoring_config,numeric_scheme_reference,score_batch
plan,gold,preds,_=load_scoring_bundle(sys.argv[1])
plan.comparison_mode="numeric"
plan.scoring_scheme_ref=numeric_scheme_reference()
config=numeric_scoring_config()
plan.auxiliary_ref.fingerprint=auxiliary_config_digest(config)
plan.program_ref=program_reference()
for paper in preds:
    for obj in paper.objects:
        for group in obj.quantities:
            for entry in group.values:
                entry.value.condition="Independent alternate wording"
report=score_batch(plan,gold,preds)
assert report.micro.l2.correct==report.micro.l2.predicted==report.micro.l2.gold==2
assert not report.auxiliary.enabled
path=Path(sys.argv[2])
save_scoring_bundle(path,plan=plan,gold=gold,predictions=preds,auxiliary=config)
assert score_scoring_bundle(path)==report
print(json.dumps({"numeric_correct":report.micro.l2.correct,"program":plan.program_ref.model_dump(mode="json")}))
"""


PIPELINE_DRIVER = '''
"""External installed shared-extraction pause/decision/recovery checks.

Use installed public APIs without repository paths. Steps: run-to-pause for expert wait, decide-and-resume with fixed simulated decisions, and journal verification of per-step call counts.
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
        mode="synthetic", method="roster", source_refs=[source],
        models=[StepModel(model="mock-default")], expert_adjudication=True,
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
"""External installed two-group comparisons for ambiguous and candidate papers.

Use installed public APIs. Simulated decisions derive from fixed materials, never Gold/scoring/diagnostics. Verify group isolation, expert counts, scientific completion, no expert decisions in AI-only runs, and null unknown expert time.
"""

import json
from pathlib import Path

from hvs_catalog.evaluation.experiment import run_two_group_experiment
from hvs_catalog.workspace import initialize_workspace

reports = []
for paper, method in [
    ("FIC-R05-A", "roster"),
    ("FIC-CANDIDATE-C", "roster"),
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
    """External subprocess environment clears source-path injection and disables user site
    packages.
    """
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    env.pop("PYTHONHOME", None)
    env["PYTHONNOUSERSITE"] = "1"
    return env


def module_origin_problems(origins: Sequence[str], venv_dir: Path) -> list[str]:
    """Verify every module originates from environment site-packages."""
    problems = []
    for origin in origins:
        path = Path(origin)
        if not path.is_relative_to(venv_dir) or "site-packages" not in origin:
            problems.append(
                f"模块来源不在安装环境 site-packages 内：{origin}（环境：{venv_dir}）"
            )
    return problems


def methods_payload_problems(payload: object) -> list[str]:
    """Check method queries expose two synthetic-mock methods without real extraction
    claims.
    """
    if not isinstance(payload, list) or len(payload) != 1:
        return [f"方法查询应返回 1 个方法，实际：{payload!r}"]
    problems = []
    expected = {("roster",)}
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
    """Verify repeated scoring of identical saved input has identical papers/digests."""
    problems = []
    for field in ("papers", "digests", "micro"):
        if first.get(field) != second.get(field):
            problems.append(f"重复评分的 {field} 不一致")
    return problems


def cross_process_problems(
    paused: object, resumed: object, journal: object
) -> list[str]:
    """Verify cross-process pause/decision/recovery and call logs."""
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
    if steps.get("roster") != 1 or steps.get("quantities") != 1:
        problems.append(f"调用日志步骤计数异常：{steps!r}")
    return problems


def experiment_report_problems(reports: Sequence[object]) -> list[str]:
    """Check group isolation, expert counts, scientific completion, and expert time."""
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
        if off.get("science_completion") != "complete":
            problems.append(
                f"{paper!r} 关闭组应完成，实际 {off.get('science_completion')!r}"
            )
        if not off.get("quality", {}).get("l2", {}).get("gold"):
            problems.append(f"{paper!r} 关闭组 Gold 分母丢失")
        for name, group in (("开启", on), ("关闭", off)):
            if group.get("expert_time_seconds") is not None:
                problems.append(f"{paper!r} {name}组专家用时应为 null")
        if report.get("isolation", {}).get("expert_off_existing_decision_refs") != []:
            problems.append(f"{paper!r} 关闭组不应消费开启组的决定")
    return problems


def materials_check_problems(payload: object) -> list[str]:
    """Check preparation completion, body organization, tables, and model-free consumption."""
    if not isinstance(payload, dict):
        return ["材料准备走查输出不是 JSON 对象"]
    problems: list[str] = []
    if payload.get("outcome") != "finished":
        problems.append(
            f"材料准备应完成，实际 {payload.get('outcome')!r}：{payload.get('errors')}"
        )
        return problems
    summary = payload.get("summary") or {}
    fingerprint = payload.get("parser_fingerprint")
    if not isinstance(fingerprint, str) or len(fingerprint) != 64:
        problems.append("安装后的PDF解析实现没有有效冻结指纹")
    if payload.get("flow_version") != "1":
        problems.append("安装后的PDF材料流程不是当前版本1")
    if int(summary.get("pages") or 0) < 1 or int(summary.get("blocks") or 0) < 1:
        problems.append("合成PDF应提供页与内容块")
    if payload.get("consume_ok") is not True:
        problems.append(f"无模型消费核对未通过：{payload.get('consume_problems')}")
    return problems


def verify_pipeline_and_scoring(
    venv_dir: Path, work_dir: Path, script: Path
) -> list[str]:
    """Exercise installed synthetic extraction, scoring, cross-process recovery, and
    comparisons externally.
    """
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

    # 1) Package provenance: modules must originate from installed site-packages.
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

    # 2) Method query: two synthetic-mock methods without real extraction claims.
    payload = run_json(
        [
            python,
            "-I",
            "-c",
            "import json; from hvs_catalog.pipeline import list_methods; print(json.dumps(list_methods()))",
        ]
    )
    if payload is None:
        return problems
    problems.extend(methods_payload_problems(payload))

    # 3) Three papers x two methods x two review modes: extraction demo and consistent rescoring.
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
    else:
        numeric = run_json(
            [
                python,
                "-I",
                "-c",
                NUMERIC_DRIVER,
                bundle_path,
                work_dir / "numeric-inputs.json",
            ]
        )
        if not isinstance(numeric, dict) or numeric.get("numeric_correct") != 2:
            problems.append("安装包完整数值评分或冻结重评失败")
        replay = run(
            [
                script,
                "score",
                "replay",
                "--inputs",
                work_dir / "numeric-inputs.json",
                "--output",
                work_dir / "numeric-replay",
            ]
        )
        if replay.returncode != 0:
            note("安装包数值评分CLI重评", replay)

    # 4) Changed plan content rejects old-fingerprint rescoring with code 2.
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

    # 5) Fresh-process pause/decision/recovery without source injection, plus call-log checks.
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

    # 6) Group comparisons: isolation, expert counts, science completion, and unknown expert time.
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

    # 7) Preparation: synthetic import, preparation, and model-free consumption.
    materials_driver = work_dir / "installed_materials_check.py"
    materials_driver.write_text(MATERIALS_DRIVER, encoding="utf-8")
    materials_space = work_dir / "materials-work"
    materials_space.mkdir(exist_ok=True)
    materials_result = run_json([python, "-I", materials_driver, materials_space])
    if materials_result is None:
        problems.append("材料准备走查驱动失败")
    else:
        problems.extend(materials_check_problems(materials_result))
    return problems

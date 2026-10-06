"""Offline unit tests for installed synthetic extraction/scoring drivers and validators.

Use synthetic return values to verify failure reporting in
tools/installed_pipeline_checks.py, without building distributions or creating virtual
environments.
"""

from __future__ import annotations

import json
import stat
import subprocess
from pathlib import Path

import installed_pipeline_checks as checks
import pytest
from installed_pipeline_checks import (
    EXPERIMENT_DRIVER,
    PIPELINE_DRIVER,
    cross_process_problems,
    experiment_report_problems,
    methods_payload_problems,
    module_origin_problems,
    scoring_repeat_problems,
    verify_pipeline_and_scoring,
)

GOOD_METHODS = [
    {
        "method_id": "roster",
        "implementation": "synthetic-mock",
        "real_extraction": False,
        "version": "1",
        "steps": ["roster", "quantities", "finalize"],
    },
]


def test_module_origin_problems_accepts_site_packages():
    venv = Path("/tmp/installed-env")
    origins = [
        str(venv / "lib/python3.12/site-packages/hvs_catalog/__init__.py"),
        str(venv / "lib/python3.12/site-packages/hvs_catalog/pipeline/__init__.py"),
    ]
    assert module_origin_problems(origins, venv) == []


def test_module_origin_problems_rejects_source_tree_or_site_injection():
    venv = Path("/tmp/installed-env")
    problems = module_origin_problems(
        [
            "/repo/src/hvs_catalog/__init__.py",
            "/tmp/installed-env/src/hvs_catalog/__init__.py",
        ],
        venv,
    )
    assert len(problems) == 2
    assert all("site-packages" in problem for problem in problems)


def test_methods_payload_problems_detects_wrong_payloads():
    assert methods_payload_problems(GOOD_METHODS) == []
    assert methods_payload_problems({"error": "x"}) != []
    assert methods_payload_problems([])
    assert methods_payload_problems([*GOOD_METHODS, *GOOD_METHODS])
    claiming_real = [dict(method, real_extraction=True) for method in GOOD_METHODS]
    assert any(
        "冒称真实提取" in problem for problem in methods_payload_problems(claiming_real)
    )
    wrong_impl = [dict(method, implementation="real") for method in GOOD_METHODS]
    assert any(
        "实现标记异常" in problem for problem in methods_payload_problems(wrong_impl)
    )


def test_scoring_repeat_problems_detects_divergence():
    report = {"papers": [{"k": 1}], "digests": {"gold": "ab"}, "micro": {"l1": {}}}
    assert scoring_repeat_problems(report, dict(report)) == []
    problems = scoring_repeat_problems(report, {**report, "papers": [{"k": 2}]})
    assert problems == ["重复评分的 papers 不一致"]


def test_cross_process_problems_detects_wrong_outcomes_and_calls():
    paused = {"outcome": "paused", "execution_id": "exec-1"}
    resumed = {
        "outcome": "finished",
        "errors": [],
        "state": "finished",
        "execution_id": "exec-1",
    }
    journal = {"steps": {"roster": 1, "quantities": 1}}
    assert cross_process_problems(paused, resumed, journal) == []

    bad_paused = {**paused, "outcome": "failed"}
    bad_resumed = {**resumed, "outcome": "failed", "errors": ["x"], "state": "paused"}
    bad_journal = {"steps": {"roster": 2, "quantities": 1}}
    problems = cross_process_problems(bad_paused, bad_resumed, bad_journal)
    assert len(problems) == 5
    assert any("应暂停" in problem for problem in problems)
    assert any("应完成" in problem for problem in problems)
    assert any("不应有错误" in problem for problem in problems)
    assert any("finished" in problem for problem in problems)
    assert any("步骤计数异常" in problem for problem in problems)


def _experiment_report(**overrides):
    group_on = {
        "execution_id": "exec-on",
        "decisions": {"human": 1},
        "science_completion": "complete",
        "expert_time_seconds": None,
        "quality": {"l2": {"gold": 1}},
    }
    group_off = {
        "execution_id": "exec-off",
        "decisions": {"human": 0},
        "science_completion": "complete",
        "expert_time_seconds": None,
        "quality": {"l2": {"gold": 1}},
    }
    report = {
        "paper_key": "FIC-R05-A",
        "groups": {"expert_on": group_on, "expert_off": group_off},
        "isolation": {"expert_off_existing_decision_refs": []},
    }
    for key, value in overrides.items():
        report[key] = value
    return report


def test_experiment_report_problems_accepts_valid_reports():
    reports = [_experiment_report(), _experiment_report(paper_key="FIC-CANDIDATE-C")]
    assert experiment_report_problems(reports) == []


def test_experiment_report_problems_detects_isolation_and_time_violations():
    shared = {"execution_id": "exec-same"}
    leaking = _experiment_report()
    leaking["isolation"]["expert_off_existing_decision_refs"] = ["dec-1"]
    leaking["groups"]["expert_off"]["decisions"]["human"] = 1
    leaking["groups"]["expert_on"]["expert_time_seconds"] = 3.5
    leaking["groups"]["expert_off"]["science_completion"] = "pending"
    reports = [leaking, _experiment_report()]
    reports[0]["groups"]["expert_on"].update(shared)
    reports[0]["groups"]["expert_off"].update(shared)
    problems = experiment_report_problems(reports)
    assert any("不同执行" in problem for problem in problems)
    assert any("不应有专家决定" in problem for problem in problems)
    assert any("专家用时应为 null" in problem for problem in problems)
    assert any("关闭组不应消费开启组的决定" in problem for problem in problems)
    assert any("关闭组应完成" in problem for problem in problems)


def test_experiment_report_problems_rejects_wrong_report_count():
    assert experiment_report_problems([]) == [
        "应有两份实验报告（歧义论文与候选论文），实际 0 份"
    ]


def test_drivers_have_no_source_path_injection_and_compile():
    for driver in (PIPELINE_DRIVER, EXPERIMENT_DRIVER):
        assert "sys.path.insert" not in driver
        assert "__SRC__" not in driver
        compile(driver, "<driver>", "exec")


def test_verify_pipeline_reports_problems_for_failing_installation(tmp_path):
    """Verify itemized failures rather than silent success in an environment guaranteed to
    fail.
    """
    venv_dir = tmp_path / "venv"
    venv_dir.mkdir()
    (venv_dir / "bin").mkdir()
    work_dir = tmp_path / "work"
    work_dir.mkdir()
    script = tmp_path / "fake-cli"
    script.write_text("#!/bin/sh\necho 'boom' >&2\nexit 3\n")
    script.chmod(script.stat().st_mode | stat.S_IEXEC)

    problems = verify_pipeline_and_scoring(venv_dir, work_dir, script)
    assert problems, "失败的安装环境必须报告问题"
    assert any("退出码" in problem or "不存在" in problem for problem in problems)


@pytest.mark.parametrize(
    "artifact",
    [
        "score1.json",
        "score2.json",
        "evaluation-inputs.json",
        "installed-experiment-reports.json",
    ],
)
@pytest.mark.parametrize(
    "damage", ["missing", "invalid-json", "invalid-utf8", "wrong-type"]
)
def test_zero_exit_with_bad_artifact_reports_context(
    tmp_path, monkeypatch, artifact, damage
):
    """A zero exit does not establish valid outputs; report every file-reading failure with
    context instead of unhandled exceptions.
    """
    venv = tmp_path / "venv"
    script = venv / "bin" / "hvs-catalog"
    monkeypatch.setattr(checks, "CHECK_PAPERS", ("FIC-R05-A",))
    monkeypatch.setattr(checks, "CHECK_METHODS", ("roster",))
    monkeypatch.setattr(checks, "CHECK_ADJUDICATION", ("off",))

    def write_output(path, payload):
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.name == artifact:
            if damage == "missing":
                return
            path.write_bytes(
                {"invalid-json": b"{", "invalid-utf8": b"\xff", "wrong-type": b"null"}[
                    damage
                ]
            )
        else:
            path.write_text(json.dumps(payload), encoding="utf-8")

    def fake_run(command, **kwargs):
        code, payload = 0, {}
        if "list_methods" in " ".join(command):
            payload = GOOD_METHODS
        elif checks.NUMERIC_DRIVER in command:
            payload = {"numeric_correct": 2}
        elif "-c" in command:
            payload = [
                str(venv / "lib/python3.12/site-packages/hvs_catalog/__init__.py")
            ]
        elif "methods" in command:
            payload = GOOD_METHODS
        elif "extract" in command:
            output = Path(command[command.index("--output") + 1])
            write_output(
                output / "evaluation-inputs.json",
                {"plan": {"program_ref": {"version": "synthetic-v1"}}},
            )
        elif "score" in command:
            if "--output" in command:
                output = Path(command[command.index("--output") + 1])
                write_output(output, {"papers": [], "digests": {}, "micro": {}})
            else:
                code = 2
        elif "run-to-pause" in command:
            payload = {"outcome": "paused", "execution_id": "execution"}
        elif "decide-and-resume" in command:
            payload = {"outcome": "finished", "state": "finished", "errors": []}
        elif "journal" in command:
            payload = {"steps": {"roster": 1, "quantities": 1}}
        elif any(part.endswith("installed_experiment_check.py") for part in command):
            write_output(
                tmp_path / "installed-experiment-reports.json",
                [_experiment_report(), _experiment_report(paper_key="FIC-CANDIDATE-C")],
            )
            return subprocess.CompletedProcess(command, 0, "EXPERIMENTS-OK", "")
        elif any(part.endswith("installed_materials_check.py") for part in command):
            payload = {
                "outcome": "finished",
                "errors": [],
                "summary": {
                    "pages": 1,
                    "blocks": 1,
                },
                "consume_ok": True,
                "consume_problems": [],
                "observer_installed": True,
                "parser_fingerprint": "a" * 64,
                "flow_version": "1",
            }
        else:
            pytest.fail(f"unexpected command: {command}")
        return subprocess.CompletedProcess(command, code, json.dumps(payload), "")

    monkeypatch.setattr(checks.subprocess, "run", fake_run)
    problems = verify_pipeline_and_scoring(venv, tmp_path, script)
    assert len(problems) == 1, problems
    assert artifact in problems[0]
    if artifact.startswith("score"):
        assert "FIC-R05-A/roster/off" in problems[0]
    elif artifact == "installed-experiment-reports.json":
        assert "two-group experiments" in problems[0]
    else:
        assert "计划篡改检查" in problems[0]

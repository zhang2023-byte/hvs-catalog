"""共享提取入口：独立进程恢复与真实中断窗口（C05）。

子进程 1 执行到暂停（或在中断窗口内 ``os._exit`` 模拟进程死亡），
子进程 2 打开同一空间恢复：固定输入、归档响应与调用日志（可选
journal 文件）跨进程核对。调用窗口分两段验证：收到响应但未归档会重
新调用；已归档未提交则复用归档响应。
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.integration

DRIVER = r"""
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, __SRC__)

from hvs_catalog.pipeline import (
    PAPER_A, ExtractionRequest, PipelineMockService, StepModel,
    import_mock_source, pending_questions, resume_extraction,
    start_extraction, submit_expert_decision, get_extraction,
)
from hvs_catalog.pipeline.flow import ExtractionFlow
from hvs_catalog.pipeline.methods import DIRECT_METHOD
from hvs_catalog.runtime import resume_execution
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
    handle = start_extraction(space, request=request, request_key="proc-pause")
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
        request_key="proc-decision", choice="no_overall_conclusion",
    )
    report = resume_extraction(
        space, execution_id, service=PipelineMockService(journal)
    )
    emit({"execution_id": execution_id, "outcome": report.outcome,
          "errors": list(report.errors)})
elif command == "crash-after-response":
    space = initialize_workspace(root)
    source = import_mock_source(space, PAPER_A)
    request = ExtractionRequest(
        mode="synthetic", method="direct", source_refs=[source],
        models=[StepModel(model="mock-default")], expert_adjudication=False,
    )
    handle = start_extraction(space, request=request, request_key="proc-crash-resp")
    def crash():
        os._exit(9)
    report = resume_execution(
        space, handle.execution_id,
        ExtractionFlow(DIRECT_METHOD, mode="synthetic",
                       fault_hooks={"after_response:direct_roster": crash}),
        service=PipelineMockService(journal),
    )
    emit({"execution_id": handle.execution_id, "outcome": report.outcome})
elif command == "crash-after-archive":
    space = initialize_workspace(root)
    source = import_mock_source(space, PAPER_A)
    request = ExtractionRequest(
        mode="synthetic", method="direct", source_refs=[source],
        models=[StepModel(model="mock-default")], expert_adjudication=False,
    )
    handle = start_extraction(space, request=request, request_key="proc-crash-arch")
    def crash():
        os._exit(9)
    resume_execution(
        space, handle.execution_id,
        ExtractionFlow(DIRECT_METHOD, mode="synthetic",
                       fault_hooks={"after_archive:direct_roster": crash}),
        service=PipelineMockService(journal),
    )
elif command == "resume-fresh":
    execution_id = sys.argv[4]
    space = open_workspace(root)
    report = resume_extraction(
        space, execution_id, service=PipelineMockService(journal)
    )
    status = get_extraction(space, execution_id)
    emit({"execution_id": execution_id, "outcome": report.outcome,
          "state": status["state"],
          "roster_calls": sum(
              1 for step in status["steps"] if step["step_id"] == "direct_roster"
              for _ in range(step["attempts"])
          )})
elif command == "journal":
    lines = Path(journal).read_text(encoding="utf-8").splitlines()
    calls = [json.loads(line) for line in lines if line.strip()]
    steps = {}
    for call in calls:
        steps[call["step"]] = steps.get(call["step"], 0) + 1
    emit({"calls": len(calls), "steps": steps})
"""


def run_driver(tmp_path, *args, timeout=90.0):
    script = tmp_path / "pipeline_driver.py"
    script.write_text(
        DRIVER.replace(
            "__SRC__", repr(str(Path(__file__).resolve().parents[1] / "src"))
        ),
        encoding="utf-8",
    )
    result = subprocess.run(
        [sys.executable, str(script), *map(str, args)],
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
        cwd=tmp_path,
    )
    lines = [line for line in result.stdout.splitlines() if line.strip()]
    payload = json.loads(lines[-1]) if lines else None
    return result.returncode, payload, result.stderr


def journal_counts(tmp_path):
    _, payload, _ = run_driver(
        tmp_path, "journal", tmp_path / "space", tmp_path / "journal.jsonl"
    )
    return payload


def _only_execution(tmp_path):
    from hvs_catalog.workspace import open_workspace

    space = open_workspace(tmp_path / "space")
    try:
        return space.store().list_executions()[0].execution_id
    finally:
        space.close()


def test_pause_decide_resume_across_processes(tmp_path):
    journal = tmp_path / "journal.jsonl"
    code, payload, stderr = run_driver(
        tmp_path, "run-to-pause", tmp_path / "space", journal
    )
    assert code == 0, stderr
    assert payload["outcome"] == "paused"
    execution_id = payload["execution_id"]
    code, payload, stderr = run_driver(
        tmp_path, "decide-and-resume", tmp_path / "space", journal, execution_id
    )
    assert code == 0, stderr
    assert payload["outcome"] == "finished"
    assert payload["errors"] == []
    counts = journal_counts(tmp_path)
    assert counts["steps"]["direct_roster"] == 1
    assert counts["steps"]["quantities"] == 1


def test_crash_after_response_repeats_call(tmp_path):
    """收到响应但未归档就退出：账上多一次调用，恢复后完成。"""
    journal = tmp_path / "journal.jsonl"
    code, payload, stderr = run_driver(
        tmp_path, "crash-after-response", tmp_path / "space", journal
    )
    assert code == 9, stderr
    execution_id = _only_execution(tmp_path)
    code, payload, stderr = run_driver(
        tmp_path, "resume-fresh", tmp_path / "space", journal, execution_id
    )
    assert code == 0, stderr
    assert payload["outcome"] == "finished"
    counts = journal_counts(tmp_path)
    # 中断窗口内的调用 + 恢复时的重新调用 = 2 次名单调用
    assert counts["steps"]["direct_roster"] == 2


def test_crash_after_archive_reuses_response(tmp_path):
    """已归档未提交就退出：恢复复用归档响应，不重复调用。"""
    journal = tmp_path / "journal.jsonl"
    code, payload, stderr = run_driver(
        tmp_path, "crash-after-archive", tmp_path / "space", journal
    )
    assert code == 9, stderr
    execution_id = _only_execution(tmp_path)
    code, payload, stderr = run_driver(
        tmp_path, "resume-fresh", tmp_path / "space", journal, execution_id
    )
    assert code == 0, stderr
    assert payload["outcome"] == "finished"
    assert payload["roster_calls"] == 1  # 只有归档前那一次真实调用
    counts = journal_counts(tmp_path)
    assert counts["steps"]["direct_roster"] == 1

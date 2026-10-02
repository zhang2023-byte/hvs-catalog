"""独立进程验证：双进程锁竞争、持锁终止与真实中断窗口（A03/A04/A15/A17）。

中断用信号文件定位故障窗口后以 ``os._exit``（或 SIGKILL）结束子进程，
等价于进程死亡：已提交事实保留，未提交事务丢弃。A17 的独立调用日志独立
于空间，调用事实不会被客户端回滚抹掉。
"""

from __future__ import annotations

import json
import signal
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest
from shared_scenarios import (
    direct_flow,
    open_service,
    pending_questions,
    start_execution,
    submit_expert_decision,
)
from shared_scenarios import material as m

from hvs_catalog.runtime import resume_execution
from hvs_catalog.workspace import (
    initialize_workspace,
)

pytestmark = pytest.mark.integration

HELPERS = Path(__file__).parent / "runtime_helpers.py"


def run_scenario(name: str, *args: object, timeout: float = 60.0):
    """在独立进程中运行驱动场景，返回 (返回码, JSON 结果)。"""
    result = subprocess.run(
        [sys.executable, str(HELPERS), name, *map(str, args)],
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    payload = None
    lines = [line for line in result.stdout.splitlines() if line.strip()]
    if lines:
        payload = json.loads(lines[-1])
    return result.returncode, payload


def wait_for_file(path: Path, proc: subprocess.Popen, timeout: float = 30.0) -> None:
    """等待子进程写出的信号文件；子进程提前退出立即失败。"""
    deadline = time.monotonic() + timeout
    while not path.exists():
        assert proc.poll() is None, f"子进程提前退出（{proc.returncode}）"
        assert time.monotonic() < deadline, f"等待信号文件超时：{path}"
        time.sleep(0.02)


def release_holder(holder: subprocess.Popen, release: Path) -> None:
    """测试结束时放行并回收持锁子进程。"""
    if holder.poll() is None:
        release.write_text("go", encoding="utf-8")
        holder.communicate(timeout=30)


@pytest.fixture()
def service(tmp_path):
    svc = open_service(tmp_path / "calls.jsonl")
    yield svc
    svc.close()


def _ledger_calls(tmp_path, step: str | None = None) -> int:
    service = open_service(tmp_path / "calls.jsonl")
    return service.summary()["calls"] if step is None else service.calls_for_step(step)


def _receipt_kinds(space, execution_id: str) -> list[str]:
    return [
        receipt.kind
        for receipt in space.store().find_receipts(execution_id=execution_id)
    ]


def _paper_revisions(space, record_id: str) -> list[int]:
    return sorted(
        record.revision
        for record in space.store().list_records()
        if record.record_id == record_id
    )


def _step_result_rows(space) -> int:
    conn = sqlite3.connect(f"file:{space.root / 'business.db'}?mode=ro", uri=True)
    try:
        return int(conn.execute("SELECT COUNT(*) FROM step_results").fetchone()[0])
    finally:
        conn.close()


def _question_ref_json(space, execution_id: str) -> str:
    view = pending_questions(space, execution_id=execution_id)[0]
    return json.dumps(view.question_ref.model_dump(mode="json"))


# ---- A03 双进程锁竞争 ----


def test_two_processes_race_same_execution(tmp_path, service):
    """一个进程推进时另一进程返回忙；无重复调用、步骤结果或决定。"""
    space = initialize_workspace(tmp_path / "space")
    holder = None
    try:
        execution, _flow, _ = start_execution(space, paper="B", request_key="b")
        marker = tmp_path / "responded.marker"
        release = tmp_path / "release.marker"
        holder = subprocess.Popen(
            [
                sys.executable,
                str(HELPERS),
                "resume-block",
                tmp_path / "space",
                execution.execution_id,
                "B",
                tmp_path / "calls.jsonl",
                marker,
                release,
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        # 持锁进程已收到名单响应：产生一次服务调用且锁保持持有
        wait_for_file(marker, holder)

        # 竞争进程：同空间同执行推进被拒绝为忙，不产生任何副作用
        code, payload = run_scenario(
            "resume",
            tmp_path / "space",
            execution.execution_id,
            "B",
            tmp_path / "calls.jsonl",
        )
        assert code == 0
        assert payload == {"busy": True}
        assert _ledger_calls(tmp_path, "direct_roster") == 1
        assert "decision_save" not in _receipt_kinds(space, execution.execution_id)

        release.write_text("go", encoding="utf-8")
        out, _err = holder.communicate(timeout=30)
        assert holder.returncode == 0
        assert json.loads(out.strip().splitlines()[-1])["outcome"] == "finished"

        # 持锁进程完成后重入：报告已完成，不再重复调用或保存
        code, payload = run_scenario(
            "resume",
            tmp_path / "space",
            execution.execution_id,
            "B",
            tmp_path / "calls.jsonl",
        )
        assert payload["outcome"] == "finished"
        assert payload["mode"] == "already-finished"
        assert _ledger_calls(tmp_path) == 2  # 名单 + 贡献，各只一次
        assert _step_result_rows(space) == 3
    finally:
        if holder is not None:
            release_holder(holder, release)
        space.close()


# ---- A04 持锁进程终止、另一空间独立运行 ----


def test_lock_holder_killed_and_other_space_independent(tmp_path, service):
    space1 = initialize_workspace(tmp_path / "space1")
    space2 = initialize_workspace(tmp_path / "space2")
    holder = None
    try:
        execution1, _, _ = start_execution(space1, paper="B", request_key="b1")
        execution2, _, _ = start_execution(space2, paper="B", request_key="b2")
        held = tmp_path / "held.marker"
        release = tmp_path / "release.marker"
        holder = subprocess.Popen(
            [
                sys.executable,
                str(HELPERS),
                "hold-lock",
                tmp_path / "space1",
                held,
                release,
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        wait_for_file(held, holder)

        # 另一空间不受影响：独立完成自己的执行
        _code, payload = run_scenario(
            "resume",
            tmp_path / "space2",
            execution2.execution_id,
            "B",
            tmp_path / "calls2.jsonl",
        )
        assert payload["outcome"] == "finished"
        assert _ledger_calls(tmp_path) == 0  # space2 使用独立调用日志

        _code, payload = run_scenario(
            "resume",
            tmp_path / "space1",
            execution1.execution_id,
            "B",
            tmp_path / "calls.jsonl",
        )
        assert payload == {"busy": True}
        # 持锁进程被杀死后正常再取锁，无持久身份或修复步骤。
        holder.send_signal(signal.SIGKILL)
        holder.communicate(timeout=30)
        assert (space1.root / ".lock").exists()
        _code, payload = run_scenario(
            "resume",
            tmp_path / "space1",
            execution1.execution_id,
            "B",
            tmp_path / "calls.jsonl",
        )
        assert payload["outcome"] == "finished"
        assert _ledger_calls(tmp_path) == 2
    finally:
        if holder is not None:
            release_holder(holder, release)
        space1.close()
        space2.close()


# ---- A15 四个中断窗口 ----


def test_exit_before_decision_save(tmp_path, service):
    """决定保存前退出：无半份决定，问题仍待答复，随后可正常继续。"""
    space = initialize_workspace(tmp_path / "space")
    try:
        execution, _flow, _ = start_execution(space, paper="A", request_key="a")
        code, _payload = run_scenario(
            "resume-pause-exit",
            tmp_path / "space",
            execution.execution_id,
            "A",
            tmp_path / "calls.jsonl",
        )
        assert code == 9
        view = pending_questions(space, execution_id=execution.execution_id)[0]
        assert view.status == "open"
        assert "decision_save" not in _receipt_kinds(space, execution.execution_id)
        assert (
            space.store().current_revision(
                m.paper_record_id("A", execution.execution_id)
            )
            == 1
        )

        # 同一问题随后仍可答复并完成
        submit_expert_decision(
            space,
            execution_id=execution.execution_id,
            question_ref=view.question_ref,
            request_key="dec-after-recovery",
        )
        svc = open_service(tmp_path / "calls.jsonl")
        try:
            report = resume_execution(
                space, execution.execution_id, direct_flow(), service=svc
            )
            assert report.outcome == "finished"
        finally:
            svc.close()
    finally:
        space.close()


def test_death_inside_decision_save_rolls_back_all_staged_rows(tmp_path, service):
    space = initialize_workspace(tmp_path / "space")
    try:
        execution, flow, _ = start_execution(space, paper="A", request_key="a")
        assert (
            resume_execution(
                space, execution.execution_id, flow, service=service
            ).outcome
            == "paused"
        )
        code, _ = run_scenario(
            "exit-in-decision-save",
            space.root,
            execution.execution_id,
            _question_ref_json(space, execution.execution_id),
            "dec",
        )
        assert code == 9
        question = pending_questions(space, execution_id=execution.execution_id)[0]
        assert question.status == "open" and not question.decisions
        assert "decision_save" not in _receipt_kinds(space, execution.execution_id)
        assert not [
            r for r in space.store().list_records() if r.RECORD_NAME == "decision"
        ]
        code, _ = run_scenario(
            "submit-exit",
            space.root,
            execution.execution_id,
            _question_ref_json(space, execution.execution_id),
            "dec",
        )
        assert code == 9
        code, report = run_scenario(
            "resume", space.root, execution.execution_id, "A", tmp_path / "calls.jsonl"
        )
        assert code == 0 and report["outcome"] == "finished"
    finally:
        space.close()


def test_exit_after_decision_save_before_apply(tmp_path, service):
    """保存后未应用退出：决定可查，结果未提交，新进程应用后完成。"""
    space = initialize_workspace(tmp_path / "space")
    try:
        execution, _flow, _ = start_execution(space, paper="A", request_key="a")
        code, payload = run_scenario(
            "resume",
            tmp_path / "space",
            execution.execution_id,
            "A",
            tmp_path / "calls.jsonl",
        )
        assert payload["outcome"] == "paused"
        code, _payload = run_scenario(
            "submit-exit",
            tmp_path / "space",
            execution.execution_id,
            _question_ref_json(space, execution.execution_id),
            "dec",
        )
        assert code == 9
        kinds = _receipt_kinds(space, execution.execution_id)
        assert "decision_save" in kinds
        assert "decision_apply" not in kinds
        view = pending_questions(space, execution_id=execution.execution_id)[0]
        assert view.status == "answered"
        assert (
            space.store().current_revision(
                m.paper_record_id("A", execution.execution_id)
            )
            == 1
        )  # 未提交结果不出现

        code, payload = run_scenario(
            "resume",
            tmp_path / "space",
            execution.execution_id,
            "A",
            tmp_path / "calls.jsonl",
        )
        assert payload["outcome"] == "finished"
        assert (
            space.store().current_revision(
                m.paper_record_id("A", execution.execution_id)
            )
            == 2
        )
    finally:
        space.close()


def test_exit_inside_apply_transaction(tmp_path, service):
    """应用事务提交前硬退出：结果不出现，决定完好，新进程重新应用一次。"""
    space = initialize_workspace(tmp_path / "space")
    try:
        execution, _flow, _ = start_execution(space, paper="A", request_key="a")
        code, _payload = run_scenario(
            "resume",
            tmp_path / "space",
            execution.execution_id,
            "A",
            tmp_path / "calls.jsonl",
        )
        assert code == 0
        question_ref = _question_ref_json(space, execution.execution_id)
        code, _payload = run_scenario(
            "exit-in-apply",
            tmp_path / "space",
            execution.execution_id,
            "A",
            tmp_path / "calls.jsonl",
            question_ref,
            "dec",
        )
        assert code == 9
        # 事务未提交：无应用回执、无第二份论文修订；决定仍可查
        assert "decision_apply" not in _receipt_kinds(space, execution.execution_id)
        view = pending_questions(space, execution_id=execution.execution_id)[0]
        assert view.status == "answered"
        assert _paper_revisions(
            space, m.paper_record_id("A", execution.execution_id)
        ) == [1]

        code, payload = run_scenario(
            "resume",
            tmp_path / "space",
            execution.execution_id,
            "A",
            tmp_path / "calls.jsonl",
        )
        assert payload["outcome"] == "finished"
        assert _paper_revisions(
            space, m.paper_record_id("A", execution.execution_id)
        ) == [1, 2]  # 只应用一次
    finally:
        space.close()


def test_exit_after_apply_before_resume(tmp_path, service):
    """应用后未恢复退出：结果已提交，恢复后不生成第二份修订。"""
    space = initialize_workspace(tmp_path / "space")
    try:
        execution, _flow, _ = start_execution(space, paper="A", request_key="a")
        run_scenario(
            "resume",
            tmp_path / "space",
            execution.execution_id,
            "A",
            tmp_path / "calls.jsonl",
        )
        question_ref = _question_ref_json(space, execution.execution_id)
        code, _payload = run_scenario(
            "exit-after-apply",
            tmp_path / "space",
            execution.execution_id,
            "A",
            tmp_path / "calls.jsonl",
            question_ref,
            "dec",
        )
        assert code == 9
        assert (
            space.store().current_revision(
                m.paper_record_id("A", execution.execution_id)
            )
            == 2
        )  # 已提交结果保留

        code, payload = run_scenario(
            "resume",
            tmp_path / "space",
            execution.execution_id,
            "A",
            tmp_path / "calls.jsonl",
        )
        assert payload["outcome"] == "finished"
        assert _paper_revisions(
            space, m.paper_record_id("A", execution.execution_id)
        ) == [1, 2]
    finally:
        space.close()


# ---- A17 响应后、保存前退出：外部调用可能重复 ----


def test_exit_after_response_calls_again_on_resume(tmp_path, service):
    space = initialize_workspace(tmp_path / "space")
    try:
        execution, _flow, _ = start_execution(space, paper="B", request_key="b")
        code, _payload = run_scenario(
            "exit-after-response",
            tmp_path / "space",
            execution.execution_id,
            "B",
            tmp_path / "calls.jsonl",
            "direct_roster",
        )
        assert code == 9
        # 日志独立于空间：已发生调用事实保留，但业务库没有步骤结果
        assert _ledger_calls(tmp_path, "direct_roster") == 1

        code, payload = run_scenario(
            "resume",
            tmp_path / "space",
            execution.execution_id,
            "B",
            tmp_path / "calls.jsonl",
        )
        assert payload["outcome"] == "finished"
        # 恢复重新调用了名单步骤：外部调用发生两次，不能宣称只发生一次
        assert _ledger_calls(tmp_path, "direct_roster") == 2
        assert (
            space.store().current_revision(
                m.paper_record_id("B", execution.execution_id)
            )
            == 1
        )
    finally:
        space.close()

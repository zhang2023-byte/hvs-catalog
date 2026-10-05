"""Independent-process tests: lock contention, termination while holding a lock, and
interruption windows (A03/A04/A15/A17).

Locate windows through signal files, then terminate using os._exit or SIGKILL. Committed
facts survive and uncommitted transactions vanish. A17 call logs are independent of the
workspace, so client rollback cannot erase call facts.
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
    """Run a driver scenario in an independent process and return (exit code, JSON result)."""
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
    """Wait for a child signal file; fail immediately if the child exits early."""
    deadline = time.monotonic() + timeout
    while not path.exists():
        assert proc.poll() is None, f"子进程提前退出（{proc.returncode}）"
        assert time.monotonic() < deadline, f"等待信号文件超时：{path}"
        time.sleep(0.02)


def release_holder(holder: subprocess.Popen, release: Path) -> None:
    """Release and reap the lock-holding child at test cleanup."""
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


# ---- A03: two-process lock contention ----


def test_two_processes_race_same_execution(tmp_path, service):
    """While one process advances, another reports busy with no duplicate calls, results,
    or decisions.
    """
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
        # The lock holder received a roster response after one service call and still retains the lock.
        wait_for_file(marker, holder)

        # The competing process cannot advance the same execution/workspace and has no side effects.
        code, payload = run_scenario(
            "resume",
            tmp_path / "space",
            execution.execution_id,
            "B",
            tmp_path / "calls.jsonl",
        )
        assert code == 0
        assert payload == {"busy": True}
        assert _ledger_calls(tmp_path, "roster") == 1
        assert "decision_save" not in _receipt_kinds(space, execution.execution_id)

        release.write_text("go", encoding="utf-8")
        out, _err = holder.communicate(timeout=30)
        assert holder.returncode == 0
        assert json.loads(out.strip().splitlines()[-1])["outcome"] == "finished"

        # After the holder finishes, reentry reports completion without repeated calls/saves.
        code, payload = run_scenario(
            "resume",
            tmp_path / "space",
            execution.execution_id,
            "B",
            tmp_path / "calls.jsonl",
        )
        assert payload["outcome"] == "finished"
        assert payload["mode"] == "already-finished"
        assert (
            _ledger_calls(tmp_path) == 2
        )  # One roster call and one contribution call.
        assert _step_result_rows(space) == 3
    finally:
        if holder is not None:
            release_holder(holder, release)
        space.close()


# ---- A04: terminate the lock holder while another workspace runs independently ----


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

        # Another workspace completes its own execution unaffected.
        _code, payload = run_scenario(
            "resume",
            tmp_path / "space2",
            execution2.execution_id,
            "B",
            tmp_path / "calls2.jsonl",
        )
        assert payload["outcome"] == "finished"
        assert _ledger_calls(tmp_path) == 0  # space2 has an independent call log.

        _code, payload = run_scenario(
            "resume",
            tmp_path / "space1",
            execution1.execution_id,
            "B",
            tmp_path / "calls.jsonl",
        )
        assert payload == {"busy": True}
        # After killing the holder, acquire normally without persistent ownership or repair steps.
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


# ---- A15: four interruption windows ----


def test_exit_before_decision_save(tmp_path, service):
    """Exit before decision save: no partial decision; the question remains answerable and
    can continue normally.
    """
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

        # Answer the same question later and finish.
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
    """Exit after save but before apply: the decision survives, results are absent, and a
    fresh process applies/completes.
    """
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
        )  # Uncommitted results are absent.

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
    """Hard exit before application commit leaves no results; retain the decision and
    reapply once in a fresh process.
    """
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
        # No commit means no application receipt or second paper revision; the decision survives.
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
        ) == [1, 2]  # Apply exactly once.
    finally:
        space.close()


def test_exit_after_apply_before_resume(tmp_path, service):
    """Exit after application but before recovery retains results without creating another
    revision after recovery.
    """
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
        )  # Retain committed results.

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


# ---- A17: exit after response but before save; external calls may repeat ----


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
            "roster",
        )
        assert code == 9
        # Independent logs retain call facts while the business database has no step results.
        assert _ledger_calls(tmp_path, "roster") == 1

        code, payload = run_scenario(
            "resume",
            tmp_path / "space",
            execution.execution_id,
            "B",
            tmp_path / "calls.jsonl",
        )
        assert payload["outcome"] == "finished"
        # Recovery calls the roster again: two external calls occurred, so never claim one.
        assert _ledger_calls(tmp_path, "roster") == 2
        assert (
            space.store().current_revision(
                m.paper_record_id("B", execution.execution_id)
            )
            == 1
        )
    finally:
        space.close()

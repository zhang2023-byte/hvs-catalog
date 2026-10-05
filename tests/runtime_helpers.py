"""Drivers for subprocess interruption and lock contention.

Each scenario prints one JSON line after its fixed actions. Signal files coordinate
failure windows so the parent can terminate or release the child at the exact point.
``os._exit`` skips cleanup, like a killed process: committed filesystem and SQLite state
survives; uncommitted transactions are discarded.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

from shared_scenarios import (
    direct_flow,
    open_service,
    submit_expert_decision,
)

from hvs_catalog.records.common import RecordRef
from hvs_catalog.runtime import resume_execution
from hvs_catalog.workspace import WorkspaceBusyError, open_workspace

WAIT_TIMEOUT = 60.0


def _wait_for(path: Path) -> None:
    deadline = time.monotonic() + WAIT_TIMEOUT
    while not path.exists():
        if time.monotonic() > deadline:
            raise TimeoutError(f"等待信号文件超时：{path}")
        time.sleep(0.02)


def _exit9() -> None:
    os._exit(9)


def _space(root: str):
    return open_workspace(Path(root))


def _resume(space, execution_id: str, paper: str, ledger: str, hooks=None):
    service = open_service(ledger)
    try:
        report = resume_execution(
            space,
            execution_id,
            direct_flow(fault_hooks=hooks),
            service=service,
        )
        return {
            "outcome": report.outcome,
            "mode": report.mode,
        }
    except WorkspaceBusyError:
        return {"busy": True}
    finally:
        service.close()


def _submit(space, execution_id: str, question_ref_json: str, request_key: str):
    receipt = submit_expert_decision(
        space,
        execution_id=execution_id,
        question_ref=RecordRef.model_validate(json.loads(question_ref_json)),
        request_key=request_key,
    )
    return {"first_save": receipt.first_save}


# ---- Scenarios ----


def scenario_hold_lock(root: str, signal: str, release: str) -> dict:
    """Hold the workspace lock until released; signal ownership to the parent through a
    file.
    """
    space = _space(root)
    with space.lock():
        Path(signal).write_text("held", encoding="utf-8")
        _wait_for(Path(release))
    space.close()
    return {"held": True}


def scenario_resume(root: str, execution_id: str, paper: str, ledger: str) -> dict:
    space = _space(root)
    result = _resume(space, execution_id, paper, ledger)
    space.close()
    return result


def scenario_resume_block(
    root: str, execution_id: str, paper: str, ledger: str, marker: str, release: str
) -> dict:
    """Advance execution and block after the roster response, retaining the lock and
    signaling through a file.
    """

    def block_after_roster():
        Path(marker).write_text("responded", encoding="utf-8")
        _wait_for(Path(release))

    space = _space(root)
    result = _resume(
        space,
        execution_id,
        paper,
        ledger,
        hooks={"after_response:roster": block_after_roster},
    )
    space.close()
    return result


def scenario_resume_pause_exit(
    root: str, execution_id: str, paper: str, ledger: str
) -> dict:
    """Exit immediately after reaching expert wait, before saving a decision."""
    space = _space(root)
    report = resume_execution(
        space, execution_id, direct_flow(), service=open_service(ledger)
    )
    assert report.outcome == "paused"
    os._exit(9)


def scenario_submit_exit(
    root: str, execution_id: str, question_ref_json: str, request_key: str
) -> dict:
    """Exit immediately after saving a decision, before applying it."""
    space = _space(root)
    _submit(space, execution_id, question_ref_json, request_key)
    os._exit(9)


def scenario_exit_in_decision_save(root, execution_id, question_ref_json, request_key):
    """Kill after decision/question rows are staged, before the save commits."""
    import hvs_catalog.workspace.store as store_mod
    from hvs_catalog.review.service import DECISION_SAVE_KIND

    original = store_mod.StoreTransaction

    class ExitInSave(original):
        def insert_receipt(self, receipt):
            if receipt.kind == DECISION_SAVE_KIND:
                os._exit(9)
            super().insert_receipt(receipt)

    store_mod.StoreTransaction = ExitInSave
    _submit(_space(root), execution_id, question_ref_json, request_key)
    raise AssertionError("decision-save transaction must terminate")


def scenario_exit_in_apply(
    root: str,
    execution_id: str,
    paper: str,
    ledger: str,
    question_ref_json: str,
    request_key: str,
) -> dict:
    """Exit abruptly before the decision application business transaction commits."""
    import hvs_catalog.workspace.store as store_mod
    from hvs_catalog.review.service import DECISION_APPLY_KIND

    original = store_mod.StoreTransaction

    class ExitInApply(original):
        def insert_receipt(self, receipt) -> None:
            if receipt.kind == DECISION_APPLY_KIND:
                os._exit(9)
            super().insert_receipt(receipt)

    store_mod.StoreTransaction = ExitInApply
    space = _space(root)
    _submit(space, execution_id, question_ref_json, request_key)
    _resume(space, execution_id, paper, ledger)
    raise AssertionError("应用事务中退出场景不应运行到这里")


def scenario_exit_after_apply(
    root: str,
    execution_id: str,
    paper: str,
    ledger: str,
    question_ref_json: str,
    request_key: str,
) -> dict:
    """Exit abruptly after the application transaction commits, before flow resumes."""
    space = _space(root)
    _submit(space, execution_id, question_ref_json, request_key)
    _resume(space, execution_id, paper, ledger, hooks={"after_apply": _exit9})
    raise AssertionError("应用后退出场景不应运行到这里")


def scenario_exit_after_response(
    root: str, execution_id: str, paper: str, ledger: str, step: str
) -> dict:
    """Simulate abrupt exit after the service records a response but before business state
    is saved; an external call may repeat.
    """
    space = _space(root)
    _resume(
        space, execution_id, paper, ledger, hooks={f"after_response:{step}": _exit9}
    )
    raise AssertionError("响应后退出场景不应运行到这里")


SCENARIOS = {
    "hold-lock": scenario_hold_lock,
    "resume": scenario_resume,
    "resume-block": scenario_resume_block,
    "resume-pause-exit": scenario_resume_pause_exit,
    "submit-exit": scenario_submit_exit,
    "exit-in-decision-save": scenario_exit_in_decision_save,
    "exit-in-apply": scenario_exit_in_apply,
    "exit-after-apply": scenario_exit_after_apply,
    "exit-after-response": scenario_exit_after_response,
}


def main(argv: list[str]) -> None:
    scenario = argv[1]
    result = SCENARIOS[scenario](*argv[2:])
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main(sys.argv)

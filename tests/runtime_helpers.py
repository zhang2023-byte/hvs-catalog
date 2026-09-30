"""子进程中断与锁竞争驱动：测试以独立进程运行这些场景。

每个场景完成固定动作后打印单行 JSON 结果；配合信号文件定位故障
窗口（父子进程通信），由父测试进程在准确的时点结束或放行子进程。
``os._exit`` 跳过一切清理，等价于进程被杀死：文件系统与 SQLite 已
提交事实保留，未提交事务丢弃。
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


# ---- 场景 ----


def scenario_hold_lock(root: str, signal: str, release: str) -> dict:
    """持有空间锁直到收到放行信号；持锁事实用信号文件告知父进程。"""
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
    """推进执行并在收到名单响应后阻塞：锁保持持有，用标记文件告知。"""

    def block_after_roster():
        Path(marker).write_text("responded", encoding="utf-8")
        _wait_for(Path(release))

    space = _space(root)
    result = _resume(
        space,
        execution_id,
        paper,
        ledger,
        hooks={"after_response:direct_roster": block_after_roster},
    )
    space.close()
    return result


def scenario_resume_pause_exit(
    root: str, execution_id: str, paper: str, ledger: str
) -> dict:
    """推进到等待专家答复后立即退出（决定保存前中断）。"""
    space = _space(root)
    report = resume_execution(
        space, execution_id, direct_flow(), service=open_service(ledger)
    )
    assert report.outcome == "paused"
    os._exit(9)


def scenario_submit_exit(
    root: str, execution_id: str, question_ref_json: str, request_key: str
) -> dict:
    """提交决定保存成功后立即退出（保存后、应用前中断）。"""
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
    """在应用决定的单次业务事务提交前硬退出（应用事务中中断）。"""
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
    """决定应用事务提交成功后、流程恢复前硬退出。"""
    space = _space(root)
    _submit(space, execution_id, question_ref_json, request_key)
    _resume(space, execution_id, paper, ledger, hooks={"after_apply": _exit9})
    raise AssertionError("应用后退出场景不应运行到这里")


def scenario_exit_after_response(
    root: str, execution_id: str, paper: str, ledger: str, step: str
) -> dict:
    """模拟服务已记录响应、业务保存前硬退出（外部调用可能重复）。"""
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

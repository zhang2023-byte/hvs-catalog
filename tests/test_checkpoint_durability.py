"""检查点必须先于外部调用落盘；真实硬退出后仍能从固定输入恢复。"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from hvs_catalog.demo import graph as demo_graph
from hvs_catalog.demo import runner
from hvs_catalog.runtime import resume_execution
from hvs_catalog.synthetic import open_service, start_execution, submit_expert_decision
from hvs_catalog.synthetic.flow import SyntheticFlow
from hvs_catalog.workspace import initialize_workspace, open_workspace

# 只延迟实际 SQLite 检查点提交，不伪造 get_state 或业务结果。
# legacy-demo 强制使用旧的异步模式，构造只有 input 检查点的历史空间。
DRIVER = """
import json
import os
import sys
import threading
from pathlib import Path
from hvs_catalog.demo import runner
from hvs_catalog.runtime import recovery
from hvs_catalog.synthetic import open_service, start_execution
from hvs_catalog.synthetic.flow import SyntheticFlow
from hvs_catalog.workspace import initialize_workspace

mode, root = sys.argv[1], Path(sys.argv[2])
entered, saved = threading.Event(), threading.Event()
base = runner.SqliteSaver
class DelayedSaver(base):
    def put(self, config, checkpoint, metadata, new_versions):
        target = metadata.get('step') == 0
        if target:
            entered.set()
            threading.Event().wait(3)
        result = super().put(config, checkpoint, metadata, new_versions)
        if target:
            saved.set()
        return result
runner.SqliteSaver = recovery.SqliteSaver = DelayedSaver

if mode == 'legacy-demo':
    original_graph = runner.DemoSpace.graph
    class AsyncGraph:
        def __init__(self, graph): self.graph = graph
        def get_state(self, *args, **kwargs): return self.graph.get_state(*args, **kwargs)
        def invoke(self, *args, **kwargs):
            kwargs['durability'] = 'async'
            return self.graph.invoke(*args, **kwargs)
    runner.DemoSpace.graph = lambda self, **kw: AsyncGraph(original_graph(self, **kw))

def crash(*args):
    assert entered.wait(10)
    (root / 'checkpoint-at-response.json').write_text(json.dumps({'saved':saved.is_set()}))
    os._exit(70)

if mode == 'runtime':
    space = initialize_workspace(root)
    execution, _, _ = start_execution(space, paper='B', request_key='durable')
    (root / 'execution-id.txt').write_text(execution.execution_id)
    recovery.resume_execution(space, execution.execution_id,
        SyntheticFlow(paper='B', fault_hooks={'after_response:extract_roster':crash}),
        service=open_service(root.parent / 'ledger.db'))
else:
    runner.run(root, after_response=crash)
raise AssertionError('hard exit expected')
"""


def _crash(tmp_path: Path, mode: str) -> Path:
    driver = tmp_path / "checkpoint_driver.py"
    driver.write_text(DRIVER, encoding="utf-8")
    root = tmp_path / "space"
    result = subprocess.run(
        [sys.executable, str(driver), mode, str(root)],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 70, result.stdout + result.stderr
    return root


@pytest.mark.parametrize("mode", ["demo", "runtime"])
def test_checkpoint_committed_before_response_and_hard_exit(tmp_path, mode):
    root = _crash(tmp_path, mode)
    assert json.loads((root / "checkpoint-at-response.json").read_text()) == {
        "saved": True
    }
    if mode == "demo":
        status = runner.status(root)
        assert status.checkpoint_next == (demo_graph.STEP_ROSTER,)
        assert status.results == []
        report = runner.run(root)
        assert report.mode == "resumed-after-exit"
        assert report.end_state == "awaiting-decision"
        assert report.billing["calls"] == 3
        assert runner.decide(root, choice="keep-separate").completed
    else:
        space = open_workspace(root)
        service = open_service(tmp_path / "ledger.db")
        try:
            execution_id = (root / "execution-id.txt").read_text()
            report = resume_execution(
                space, execution_id, SyntheticFlow(paper="B"), service=service
            )
            assert report.outcome == "finished"
            assert report.errors == []
            before = service.summary()["calls"]
            assert (
                resume_execution(
                    space, execution_id, SyntheticFlow(paper="B"), service=service
                ).mode
                == "already-finished"
            )
            assert service.summary()["calls"] == before
        finally:
            service.close()
            space.close()


def test_legacy_input_checkpoint_is_not_finished_and_resumes_fixed_input(
    tmp_path, monkeypatch
):
    root = _crash(tmp_path, "legacy-demo")
    assert json.loads((root / "checkpoint-at-response.json").read_text()) == {
        "saved": False
    }
    status = runner.status(root)
    assert status.started and not status.checkpoint_next
    assert status.results == [] and status.summary is None
    assert "流程已全部执行完毕" not in "\n".join(status.lines())
    assert "需恢复核对" in "\n".join(status.lines())

    def forbid_current_input():
        pytest.fail("恢复不能重新构造当前输入")

    monkeypatch.setattr(demo_graph, "initial_state", forbid_current_input)
    report = runner.run(root)
    assert report.mode == "resumed-after-exit"
    assert report.end_state == "awaiting-decision"
    assert report.billing["calls"] == 3
    assert runner.decide(root, choice="keep-separate").completed


def test_finished_checkpoint_without_business_summary_is_not_success(
    tmp_path, monkeypatch
):
    from hvs_catalog.demo import store

    root = tmp_path / "demo"
    runner.run(root)
    runner.decide(root, choice="keep-separate")
    calls = runner.status(root).billing["calls"]
    monkeypatch.setattr(store.BusinessStore, "find_summary", lambda *_: None)
    assert "流程已全部执行完毕" not in "\n".join(runner.status(root).lines())
    with pytest.raises(ValueError, match="不能报告完成"):
        runner.run(root)
    assert runner.status(root).billing["calls"] == calls


def test_decision_rejects_missing_business_summary_after_final_checkpoint(
    tmp_path, monkeypatch
):
    from hvs_catalog.demo import store

    root = tmp_path / "demo"
    assert runner.run(root).end_state == "awaiting-decision"
    save = store.BusinessStore.save_summary

    def lose_summary(self, paper_id, summary):
        result = save(self, paper_id, summary)
        self._conn.execute("DELETE FROM summaries WHERE paper_id = ?", (paper_id,))
        self._conn.commit()
        return result

    monkeypatch.setattr(store.BusinessStore, "save_summary", lose_summary)
    with pytest.raises(ValueError, match="裁决恢复后业务汇总缺失"):
        runner.decide(root, choice="keep-separate")
    status = runner.status(root)
    assert status.started and not status.checkpoint_next
    assert status.summary is None
    assert "流程已全部执行完毕" not in "\n".join(status.lines())
    assert len(status.decisions) == 1
    assert status.billing["calls"] == 2


def test_initial_and_decision_invocations_use_synchronous_durability(
    tmp_path, monkeypatch
):
    from langgraph.graph.state import CompiledStateGraph

    invoke = CompiledStateGraph.invoke
    calls = []

    def check_durability(self, *args, **kwargs):
        assert kwargs.get("durability") == "sync"
        calls.append(type(args[0]).__name__)
        return invoke(self, *args, **kwargs)

    monkeypatch.setattr(CompiledStateGraph, "invoke", check_durability)
    root = tmp_path / "demo"
    runner.run(root)
    runner.decide(root, choice="keep-separate")
    space = initialize_workspace(tmp_path / "runtime")
    service = open_service(tmp_path / "ledger.db")
    try:
        execution, flow, _ = start_execution(space, paper="A", request_key="durable")
        paused = resume_execution(space, execution.execution_id, flow, service=service)
        assert paused.outcome == "paused"
        submit_expert_decision(
            space,
            execution_id=execution.execution_id,
            question_ref=paused.pending_question.question_ref,
            request_key="decision",
        )
        assert (
            resume_execution(
                space, execution.execution_id, flow, service=service
            ).outcome
            == "finished"
        )
    finally:
        service.close()
        space.close()
    assert calls == ["dict", "Command", "dict", "Command"]

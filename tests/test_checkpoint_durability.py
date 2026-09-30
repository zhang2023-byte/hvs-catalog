"""Synchronous shared checkpoints survive response-window process death."""

import json
import subprocess
import sys

from shared_scenarios import (
    direct_flow,
    open_service,
    start_execution,
    submit_expert_decision,
)

from hvs_catalog.runtime import resume_execution
from hvs_catalog.workspace import initialize_workspace, open_workspace

DRIVER = """
import json, os, sys, threading
from pathlib import Path
from hvs_catalog.runtime import recovery
from hvs_catalog.workspace import initialize_workspace
from shared_scenarios import direct_flow, open_service, start_execution
root = Path(sys.argv[1])
entered, saved, release = threading.Event(), threading.Event(), threading.Event()
base = recovery.SqliteSaver
class DelayedSaver(base):
    def put(self, config, checkpoint, metadata, new_versions):
        target = metadata.get('step') == 0
        if target:
            entered.set()
            assert release.wait(10)
        result = super().put(config, checkpoint, metadata, new_versions)
        if target: saved.set()
        return result
recovery.SqliteSaver = DelayedSaver
# Release precisely after put enters; no fixed sleep to guess the checkpoint.
def unblock():
    assert entered.wait(10)
    release.set()
threading.Thread(target=unblock, daemon=True).start()
space = initialize_workspace(root)
execution, _, _ = start_execution(space, paper='B', request_key='durable')
(root / 'execution-id.txt').write_text(execution.execution_id)
def crash():
    (root / 'checkpoint-at-response.json').write_text(json.dumps({'saved': saved.is_set()}))
    os._exit(70)
recovery.resume_execution(space, execution.execution_id,
    direct_flow(fault_hooks={'after_response:direct_roster':crash}),
    service=open_service(root.parent / 'calls.jsonl'))
raise AssertionError('hard exit expected')
"""


def test_checkpoint_committed_before_response_and_hard_exit(tmp_path):
    driver = tmp_path / "checkpoint.py"
    driver.write_text(DRIVER)
    root = tmp_path / "space"
    result = subprocess.run(
        [sys.executable, str(driver), str(root)],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 70, result.stdout + result.stderr
    assert json.loads((root / "checkpoint-at-response.json").read_text()) == {
        "saved": True
    }
    space = open_workspace(root)
    try:
        service = open_service(tmp_path / "calls.jsonl")
        execution_id = (root / "execution-id.txt").read_text()
        report = resume_execution(space, execution_id, direct_flow(), service=service)
        assert report.outcome == "finished"
        assert service.summary()["calls"] == 3
        assert (
            resume_execution(space, execution_id, direct_flow(), service=service).mode
            == "already-finished"
        )
        assert service.summary()["calls"] == 3
    finally:
        space.close()


def test_initial_and_decision_invocations_use_synchronous_durability(
    tmp_path, monkeypatch
):
    from langgraph.graph.state import CompiledStateGraph

    invoke = CompiledStateGraph.invoke
    calls = []

    def check(self, *args, **kwargs):
        assert kwargs.get("durability") == "sync"
        calls.append(type(args[0]).__name__)
        return invoke(self, *args, **kwargs)

    monkeypatch.setattr(CompiledStateGraph, "invoke", check)
    space = initialize_workspace(tmp_path / "space")
    try:
        service = open_service(tmp_path / "calls.jsonl")
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
        space.close()
    assert calls == ["dict", "Command"]

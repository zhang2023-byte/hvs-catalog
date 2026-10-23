"""Roster recovery uses original calls, exact references, and bounded allowances."""

import hashlib
import json
import subprocess
import sys
import threading

import pytest
from llm_helpers import completion, endpoint
from pydantic import SecretStr
from test_roster import ScriptedAdapter, run, submission

from hvs_catalog.llm.managed import CallService
from hvs_catalog.pipeline import (
    ExtractionTask,
    resume_extraction,
    resume_extractions,
    start_roster_extraction,
)
from hvs_catalog.pipeline.network_retry import continue_format_corrections
from hvs_catalog.pipeline.roster import saved_roster
from hvs_catalog.workspace import (
    OperationKeyConflictError,
    RecoveryInconsistentError,
    WorkspaceIntegrityError,
    backup_workspace,
)


def test_changed_key_input_and_finished_reentry(prepared):
    space, prep, config = prepared
    handle, report, adapter, _ = run(prepared, [submission()])
    assert report.outcome == "finished"
    with pytest.raises(OperationKeyConflictError):
        start_roster_extraction(
            space,
            preparation_execution_id=prep,
            request_key="extract",
            configuration=config.model_copy(update={"max_format_corrections": 0}),
        )
    assert resume_extraction(space, handle.execution_id).outcome == "finished"
    assert len(adapter.seen) == 1


def test_continuation_keeps_initial_history_and_total_allowance(prepared):
    config = prepared[2].model_copy(update={"max_format_corrections": 0})
    bad = submission()
    bad["notes"] = None
    handle, report, adapter, _ = run(prepared, [bad], config=config)
    assert report.outcome == "finished"
    assert report.result_status != "complete"
    space = prepared[0]
    continue_format_corrections(
        space, handle.execution_id, request_key="extend", maximum=2
    )
    correction = ScriptedAdapter([bad, submission()])
    result = resume_extraction(
        space,
        handle.execution_id,
        service=CallService(
            config.runtime, credential=SecretStr("synthetic-key"), adapter=correction
        ),
    )
    assert result.outcome == "finished", result.errors
    assert [len(s["messages"]) for s in correction.seen] == [5, 8]
    assert len(adapter.seen) == 1
    assert saved_roster(space, handle.execution_id).data.source_round == 2


def test_exhausted_failure_does_not_reset_allowance(prepared):
    bad = submission()
    bad["notes"] = None
    handle, report, adapter, service = run(prepared, [bad])
    assert report.outcome == "finished"
    assert report.result_status != "complete"
    again = resume_extraction(prepared[0], handle.execution_id, service=service)
    assert again.outcome == "finished"
    assert again.result_status != "complete"
    assert len(adapter.seen) == 3
    with pytest.raises(ValueError):
        continue_format_corrections(
            prepared[0], handle.execution_id, request_key="more"
        )


def test_reasoning_stored_once_and_corruption_blocks_resume_and_backup(
    prepared, tmp_path
):
    bad = submission()
    bad["notes"] = None
    handle, _, _, _ = run(prepared, [bad])
    space = prepared[0]
    reasoning = "Synthetic reasoning for call 1"
    digest = hashlib.sha256(reasoning.encode()).hexdigest()
    shared = space.root / "runs" / "inputs" / "shared" / (digest + ".txt")
    assert shared.read_text() == reasoning
    assert (
        len([p for p in space.root.rglob("*.txt") if p.read_text() == reasoning]) == 1
    )
    shared.write_text("corrupt")
    with pytest.raises(WorkspaceIntegrityError):
        backup_workspace(space, tmp_path / "backup")
    with pytest.raises((WorkspaceIntegrityError, RecoveryInconsistentError)):
        resume_extraction(space, handle.execution_id)


@pytest.mark.integration
@pytest.mark.parametrize(
    "hook,expected_calls,outcome",
    [
        ("after_pending", 0, "failed"),
        ("after_response", 1, "failed"),
        ("after_archive", 1, "finished"),
        ("after_commit", 1, "finished"),
    ],
)
def test_real_process_exit_windows(prepared, hook, expected_calls, outcome):
    space, prep, config = prepared

    def reply(payload, number):
        body = completion(payload)
        body["choices"][0]["message"]["tool_calls"][0]["function"]["arguments"] = (
            json.dumps(submission())
        )
        return {"body": body}

    with endpoint(reply) as (url, seen, _):
        config = config.model_copy(
            update={"runtime": config.runtime.model_copy(update={"base_url": url})}
        )
        handle = start_roster_extraction(
            space,
            preparation_execution_id=prep,
            request_key="crash",
            configuration=config,
        )
        code = """
import os,sys
from pathlib import Path
from pydantic import SecretStr
from hvs_catalog.workspace import open_workspace
from hvs_catalog.pipeline import resume_extraction
from hvs_catalog.pipeline.configuration import stored_configuration
from hvs_catalog.runtime import get_execution
from hvs_catalog.runtime.execution import RuntimeContext
from hvs_catalog.llm.managed import CallService
space=open_workspace(Path(sys.argv[1])); eid=sys.argv[2]; hook=sys.argv[3]
if hook == 'after_commit':
    original=RuntimeContext.commit_step
    def commit(self, **kwargs):
        result=original(self, **kwargs)
        if kwargs['step_id']=='roster': os._exit(70)
        return result
    RuntimeContext.commit_step=commit
service=CallService(stored_configuration(get_execution(space,eid).input_snapshot).runtime,credential=SecretStr('synthetic-key'),hooks={hook:lambda:os._exit(70)})
resume_extraction(space,eid,service=service)
"""
        child = subprocess.run(
            [sys.executable, "-c", code, str(space.root), handle.execution_id, hook],
            check=False,
            capture_output=True,
            text=True,
            timeout=40,
        )
        assert child.returncode == 70, child.stderr
        service = CallService(config.runtime, credential=SecretStr("synthetic-key"))
        report = resume_extraction(space, handle.execution_id, service=service)
        assert report.outcome == outcome, report.errors
        assert len(seen) == expected_calls


@pytest.mark.integration
def test_crash_during_second_submission_preserves_history_and_quota(prepared):
    space, prep, config = prepared
    bad = submission()
    bad["notes"] = None

    def reply(payload, number):
        body = completion(payload)
        body["choices"][0]["message"]["tool_calls"][0]["function"]["arguments"] = (
            json.dumps(bad if number < 3 else submission())
        )
        return {"body": body}

    with endpoint(reply) as (url, seen, _):
        config = config.model_copy(
            update={"runtime": config.runtime.model_copy(update={"base_url": url})}
        )
        handle = start_roster_extraction(
            space,
            preparation_execution_id=prep,
            request_key="second",
            configuration=config,
        )
        code = """
import os,sys
from pathlib import Path
from pydantic import SecretStr
from hvs_catalog.workspace import open_workspace
from hvs_catalog.pipeline import resume_extraction
from hvs_catalog.pipeline.configuration import stored_configuration
from hvs_catalog.runtime import get_execution
from hvs_catalog.llm.managed import CallService
space=open_workspace(Path(sys.argv[1])); eid=sys.argv[2]; count=[0]
def archived():
    count[0]+=1
    if count[0]==2: os._exit(70)
service=CallService(stored_configuration(get_execution(space,eid).input_snapshot).runtime,credential=SecretStr('synthetic-key'),hooks={'after_archive':archived})
resume_extraction(space,eid,service=service)
"""
        child = subprocess.run(
            [sys.executable, "-c", code, str(space.root), handle.execution_id],
            check=False,
            capture_output=True,
            text=True,
            timeout=40,
        )
        assert child.returncode == 70, child.stderr
        report = resume_extraction(
            space,
            handle.execution_id,
            service=CallService(config.runtime, credential=SecretStr("synthetic-key")),
        )
        assert report.outcome == "finished", report.errors
        assert [len(p["messages"]) for p in seen] == [2, 5, 8]
        assert seen[2]["messages"][2]["reasoning_content"] == "Synthetic Ω reasoning."
        assert seen[2]["messages"][5]["reasoning_content"] == "Synthetic Ω reasoning."


@pytest.mark.integration
def test_parallel_rosters_archive_to_their_own_executions(prepared):
    space, prep, config = prepared
    barrier = threading.Barrier(2)

    class ConcurrentAdapter(ScriptedAdapter):
        def send(self, *args):
            barrier.wait(timeout=10)
            return super().send(*args)

    handles = [
        start_roster_extraction(
            space,
            preparation_execution_id=prep,
            request_key=f"parallel-{i}",
            configuration=config,
        )
        for i in range(2)
    ]
    adapters = [ConcurrentAdapter([submission()]) for _ in handles]
    tasks = [
        ExtractionTask(
            space,
            h.execution_id,
            CallService(
                config.runtime, credential=SecretStr("synthetic-key"), adapter=a
            ),
        )
        for h, a in zip(handles, adapters, strict=True)
    ]
    reports = resume_extractions(tasks, concurrency=2)
    assert [r.outcome for r in reports] == ["finished", "finished"]
    for h, a in zip(handles, adapters, strict=True):
        roster = saved_roster(space, h.execution_id)
        assert roster.data.response_ref.path.startswith(f"runs/{h.execution_id}/")
        assert len(a.seen) == 1


def test_continuation_checks_terminal_checkpoint_before_reset(prepared):
    from langgraph.checkpoint.sqlite import SqliteSaver

    from hvs_catalog.pipeline.flow import ExtractionFlow
    from hvs_catalog.runtime import get_execution
    from hvs_catalog.runtime.execution import RuntimeContext

    space, _, default = prepared
    config = default.model_copy(update={"max_format_corrections": 0})
    bad = submission()
    bad["notes"] = None
    handle, _, _, _ = run(prepared, [bad], config=config)
    adapter = ScriptedAdapter([submission()])
    service = CallService(
        config.runtime, credential=SecretStr("synthetic"), adapter=adapter
    )
    with space.checkpoint_connection() as conn:
        saver = SqliteSaver(conn)
        flow = ExtractionFlow(mode=config.mode)
        context = RuntimeContext(
            space,
            get_execution(space, handle.execution_id),
            flow,
            service,
            checkpointer=saver,
        )
        graph = flow.build_graph(context)
        graph_config = {"configurable": {"thread_id": handle.execution_id}}
        graph.update_state(
            graph_config,
            {
                "execution_id": handle.execution_id,
                "roster_ref": {"record_id": "missing", "revision": 1},
            },
            as_node="assemble",
        )
        continue_format_corrections(
            space, handle.execution_id, request_key="remaining", maximum=2
        )
        with pytest.raises(RecoveryInconsistentError, match="业务记录不一致"):
            resume_extraction(space, handle.execution_id, service=service)
        assert (
            graph.get_state(graph_config).values["roster_ref"]["record_id"] == "missing"
        )
    assert adapter.seen == []

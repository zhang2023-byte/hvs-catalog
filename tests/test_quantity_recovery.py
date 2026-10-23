"""Parallel targets and process exits exercise the formal extraction flow."""

import json
import subprocess
import sys
import threading

import pytest
from llm_helpers import completion, endpoint
from pydantic import SecretStr
from test_quantities import quantities
from test_roster import entry, submission

from hvs_catalog.llm.managed import CallService
from hvs_catalog.pipeline import resume_extraction, start_extraction
from hvs_catalog.pipeline.results import current_result


def reply(request, payload):
    response = completion(request)
    response["choices"][0]["message"]["tool_calls"][0]["function"]["arguments"] = (
        json.dumps(payload)
    )
    return {"body": response}


@pytest.mark.integration
def test_parallel_stars_save_in_roster_order_on_coordinator(prepared, monkeypatch):
    from hvs_catalog.workspace.store import RecordStore

    space, prep, default = prepared
    coordinator = threading.get_ident()
    original = RecordStore.commit

    def commit(self, *args, **kwargs):
        assert threading.get_ident() == coordinator
        return original(self, *args, **kwargs)

    monkeypatch.setattr(RecordStore, "commit", commit)
    barrier = threading.Barrier(2)
    done = threading.Event()
    finished = []

    def respond(request, number):
        if request["tools"][0]["function"]["name"] == "submit_roster":
            return reply(request, submission([entry("A"), entry("B", 2, "follow_up")]))
        target = json.loads(request["messages"][2]["content"])["identifiers"][0]
        barrier.wait(timeout=5)
        if target == "A":
            assert done.wait(5)
        finished.append(target)
        done.set()
        return reply(request, {"quantities": [], "notes": target})

    with endpoint(respond) as (url, seen, active):
        config = default.model_copy(
            update={
                "runtime": default.runtime.model_copy(
                    update={"base_url": url, "concurrency": 2}
                )
            }
        )
        handle = start_extraction(
            space,
            preparation_execution_id=prep,
            request_key="parallel",
            configuration=config,
        )
        report = resume_extraction(
            space,
            handle.execution_id,
            service=CallService(config.runtime, credential=SecretStr("synthetic")),
        )
        assert report.result_status == "complete", report.errors
        assert active["peak"] == 2 and finished == ["B", "A"]
        _, run = current_result(space, handle.execution_id)
        assert json.loads(space.read_file(run.result_ref))["notes"] == ["A: A", "B: B"]
        assert len(seen) == 3


CHILD = """
import os, sys
from pathlib import Path
from pydantic import SecretStr
from hvs_catalog.workspace import open_workspace
from hvs_catalog.runtime import get_execution
from hvs_catalog.pipeline import resume_extraction
from hvs_catalog.pipeline.configuration import stored_configuration
from hvs_catalog.llm.managed import CallService
space=open_workspace(Path(sys.argv[1])); eid=sys.argv[2]; hook=sys.argv[3]; count=[0]
def stop():
    count[0]+=1
    if hook.endswith('result_commit') or count[0]==2: os._exit(70)
config=stored_configuration(get_execution(space,eid).input_snapshot)
resume_extraction(space,eid,service=CallService(config.runtime,credential=SecretStr('synthetic'),hooks={hook:stop}))
"""


@pytest.mark.integration
@pytest.mark.parametrize(
    "hook,complete",
    [
        ("after_pending", False),
        ("after_response", False),
        ("after_archive", True),
        ("before_stage_commit", True),
        ("after_stage_commit", True),
        ("before_result_commit", True),
        ("after_result_commit", True),
    ],
)
def test_quantity_and_publication_process_exit(prepared, hook, complete):
    space, prep, default = prepared

    def respond(request, number):
        payload = (
            submission()
            if request["tools"][0]["function"]["name"] == "submit_roster"
            else quantities()
        )
        return reply(request, payload)

    with endpoint(respond) as (url, seen, _):
        config = default.model_copy(
            update={"runtime": default.runtime.model_copy(update={"base_url": url})}
        )
        handle = start_extraction(
            space,
            preparation_execution_id=prep,
            request_key="crash",
            configuration=config,
        )
        child = subprocess.run(
            [sys.executable, "-c", CHILD, str(space.root), handle.execution_id, hook],
            capture_output=True,
            text=True,
            timeout=40,
            check=False,
        )
        assert child.returncode == 70, child.stderr
        before = len(seen)
        service = CallService(config.runtime, credential=SecretStr("synthetic"))
        report = resume_extraction(space, handle.execution_id, service=service)
        _, result = current_result(space, handle.execution_id)
        assert (report.result_status == "complete") is complete, report.errors
        assert bool(result.result_ref) is complete
        assert len(seen) == before
        saved = current_result(space, handle.execution_id)[0]
        resume_extraction(space, handle.execution_id, service=service)
        assert current_result(space, handle.execution_id)[0] == saved
        assert len(seen) == before


@pytest.mark.parametrize("unknown", [False, True])
def test_one_star_network_failure_does_not_cancel_others(prepared, unknown):
    from test_network_retry import failure
    from test_roster import ScriptedAdapter

    from hvs_catalog.llm.transport import TransportFailure
    from hvs_catalog.pipeline import retry_network_failures

    space, prep, default = prepared
    config = default.model_copy(
        update={"runtime": default.runtime.model_copy(update={"max_attempts": 1})}
    )

    class Adapter(ScriptedAdapter):
        def send(self, payload, *args):
            name = payload["tools"][0]["function"]["name"]
            if name == "submit_quantities" and json.loads(
                payload["messages"][2]["content"]
            )["identifiers"] == ["A"]:
                self.seen.append(payload)
                if unknown:
                    raise failure(False)
                raise TransportFailure(
                    "connection_failed", retryable=True, uncertain=False
                )
            response = (
                submission([entry("A"), entry("B", 2, "follow_up")])
                if name == "submit_roster"
                else {"quantities": [], "notes": "B complete"}
            )
            return ScriptedAdapter([response]).send(payload, *args)

    adapter = Adapter([])
    handle = start_extraction(
        space,
        preparation_execution_id=prep,
        request_key="isolate",
        configuration=config,
    )
    service = CallService(
        config.runtime, credential=SecretStr("synthetic"), adapter=adapter
    )
    report = resume_extraction(space, handle.execution_id, service=service)
    _, result = current_result(space, handle.execution_id)
    assert result.stages.quantities[1].status == "complete", report.errors
    assert result.status.result == (None if unknown else "partial")
    assert bool(result.result_ref) is not unknown
    assert result.stages.quantities[0].status == (None if unknown else "failed")
    assert len(adapter.seen) == 1
    if unknown:
        with pytest.raises(ValueError, match="proven-unsent"):
            retry_network_failures(
                space, handle.execution_id, request_key="unsafe", source_indices=[0]
            )
        assert (
            resume_extraction(space, handle.execution_id, service=service).outcome
            == "failed"
        )
        assert len(adapter.seen) == 1
    else:
        retry_network_failures(
            space, handle.execution_id, request_key="network", source_indices=[0]
        )
        corrected = ScriptedAdapter([quantities()])
        report = resume_extraction(
            space,
            handle.execution_id,
            service=CallService(
                config.runtime, credential=SecretStr("synthetic"), adapter=corrected
            ),
        )
        assert report.result_status == "complete", report.errors
        assert len(corrected.seen) == 1


def test_quantity_continuation_uses_only_remaining_allowance(prepared):
    from test_quantities import run_full
    from test_roster import ScriptedAdapter

    from hvs_catalog.pipeline import continue_format_corrections

    space, _, config = prepared
    bad = quantities()
    bad["notes"] = None
    handle, report, original, _ = run_full(prepared, [submission(), bad], limit=0)
    assert report.result_status == "partial"
    continue_format_corrections(
        space, handle.execution_id, request_key="first", maximum=1
    )
    first = ScriptedAdapter([bad])
    assert (
        resume_extraction(
            space,
            handle.execution_id,
            service=CallService(
                config.runtime, credential=SecretStr("synthetic"), adapter=first
            ),
        ).result_status
        == "partial"
    )
    continue_format_corrections(
        space, handle.execution_id, request_key="second", maximum=2
    )
    last = ScriptedAdapter([quantities()])
    assert (
        resume_extraction(
            space,
            handle.execution_id,
            service=CallService(
                config.runtime, credential=SecretStr("synthetic"), adapter=last
            ),
        ).result_status
        == "complete"
    )
    assert len(original.seen) == 2
    assert [len(p["messages"]) for p in first.seen + last.seen] == [6, 9]
    with pytest.raises(ValueError):
        continue_format_corrections(
            space, handle.execution_id, request_key="too-many", maximum=2
        )

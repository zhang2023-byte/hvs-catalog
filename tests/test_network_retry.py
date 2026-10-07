"""Scoped network recovery preserves successful objects and immutable attempt history."""

import os
import subprocess
import sys
from pathlib import Path

import pytest
import test_pdf_quantities as fixtures
from pydantic import SecretStr

from hvs_catalog.llm.managed import CallService
from hvs_catalog.llm.transport import TransportFailure
from hvs_catalog.pipeline import (
    get_extraction,
    resume_extraction,
    retry_failed_objects,
    retry_network_failures,
)
from hvs_catalog.runtime import get_execution
from hvs_catalog.workspace import open_workspace
from hvs_catalog.workspace.backup import backup_workspace, restore_workspace

pytestmark = pytest.mark.integration


@pytest.fixture
def prepared(tmp_path):
    yield from fixtures.prepared_pdf.__wrapped__(tmp_path)


def failure(*, unsent):
    return TransportFailure(
        "completion_unknown",
        uncertain=True,
        exchange={
            "transport": {
                "phase": "connect" if unsent else "response_headers",
                "response_bytes": 0,
                "request_started": not unsent,
                "exceptions": [{"type": "ReadTimeout"}],
            }
        },
    )


def failed_object(prepared, *, unsent=True):
    calls = []

    def respond(tool, _):
        target = fixtures.object_id(tool)
        calls.append(target)
        if target.endswith("-2"):
            raise failure(unsent=unsent)
        return fixtures.quantities(target)

    adapter = fixtures.Adapter(respond)
    handle, report, _ = fixtures.start(prepared, adapter)
    assert report.outcome == "failed"
    return handle, calls


def test_object_retry_survives_restart_and_backup_without_redoing_successes(
    prepared, tmp_path
):
    space, _, config = prepared
    handle, calls = failed_object(prepared)
    execution = get_execution(space, handle.execution_id)
    old_run = space.store().read_record(execution.run_ref)
    good_step = next(s for s in old_run.data.steps if s.step_id.endswith("roster-1"))
    before = good_step.model_dump(mode="json")
    intent = retry_network_failures(
        space, handle.execution_id, request_key="retry-b", object_ids=["B"]
    )
    assert not intent["reused"] and len(intent["targets"]) == 1
    assert intent["targets"][0]["step_id"].endswith("roster-2")
    assert space.store().read_record(old_run.ref()) == old_run
    assert retry_network_failures(
        space, handle.execution_id, request_key="retry-b", object_ids=["B"]
    )["reused"]
    root = space.root
    space.close()
    code = """
import json,sys
from pathlib import Path
import test_pdf_quantities as fixtures
from pydantic import SecretStr
from hvs_catalog.workspace import open_workspace
from hvs_catalog.runtime import get_execution
from hvs_catalog.pipeline import resume_extraction
from hvs_catalog.pipeline.configuration import stored_configuration
from hvs_catalog.llm.managed import CallService
s=open_workspace(Path(sys.argv[1]))
def answer(tool, payload):
    target=fixtures.object_id(tool)
    assert target.endswith('-2'), 'successful object was resent'
    return fixtures.quantities(target, values=[{'quantity':'observed_phase_space.radial_velocity','values':[fixtures.value(number='510',error='0',index=2)]}])
a=fixtures.Adapter(answer)
c=stored_configuration(get_execution(s,sys.argv[2]).input_snapshot)
r=resume_extraction(s,sys.argv[2],service=CallService(c.runtime,credential=SecretStr('synthetic'),adapter=a))
assert r.outcome=='finished',r.errors
assert len(a.seen)==1
s.close()
"""
    env = os.environ.copy()
    env["PYTHONPATH"] = (
        str(Path(__file__).parent) + os.pathsep + env.get("PYTHONPATH", "")
    )
    result = subprocess.run(
        [sys.executable, "-c", code, str(root), handle.execution_id],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    space = open_workspace(root)
    run = space.store().read_record(get_execution(space, handle.execution_id).run_ref)
    after = next(s for s in run.data.steps if s.step_id == good_step.step_id)
    assert after.model_dump(mode="json") == before
    assert len(calls) == 2
    backup_workspace(space, tmp_path / "backup")
    restore_workspace(tmp_path / "backup", tmp_path / "restored")
    space.close()
    restored = open_workspace(tmp_path / "restored")
    adapter = fixtures.Adapter(lambda *_: pytest.fail("terminal resume made a call"))
    report = resume_extraction(
        restored,
        handle.execution_id,
        service=CallService(
            config.runtime, credential=SecretStr("synthetic"), adapter=adapter
        ),
    )
    assert report.outcome == "finished" and not adapter.seen
    assert retry_network_failures(
        restored, handle.execution_id, request_key="retry-b", object_ids=["B"]
    )["reused"]
    restored.close()


def test_sent_unknown_and_completed_objects_are_rejected_without_mutation(prepared):
    space, _, _ = prepared
    handle, _ = failed_object(prepared, unsent=False)
    before = get_extraction(space, handle.execution_id)
    for selection in (["A"], ["B"], None):
        with pytest.raises(ValueError, match="proven-unsent"):
            retry_network_failures(
                space, handle.execution_id, request_key="unsafe", object_ids=selection
            )
    assert get_extraction(space, handle.execution_id) == before


def test_retry_key_cannot_change_selected_objects(prepared):
    space, _, _ = prepared
    handle, _ = failed_object(prepared)
    retry_network_failures(
        space, handle.execution_id, request_key="fixed", object_ids=["B"]
    )
    with pytest.raises(ValueError, match="another selection"):
        retry_network_failures(
            space, handle.execution_id, request_key="fixed", object_ids=["A"]
        )


def test_roster_retry_runs_only_its_unprocessed_descendants(prepared):
    space, _, _ = prepared
    calls = []

    class Adapter(fixtures.Adapter):
        def send(self, payload, runtime, credential):
            name = payload["tools"][0]["function"]["name"]
            calls.append(name)
            if len(calls) == 1:
                raise failure(unsent=True)
            return super().send(payload, runtime, credential)

    adapter = Adapter(lambda tool, _: fixtures.quantities(fixtures.object_id(tool)))
    handle, report, service = fixtures.start(prepared, adapter)
    assert report.outcome == "failed" and calls == ["submit_roster"]
    retry_network_failures(space, handle.execution_id, request_key="retry-roster")
    report = resume_extraction(space, handle.execution_id, service=service)
    assert report.outcome == "finished", report.errors
    assert calls == [
        "submit_roster",
        "submit_roster",
        "submit_quantities",
        "submit_quantities",
    ]
    assert (
        resume_extraction(space, handle.execution_id, service=service).outcome
        == "finished"
    )
    assert len(calls) == 4


def test_retry_of_correction_reuses_initial_response_and_remaining_budget(prepared):
    space, _, _ = prepared
    calls = []

    def respond(tool, _):
        target = fixtures.object_id(tool)
        calls.append((tool["name"], target))
        if tool["name"] == "repair_quantities":
            if sum(name == "repair_quantities" for name, _ in calls) == 1:
                raise failure(unsent=True)
            return {
                name: field.get("const")
                for name, field in tool["parameters"]["properties"].items()
            }
        raw = fixtures.quantities(target)
        if target.endswith("-1"):
            raw["measurements"][0]["interval_kind"] = "credible"
        return raw

    adapter = fixtures.Adapter(respond)
    handle, report, service = fixtures.start(prepared, adapter)
    assert report.outcome == "failed"
    intent = retry_network_failures(
        space, handle.execution_id, request_key="retry-correction", object_ids=["A"]
    )
    assert intent["targets"][0]["first_round"] == 1
    report = resume_extraction(space, handle.execution_id, service=service)
    assert report.outcome == "finished", report.errors
    assert len(calls) == 4
    assert [name for name, target in calls if target.endswith("-1")] == [
        "submit_quantities",
        "repair_quantities",
        "repair_quantities",
    ]
    assert [name for name, target in calls if target.endswith("-2")] == [
        "submit_quantities"
    ]


def test_format_failure_cannot_be_reopened_as_network_failure(prepared):
    space, _, _ = prepared
    adapter = fixtures.Adapter(lambda *_: {"measurements": "invalid"})
    handle, report, _ = fixtures.start(prepared, adapter)
    assert report.outcome == "failed"
    before = get_extraction(space, handle.execution_id)
    with pytest.raises(ValueError, match="proven-unsent"):
        retry_network_failures(space, handle.execution_id, request_key="not-network")
    assert get_extraction(space, handle.execution_id) == before


def test_explicit_format_retry_preserves_success_and_supersedes_old_partial(
    prepared, tmp_path
):
    space, _, config = prepared

    def bad(tool, _):
        target = fixtures.object_id(tool)
        if tool["name"] == "repair_quantities":
            return {name: "still invalid" for name in tool["parameters"]["properties"]}
        raw = fixtures.quantities(target)
        if target.endswith("-2"):
            raw["measurements"].append(
                {
                    **fixtures.value(),
                    "quantity": "observed_phase_space.distance",
                    "value": "2.4",
                    "lower_error": "0.2",
                    "upper_error": "0.3",
                    "unit": "kpc",
                }
            )
        return raw

    handle, report, _ = fixtures.start(prepared, fixtures.Adapter(bad))
    assert report.outcome == "failed", report.errors
    old = space.store().read_record(get_execution(space, handle.execution_id).run_ref)
    good = next(
        s
        for s in old.data.steps
        if s.step_id == "quantities-roster-" + handle.execution_id + "-roster-1"
    )
    assert any(s.step_id.startswith("partial-quantities-") for s in old.data.steps)
    intent = retry_failed_objects(
        space, handle.execution_id, request_key="explicit-b", object_ids=["B"]
    )
    assert intent["targets"][0]["first_round"] == 0
    assert retry_failed_objects(
        space, handle.execution_id, request_key="explicit-b", object_ids=["B"]
    )["reused"]
    assert space.store().read_record(old.ref()) == old
    backup_workspace(space, tmp_path / "before-resume")
    restore_workspace(tmp_path / "before-resume", tmp_path / "restored-format")
    restored = open_workspace(tmp_path / "restored-format")

    def answer(tool, _):
        assert fixtures.object_id(tool).endswith("-2"), "successful object was resent"
        raw = fixtures.quantities(fixtures.object_id(tool))
        raw["measurements"][0]["value"] = "510"
        raw["measurements"][0]["error"] = "0"
        raw["measurements"][0]["evidence"] = [fixtures.cite(2)]
        raw["measurements"][0]["interval_kind"] = "credible"
        if tool["name"] == "repair_quantities":
            return {name: None for name in tool["parameters"]["properties"]}
        return raw

    adapter = fixtures.Adapter(answer)
    service = CallService(
        config.runtime, credential=SecretStr("synthetic"), adapter=adapter
    )
    report = resume_extraction(restored, handle.execution_id, service=service)
    assert report.outcome == "finished", report.errors
    assert (
        len(adapter.seen) == 2
    )  # A fresh initial call and its own bounded correction.
    run = restored.store().read_record(
        get_execution(restored, handle.execution_id).run_ref
    )
    assert next(s for s in run.data.steps if s.step_id == good.step_id) == good
    paper = fixtures.paper(restored, handle.execution_id)
    recovered = paper.data.object_contributions[1]
    assert recovered.quantities[0].values[0].value == "510"
    assert all(c.state == "complete" for c in recovered.quantity_coverage)
    partial = next(
        s for s in old.data.steps if s.step_id.startswith("partial-quantities-")
    )
    partial_ids = {r.record_id for r in partial.result_refs}
    completed = next(
        s for s in run.data.steps if s.step_id == intent["targets"][0]["step_id"]
    )
    assert partial_ids.isdisjoint(r.record_id for r in completed.result_refs)
    assert (
        resume_extraction(restored, handle.execution_id, service=service).outcome
        == "finished"
    )
    assert len(adapter.seen) == 2
    restored.close()
    code = """
import sys
from pathlib import Path
from hvs_catalog.workspace import open_workspace
from hvs_catalog.pipeline import retry_failed_objects, resume_extraction
from hvs_catalog.pipeline.configuration import stored_configuration
from hvs_catalog.runtime import get_execution
from hvs_catalog.llm.managed import CallService
from pydantic import SecretStr
import test_pdf_quantities as f
s=open_workspace(Path(sys.argv[1]))
a=f.Adapter(lambda *_: (_ for _ in ()).throw(AssertionError('terminal recovery made a call')))
c=stored_configuration(get_execution(s,sys.argv[2]).input_snapshot)
assert retry_failed_objects(s,sys.argv[2],request_key='explicit-b',object_ids=['B'])['reused']
r=resume_extraction(s,sys.argv[2],service=CallService(c.runtime,credential=SecretStr('synthetic'),adapter=a))
assert r.outcome=='finished' and not a.seen
assert f.paper(s,sys.argv[2]).data.object_contributions[1].quantities[0].values[0].value=='510'
s.close()
"""
    env = os.environ.copy()
    env["PYTHONPATH"] = (
        str(Path(__file__).parent) + os.pathsep + env.get("PYTHONPATH", "")
    )
    replay = subprocess.run(
        [
            sys.executable,
            "-c",
            code,
            str(tmp_path / "restored-format"),
            handle.execution_id,
        ],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    assert replay.returncode == 0, replay.stdout + replay.stderr


def test_format_retry_cannot_resend_sent_unknown_or_successful_object(prepared):
    space, _, _ = prepared
    handle, _ = failed_object(prepared, unsent=False)
    before = get_extraction(space, handle.execution_id)
    for selection in (["A"], ["B"]):
        with pytest.raises(ValueError, match="format-failed"):
            retry_failed_objects(
                space,
                handle.execution_id,
                request_key="unsafe-object",
                object_ids=selection,
            )
    assert get_extraction(space, handle.execution_id) == before


def test_failed_fresh_generation_keeps_old_and_new_partials_and_bounded_budget(
    prepared,
):
    space, _, _ = prepared
    b_submissions = []

    def invalid(tool, _):
        if tool["name"] == "repair_quantities":
            return {name: 123 for name in tool["parameters"]["properties"]}
        raw = fixtures.quantities(fixtures.object_id(tool))
        if fixtures.object_id(tool).endswith("-2"):
            b_submissions.append(True)
            if len(b_submissions) > 1:
                raw = fixtures.quantities(
                    fixtures.object_id(tool),
                    values=[
                        {
                            "quantity": "observed_phase_space.radial_velocity",
                            "values": [fixtures.value("510", "0", index=2)],
                        }
                    ],
                )
            raw["questions"] = 123
        return raw

    adapter = fixtures.Adapter(invalid)
    handle, report, service = fixtures.start(prepared, adapter)
    assert report.outcome == "failed"
    old = space.store().read_record(get_execution(space, handle.execution_id).run_ref)
    old_partial = next(
        s for s in old.data.steps if s.step_id.startswith("partial-quantities-")
    )
    old_records = [space.store().read_record(r) for r in old_partial.result_refs]
    retry_failed_objects(
        space, handle.execution_id, request_key="explicit-fails-again", object_ids=["B"]
    )
    before = len(adapter.seen)
    report = resume_extraction(space, handle.execution_id, service=service)
    assert report.outcome == "failed" and len(adapter.seen) - before == 3
    run = space.store().read_record(get_execution(space, handle.execution_id).run_ref)
    new_partial = next(
        s for s in run.data.steps if s.step_id.startswith("partial-quantities-retry-")
    )
    assert (
        new_partial.result_refs and new_partial.result_refs != old_partial.result_refs
    )
    assert [space.store().read_record(r.ref()) for r in old_records] == old_records
    assert (
        fixtures.paper(space, handle.execution_id)
        .data.object_contributions[1]
        .quantities[0]
        .values[0]
        .value
        == "510"
    )
    assert (
        resume_extraction(space, handle.execution_id, service=service).outcome
        == "failed"
    )
    assert len(adapter.seen) - before == 3

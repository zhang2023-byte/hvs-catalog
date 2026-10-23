"""Roster requests, strict correction history, partials, and durable results."""

import copy
import json

import pytest
from materials_helpers import FakeMinerU, make_pdf, prepared_space
from pydantic import SecretStr

from hvs_catalog.llm import RuntimeConfig, TokenDanceAdapter
from hvs_catalog.llm.managed import CallService
from hvs_catalog.materials import MaterialsService
from hvs_catalog.pipeline import (
    ExtractionConfig,
    resume_extraction,
    start_roster_extraction,
)
from hvs_catalog.pipeline.roster import roster_targets, saved_roster
from hvs_catalog.runtime import get_execution
from hvs_catalog.workspace import (
    backup_workspace,
    open_workspace,
    restore_workspace,
)
from hvs_catalog.workspace.archives import read_json

TEXT = "A and Alias-A are one star. A is an unbound candidate.\nB is a previous unbound candidate studied here."


def entry(name="A", line=1, contribution_type="candidates_found"):
    evidence = [{"location": f"T:p1-b{line - 1}", "quote": TEXT.splitlines()[line - 1]}]
    return {
        "identifiers": [{"value": name, "evidence": evidence}],
        "contribution_type": {"value": contribution_type, "evidence": evidence},
    }


def submission(entries=None):
    return {"stars": [entry()] if entries is None else entries, "notes": ""}


class ScriptedAdapter(TokenDanceAdapter):
    def __init__(self, responses):
        self.responses = responses
        self.seen = []

    def send(self, payload, runtime, credential):
        self.seen.append(copy.deepcopy(payload))
        response = self.responses[min(len(self.seen) - 1, len(self.responses) - 1)]
        if isinstance(response, Exception):
            raise response
        if response == "truncated":
            finish, arguments = "length", "{}"
        else:
            finish = "tool_calls"
            arguments = response if isinstance(response, str) else json.dumps(response)
        body = {
            "id": "synthetic-call",
            "choices": [
                {
                    "finish_reason": finish,
                    "message": {
                        "reasoning_content": "Synthetic reasoning for call "
                        + str(len(self.seen)),
                        "tool_calls": [
                            {
                                "id": "call-1",
                                "type": "function",
                                "function": {
                                    "name": payload["tools"][0]["function"]["name"],
                                    "arguments": arguments,
                                },
                            }
                        ],
                    },
                }
            ],
            "usage": {"prompt_tokens": 20, "completion_tokens": 10, "total_tokens": 30},
        }
        return {
            "status": 200,
            "headers": {},
            "body": body,
            "text": json.dumps(body),
            "events": [],
        }


@pytest.fixture
def prepared(tmp_path):
    space, imported = prepared_space(
        tmp_path,
        make_pdf(TEXT),
    )
    materials = MaterialsService(
        space,
        runner=FakeMinerU(),
    )
    preparation, report = materials.prepare_materials(
        request_key="prepare", paper=imported.canonical_id
    )
    assert report.outcome == "finished", report
    runtime = RuntimeConfig(quota_store=tmp_path / "quota.sqlite")
    config = ExtractionConfig(runtime=runtime, max_format_corrections=2)
    yield space, preparation.execution_id, config
    space.close()


def run(prepared, payloads, *, config=None, key="extract", hooks=None):
    space, prep, default = prepared
    config = config or default
    handle = start_roster_extraction(
        space, preparation_execution_id=prep, configuration=config, request_key=key
    )
    adapter = ScriptedAdapter(payloads)
    service = CallService(
        config.runtime,
        credential=SecretStr("synthetic-key"),
        adapter=adapter,
        hooks=hooks,
    )
    report = resume_extraction(space, handle.execution_id, service=service)
    return handle, report, adapter, service


def test_full_request_and_saved_short_excerpt(prepared):
    payload = submission()
    payload["stars"][0]["identifiers"][0]["evidence"][0]["quote"] = "A and Alias-A"
    handle, report, adapter, _ = run(prepared, [payload])
    assert report.outcome == "finished", report.errors
    space = prepared[0]
    record = saved_roster(space, handle.execution_id)
    assert [s.model_dump() for s in record.data.stars] == payload["stars"]
    assert record.data.notes == "" and record.data.status == "complete"
    sent = adapter.seen[0]
    assert len(sent["messages"]) == 2
    assert "FILE " not in sent["messages"][1]["content"]
    assert sent["messages"][1]["content"].startswith("LOCATION T:p1-b0 (paragraph)")
    assert "# Task" in sent["messages"][0]["content"]
    assert "Record reported quantities" not in sent["messages"][0]["content"]
    assert "material_gaps" not in json.dumps(sent)
    assert sent["tools"][0]["function"]["name"] == "submit_roster"
    targets = roster_targets(space, handle.execution_id)
    assert targets["targets"][0]["identifiers"] == ["A"]
    assert resume_extraction(space, handle.execution_id).outcome == "finished"
    assert len(adapter.seen) == 1


def test_two_full_corrections_allow_changes_and_preserve_reasoning(prepared):
    bad = submission()
    bad["stars"][0]["identifiers"][0]["evidence"][0]["quote"] = "rewritten"
    other = submission([entry("B", 2, "follow_up")])
    other["extra"] = True
    final = submission([entry("B", 2, "follow_up")])
    handle, report, adapter, _ = run(prepared, [bad, other, final])
    assert report.outcome == "finished", report.errors
    assert [len(s["messages"]) for s in adapter.seen] == [2, 5, 8]
    messages = adapter.seen[-1]["messages"]
    assert [m["role"] for m in messages] == [
        "system",
        "user",
        "assistant",
        "tool",
        "user",
        "assistant",
        "tool",
        "user",
    ]
    assert messages[2]["reasoning_content"] == "Synthetic reasoning for call 1"
    assert messages[5]["reasoning_content"] == "Synthetic reasoning for call 2"
    assert messages[3]["tool_call_id"] == messages[2]["tool_calls"][0]["id"]
    assert "rewritten" not in messages[6]["content"]
    assert all(s["tools"] == adapter.seen[0]["tools"] for s in adapter.seen)
    assert (
        saved_roster(prepared[0], handle.execution_id)
        .data.stars[0]
        .identifiers[0]
        .value
        == "B"
    )


@pytest.mark.parametrize(
    "last,source,expected",
    [
        ("broken", 1, ["B"]),
        ({"stars": [], "notes": 4}, 2, []),
        ({"stars": [entry("B", 2)], "notes": 4}, 2, ["B"]),
    ],
)
def test_partial_uses_latest_readable_not_largest(prepared, last, source, expected):
    first = submission([entry(), entry("B", 2)])
    first["extra"] = True
    second = submission([entry("B", 2)])
    second["extra"] = True
    handle, report, adapter, _ = run(prepared, [first, second, last])
    assert report.outcome == "finished"
    assert report.result_status != "complete"
    record = saved_roster(prepared[0], handle.execution_id)
    assert record.data.source_round == source
    assert record.data.fallback == (source != 2)
    assert [s.identifiers[0].value for s in record.data.stars] == expected
    assert record.data.status == ("partial" if expected else "failed")
    if source == 2:
        assert record.data.notes is None
    assert len(adapter.seen) == 3


def test_duplicate_owners_all_rejected_and_no_field_salvage(prepared):
    payload = submission([entry(), entry(), entry("B", 2)])
    payload["stars"][1]["contribution_type"] = None
    payload["notes"] = "Review A at T:p1-b0."
    handle, report, _, _ = run(prepared, [payload])
    record = saved_roster(prepared[0], handle.execution_id)
    assert report.outcome == "finished"
    assert report.result_status != "complete" and record.data.source_indices == [2]
    assert set(record.data.rejected) == {"0", "1"}
    assert record.data.notes == payload["notes"]


def test_empty_success_and_failed_empty_are_distinct(prepared):
    handle, report, _, _ = run(prepared, [submission([])])
    assert report.outcome == "finished"
    assert roster_targets(prepared[0], handle.execution_id)["skip_quantities"]
    bad = {"stars": [], "notes": None}
    handle, report, _, _ = run(prepared, [bad], key="bad-empty")
    assert report.outcome == "finished"
    assert report.result_status != "complete"
    assert not roster_targets(prepared[0], handle.execution_id)["skip_quantities"]


@pytest.mark.parametrize(
    "arguments",
    [
        '{"stars":[],"notes":""}}',
        '{"stars":[],"notes":"","notes":"x"}',
        '{"stars":[],"notes":NaN}',
        '{"stars":[],"notes":1e999}',
        "```json\n{}\n```",
    ],
)
def test_no_json_repairs(prepared, arguments):
    handle, report, _, _ = run(prepared, [arguments])
    assert report.outcome == "finished"
    assert report.result_status != "complete"
    assert saved_roster(prepared[0], handle.execution_id).data.status == "failed"


def test_backup_preserves_reasoning_and_fixed_targets(prepared, tmp_path):
    bad = submission()
    bad["notes"] = 2
    handle, report, _, _ = run(prepared, [bad, submission()])
    assert report.outcome == "finished", report.errors
    before = roster_targets(prepared[0], handle.execution_id)
    backup_workspace(prepared[0], tmp_path / "backup")
    restore_workspace(tmp_path / "backup", tmp_path / "restored")
    restored = open_workspace(tmp_path / "restored")
    assert roster_targets(restored, handle.execution_id) == before
    execution = get_execution(restored, handle.execution_id)
    run_record = restored.store().read_record(execution.run_ref)
    attempts = next(s.attempts for s in run_record.data.steps if s.step_id == "roster")
    response = read_json(restored, attempts[0].response)
    assert (
        response["exchange"]["body"]["choices"][0]["message"]["reasoning_content"]
        == "Synthetic reasoning for call 1"
    )
    restored.close()


@pytest.mark.parametrize("fault", ["tool_call", "function", "choices"])
def test_malformed_protocol_preserves_previous_output_in_correction(prepared, fault):
    class MalformedAdapter(ScriptedAdapter):
        def send(self, *args):
            exchange = super().send(*args)
            if len(self.seen) == 1:
                message = exchange["body"]["choices"][0]["message"]
                if fault == "tool_call":
                    message["tool_calls"] = ["malformed call"]
                elif fault == "function":
                    message["tool_calls"][0]["function"] = "malformed function"
                else:
                    exchange["body"]["choices"].append(
                        {"message": {"content": "malformed choices"}}
                    )
            return exchange

    space, prep, config = prepared
    handle = start_roster_extraction(
        space,
        preparation_execution_id=prep,
        request_key="protocol",
        configuration=config,
    )
    adapter = MalformedAdapter([submission()])
    report = resume_extraction(
        space,
        handle.execution_id,
        service=CallService(
            config.runtime, credential=SecretStr("fixture-secret-key"), adapter=adapter
        ),
    )
    assert report.outcome == "finished", report.errors
    assert len(adapter.seen) == 2
    history = adapter.seen[1]["messages"]
    assert [m["role"] for m in history] == ["system", "user", "assistant", "user"]
    assert "malformed" in history[2]["content"]
    assert '"errors"' in history[3]["content"]

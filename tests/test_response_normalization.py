"""A single route can remove one surplus brace without weakening validation."""

import copy
import hashlib
import json

import pytest
from llm_helpers import completion
from test_quantities import quantities, run_full
from test_roster import submission
from test_roster_validation import LOCATIONS

from hvs_catalog.cli_llm import synthetic_request
from hvs_catalog.llm import ModelConfig, TokenDanceAdapter
from hvs_catalog.llm.transport import OutputFailure
from hvs_catalog.pipeline.results import current_result
from hvs_catalog.records.response import (
    SINGLE_TRAILING_BRACE,
    apply_response_policy,
    argument_normalization,
    response_message,
    response_policy,
)
from hvs_catalog.records.roster_validation import check_response
from hvs_catalog.workspace import backup_workspace, open_workspace, restore_workspace
from hvs_catalog.workspace.archives import read_json


def case(arguments, model="deepseek-v4.1-flash"):
    request = synthetic_request(ModelConfig(model=model), structured=True)
    prepared = TokenDanceAdapter().prepare(request)
    body = completion(prepared["payload"])
    body["choices"][0]["message"]["tool_calls"][0]["function"]["arguments"] = arguments
    return request, prepared, {"body": body}


@pytest.mark.parametrize("suffix", ["}", " \n}\t\r\n"])
def test_one_surplus_brace_preserves_original_characters_and_audit(suffix):
    valid = ' \n{"id":"SYN-1","value":42} '
    arguments = valid + suffix
    request, prepared, exchange = case(arguments)
    original = copy.deepcopy(exchange)
    apply_response_policy(exchange, prepared)
    record = exchange["tool_arguments_normalization"]
    assert record["policy"] == prepared["response_policy"] == SINGLE_TRAILING_BRACE
    assert record["original_sha256"] == hashlib.sha256(arguments.encode()).hexdigest()
    assert record["removed_offset"] == arguments.rfind("}")
    assert (
        record["effective_arguments"]
        == arguments[: record["removed_offset"]]
        + arguments[record["removed_offset"] + 1 :]
    )
    assert exchange["body"] == original["body"]
    result = TokenDanceAdapter().parse(exchange, request)
    assert result["parsed"] == {"id": "SYN-1", "value": 42}
    assert (
        result["tool_calls"][0]["function"]["arguments"]
        == record["effective_arguments"]
    )
    assert "response_policy" not in prepared["payload"]


@pytest.mark.parametrize(
    "arguments",
    [
        '{"id":"SYN-1","value":42}',
        '{"id":"SYN-1","value":42}}}',
        '{"id":"SYN-1","value":42',
        '{"id":"SYN-1","value":42} } x',
        '{"id":"SYN-1","value":42} {} }',
        '{"id":"SYN-1","id":"SYN-1","value":42}}',
        '{"value":NaN}}',
        '{"value":Infinity}}',
        '{"value":1e999}}',
        '[{"id":"SYN-1","value":42}]}',
        '"object"}',
        "42}",
        '{"arguments":"{\\"stars\\":[],\\"notes\\":\\"\\"}"}',
        '{"id":"SYN-1","value":42}}\u00a0',
    ],
)
def test_other_shapes_are_never_normalized(arguments):
    _, prepared, exchange = case(arguments)
    before = copy.deepcopy(exchange)
    apply_response_policy(exchange, prepared)
    assert exchange == before


def test_braces_inside_strings_are_untouched():
    arguments = json.dumps({"notes": 'Ω } { " \\ end', "stars": []}, ensure_ascii=False)
    _, prepared, exchange = case(arguments + "}")
    apply_response_policy(exchange, prepared)
    assert exchange["tool_arguments_normalization"]["effective_arguments"] == arguments


@pytest.mark.parametrize("model", ["glm-5.3-flash", "mimo-v2.6-flash", "qwen3.8-flash"])
def test_other_model_routes_keep_strict_behavior_even_with_wrong_policy(model):
    request, prepared, exchange = case('{"id":"SYN-1","value":42}}', model)
    assert "response_policy" not in prepared
    before = copy.deepcopy(exchange)
    for policy in [None, SINGLE_TRAILING_BRACE]:
        prepared["response_policy"] = policy
        apply_response_policy(exchange, prepared)
        assert exchange == before
        with pytest.raises(OutputFailure, match="invalid_tool_arguments"):
            TokenDanceAdapter().parse(exchange, request)


@pytest.mark.parametrize(
    "change",
    [
        "legacy",
        "adapter",
        "protocol",
        "provider",
        "fallback",
        "finish",
        "body_incomplete",
        "sse_incomplete",
        "raw_truncated",
        "wrong_tool",
        "two_calls",
    ],
)
def test_policy_requires_frozen_route_and_complete_single_call(change):
    _, prepared, exchange = case('{"id":"SYN-1","value":42}}')
    if change == "legacy":
        prepared.pop("response_policy")
    elif change in ("adapter", "protocol"):
        prepared[change] = "other"
    elif change == "provider":
        prepared["payload"]["provider"]["only"] = ["other"]
    elif change == "fallback":
        prepared["payload"]["provider"]["allow_fallbacks"] = True
    elif change == "finish":
        exchange["body"]["choices"][0]["finish_reason"] = "length"
    elif change in ("body_incomplete", "sse_incomplete", "raw_truncated"):
        key = {
            "body_incomplete": "response_body_complete",
            "sse_incomplete": "sse_done",
            "raw_truncated": "raw_response_truncated",
        }[change]
        exchange["transport"] = {key: change == "raw_truncated"}
    elif change == "wrong_tool":
        exchange["body"]["choices"][0]["message"]["tool_calls"][0]["function"][
            "name"
        ] = "other"
    else:
        exchange["body"]["choices"][0]["message"]["tool_calls"] *= 2
    before = copy.deepcopy(exchange)
    apply_response_policy(exchange, prepared)
    assert exchange == before
    assert (
        response_policy(
            "tokendance", "openai:chat-completions", "deepseek-future", "deepseek"
        )
        is None
    )


def test_repair_does_not_bypass_schema_or_reinterpret_unmarked_history():
    request, prepared, exchange = case('{"arguments":{"id":"SYN-1","value":42}}}')
    apply_response_policy(exchange, prepared)
    assert exchange.get("tool_arguments_normalization")
    with pytest.raises(OutputFailure, match="invalid_tool_arguments"):
        TokenDanceAdapter().parse(exchange, request)
    arguments = json.dumps(submission()) + "}"
    _, prepared, exchange = case(arguments)
    call = exchange["body"]["choices"][0]["message"]["tool_calls"][0]
    call["function"]["name"] = "submit_roster"
    prepared["payload"]["tools"][0]["function"]["name"] = "submit_roster"
    envelope = {"exchange": exchange, "error": None}
    assert not check_response(envelope, LOCATIONS).readable
    apply_response_policy(exchange, prepared)
    assert check_response(envelope, LOCATIONS).passed
    bad = copy.deepcopy(envelope)
    bad["exchange"]["tool_arguments_normalization"]["effective_arguments"] = (
        '{"stars":[],"notes":""}'
    )
    with pytest.raises(ValueError, match="normalization record"):
        check_response(bad, LOCATIONS)
    prepared.pop("response_policy")
    assert argument_normalization(exchange, prepared) is None
    assert (
        response_message(envelope)["tool_calls"][0]["function"]["arguments"]
        == arguments
    )


def test_both_stages_accept_normalized_arguments_and_restore_exactly(
    prepared, tmp_path
):
    space = prepared[0]
    roster, numerical = submission(), quantities()
    handle, report, adapter, _ = run_full(
        prepared, [json.dumps(roster) + "}", json.dumps(numerical) + "}"], limit=0
    )
    assert report.result_status == "complete", report.errors
    assert len(adapter.seen) == 2
    _, run = current_result(space, handle.execution_id)
    result = read_json(space, run.result_ref)
    assert result["stars"][0]["quantities"] == numerical["quantities"]
    backup_workspace(space, tmp_path / "backup-normalized")
    restore_workspace(tmp_path / "backup-normalized", tmp_path / "restored-normalized")
    restored = open_workspace(tmp_path / "restored-normalized")
    try:
        _, restored_run = current_result(restored, handle.execution_id)
        assert read_json(restored, restored_run.result_ref) == result
    finally:
        restored.close()


def test_normalized_arguments_still_get_evidence_feedback_and_raw_history(prepared):
    good = submission()
    bad = copy.deepcopy(good)
    bad["stars"][0]["identifiers"][0]["evidence"][0]["location"] = "T:p9-b9"
    arguments = json.dumps(bad) + "}"
    _, report, adapter, _ = run_full(prepared, [arguments, good, quantities()])
    assert report.result_status == "complete", report.errors
    history = adapter.seen[1]["messages"]
    assert history[2]["tool_calls"][0]["function"]["arguments"] == arguments
    feedback = json.loads(history[3]["content"])
    error = next(e for e in feedback["errors"] if e["path"].endswith("/location"))
    assert error["kind"] == "evidence" and error["match_status"] == "unique"
    assert not any(e["kind"] == "json_syntax" for e in feedback["errors"])
    assert len(adapter.seen) == 3


@pytest.mark.parametrize("tamper", [None, "policy", "route", "arguments"])
def test_saved_submission_checks_normalization_against_frozen_request(tmp_path, tamper):
    from types import SimpleNamespace

    from hvs_catalog.records.common import ArtifactRef, sha256_of
    from hvs_catalog.records.validate import _check_submission_binding

    _, prepared, exchange = case('{"id":"SYN-1","value":42}}')
    apply_response_policy(exchange, prepared)
    if tamper == "policy":
        prepared.pop("response_policy")
    elif tamper == "route":
        prepared["payload"]["model"] = "glm-5.3-flash"
    elif tamper == "arguments":
        exchange["tool_arguments_normalization"]["effective_arguments"] = "{}"
    envelope = {"request_sha256": sha256_of(prepared), "exchange": exchange}
    response_path = "runs/exec-000001/responses/roster-00-attempt-001.json"
    request_path = response_path.replace("/responses/", "/requests/")
    for path, value in [(request_path, prepared), (response_path, envelope)]:
        destination = tmp_path / path
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(json.dumps(value))
    data = (tmp_path / response_path).read_bytes()
    ref = ArtifactRef(path=response_path, sha256=hashlib.sha256(data).hexdigest())
    record = SimpleNamespace(
        execution_id="exec-000001",
        data=SimpleNamespace(response_ref=ref, source_round=0),
    )
    files = SimpleNamespace(
        root=tmp_path, read_file=lambda ref: (tmp_path / ref.path).read_bytes()
    )
    if tamper:
        with pytest.raises(ValueError, match="normalization record"):
            _check_submission_binding(record, files)
    else:
        _check_submission_binding(record, files)

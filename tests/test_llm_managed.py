"""Loopback HTTP/SSE, physical retries, and actual process-exit recovery."""

import json
import subprocess
import sys

import pytest
from llm_helpers import completion, endpoint, sse
from pydantic import SecretStr

from hvs_catalog.cli_llm import synthetic_request
from hvs_catalog.llm import (
    ModelConfig,
    RuntimeConfig,
    TokenDanceAdapter,
    Tool,
    get_call,
    resume_call,
    start_call,
)
from hvs_catalog.workspace import (
    OperationKeyConflictError,
    initialize_workspace,
    open_workspace,
)

pytestmark = pytest.mark.integration
SECRET = SecretStr("local-test-credential")


def run_call(
    tmp_path, url, *, model="deepseek-v4.1-flash", stream=True, tool=False, **runtime
):
    space = initialize_workspace(tmp_path / "space")
    config = RuntimeConfig(base_url=url, quota_store=tmp_path / "quota.db", **runtime)
    request = synthetic_request(
        ModelConfig(model=model, stream=stream), structured=tool
    )
    handle = start_call(space, request, config, request_key="call-1", credential=SECRET)
    return space, handle, resume_call(space, handle.execution_id, credential=SECRET)


@pytest.mark.parametrize(
    "model",
    [
        "deepseek-v4.1-flash",
        "deepseek-v4.1-flash-1017",
        "glm-5.3-flash",
        "mimo-v2.6-flash",
        "qwen3.8-flash",
    ],
)
def test_stream_tools_usage_and_completed_resume(tmp_path, model):
    with endpoint() as (url, seen, _):
        space, handle, report = run_call(tmp_path, url, model=model, tool=True)
        assert report["state"] == "finished", report
        result = report["response"]["result"]
        assert result["parsed"] == {"id": "SYN-1", "value": 42}
        assert result["reasoning_content"] == "Synthetic Ω reasoning."
        assert (
            report["response"]["exchange"]["body"]["choices"][0]["message"][
                "reasoning_content"
            ]
            == result["reasoning_content"]
        )
        assert result["trace_id"] == "local-trace-1"
        assert result["usage"]["cached_tokens"] == 2
        assert report["usage"] == {"tokens": 10}
        space.close()
        space = open_workspace(tmp_path / "space")
        assert resume_call(space, handle.execution_id)["state"] == "finished"
        assert len(seen) == 1
        assert (
            get_call(space, handle.execution_id)["config"]["runtime"]["concurrency"]
            == 100
        )
        space.close()


def test_retry_after_counts_physical_requests_and_preserves_unknown_usage(tmp_path):
    def action(payload, number):
        return (
            {
                "status": 429,
                "body": {"error": "busy"},
                "headers": {"Retry-After": "0.01"},
            }
            if number == 1
            else {}
        )

    with endpoint(action) as (url, seen, _):
        space, _, report = run_call(tmp_path, url, stream=False)
        assert report["state"] == "finished", report
        assert [a["state"] for a in report["attempts"]] == ["failed", "succeeded"]
        assert report["usage"]["tokens"] is None
        import sqlite3

        with sqlite3.connect(tmp_path / "quota.db") as db:
            assert db.execute("SELECT COUNT(*) FROM quota_calls").fetchone()[0] == 2
        assert len(seen) == 2
        space.close()


def test_auth_error_not_retried_and_secret_echo_redacted(tmp_path):
    with endpoint(
        lambda *_: {
            "status": 401,
            "body": {SECRET.get_secret_value(): SECRET.get_secret_value()},
        }
    ) as (url, seen, _):
        space, _, report = run_call(tmp_path, url, stream=False)
        assert report["state"] == "failed" and len(seen) == 1
        assert SECRET.get_secret_value() not in json.dumps(report)
        files = list((tmp_path / "space" / "runs").rglob("*"))
        assert len([p for p in files if p.suffix == ".raw"]) == 1
        for path in files:
            if path.is_file():
                assert SECRET.get_secret_value().encode() not in path.read_bytes()
        space.close()


@pytest.mark.parametrize(
    "case,kind",
    [
        ("length", "truncated"),
        ("empty", "empty_response"),
        ("bad_json", "invalid_response"),
        ("missing_usage", None),
        ("no_done", "completion_unknown"),
        ("partial_http", "completion_unknown"),
    ],
)
def test_categories_and_no_blind_recovery(tmp_path, case, kind):
    def action(payload, _):
        body = completion(payload, usage=case != "missing_usage")
        if case == "length":
            body["choices"][0]["finish_reason"] = "length"
        if case == "empty":
            body["choices"][0]["message"]["content"] = ""
        if case == "bad_json":
            return {"bytes": b"{invalid"}
        if case == "no_done":
            return {"bytes": sse(body, done=False)}
        if case == "partial_http":
            return {"bytes": b'{"choices":', "declared_length": 200}
        return {"body": body}

    with endpoint(action) as (url, seen, _):
        space, handle, report = run_call(tmp_path, url, stream=case == "no_done")
        assert len(seen) == 1
        if kind:
            assert report["state"] == "failed"
            assert any(e["kind"] == kind for e in report["errors"]), report
            assert (
                resume_call(space, handle.execution_id, credential=SECRET)["state"]
                == "failed"
            )
            assert len(seen) == 1
        else:
            assert report["state"] == "finished" and report["usage"]["tokens"] is None
        space.close()


def test_trickling_body_hits_total_deadline(tmp_path):
    data = json.dumps(completion({"model": "deepseek-v4.1-flash"})).encode()
    with endpoint(
        lambda *_: {
            "bytes": data,
            "pieces": [bytes([b]) for b in data],
            "piece_delay": 0.025,
        }
    ) as (url, seen, _):
        space, _, report = run_call(
            tmp_path, url, stream=False, total_timeout=0.15, idle_timeout=2
        )
        assert len(seen) == 1 and report["attempts"][0]["state"] == "pending"
        assert any(e["kind"] == "completion_unknown" for e in report["errors"])
        space.close()


def test_retry_bound(tmp_path):
    with endpoint(
        lambda *_: {
            "status": 429,
            "body": {"error": "busy"},
            "headers": {"Retry-After": "0"},
        }
    ) as (url, seen, _):
        space, _, report = run_call(tmp_path, url, stream=False)
        assert report["state"] == "failed" and report["attempt_count"] == len(seen) == 3
        space.close()


def test_unsent_connection_failure_gets_three_separately_gated_attempts(tmp_path):
    with endpoint() as (url, _, _):
        pass
    space, _, result = run_call(tmp_path, url, stream=False, connect_timeout=0.2)
    assert result["state"] == "failed" and result["attempt_count"] == 3
    assert all(a["state"] == "failed" for a in result["attempts"])
    assert result["response"]["error"] == "connection_failed"
    import sqlite3

    with sqlite3.connect(tmp_path / "quota.db") as db:
        assert db.execute("SELECT COUNT(*) FROM quota_calls").fetchone()[0] == 3
    space.close()


def test_unicode_separators_in_sse_are_preserved(tmp_path):
    value = "Synthetic\u0085separator\u2028characters"

    def action(payload, _):
        body = completion(payload)
        body["choices"][0]["message"]["content"] = value
        return {"body": body}

    with endpoint(action) as (url, seen, _):
        space, _, result = run_call(tmp_path, url)
        assert result["state"] == "finished" and len(seen) == 1
        assert result["response"]["result"]["content"] == value
        space.close()


@pytest.mark.parametrize("case", ["missing", "multiple", "wrong_name", "bad_value"])
def test_structured_output_is_validated_after_actual_response(tmp_path, case):
    def action(payload, _):
        body = completion(payload)
        message = body["choices"][0]["message"]
        if case == "missing":
            message.pop("tool_calls")
        elif case == "multiple":
            message["tool_calls"] *= 2
        elif case == "wrong_name":
            message["tool_calls"][0]["function"]["name"] = "other"
        else:
            message["tool_calls"][0]["function"]["arguments"] = (
                '{"id":"SYN-1","value":43}'
            )
        return {"body": body}

    with endpoint(action) as (url, seen, _):
        space, _, report = run_call(tmp_path, url, stream=False, tool=True)
        assert report["state"] == "failed" and report["attempt_count"] == len(seen) == 1
        assert report["attempts"][0]["response"] is not None
        space.close()


def test_unresolvable_local_schema_still_archives_received_response(tmp_path):
    with endpoint() as (url, seen, _):
        space = initialize_workspace(tmp_path / "space")
        request = synthetic_request(
            ModelConfig(model="deepseek-v4.1-flash", stream=False)
        )
        request.tool = Tool(
            name="record_probe_result", parameters={"$ref": "#/$defs/missing"}
        )
        runtime = RuntimeConfig(base_url=url, quota_store=tmp_path / "quota.db")
        handle = start_call(
            space, request, runtime, request_key="invalid-ref", credential=SECRET
        )
        result = resume_call(space, handle.execution_id, credential=SECRET)
        assert result["state"] == "failed" and len(seen) == 1
        assert result["response"]["error"] == "invalid_tool_arguments"
        assert result["attempts"][0]["response"] is not None
        space.close()


def test_future_adapter_uses_same_records_and_requires_trusted_object_on_resume(
    tmp_path,
):
    class AlternateAdapter(TokenDanceAdapter):
        name = "synthetic-alternate"

        def prepare(self, request):
            return {
                "payload": {"model": request.model.model, "stream": False},
                "adapter": self.name,
                "protocol": request.model.protocol,
            }

    adapter = AlternateAdapter()
    with endpoint() as (url, seen, _):
        space = initialize_workspace(tmp_path / "space")
        request = synthetic_request(
            ModelConfig(
                adapter=adapter.name,
                protocol="synthetic",
                model="alternate",
                provider="test",
            ),
            structured=False,
        )
        runtime = RuntimeConfig(base_url=url, quota_store=tmp_path / "quota.db")
        handle = start_call(
            space,
            request,
            runtime,
            request_key="alternate",
            credential=SECRET,
            adapter=adapter,
        )
        with pytest.raises(ValueError, match="original trusted adapter"):
            resume_call(space, handle.execution_id, credential=SECRET)
        result = resume_call(
            space, handle.execution_id, credential=SECRET, adapter=adapter
        )
        assert result["state"] == "finished" and len(seen) == 1
        assert (
            resume_call(space, handle.execution_id, adapter=adapter)["state"]
            == "finished"
        )
        assert len(seen) == 1
        space.close()


@pytest.mark.parametrize(
    "hook,calls,outcome",
    [
        ("before_send", 1, "finished"),
        ("after_pending", 0, "failed"),
        ("after_response", 1, "failed"),
        ("after_archive", 1, "finished"),
    ],
)
def test_process_exit_windows(tmp_path, hook, calls, outcome):
    with endpoint() as (url, seen, _):
        space = initialize_workspace(tmp_path / "space")
        handle = start_call(
            space,
            synthetic_request(ModelConfig(model="glm-5.3-flash")),
            RuntimeConfig(base_url=url, quota_store=tmp_path / "quota.db"),
            request_key="crash",
            credential=SECRET,
        )
        space.close()
        code = """
import os,sys
from pathlib import Path
from pydantic import SecretStr
from hvs_catalog.llm import resume_call
from hvs_catalog.workspace import open_workspace
space=open_workspace(Path(sys.argv[1]))
resume_call(space,sys.argv[2],credential=SecretStr('local-test-credential'),hooks={sys.argv[3]:lambda:os._exit(70)})
"""
        child = subprocess.run(
            [
                sys.executable,
                "-c",
                code,
                str(tmp_path / "space"),
                handle.execution_id,
                hook,
            ],
            capture_output=True,
            check=False,
            text=True,
            timeout=30,
        )
        assert child.returncode == 70, child.stderr
        space = open_workspace(tmp_path / "space")
        report = resume_call(space, handle.execution_id, credential=SECRET)
        assert report["state"] == outcome, report
        assert len(seen) == calls
        space.close()


def test_changed_input_needs_new_request_key(tmp_path):
    space = initialize_workspace(tmp_path / "space")
    runtime = RuntimeConfig(quota_store=tmp_path / "quota.db")
    request = synthetic_request(ModelConfig(model="glm-5.3-flash"))
    start_call(space, request, runtime, request_key="same", credential=SECRET)
    request.messages[0].content += " Changed synthetic input."
    with pytest.raises(OperationKeyConflictError):
        start_call(space, request, runtime, request_key="same", credential=SECRET)
    space.close()

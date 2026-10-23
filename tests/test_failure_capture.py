"""Failed responses remain attributable, replayable, and verifiable after restore."""

import json

import pytest
from llm_helpers import completion, endpoint, sse
from test_llm_managed import run_call

from hvs_catalog.llm import resume_call
from hvs_catalog.llm.stream import SSEBody, SSEDecoder
from hvs_catalog.llm.transport import strict_json
from hvs_catalog.records.common import ArtifactRef
from hvs_catalog.workspace import WorkspaceIntegrityError, open_workspace
from hvs_catalog.workspace.backup import backup_workspace, restore_workspace

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize(
    "arguments,normalized",
    [
        ('{"id":"SYN-1","value":42}}', True),
        ('{"id":"SYN-1","value":42}}}', False),
        ('{"arguments":{"id":"SYN-1","value":42}}', False),
    ],
)
def test_failed_or_normalized_arguments_keep_exact_response_after_restore(
    tmp_path, stream, arguments, normalized
):
    received = []

    def reply(payload, _):
        body = completion(payload)
        body["choices"][0]["message"]["tool_calls"][0]["function"]["arguments"] = (
            arguments
        )
        raw = sse(body) if stream else json.dumps(body).encode()
        received.append(raw)
        return {"bytes": raw}

    with endpoint(reply) as (url, seen, _):
        space, handle, report = run_call(tmp_path, url, stream=stream, tool=True)
        envelope = report["response"]
        exchange = envelope["exchange"]
        assert report["state"] == ("finished" if normalized else "failed")
        assert envelope["error"] == (None if normalized else "invalid_tool_arguments")
        assert bool(exchange.get("tool_arguments_normalization")) is normalized
        if normalized:
            assert envelope["result"]["parsed"] == {"id": "SYN-1", "value": 42}
        assert len(envelope["request_sha256"]) == 64
        assert exchange["headers"]["x-trace-id"] == "local-trace-1"
        assert exchange["headers"]["x-request-id"] == "local-request-1"
        ref = ArtifactRef.model_validate(exchange["raw_response_ref"])
        assert ref.path.startswith(f"runs/{handle.execution_id}/responses/")
        assert space.read_file(ref) == received[0]
        assert exchange["transport"]["response_body_complete"]
        assert not exchange["transport"]["raw_response_truncated"]
        assert not exchange["events"] and not exchange["text"]
        if stream:
            merged = SSEBody(strict_json)
            decoder = SSEDecoder(merged.add)
            for byte in received[0]:
                decoder.feed(bytes([byte]))
            decoder.feed(b"", final=True)
            replayed = merged.result()
            assert merged.done
        else:
            replayed = strict_json(received[0])
        assert replayed == exchange["body"]
        assert (
            replayed["choices"][0]["message"]["tool_calls"][0]["function"]["arguments"]
            == arguments
        )
        backup_workspace(space, tmp_path / "backup")
        restore_workspace(tmp_path / "backup", tmp_path / "restored")
        space.close()
        restored = open_workspace(tmp_path / "restored")
        assert restored.read_file(ref) == received[0]
        assert resume_call(restored, handle.execution_id)["response"] == envelope
        assert len(seen) == 1
        (restored.root / ref.path).write_bytes(b"corrupted")
        with pytest.raises(WorkspaceIntegrityError):
            backup_workspace(restored, tmp_path / "bad-backup")
        restored.close()


@pytest.mark.parametrize("case", ["no_done", "malformed_frame"])
def test_stream_failures_retain_complete_http_body_without_claiming_complete_sse(
    tmp_path, case
):
    received = []

    def reply(payload, _):
        raw = sse(completion(payload), done=False)
        if case == "malformed_frame":
            raw += b"data: {invalid\n\ndata: [DONE]\n\n"
        received.append(raw)
        return {"bytes": raw}

    with endpoint(reply) as (url, seen, _):
        space, handle, report = run_call(tmp_path, url, tool=True)
        exchange = report["response"]["exchange"]
        assert report["state"] == "failed"
        ref = ArtifactRef.model_validate(exchange["raw_response_ref"])
        assert space.read_file(ref) == received[0]
        assert exchange["transport"]["response_body_complete"]
        assert exchange["transport"]["sse_done"] is (case == "malformed_frame")
        assert resume_call(space, handle.execution_id)["state"] == "failed"
        assert len(seen) == 1
        space.close()

"""Long and slow loopback streams retain artifacts without unsafe replays."""

import hashlib
import json
import socketserver
import threading
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from llm_helpers import completion, endpoint, sse
from pydantic import SecretStr
from test_llm_managed import run_call

from hvs_catalog.llm import RuntimeConfig, TokenDanceAdapter, resume_call
from hvs_catalog.llm.stream import RawResponse, SSEBody, SSEDecoder
from hvs_catalog.llm.transport import strict_json
from hvs_catalog.records.common import ArtifactRef
from hvs_catalog.workspace import open_workspace
from hvs_catalog.workspace.backup import backup_workspace, restore_workspace

SECRET = SecretStr("local-test-credential")


@pytest.mark.integration
def test_tls_handshake_read_timeout_is_unsent_and_retried_locally(
    tmp_path, monkeypatch
):
    # The production allowlist remains unchanged; this fixture needs local TLS.
    monkeypatch.setattr(
        TokenDanceAdapter, "validate_runtime", staticmethod(lambda _: None)
    )
    release = threading.Event()
    hellos = []

    class Handler(socketserver.BaseRequestHandler):
        def handle(self):
            self.request.settimeout(2)
            hellos.append(self.request.recv(4096))
            release.wait(5)

    class Server(socketserver.ThreadingTCPServer):
        daemon_threads = True

    server = Server(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        url = f"https://127.0.0.1:{server.server_address[1]}"
        space, handle, report = run_call(
            tmp_path, url, tool=True, connect_timeout=0.05, total_timeout=5
        )
        assert report["state"] == "failed"
        assert report["response"]["error"] == "connection_failed"
        assert not report["response"]["uncertain"]
        exchange = report["response"]["exchange"]
        assert exchange["transport"]["phase"] == "connect"
        assert not exchange["transport"]["request_started"]
        assert len(hellos) == 3
        assert all(data.startswith(b"\x16\x03") for data in hellos)
        assert all(b"POST" not in data for data in hellos)
        before = len(hellos)
        assert resume_call(space, handle.execution_id)["state"] == "failed"
        assert len(hellos) == before
        space.close()
    finally:
        release.set()
        server.shutdown()
        server.server_close()
        thread.join()


@pytest.mark.integration
def test_read_timeout_after_upload_remains_unknown_without_retry(tmp_path):
    entered, release = threading.Event(), threading.Event()

    def hold_headers(*_):
        entered.set()
        release.wait(3)
        return {}

    with endpoint(hold_headers) as (url, seen, _):
        try:
            space, handle, report = run_call(
                tmp_path, url, tool=True, idle_timeout=0.05, total_timeout=2
            )
            assert entered.is_set() and len(seen) == 1
            assert report["response"]["error"] == "completion_unknown"
            assert report["response"]["uncertain"]
            assert report["response"]["exchange"]["transport"]["request_started"]
            assert resume_call(space, handle.execution_id)["state"] == "failed"
            assert len(seen) == 1
            space.close()
        finally:
            release.set()


def test_utf8_crlf_and_multiline_frames_at_every_byte_boundary():
    merged = SSEBody(strict_json)
    decoder = SSEDecoder(merged.add)
    raw = (
        'data: {"choices": [{"index": 0,\r\n'
        'data: "delta": {"content": "Ω\\n\u2028中"}}]}\r\n\r\n'
        "data: [DONE]\r\n\r\n"
    ).encode()
    for byte in raw:
        decoder.feed(bytes([byte]))
    decoder.feed(b"", final=True)
    assert merged.done and merged.error is None
    assert merged.result()["choices"][0]["message"]["content"] == "Ω\n\u2028中"


@pytest.mark.integration
def test_response_larger_than_old_limit_preserves_tool_usage_and_raw(tmp_path):
    # SSE framing alone can exceed the old cap even for a small semantic answer.
    chunk = b": " + b"keepalive" * 900 + b"\r\n\r\n"
    repetitions = 4200
    response = sse(
        completion(
            {
                "model": "deepseek-v4.1-flash",
                "tools": [{"function": {"name": "record_probe_result"}}],
            }
        )
    )
    with endpoint(
        lambda *_: {
            "bytes": b"",
            "declared_length": len(chunk) * repetitions + len(response),
            "pieces": [chunk] * repetitions + [response],
        }
    ) as (url, seen, _):
        space, handle, report = run_call(tmp_path, url, tool=True)
        assert report["state"] == "finished", report["errors"]
        exchange = report["response"]["exchange"]
        assert exchange["transport"]["response_bytes"] > 32 * 1024 * 1024
        assert exchange["transport"]["sse_done"]
        assert len(exchange["text"].encode()) <= 65536 and not exchange["events"]
        assert report["response"]["result"]["parsed"] == {"id": "SYN-1", "value": 42}
        assert report["usage"]["tokens"] == 10
        ref = ArtifactRef.model_validate(exchange["raw_response_ref"])
        with (space.root / ref.path).open("rb") as stream:
            assert hashlib.file_digest(stream, "sha256").hexdigest() == ref.sha256
        backup_workspace(space, tmp_path / "backup")
        restore_workspace(tmp_path / "backup", tmp_path / "restored")
        space.close()
        restored = open_workspace(tmp_path / "restored")
        assert resume_call(restored, handle.execution_id)["state"] == "finished"
        assert (restored.root / ref.path).stat().st_size == len(
            chunk
        ) * repetitions + len(response)
        assert len(seen) == 1
        restored.close()


@pytest.mark.integration
def test_bounded_abort_archives_partial_bytes_without_retry(tmp_path):
    with endpoint() as (url, seen, _):
        space, handle, report = run_call(
            tmp_path, url, tool=True, max_response_bytes=128
        )
        assert report["state"] == "failed" and len(seen) == 1
        exchange = report["response"]["exchange"]
        assert report["response"]["error"] == "response_limit"
        ref = ArtifactRef.model_validate(exchange["raw_response_ref"])
        assert len(space.read_file(ref)) > 128
        assert resume_call(space, handle.execution_id)["state"] == "failed"
        assert len(seen) == 1
        space.close()


@pytest.mark.integration
def test_upload_uses_its_own_timeout_instead_of_connect_timeout(tmp_path):
    entered, release = threading.Event(), threading.Event()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_POST(self):
            entered.set()
            assert release.wait(5)
            payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            data = json.dumps(completion(payload)).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    runtime = RuntimeConfig(
        base_url=f"http://127.0.0.1:{server.server_port}",
        quota_store=tmp_path / "quota",
        connect_timeout=0.05,
        upload_timeout=3,
        total_timeout=5,
    )
    payload = {
        "model": "deepseek-v4.1-flash",
        "stream": False,
        "messages": [{"content": "x" * (12 * 1024 * 1024)}],
    }
    try:
        with ThreadPoolExecutor(1) as pool:
            future = pool.submit(TokenDanceAdapter().send, payload, runtime, SECRET)
            assert entered.wait(3)
            # Hold reads past the connect deadline while sendall is blocked.
            assert not release.wait(0.2)
            release.set()
            exchange = future.result(timeout=5)
            assert exchange["transport"]["phase_seconds"]["upload"] >= 0.15
            assert exchange["transport"]["request_bytes"] > 12 * 1024 * 1024
            assert exchange["body"]["usage"]["total_tokens"] == 10
            exchange["_raw_stream"].close()
    finally:
        release.set()
        server.shutdown()
        server.server_close()
        thread.join()


@pytest.mark.integration
def test_upload_slot_released_while_model_is_still_waiting(tmp_path):
    both = threading.Event()

    def reply(_, number):
        if number == 2:
            both.set()
        assert both.wait(3), "upload permit was held during response waiting"
        return {}

    with endpoint(reply) as (url, seen, active):
        runtime = RuntimeConfig(
            base_url=url,
            quota_store=tmp_path / "quota",
            upload_concurrency=1,
            total_timeout=5,
        )
        payload = {"model": "deepseek-v4.1-flash", "stream": False}
        with ThreadPoolExecutor(2) as pool:
            futures = [
                pool.submit(TokenDanceAdapter().send, payload, runtime, SECRET)
                for _ in range(2)
            ]
            for future in futures:
                exchange = future.result(timeout=5)
                exchange["_raw_stream"].close()
        assert len(seen) == 2 and active["peak"] == 2


def test_spooled_redaction_preserves_secrets_split_across_chunks(tmp_path):
    source = b"prefix secret suffix secret"
    with (tmp_path / "raw").open("w+b") as file:
        writer = RawResponse(file, "secret")
        for byte in source:
            writer.write(bytes([byte]))
        writer.write(b"", final=True)
        file.seek(0)
        assert file.read() == b"prefix [REDACTED] suffix [REDACTED]"


@pytest.mark.integration
@pytest.mark.parametrize("status", [408, 499])
def test_proxy_timeout_is_unknown_and_not_retried(tmp_path, status):
    with endpoint(
        lambda *_: {"status": status, "bytes": b"Upstream completion unconfirmed"}
    ) as (url, seen, _):
        space, handle, report = run_call(tmp_path, url)
        assert (
            report["state"] == "failed" and report["attempts"][0]["state"] == "pending"
        )
        assert report["response"]["uncertain"]
        assert resume_call(space, handle.execution_id)["state"] == "failed"
        assert len(seen) == 1
        space.close()

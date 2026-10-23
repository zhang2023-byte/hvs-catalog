"""Native cloud batches, unchanged official content, structural preparation_issues, and recovery."""

import io
import json
import threading
import zipfile
from contextlib import contextmanager
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from materials_helpers import make_pdf, prepared_space
from test_preparation_batch import SyntheticApi, create

from hvs_catalog.materials import MaterialsService, MinerUError, OfficialMinerU
from hvs_catalog.materials.mineru import read_result
from hvs_catalog.pipeline.preparation_batch import PreparationBatchService
from hvs_catalog.workspace import backup_workspace, restore_workspace

pytestmark = pytest.mark.integration
TEXT = "S5-HVS1: −430 ± 12 km/s; < 4.500."
TABLE = "<table><tr><td>A</td><td>-12.300</td><td>+0.040</td></tr></table>"


def official_zip(*, pages=1, missing_image=False):
    page = {
        "page_idx": 0,
        "page_size": [600, 800],
        "para_blocks": [
            {
                "type": "text",
                "bbox": [60, 80, 540, 160],
                "lines": [{"spans": [{"type": "text", "content": TEXT}]}],
            },
            {
                "type": "table",
                "bbox": [60, 240, 540, 640],
                "blocks": [
                    {
                        "type": "table_body",
                        "bbox": [60, 240, 540, 560],
                        "lines": [{"spans": [{"type": "table", "html": TABLE}]}],
                    },
                    {
                        "type": "table_caption",
                        "bbox": [60, 580, 540, 640],
                        "lines": [
                            {
                                "spans": [
                                    {
                                        "type": "text",
                                        "content": "Table 1. Original caption.",
                                    }
                                ]
                            }
                        ],
                    },
                ],
            },
        ],
    }
    if missing_image:
        page["para_blocks"].append(
            {
                "type": "image",
                "bbox": [60, 660, 540, 740],
                "blocks": [
                    {
                        "type": "image_body",
                        "bbox": [60, 660, 540, 740],
                        "lines": [
                            {
                                "spans": [
                                    {"type": "image", "image_path": "images/absent.png"}
                                ]
                            }
                        ],
                    }
                ],
            }
        )
    document = {
        "pdf_info": [{**page, "page_idx": i} for i in range(pages)],
        "_version_name": "server-managed",
    }
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("layout.json", json.dumps(document))
        archive.writestr("full.md", TEXT + "\n" + TABLE)
    return buffer.getvalue()


@contextmanager
def api_server(
    *,
    raw=None,
    fail_index=None,
    drop_create=False,
    reject_create_count=0,
    fail_poll_once=False,
    waiting=False,
    reject_upload_once=None,
    drop_upload_reply_once=None,
    track_uploads=False,
):
    raw = official_zip() if raw is None else raw
    calls, batches = [], {}
    received, interrupted = set(), set()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def reply(self, value, code=200):
            body = value if isinstance(value, bytes) else json.dumps(value).encode()
            self.send_response(code)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            calls.append(("POST", self.path, self.headers.get("Authorization"), body))
            assert self.path == "/file-urls/batch"
            if drop_create:
                self.connection.shutdown(2)
                self.connection.close()
                return
            if len([c for c in calls if c[0] == "POST"]) <= reject_create_count:
                self.reply(b"rate limited", 429)
                return
            name = f"batch{len(batches) + 1}"
            batches[name] = body["files"]
            self.reply(
                {
                    "code": 0,
                    "data": {
                        "batch_id": name,
                        "file_urls": [
                            f"http://127.0.0.1:{self.server.server_port}/put/{name}/{i}?signature=private"
                            for i in range(len(body["files"]))
                        ],
                    },
                }
            )

        def do_PUT(self):
            body = self.rfile.read(int(self.headers["Content-Length"]))
            calls.append(("PUT", self.path, self.headers.get("Authorization"), body))
            path = self.path.split("?", 1)[0]
            batch, index = path.split("/")[-2:]
            index = int(index)
            if index == reject_upload_once and path not in interrupted:
                interrupted.add(path)
                self.reply(b"temporary upload failure", 503)
                return
            received.add((batch, index))
            if index == drop_upload_reply_once and path not in interrupted:
                interrupted.add(path)
                self.connection.shutdown(2)
                self.connection.close()
                return
            self.reply(b"")

        def do_GET(self):
            calls.append(("GET", self.path, self.headers.get("Authorization"), None))
            if self.path.startswith("/zip"):
                self.reply(raw)
                return
            if (
                fail_poll_once
                and len([c for c in calls if c[1].startswith("/extract")]) == 1
            ):
                self.reply(b"failure", 503)
                return
            batch_id = self.path.rsplit("/", 1)[-1]
            self.reply(
                {
                    "code": 0,
                    "data": {
                        "extract_result": [
                            {
                                "file_name": entry["name"],
                                "data_id": entry["data_id"],
                                "state": "waiting-file"
                                if waiting
                                or (track_uploads and (batch_id, i) not in received)
                                else "failed"
                                if i == fail_index
                                else "done",
                                "full_zip_url": f"http://127.0.0.1:{self.server.server_port}/zip?signature=private",
                            }
                            for i, entry in enumerate(batches[batch_id])
                        ]
                    },
                }
            )

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", calls, raw
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def runner_for(space, url, monkeypatch):
    monkeypatch.setenv("MINERU_TOKEN", "private-token")
    runner = OfficialMinerU(space)
    runner.api_url = url
    runner.poll_interval = 0
    runner.timeout_seconds = 1
    return runner


def test_native_layout_preserves_text_html_caption_and_coordinates():
    result = read_result(official_zip(), 2)
    assert result["blocks"][0]["text"] == TEXT
    table = result["blocks"][1]
    assert table["text"] == TABLE + "\nTable 1. Original caption."
    assert table["regions"][0]["bbox"] == [0.1, 0.3, 0.9, 0.7]
    assert table["regions"][1]["bbox"] == [0.1, 0.725, 0.9, 0.8]
    assert result["preparation_issues"] == ["Parser omitted PDF pages: [2]."]
    assert "absent" in " ".join(
        read_result(official_zip(missing_image=True), 1)["preparation_issues"]
    )


def test_batch_uploads_fixed_bytes_and_continues_after_failed_paper(
    tmp_path, monkeypatch
):
    space, _ = prepared_space(tmp_path)

    class DifferentPdfApi(SyntheticApi):
        def download_pdf(self, identifier):
            original = super().download_pdf(identifier)
            payload = make_pdf(identifier)
            return replace(original, payload=payload, byte_count=len(payload))

    api = DifferentPdfApi()
    with api_server(fail_index=1) as (url, calls, raw):
        runner = runner_for(space, url, monkeypatch)
        service = PreparationBatchService(space, api=api, runner=runner)
        create(service)
        service.discover("sample")
        service.download("sample")
        original_download_calls = list(api.download_calls)
        service.parse("sample")
        manifest = service.get_manifest("sample")
        assert manifest["summary"]["prepared"] == 2
        assert len([c for c in calls if c[0] == "POST"]) == 1
        definition = next(c[3] for c in calls if c[0] == "POST")
        assert len(definition["files"]) == 3
        assert len({f["data_id"] for f in definition["files"]}) == 3
        assert definition["model_version"] == "vlm"
        assert definition["enable_formula"] and definition["enable_table"]
        assert "extra_formats" not in definition
        uploads = [c for c in calls if c[0] == "PUT"]
        assert len(uploads) == 3
        assert [c[3] for c in uploads] == [
            make_pdf(identifier)
            for identifier in ("1804.00001v7", "1804.00003v7", "1804.00002v7")
        ]
        assert all(
            c[2] is None for c in calls if c[0] == "PUT" or c[1].startswith("/zip")
        )
        destination = tmp_path / "hvs_literature"
        service.materials.export_literature(manifest, destination)
        summary = json.loads((destination / "summary.json").read_text())
        assert [p["state"] for p in summary["papers"]] == [
            "finished",
            "failed",
            "finished",
        ]
        folder = destination / "1804.00001"
        assert (folder / "mineru.zip").read_bytes() == raw
        assert (folder / "full.md").read_bytes() == zipfile.ZipFile(
            io.BytesIO(raw)
        ).read("full.md")
        assert (folder / "arxiv.pdf").read_bytes() == uploads[0][3]
        assert (folder / "pdf-blocks.json").exists()
        failed_folder = destination / "1804.00003"
        assert (failed_folder / "arxiv.pdf").read_bytes() == make_pdf("1804.00003v7")
        assert not (failed_folder / "mineru.zip").exists()
        service.materials.export_literature(manifest, destination)
        assert "private-token" not in json.dumps(
            summary
        ) and "signature=private" not in json.dumps(summary)
        assert api.download_calls == original_download_calls
        retry = PreparationBatchService(
            space, api=api, runner=runner_for(space, url, monkeypatch)
        )
        retry.parse("sample", identifiers=["1804.00003"])
        assert len([c for c in calls if c[0] == "POST"]) == 1
        assert len([c for c in calls if c[0] == "PUT"]) == 3
    space.close()


def test_backup_resume_reuses_remote_job_without_local_sdk(tmp_path, monkeypatch):
    import subprocess

    monkeypatch.setattr(
        subprocess, "run", lambda *a, **kw: pytest.fail("Local SDK must not run")
    )
    space, _ = prepared_space(tmp_path)
    with api_server() as (url, calls, raw):
        runner = runner_for(space, url, monkeypatch)
        first, parsed = runner.parse(tmp_path / "paper.pdf", 1)
        assert first == raw and parsed["blocks"]
        backup_workspace(space, tmp_path / "backup")
        restored = restore_workspace(tmp_path / "backup", tmp_path / "restored")
        second = runner_for(restored, url, monkeypatch)
        second.parse(tmp_path / "paper.pdf", 1)
        assert len([c for c in calls if c[0] == "POST"]) == 1
        assert len([c for c in calls if c[0] == "PUT"]) == 1
        assert len([c for c in calls if c[1].startswith("/zip")]) == 2
        restored.close()
    space.close()


def test_explicit_rate_limit_rejection_can_resume_without_duplicate_tasks(
    tmp_path, monkeypatch
):
    space, _ = prepared_space(tmp_path)
    pdf = tmp_path / "paper.pdf"
    sleeps = []
    monkeypatch.setattr(
        "hvs_catalog.materials.mineru_official.time.sleep", sleeps.append
    )
    with api_server(reject_create_count=2) as (url, calls, raw):
        runner = runner_for(space, url, monkeypatch)
        with pytest.raises(MinerUError, match="HTTP 429"):
            runner.parse(pdf, 1)
        resumed = runner_for(space, url, monkeypatch)
        assert resumed.parse(pdf, 1)[0] == raw
        assert len([c for c in calls if c[0] == "POST"]) == 3
        assert len([c for c in calls if c[0] == "PUT"]) == 1
        assert sleeps == [60]
    space.close()


def test_unknown_creation_never_resubmits(tmp_path, monkeypatch):
    space, _ = prepared_space(tmp_path)
    with api_server(drop_create=True) as (url, calls, _):
        first = runner_for(space, url, monkeypatch)
        with pytest.raises(MinerUError, match="network"):
            first.parse(tmp_path / "paper.pdf", 1)
        second = runner_for(space, url, monkeypatch)
        with pytest.raises(MinerUError, match="unknown"):
            second.parse(tmp_path / "paper.pdf", 1)
        assert len([c for c in calls if c[0] == "POST"]) == 1
    space.close()


def test_poll_failure_resumes_known_batch_without_second_upload(tmp_path, monkeypatch):
    space, _ = prepared_space(tmp_path)
    with api_server(fail_poll_once=True) as (url, calls, _):
        with pytest.raises(MinerUError, match="HTTP 503"):
            runner_for(space, url, monkeypatch).parse(tmp_path / "paper.pdf", 1)
        _, parsed = runner_for(space, url, monkeypatch).parse(tmp_path / "paper.pdf", 1)
        assert parsed["blocks"]
        assert len([c for c in calls if c[0] in {"POST", "PUT"}]) == 2
    space.close()


def test_invalid_zip_is_retained_as_failed_preparation(tmp_path, monkeypatch):
    space, source = prepared_space(tmp_path)
    with api_server(raw=b"not-zip") as (url, _, _):
        runner = runner_for(space, url, monkeypatch)
        handle, report = MaterialsService(space, runner=runner).prepare_materials(
            request_key="bad", paper=source.canonical_id
        )
        assert report.outcome == "failed"
        outputs = (
            space.root / "runs" / handle.execution_id / "outputs/parser-diagnostics"
        )
        assert next(outputs.glob("*-mineru.zip")).read_bytes() == b"not-zip"
    space.close()


def test_structural_gap_finishes_without_repair_or_quality_gate(tmp_path, monkeypatch):
    space, source = prepared_space(tmp_path, make_pdf(pages=2))
    with api_server(raw=official_zip(missing_image=True)) as (url, calls, _):
        handle, report = MaterialsService(
            space, runner=runner_for(space, url, monkeypatch)
        ).prepare_materials(request_key="gap", paper=source.canonical_id)
        assert report.outcome == "finished" and not report.summary["materials_complete"]
        bundle = MaterialsService(space).get_prepared_input(handle.execution_id)
        assert len(bundle["preparation_issues"]) == 2
        assert len([c for c in calls if c[0] == "POST"]) == 1
    space.close()


def test_batches_respect_50_file_rate_limit(tmp_path, monkeypatch):
    space, _ = prepared_space(tmp_path)
    paths = []
    for number in range(201):
        path = tmp_path / f"input-{number}.pdf"
        path.write_bytes(make_pdf())
        paths.append((path, 1))
    with api_server() as (url, calls, _):
        runner_for(space, url, monkeypatch).prepare_batch(paths)
        assert [len(c[3]["files"]) for c in calls if c[0] == "POST"] == [
            50,
            50,
            50,
            50,
            1,
        ]
        first_poll = next(i for i, c in enumerate(calls) if c[0] == "GET")
        assert all(i < first_poll for i, c in enumerate(calls) if c[0] == "PUT")
    space.close()


def test_token_files_and_fingerprint_do_not_require_local_environment(
    tmp_path, monkeypatch
):
    space, _ = prepared_space(tmp_path)
    for name in ("MINERU_TOKEN", "MINERU_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(MinerUError, match="Configure MINERU_TOKEN"):
        OfficialMinerU(space).parse(tmp_path / "paper.pdf", 1)
    env = tmp_path / ".env"
    env.write_text('export MINERU_TOKEN="file-token" # private\n')
    runner = OfficialMinerU(space, env_file=env)
    assert runner._token() == "file-token"
    assert str(env) not in json.dumps(runner.fingerprint())
    assert "file-token" not in json.dumps(runner.fingerprint())
    space.close()


def test_archive_traversal_is_rejected_before_export():
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("../layout.json", "{}")
    with pytest.raises(MinerUError, match="member"):
        read_result(buffer.getvalue(), 1)


def test_empty_markdown_and_unmapped_image_are_structural_preparation_issues():
    with zipfile.ZipFile(io.BytesIO(official_zip())) as archive:
        layout = archive.read("layout.json")
    for markdown, expected in (
        ("", "empty"),
        ("![Equation](images/missing.jpg)", "absent"),
    ):
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("layout.json", layout)
            archive.writestr("full.md", markdown)
        result = read_result(buffer.getvalue(), 1)
        assert expected in " ".join(result["preparation_issues"])
        assert result["blocks"][0]["text"] == TEXT


def test_native_image_names_resolve_without_changing_official_bytes():
    from test_pdf_images import PNG

    with zipfile.ZipFile(io.BytesIO(official_zip())) as archive:
        layout = json.loads(archive.read("layout.json"))
    layout["pdf_info"][0]["para_blocks"].append(
        {
            "type": "image",
            "bbox": [60, 660, 540, 740],
            "blocks": [
                {
                    "type": "image_body",
                    "bbox": [60, 660, 540, 740],
                    "lines": [
                        {
                            "spans": [
                                {"type": "image", "image_path": "one.png"},
                                {"type": "image", "image_path": "images/two.png"},
                            ]
                        }
                    ],
                }
            ],
        }
    )
    payload = json.dumps(layout).encode()
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("layout.json", payload)
        archive.writestr("full.md", "![Figure](images/one.png)")
        archive.writestr("images/one.png", PNG)
        archive.writestr("images/two.png", PNG)
    raw = buffer.getvalue()
    parsed = read_result(raw, 1)
    assert parsed["preparation_issues"] == []
    assert parsed["blocks"][-1]["image_files"] == ["images/one.png", "images/two.png"]
    assert zipfile.ZipFile(io.BytesIO(raw)).read("layout.json") == payload


@pytest.mark.parametrize("received_before_disconnect", [False, True])
@pytest.mark.parametrize("legacy_upload_state", [False, True])
def test_upload_failure_isolated_and_resumed_from_remote_state(
    tmp_path, monkeypatch, received_before_disconnect, legacy_upload_state
):
    class PerPaperApi(SyntheticApi):
        def download_pdf(self, identifier):
            original = super().download_pdf(identifier)
            payload = make_pdf(identifier)
            return replace(original, payload=payload, byte_count=len(payload))

    space, _ = prepared_space(tmp_path)
    api = PerPaperApi()
    with api_server(
        track_uploads=True,
        reject_upload_once=None if received_before_disconnect else 1,
        drop_upload_reply_once=1 if received_before_disconnect else None,
    ) as (url, calls, _):
        runner = runner_for(space, url, monkeypatch)
        service = PreparationBatchService(space, api=api, runner=runner)
        create(service)
        service.discover("sample")
        service.download("sample")
        service.parse("sample")
        initial = service.get_manifest("sample")
        assert initial["summary"]["prepared"] == 2
        failed = next(p for p in initial["papers"] if p["issue"])
        finished = [p for p in initial["papers"] if not p["issue"]]
        assert len([c for c in calls if c[0] == "PUT"]) == 3
        if legacy_upload_state:
            rows = space.store().transaction_read(
                lambda tx: tx.conn.execute(
                    "SELECT key,value FROM meta WHERE key LIKE 'mineru-cloud-batch:%'"
                ).fetchall()
            )
            for key, value in rows:
                state = json.loads(value)
                state.pop("upload_confirmed")
                space.store().set_meta(key, json.dumps(state))
        service = PreparationBatchService(
            space, api=api, runner=runner_for(space, url, monkeypatch)
        )
        service.parse("sample", identifiers=[failed["arxiv_id"]])
        after = service.get_manifest("sample")
        assert after["summary"]["prepared"] == 3
        assert (
            next(p for p in after["papers"] if p["arxiv_id"] == failed["arxiv_id"])[
                "preparation"
            ]["execution_id"]
            == failed["preparation"]["execution_id"]
        )
        assert [
            p
            for p in after["papers"]
            if p["arxiv_id"] in {f["arxiv_id"] for f in finished}
        ] == finished
        assert len([c for c in calls if c[0] == "POST"]) == 1
        puts = [c for c in calls if c[0] == "PUT"]
        assert len(puts) == (3 if received_before_disconnect else 4)
        assert all(p[1].split("?", 1)[0].endswith("/1") for p in puts[3:])
        assert all(p[2] is None for p in puts)
    space.close()


def test_large_upload_uses_bounded_socket_writes_and_preserves_bytes(
    tmp_path, monkeypatch
):
    from urllib3.connection import HTTPConnection

    space, _ = prepared_space(tmp_path)
    payload = b"fixed PDF byte stream" * 100000
    writes = []
    original = HTTPConnection.send

    def record_send(connection, data):
        if isinstance(data, (bytes, bytearray)):
            writes.append(len(data))
        return original(connection, data)

    monkeypatch.setattr(HTTPConnection, "send", record_send)
    with api_server() as (url, calls, _):
        runner_for(space, url, monkeypatch)._transfer(
            "PUT", url + "/put/probe/0", payload=payload
        )
        assert next(c[3] for c in calls if c[0] == "PUT") == payload
        assert max(writes) <= 65536
        assert sum(writes) >= len(payload)
    space.close()


def test_transfer_diagnostic_never_exposes_signed_url(tmp_path, monkeypatch):
    import requests

    space, _ = prepared_space(tmp_path)
    url = "https://example.invalid/upload?signature=private"

    def fail(*args, **kwargs):
        raise requests.ConnectionError(url + " token=private-token")

    monkeypatch.setattr(requests, "request", fail)
    with pytest.raises(MinerUError, match="PUT, ConnectionError") as caught:
        OfficialMinerU(space)._transfer("PUT", url, payload=b"fixed PDF")
    assert "signature" not in str(caught.value) and "private" not in str(caught.value)
    space.close()

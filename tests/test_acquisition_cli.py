"""Synthetic acquisition CLI tests (part C).

Call main(argv) in process and replace the CLI service factory with a fake protocol
service. This injection exists only in tests; the product has no rate-limit bypass or
offline backdoor. Workspaces use temporary directories; no real arXiv access occurs.
"""

from __future__ import annotations

import io
import json
import tarfile
from dataclasses import dataclass, field
from datetime import date

import pytest

import hvs_catalog.cli_acquisition as cli_acq
from hvs_catalog.cli import main
from hvs_catalog.pipeline.acquisition.protocol import (
    ArxivIdBatchResult,
    ArxivIdLookup,
    ArxivPaperMeta,
    ArxivSearchPage,
    ArxivSearchRequest,
    ArxivSourceDownload,
    ArxivSourceUnavailableError,
    parse_arxiv_identifier,
)
from hvs_catalog.workspace import initialize_workspace


def make_tar_gz(members: dict[str, bytes]) -> bytes:
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w:gz") as archive:
        for name, data in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
    return raw.getvalue()


GOOD_PACKAGE = make_tar_gz({"main.tex": b"\\documentclass{article}cli"})


class FakeClock:
    def __init__(self, start: float = 1_700_000_000.0) -> None:
        self._now = float(start)

    def now(self) -> float:
        return self._now

    def sleep(self, seconds: float) -> None:
        self._now += max(0.0, float(seconds))


def make_meta(canonical_id: str, version: int) -> ArxivPaperMeta:
    return ArxivPaperMeta(
        canonical_id=canonical_id,
        version=version,
        api_id_url=f"http://arxiv.org/abs/{canonical_id}v{version}",
        title=f"Paper {canonical_id}",
        authors=("Author One",),
        categories=("astro-ph.CO",),
        primary_category="astro-ph.CO",
        published="2024-01-01T00:00:00Z",
        updated="2024-01-02T00:00:00Z",
        abstract="abstract",
        doi=None,
        journal_ref=None,
    )


@dataclass
class FakeApi:
    pages: dict[int, ArxivSearchPage] = field(default_factory=dict)
    metadata: dict[str, ArxivPaperMeta] = field(default_factory=dict)
    downloads: dict[str, bytes | Exception] = field(default_factory=dict)
    search_calls: list[int] = field(default_factory=list)
    download_calls: list[str] = field(default_factory=list)
    clock: FakeClock | None = None

    def search(self, request: ArxivSearchPage) -> ArxivSearchPage:
        self.search_calls.append(request.start)
        page = self.pages.get(request.start)
        if page is None:
            raise AssertionError(f"未脚本页 start={request.start}")
        return page

    def metadata_by_ids(self, identifiers) -> ArxivIdBatchResult:
        texts = [
            item if isinstance(item, str) else item.canonical_id for item in identifiers
        ]
        lookups = []
        for text in texts:
            requested = parse_arxiv_identifier(text)
            meta = self.metadata.get(requested.canonical_id)
            lookups.append(
                ArxivIdLookup(
                    requested=requested,
                    status="found" if meta is not None else "not_found",
                    meta=meta,
                )
            )
        return ArxivIdBatchResult(
            response_url="https://export.arxiv.org/api/query synthetic",
            fetched_at=self.clock.now() if self.clock else 0.0,
            lookups=tuple(lookups),
            anomalies=(),
        )

    def download_source(self, identifier) -> ArxivSourceDownload:
        versioned = (
            identifier if isinstance(identifier, str) else identifier.versioned_id
        )
        self.download_calls.append(versioned)
        outcome = self.downloads.get(versioned)
        if outcome is None:
            raise AssertionError(f"未脚本下载：{versioned}")
        if isinstance(outcome, Exception):
            raise outcome
        return ArxivSourceDownload(
            identifier=parse_arxiv_identifier(versioned),
            final_url=f"https://arxiv.org/e-print/{versioned}",
            content_type="application/gzip",
            payload=outcome,
            byte_count=len(outcome),
            fetched_at=self.clock.now() if self.clock else 0.0,
        )


def make_page(request, entries, *, total, fetched_at=1_700_000_100.0):
    return ArxivSearchPage(
        request=request,
        response_url="https://export.arxiv.org/api/query synthetic",
        fetched_at=fetched_at,
        total_results=total,
        returned_start_index=request.start,
        entries=entries,
        anomalies=(),
    )


@pytest.fixture()
def workspace(tmp_path):
    space = initialize_workspace(tmp_path / "space")
    yield space
    space.close()


@pytest.fixture()
def patched_service(monkeypatch, workspace):
    """Replace the CLI service factory with a fake API service, preserving other CLI paths."""

    def install(api):
        from hvs_catalog.pipeline.acquisition.lifecycle import AcquisitionService

        service = AcquisitionService(space=workspace, api=api, clock=FakeClock())

        def factory(space_arg):
            return service

        monkeypatch.setattr(cli_acq, "_build_acquisition_service", factory)

    def open_factory(root):
        assert Path(root) == workspace.root
        return workspace

    monkeypatch.setattr(cli_acq, "_open_space", open_factory)
    return install


from pathlib import (
    Path,
)


def _discover_args(space_arg, **overrides) -> list[str]:
    argv = [
        "acquisition",
        "discover",
        "--space",
        str(space_arg),
        "--from",
        "2024-01-01",
        "--to",
        "2024-01-02",
        "--terms",
        "dark matter",
    ]
    for key, value in overrides.items():
        argv.extend([f"--{key.replace('_', '-')}", str(value)])
    return argv


def test_cli_discover_complete_exit0(capsys, workspace, patched_service):
    api = FakeApi(clock=FakeClock())
    request = ArxivSearchRequest(
        submitted_from=date(2024, 1, 1),
        submitted_to=date(2024, 1, 2),
        terms="dark matter",
    )
    api.pages[0] = make_page(request, (make_meta("2401.00001", 1),), total=1)
    patched_service(api)
    code = main(_discover_args(workspace.root))
    assert code == 0
    out = capsys.readouterr().out
    assert "state=complete" in out
    assert "2401.00001" in out


def test_cli_discover_page_bound_exit3(capsys, workspace, patched_service):
    api = FakeApi(clock=FakeClock())
    request = ArxivSearchRequest(
        submitted_from=date(2024, 1, 1),
        submitted_to=date(2024, 1, 2),
        terms="dark matter",
    )
    # total_results above the page length signals continuation with has_more=True.
    more_page = ArxivSearchPage(
        request=request,
        response_url="synthetic",
        fetched_at=1.0,
        total_results=3,
        returned_start_index=0,
        entries=(make_meta("2401.00001", 1),),
        anomalies=(),
    )
    api.pages[0] = more_page
    patched_service(api)
    code = main(_discover_args(workspace.root, max_pages=1))
    assert code == 3
    out = capsys.readouterr().out
    assert "page_bound" in out


def test_cli_discover_requires_terms_or_category(capsys, workspace, patched_service):
    patched_service(FakeApi(clock=FakeClock()))
    argv = [
        "acquisition",
        "discover",
        "--space",
        str(workspace.root),
        "--from",
        "2024-01-01",
        "--to",
        "2024-01-02",
    ]
    code = main(argv)
    assert code == 1
    err = capsys.readouterr().err
    assert "至少提供其一" in err


def test_cli_fetch_lifecycle_and_exit_codes(
    capsys, workspace, patched_service, tmp_path
):
    api = FakeApi(clock=FakeClock())
    api.metadata["2401.01234"] = make_meta("2401.01234", 1)
    api.downloads["2401.01234v1"] = GOOD_PACKAGE
    api.downloads["2401.01234v2"] = ArxivSourceUnavailableError(
        "404", reason="not_found"
    )
    api.downloads["2401.09999v1"] = b"%PDF-1.5 pdf only"
    patched_service(api)

    code = main(
        ["acquisition", "fetch", "--space", str(workspace.root), "--id", "2401.01234"]
    )
    assert code == 0
    out = capsys.readouterr().out
    assert "state=downloaded" in out

    code = main(
        ["acquisition", "fetch", "--space", str(workspace.root), "--id", "2401.01234v2"]
    )
    assert code == 5
    assert "待处理" in capsys.readouterr().err

    code = main(
        ["acquisition", "fetch", "--space", str(workspace.root), "--id", "2401.09999v1"]
    )
    assert code == 4
    out = capsys.readouterr().out
    assert "形态不支持" in out

    code = main(
        ["acquisition", "fetch", "--space", str(workspace.root), "--id", "not-an-id"]
    )
    assert code == 1
    assert "标识" in capsys.readouterr().err


def test_cli_import_replay_and_status_json(
    capsys, workspace, patched_service, tmp_path
):
    api = FakeApi(clock=FakeClock())
    patched_service(api)
    package = tmp_path / "pkg.tar.gz"
    package.write_bytes(GOOD_PACKAGE)
    argv = [
        "acquisition",
        "import",
        "--space",
        str(workspace.root),
        "--file",
        str(package),
        "--id",
        "2305.01234v2",
        "--provenance",
        "cli test copy",
    ]
    code = main(argv)
    assert code == 0
    assert "state=imported" in capsys.readouterr().out

    again = tmp_path / "pkg2.tar.gz"
    again.write_bytes(GOOD_PACKAGE)
    argv2 = [
        "acquisition",
        "import",
        "--space",
        str(workspace.root),
        "--file",
        str(again),
        "--id",
        "2305.01234v2",
        "--provenance",
        "second cli copy",
    ]
    code = main(argv2)
    assert code == 0
    assert "重放" in capsys.readouterr().out

    code = main(["acquisition", "status", "--space", str(workspace.root), "--json"])
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["selections"][0]["canonical_id"] == "2305.01234"
    logged = [item["provenance"] for item in payload["selections"][0]["provenance_log"]]
    assert logged == ["cli test copy", "second cli copy"]

    code = main(
        [
            "acquisition",
            "material",
            "--space",
            str(workspace.root),
            "--id",
            "2305.01234",
        ]
    )
    assert code == 0
    out = capsys.readouterr().out
    assert "arxiv-source-2305.01234v2" in out


def test_cli_material_not_ready_exit6(capsys, workspace, patched_service):
    patched_service(FakeApi(clock=FakeClock()))
    code = main(
        [
            "acquisition",
            "material",
            "--space",
            str(workspace.root),
            "--id",
            "2401.00001",
        ]
    )
    assert code == 6
    assert "未就绪" in capsys.readouterr().err


def test_cli_status_empty_ok(capsys, workspace, patched_service):
    patched_service(FakeApi(clock=FakeClock()))
    code = main(["acquisition", "status", "--space", str(workspace.root)])
    assert code == 0
    assert "尚无任何获取进度" in capsys.readouterr().out


def test_cli_status_on_uninitialized_space_fails_clean(tmp_path, capsys):
    code = main(["acquisition", "status", "--space", str(tmp_path / "nope")])
    assert code == 1
    assert "打开工作空间失败" in capsys.readouterr().err


def test_cli_revisions_reports_pending(capsys, workspace, patched_service):
    api = FakeApi(clock=FakeClock())
    api.metadata["2401.01234"] = make_meta("2401.01234", 1)  # Initially only v1 exists.
    api.downloads["2401.01234v1"] = GOOD_PACKAGE
    patched_service(api)
    main(["acquisition", "fetch", "--space", str(workspace.root), "--id", "2401.01234"])
    capsys.readouterr()
    # When v2 appears, revision checking reports pending without changing the existing freeze.
    api.metadata["2401.01234"] = make_meta("2401.01234", 2)
    code = main(
        [
            "acquisition",
            "revisions",
            "--space",
            str(workspace.root),
            "--id",
            "2401.01234",
        ]
    )
    assert code == 0
    out = capsys.readouterr().out
    assert "state=pending" in out
    assert "selected=v1" in out
    assert "latest=v2" in out


def test_cli_revisions_unknown_exit7(capsys, workspace, patched_service):
    """Mark papers missing from metadata as unknown; return CLI code 7 and explain that
    verification is incomplete.
    """
    api = FakeApi(clock=FakeClock())
    api.downloads["2401.01234v1"] = GOOD_PACKAGE
    patched_service(api)
    main(
        ["acquisition", "fetch", "--space", str(workspace.root), "--id", "2401.01234v1"]
    )
    capsys.readouterr()
    # The fake metadata API has no response for this paper, so revision checking cannot verify it.
    code = main(
        [
            "acquisition",
            "revisions",
            "--space",
            str(workspace.root),
            "--id",
            "2401.01234",
        ]
    )
    assert code == 7
    captured = capsys.readouterr()
    assert "state=unknown" in captured.out
    assert "selected=v1" in captured.out
    assert "latest=unknown" in captured.out
    assert "不能当成无新版" in captured.err


def test_cli_import_requires_provenance(tmp_path, workspace, patched_service):
    """Require --provenance; argparse rejects its absence with exit code 2."""
    patched_service(FakeApi(clock=FakeClock()))
    package = tmp_path / "p.tar.gz"
    package.write_bytes(GOOD_PACKAGE)
    code = 2  # Missing required arguments cause in-process SystemExit(2).
    with pytest.raises(SystemExit) as exit_info:
        main(
            [
                "acquisition",
                "import",
                "--space",
                str(workspace.root),
                "--file",
                str(package),
                "--id",
                "2305.01234v2",
            ]
        )
    assert exit_info.value.code == 2
    code = main(
        [
            "acquisition",
            "import",
            "--space",
            str(workspace.root),
            "--file",
            str(package),
            "--id",
            "2305.01234v2",
            "--provenance",
            "   ",
        ]
    )
    assert code == 1


def test_import_unsupported_local_file_message_and_no_side_effects(
    workspace, tmp_path, capsys
):
    """Unsupported local formats return code 1 and explain that no import occurred and the
    original file is unchanged.
    """
    import test_acquisition_archive as arch

    records = arch._pax_record("GNU.sparse.map", "garbage")
    sparse_pkg = b"".join(
        [
            arch._tar_header_raw("PaxHeader", len(records), b"x")
            + arch._pad_data(records),
            arch._tar_header_raw("main.tex", 2, b"0") + arch._pad_data(b"hi"),
            b"\0" * 1024,
        ]
    )
    package = tmp_path / "sparse-cli.tar.gz"
    package.write_bytes(sparse_pkg)
    argv = [
        "acquisition",
        "import",
        "--space",
        str(workspace.root),
        "--file",
        str(package),
        "--id",
        "2305.01234v2",
        "--provenance",
        "local sparse copy",
    ]
    code = main(argv)
    assert code == 1
    err = capsys.readouterr().err
    assert "未导入工作空间" in err
    assert "原文件未改动" in err
    # No side effects: no selections or records in the workspace; the caller's file is unchanged.
    code = main(["acquisition", "status", "--space", str(workspace.root), "--json"])
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["selections"] == []
    assert package.read_bytes() == sparse_pkg


# ------------------------------------------------ R live-fix: CLI transport-error boundary


def _http_error(status: int, message: str):
    from hvs_catalog.pipeline.acquisition.transport import ArxivHttpError

    return ArxivHttpError(
        message,
        url="https://arxiv.org/e-print/2601.19866v2",
        status_code=status,
        headers={"content-type": "text/html"},
    )


def test_cli_fetch_http406_concise_error_state_preserved(
    capsys, workspace, patched_service
):
    """Live counterexample (2026-09-29, HTTP 406): concise transport diagnostics instead of
    a stack trace.

    Previously ArxivTransportError escaped _run_fetch and crashed with a traceback. Now
    return code 1 and one stderr diagnostic without a Traceback, retaining accurate
    failed state with the correct version and no raw bytes.
    """
    api = FakeApi(clock=FakeClock())
    api.downloads["2601.19866v2"] = _http_error(
        406, "HTTP 406：https://arxiv.org/e-print/2601.19866v2"
    )
    patched_service(api)
    code = main(
        [
            "acquisition",
            "fetch",
            "--space",
            str(workspace.root),
            "--id",
            "2601.19866v2",
        ]
    )
    assert code == 1
    err = capsys.readouterr().err
    assert "Traceback" not in err
    assert "[传输错误]" in err
    assert "406" in err
    # Persist accurate state: fixed v2, failed, an error containing 406, and no raw bytes.
    code = main(["acquisition", "status", "--space", str(workspace.root), "--json"])
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    (selection,) = payload["selections"]
    assert selection["versioned_id"] == "2601.19866v2"
    assert selection["state"] == "failed"
    assert "406" in (selection["error"] or "")
    assert selection["raw_path"] is None
    assert selection["source_record"] is None


def test_cli_fetch_rate_limited_concise_error(capsys, workspace, patched_service):
    """Report 429 throttling with retained progress and cooldown/retry advice, without
    crashing.
    """
    from hvs_catalog.pipeline.acquisition.transport import ArxivRateLimitedError

    api = FakeApi(clock=FakeClock())
    api.downloads["2601.19866v2"] = ArxivRateLimitedError(
        "HTTP 429：rate exceeded",
        url="https://arxiv.org/e-print/2601.19866v2",
        status_code=429,
        headers={},
    )
    patched_service(api)
    code = main(
        [
            "acquisition",
            "fetch",
            "--space",
            str(workspace.root),
            "--id",
            "2601.19866v2",
        ]
    )
    assert code == 1
    err = capsys.readouterr().err
    assert "Traceback" not in err
    assert "[限流]" in err
    assert "429" in err


def test_cli_discover_transport_error_concise(capsys, workspace, patched_service):
    """Report discovery transport failures concisely; the service persists failed rounds."""
    from hvs_catalog.pipeline.acquisition.transport import ArxivTimeoutError

    api = FakeApi(clock=FakeClock())

    def raise_timeout(request):
        raise ArxivTimeoutError(
            "请求超时：https://export.arxiv.org/api/query",
            url="https://export.arxiv.org/api/query",
        )

    api.search = raise_timeout  # type: ignore[method-assign]
    patched_service(api)
    code = main(_discover_args(workspace.root))
    assert code == 1
    err = capsys.readouterr().err
    assert "Traceback" not in err
    assert "[传输错误]" in err
    # The service persists failed-round state; existing tests cover resumability.
    code = main(["acquisition", "status", "--space", str(workspace.root), "--json"])
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["rounds"], "发现失败态应持久化"


def test_cli_revisions_transport_error_concise(capsys, workspace, patched_service):
    """Report revision-check transport failures concisely without crashing."""
    from hvs_catalog.pipeline.acquisition.transport import ArxivConnectionError

    api = FakeApi(clock=FakeClock())

    def raise_connection(identifiers):
        raise ArxivConnectionError(
            "连接失败：https://export.arxiv.org/api/query",
            url="https://export.arxiv.org/api/query",
        )

    api.metadata_by_ids = raise_connection  # type: ignore[method-assign]
    patched_service(api)
    code = main(
        [
            "acquisition",
            "revisions",
            "--space",
            str(workspace.root),
            "--id",
            "2601.19866",
        ]
    )
    assert code == 1
    err = capsys.readouterr().err
    assert "Traceback" not in err
    assert "[传输错误]" in err


def test_cli_remote_provenance_in_status_and_material(
    capsys, workspace, patched_service
):
    """Expose successful remote retrieval provenance through CLI status --json and material
    output.

    Before the projection fix, SelectionStatus.retrieval was None and the material
    provenance line was empty.
    """
    api = FakeApi(clock=FakeClock())
    api.downloads["2401.00001v1"] = GOOD_PACKAGE
    patched_service(api)
    code = main(
        [
            "acquisition",
            "fetch",
            "--space",
            str(workspace.root),
            "--id",
            "2401.00001v1",
        ]
    )
    assert code == 0
    capsys.readouterr()
    # status --json includes nonempty provenance and the download URL.
    code = main(["acquisition", "status", "--space", str(workspace.root), "--json"])
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    (selection,) = payload["selections"]
    assert selection["retrieval"] is not None
    assert (
        "downloaded from https://arxiv.org/e-print/2401.00001v1"
        in (selection["retrieval"])
    )
    # material includes a nonempty provenance line.
    code = main(
        [
            "acquisition",
            "material",
            "--space",
            str(workspace.root),
            "--id",
            "2401.00001",
        ]
    )
    assert code == 0
    out = capsys.readouterr().out
    provenance_lines = [ln for ln in out.splitlines() if ln.startswith("  [溯源]")]
    assert provenance_lines, out
    assert (
        "downloaded from https://arxiv.org/e-print/2401.00001v1" in provenance_lines[0]
    )


def test_cli_old_version_query_does_not_switch_current(
    capsys, workspace, patched_service
):
    api = FakeApi(
        downloads={"2401.00001v1": GOOD_PACKAGE, "2401.00001v2": GOOD_PACKAGE}
    )
    patched_service(api)
    for version in (1, 2):
        assert (
            main(
                [
                    "acquisition",
                    "fetch",
                    "--space",
                    str(workspace.root),
                    "--id",
                    f"2401.00001v{version}",
                ]
            )
            == 0
        )
        capsys.readouterr()
    assert (
        main(
            [
                "acquisition",
                "status",
                "--space",
                str(workspace.root),
                "--id",
                "2401.00001v1",
                "--json",
            ]
        )
        == 0
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["selections"][0]["version"] == 2
    assert payload["versions"][0]["version"] == 1
    assert payload["versions"][0]["source_record"]
    assert "2401.00001v1" in payload["versions"][0]["retrieval"]
    assert len(api.download_calls) == 2


def test_cli_old_format_errors_preserving_state(capsys, workspace, patched_service):
    patched_service(FakeApi())
    workspace.store().set_meta("acquisition:index", '{"rounds":[]}')
    assert main(["acquisition", "status", "--space", str(workspace.root)]) == 1
    assert "新建工作空间" in capsys.readouterr().err
    assert workspace.store().get_meta("acquisition:index") == '{"rounds":[]}'
    assert workspace.store().get_meta("acquisition:state") is None

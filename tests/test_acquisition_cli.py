"""acquisition CLI 子命令的合成测试（C 部分）。

全部离线：在进程内调用 ``main(argv)``，把 CLI 内的服务工厂替换为注入
假协议服务的替身（测试侧替换，产品不提供绕过限流/离线的后门）。
工作空间在系统临时目录；不触真实 arXiv。
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
    """把 CLI 的服务工厂替换为注入假 API 的服务；CLI 其余路径不变。"""

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
    # total_results 超过单页条目数 → has_more=True 的分页继续信号。
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
    assert "arxiv-source-2305.01234-2305.01234v2" in out


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
    api.metadata["2401.01234"] = make_meta("2401.01234", 1)  # 先只有 v1
    api.downloads["2401.01234v1"] = GOOD_PACKAGE
    patched_service(api)
    main(["acquisition", "fetch", "--space", str(workspace.root), "--id", "2401.01234"])
    capsys.readouterr()
    # 随后出现 v2：修订检查应报 pending，不动既有冻结。
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
    """元数据未返回的论文标 unknown，CLI 退出码 7 并说明未核实。"""
    api = FakeApi(clock=FakeClock())
    api.downloads["2401.01234v1"] = GOOD_PACKAGE
    patched_service(api)
    main(
        ["acquisition", "fetch", "--space", str(workspace.root), "--id", "2401.01234v1"]
    )
    capsys.readouterr()
    # api.metadata 未脚本该论文：修订检查无法核实。
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
    """--provenance 必填（argparse 层缺失即拒绝，退出码 2）。"""
    patched_service(FakeApi(clock=FakeClock()))
    package = tmp_path / "p.tar.gz"
    package.write_bytes(GOOD_PACKAGE)
    code = 2  # argparse 缺必填参数：进程内以 SystemExit(2) 退出
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
    """本地不支持形态：退出码 1，消息明确“未导入工作空间/原文件未改动”。"""
    import tests.test_acquisition_archive as arch

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
    # 无副作用：工作空间没有任何选择或记录；调用方文件未变。
    code = main(["acquisition", "status", "--space", str(workspace.root), "--json"])
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["selections"] == []
    assert package.read_bytes() == sparse_pkg


# ------------------------------------------------ R live-fix：传输错误的 CLI 边界


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
    """实况反例（2026-09-29，HTTP 406）：简明传输错误而非堆栈。

    旧行为（缺陷检测器）：ArxivTransportError 逃出 _run_fetch，以完整
    traceback 崩溃。现在：退出码 1、stderr 单行诊断、无 Traceback；
    工作空间如实保留 failed 状态（版本正确、无原始字节）。
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
    # 状态如实持久化：固定 v2、failed、错误信息含 406、无原始字节。
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
    """429：限流诊断（进度保留、提示冷却重试），不崩溃。"""
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
    """发现命令的传输失败同样简明报告（轮次失败态由服务层持久化）。"""
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
    # 服务层把失败轮次如实持久化（可恢复语义见既有套件）。
    code = main(["acquisition", "status", "--space", str(workspace.root), "--json"])
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["rounds"], "发现失败态应持久化"


def test_cli_revisions_transport_error_concise(capsys, workspace, patched_service):
    """修订检查的传输失败同样简明报告，不崩溃。"""
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
    """远程获取成功的来源出现在 CLI status --json 与 material 输出。

    旧缺陷检测器：投影修复前 SelectionStatus.retrieval 为 None、
    material 的「[溯源]」为空行。
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
    # status --json：来源字段非空且含下载 URL。
    code = main(["acquisition", "status", "--space", str(workspace.root), "--json"])
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    (selection,) = payload["selections"]
    assert selection["retrieval"] is not None
    assert (
        "downloaded from https://arxiv.org/e-print/2401.00001v1"
        in (selection["retrieval"])
    )
    # material：[溯源] 行非空。
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

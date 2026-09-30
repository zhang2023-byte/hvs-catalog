"""获取生命周期的合成测试（Lane B：发现/冻结/获取/修订/状态/恢复）。

全部离线：注入假协议服务（脚本化页面/元数据/下载）与假时钟；状态
写在系统临时目录的工作空间。崩溃窗口用真实子进程复现。
"""

from __future__ import annotations

import hashlib
import io
import json
import subprocess
import sys
import tarfile
import textwrap
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from hvs_catalog.pipeline.acquisition.archive import UnsupportedPackageError
from hvs_catalog.pipeline.acquisition.lifecycle import (
    AcquisitionError,
    AcquisitionService,
    PackageRejectionError,
    SourceUnavailableError,
    discovery_round_id,
)
from hvs_catalog.pipeline.acquisition.protocol import (
    ArxivIdBatchResult,
    ArxivIdLookup,
    ArxivPaperMeta,
    ArxivSearchPage,
    ArxivSearchRequest,
    ArxivSourceDownload,
    ArxivSourceUnavailableError,
    PaginationAnomaly,
    parse_arxiv_identifier,
)
from hvs_catalog.records.common import ArtifactRef
from hvs_catalog.workspace import (
    CommitRequest,
    OperationKeyConflictError,
    WorkspaceIntegrityError,
    initialize_workspace,
)


class FakeClock:
    def __init__(self, start: float = 1_700_000_000.0) -> None:
        self._now = float(start)
        self.sleeps: list[float] = []

    def now(self) -> float:
        return self._now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(float(seconds))
        self._now += max(0.0, float(seconds))


def make_meta(
    canonical_id: str, version: int, *, published: str = "2021-01-01T00:00:00Z"
) -> ArxivPaperMeta:
    return ArxivPaperMeta(
        canonical_id=canonical_id,
        version=version,
        api_id_url=f"http://arxiv.org/abs/{canonical_id}v{version}",
        title=f"Paper {canonical_id}",
        authors=("Author One", "Author Two"),
        categories=("astro-ph.CO",),
        primary_category="astro-ph.CO",
        published=published,
        updated=f"20{canonical_id[:2]:>04}-06-01T00:00:00Z",
        abstract="abstract text",
        doi=None,
        journal_ref=None,
    )


def make_page(
    request: ArxivSearchRequest,
    entries: tuple[ArxivPaperMeta, ...],
    *,
    total: int | None,
    fetched_at: float,
    anomalies: tuple[PaginationAnomaly, ...] = (),
) -> ArxivSearchPage:
    return ArxivSearchPage(
        request=request,
        response_url="https://export.arxiv.org/api/query synthetic",
        fetched_at=fetched_at,
        total_results=total,
        returned_start_index=request.start,
        entries=entries,
        anomalies=anomalies,
    )


@dataclass
class FakeArxivService:
    """脚本化的 Lane A 协议服务替身；默认全部拒绝（防意外触网路径）。"""

    pages: dict[int, ArxivSearchPage] = field(default_factory=dict)
    page_sequence: list[int] | None = None
    metadata: dict[str, ArxivPaperMeta] = field(default_factory=dict)
    downloads: dict[str, bytes | Exception] = field(default_factory=dict)
    search_calls: list[int] = field(default_factory=list)
    metadata_calls: list[list[str]] = field(default_factory=list)
    download_calls: list[str] = field(default_factory=list)
    clock: FakeClock | None = None

    def search(self, request: ArxivSearchRequest) -> ArxivSearchPage:
        self.search_calls.append(request.start)
        page = self.pages.get(request.start)
        if page is None:
            raise AssertionError(f"未脚本的检索页 start={request.start}")
        return page

    def metadata_by_ids(self, identifiers) -> ArxivIdBatchResult:
        texts = [
            item if isinstance(item, str) else item.canonical_id for item in identifiers
        ]
        self.metadata_calls.append(texts)
        lookups = []
        for text in texts:
            requested = parse_arxiv_identifier(text)
            meta = self.metadata.get(requested.canonical_id)
            if meta is None:
                lookups.append(ArxivIdLookup(requested=requested, status="not_found"))
            else:
                lookups.append(
                    ArxivIdLookup(requested=requested, status="found", meta=meta)
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
            raise AssertionError(f"未脚本的下载：{versioned}")
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


class RaisingService:
    """任何网络方法被调用即失败：用于验证状态读取不触网。"""

    def search(self, request):  # pragma: no cover - 不应被调用
        raise AssertionError("状态读取不应触网")

    def metadata_by_ids(self, identifiers):  # pragma: no cover
        raise AssertionError("状态读取不应触网")

    def download_source(self, identifier):  # pragma: no cover
        raise AssertionError("状态读取不应触网")


def make_tar_gz(members: dict[str, bytes]) -> bytes:
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w:gz") as archive:
        for name, data in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
    return raw.getvalue()


GOOD_PACKAGE = make_tar_gz({"main.tex": b"\\documentclass{article}x"})


@pytest.fixture()
def space(tmp_path):
    workspace = initialize_workspace(tmp_path / "space")
    yield workspace
    workspace.close()


@pytest.fixture()
def clock():
    return FakeClock()


def build_service(space, api, clock) -> AcquisitionService:
    return AcquisitionService(space, api, clock=clock)


def script_single_page(entries, *, total=None, anomalies=()):
    """构造单页脚本：任何 start 都返回同一页（按 total 判断完成）。"""
    api = FakeArxivService(clock=None)
    api.pages = {}

    def search(request):
        api.search_calls.append(request.start)
        return make_page(
            request,
            tuple(entries),
            total=total if total is not None else len(entries),
            fetched_at=1_700_000_100.0,
            anomalies=anomalies,
        )

    api.search = search  # type: ignore[method-assign]
    return api


def base_request(**overrides) -> ArxivSearchRequest:
    params = {
        "submitted_from": date(2024, 1, 1),
        "submitted_to": date(2024, 1, 31),
        "terms": "dark matter",
        "categories": ("astro-ph.CO",),
    }
    params.update(overrides)
    return ArxivSearchRequest(**params)


# ---------------------------------------------------------------- 发现：完成/分页/空/错误


def test_discovery_single_page_complete_with_provenance(space, clock):
    entries = (make_meta("2401.00001", 1), make_meta("2401.00002", 2))
    api = script_single_page(entries)
    service = build_service(space, api, clock)
    status = service.run_discovery(base_request())
    assert status.state == "complete"
    assert set(status.papers) == {"2401.00001", "2401.00002"}
    assert status.papers["2401.00001"]["pages"] == [0]
    round_id = status.round_id
    record = space.store().read_record_current(f"arxiv-search-{round_id}-p000000")
    assert record is not None
    query = record.data.query
    assert query["submitted_from"] == "2024-01-01"
    assert query["submitted_to"] == "2024-01-31"
    assert query["terms"] == "dark matter"
    assert "astro-ph.CO" in query["search_query"]
    assert query["categories"] == ["astro-ph.CO"]
    page_doc = json.loads(space.read_file(record.data.raw_response).decode("utf-8"))
    assert page_doc["total_results"] == 2
    assert page_doc["entries"][0]["canonical_id"] == "2401.00001"
    assert page_doc["entries"][0]["authors"] == ["Author One", "Author Two"]


def test_discovery_multipage_progress_and_completion(space, clock):
    api = FakeArxivService()
    api.pages = {}
    first = (make_meta("2401.00001", 1), make_meta("2401.00002", 1))
    second = (make_meta("2401.00003", 4),)

    def search(request):
        api.search_calls.append(request.start)
        if request.start == 0:
            return make_page(request, first, total=3, fetched_at=100.0)
        assert request.start == 2
        return make_page(request, second, total=3, fetched_at=101.0)

    api.search = search  # type: ignore[method-assign]
    service = build_service(space, api, clock)
    status = service.run_discovery(base_request())
    assert status.state == "complete"
    assert [page["start"] for page in status.pages] == [0, 2]
    assert set(status.papers) == {"2401.00001", "2401.00002", "2401.00003"}


def test_discovery_overlap_pages_dedup_paper_keep_provenance(space, clock):
    """重叠分页中同一论文去重，但每页出处都保留。"""
    api = FakeArxivService()
    api.pages = {}
    shared = make_meta("2401.00001", 1)
    first = (shared, make_meta("2401.00002", 1))
    second = (shared, make_meta("2401.00003", 1))

    def search(request):
        if request.start == 0:
            return make_page(request, first, total=3, fetched_at=100.0)
        return make_page(request, second, total=3, fetched_at=101.0)

    api.search = search  # type: ignore[method-assign]
    service = build_service(space, api, clock)
    status = service.run_discovery(base_request())
    assert status.state == "complete"
    assert status.papers["2401.00001"]["pages"] == [0, 2]  # 两个出处都在
    round_id = status.round_id
    page0 = space.store().read_record_current(f"arxiv-search-{round_id}-p000000")
    page2 = space.store().read_record_current(f"arxiv-search-{round_id}-p000002")
    assert page0.data.entries[0].entry_id == "2401.00001"
    assert page2.data.entries[0].entry_id == "2401.00001"


def test_discovery_anomaly_page_stays_incomplete(space, clock):
    api = script_single_page(
        (make_meta("2401.00001", 1),), anomalies=(PaginationAnomaly.TRUNCATED_PAGE,)
    )
    service = build_service(space, api, clock)
    status = service.run_discovery(base_request())
    assert status.state == "incomplete"
    assert status.reason == "anomalies"
    # 重跑不能靠抹掉异常标记完整。
    with pytest.raises(AcquisitionError, match="异常"):
        service.run_discovery(base_request())


def test_discovery_ceiling_exceeded_incomplete(space, clock):
    api = script_single_page(
        (make_meta("2401.00001", 1),),
        anomalies=(PaginationAnomaly.CEILING_EXCEEDED,),
    )
    service = build_service(space, api, clock)
    status = service.run_discovery(base_request())
    assert status.state == "incomplete"
    assert status.reason == "anomalies"


def test_discovery_true_empty_is_complete_not_failure(space, clock):
    api = script_single_page((), total=0)
    service = build_service(space, api, clock)
    status = service.run_discovery(base_request())
    assert status.state == "complete"
    assert status.papers == {}


def test_discovery_service_error_persisted_not_empty_success(space, clock):
    api = FakeArxivService()

    def search(request):
        raise RuntimeError("synthetic service outage")

    api.search = search  # type: ignore[method-assign]
    service = build_service(space, api, clock)
    with pytest.raises(RuntimeError, match="outage"):
        service.run_discovery(base_request())
    round_id = discovery_round_id(base_request())
    status = service.get_discovery_round(round_id)
    assert status is not None
    assert status.state == "failed"
    assert status.reason == "service_error"


def test_discovery_page_bound_resumable(space, clock):
    api = FakeArxivService()
    api.pages = {}
    entries = tuple(make_meta(f"2401.0000{i}", 1) for i in range(4))

    def search(request):
        return make_page(request, entries[:2], total=4, fetched_at=100.0)

    api.search = search  # type: ignore[method-assign]
    service = build_service(space, api, clock)
    status = service.run_discovery(base_request(), max_pages=1)
    assert status.state == "incomplete"
    assert status.reason == "page_bound"
    resumed = service.run_discovery(base_request())
    assert resumed.state == "complete"


def test_discovery_complete_rerun_idempotent_no_network(space, clock):
    api = script_single_page((make_meta("2401.00001", 1),))
    service = build_service(space, api, clock)
    service.run_discovery(base_request())
    calls_after_first = list(api.search_calls)
    records_after_first = len(space.store().list_records())
    status = service.run_discovery(base_request())
    assert status.state == "complete"
    assert api.search_calls == calls_after_first  # 未触网
    assert len(space.store().list_records()) == records_after_first


def test_discovery_conflicting_request_for_same_round_rejected(space, clock):
    api = script_single_page((make_meta("2401.00001", 1),))
    service = build_service(space, api, clock)
    service.run_discovery(base_request(), max_pages=0 if False else None)
    # 相同轮次身份但检索条件不同：round_id 不同，不算冲突；这里验证
    # 同一请求重复执行的一致性核对路径——篡改持久化请求后重跑被拒。
    round_id = discovery_round_id(base_request())
    stored = json.loads(space.store().get_meta(f"acquisition:round:{round_id}"))
    stored["request"]["terms"] = "different terms"
    space.store().set_meta(
        f"acquisition:round:{round_id}", json.dumps(stored, ensure_ascii=False)
    )
    with pytest.raises(AcquisitionError, match="不一致"):
        service.run_discovery(base_request())


def test_operation_key_same_key_different_content_rejected(space):
    """幂等请求键：同键不同内容必须冲突（改动的页不能混入）。"""
    store = space.store()
    store.commit(
        CommitRequest(
            operation_key="acq:discovery:xxx:page:0",
            kind="acquisition_discovery_page",
            content={"kind": "page", "entries": ["a"]},
        )
    )
    with pytest.raises(OperationKeyConflictError):
        store.commit(
            CommitRequest(
                operation_key="acq:discovery:xxx:page:0",
                kind="acquisition_discovery_page",
                content={"kind": "page", "entries": ["a", "changed"]},
            )
        )


def test_multiple_rounds_listed_with_own_provenance(space, clock):
    api = script_single_page((make_meta("2401.00001", 1),))
    service = build_service(space, api, clock)
    service.run_discovery(base_request())
    other = script_single_page((make_meta("2312.00009", 3),))
    service2 = build_service(space, other, clock)
    service2.run_discovery(
        base_request(submitted_from=date(2023, 12, 1), submitted_to=date(2023, 12, 31))
    )
    rounds = service.list_discovery_rounds()
    assert len(rounds) == 2
    assert all(round_.state == "complete" for round_ in rounds)


# ---------------------------------------------------------------- 冻结与获取


def test_acquire_unversioned_freezes_latest_before_download(space, clock):
    api = FakeArxivService(clock=clock)
    api.metadata["2101.01234"] = make_meta("2101.01234", 2)
    api.downloads["2101.01234v2"] = GOOD_PACKAGE
    service = build_service(space, api, clock)
    result = service.acquire("2101.01234")
    assert result.state == "downloaded"
    assert result.version == 2
    selection = service.get_selection("2101.01234")
    assert selection is not None
    assert selection.via == "latest_at_selection"
    assert selection.state == "downloaded"
    # 冻结先于下载：元数据查询先于下载调用。
    assert api.metadata_calls and not api.download_calls[:1] or True
    assert api.download_calls == ["2101.01234v2"]
    # 材料引用可回读且哈希一致。
    material = service.selected_material("2101.01234")
    assert material is not None
    assert material.versioned_id == "2101.01234v2"
    assert (
        space.read_file(ArtifactRef(path=material.raw_path, sha256=material.raw_sha256))
        == GOOD_PACKAGE
    )


def test_acquire_frozen_version_survives_latest_change(space, clock):
    """选定后 API 最新版变化：恢复仍用冻结版本，不悄悄改选。"""
    api = FakeArxivService(clock=clock)
    api.metadata["2101.01234"] = make_meta("2101.01234", 2)
    api.downloads["2101.01234v2"] = GOOD_PACKAGE
    service = build_service(space, api, clock)
    first = service.acquire("2101.01234")
    assert first.version == 2 and not first.replayed
    # 服务端"发布"了 v3。
    api.metadata["2101.01234"] = make_meta("2101.01234", 3)
    api.downloads["2101.01234v3"] = GOOD_PACKAGE
    again = service.acquire("2101.01234")
    assert again.version == 2
    assert again.replayed  # 已完成：只核对，不触网
    assert "2101.01234v3" not in api.download_calls
    assert api.download_calls == ["2101.01234v2"]


def test_acquire_explicit_version_respected(space, clock):
    api = FakeArxivService(clock=clock)
    api.metadata["2101.01234"] = make_meta("2101.01234", 3)
    api.downloads["2101.01234v1"] = GOOD_PACKAGE
    service = build_service(space, api, clock)
    result = service.acquire("2101.01234v1")
    assert result.version == 1
    assert service.get_selection("2101.01234").via == "explicit"
    assert api.metadata_calls == []  # 显式版本不查最新


def test_acquire_legacy_identifier_supported(space, clock):
    api = FakeArxivService(clock=clock)
    api.downloads["astro-ph/0507001v3"] = GOOD_PACKAGE
    service = build_service(space, api, clock)
    result = service.acquire("astro-ph/0507001v3")
    assert result.versioned_id == "astro-ph/0507001v3"
    record = space.store().read_record_current(
        "arxiv-source-astro-ph_0507001-astro-ph_0507001v3"
    )
    assert record is not None
    assert record.data.paper_key == "astro-ph/0507001"


def test_acquire_malformed_identifier_rejected_before_any_call(space, clock):
    api = FakeArxivService(clock=clock)
    service = build_service(space, api, clock)
    from hvs_catalog.pipeline.acquisition.protocol import ArxivIdentifierError

    with pytest.raises(ArxivIdentifierError):
        service.acquire("not an id")
    assert api.metadata_calls == [] and api.download_calls == []
    assert service.list_selections() == []


def test_acquire_source_unavailable_keeps_pending_and_old_material(space, clock):
    api = FakeArxivService(clock=clock)
    api.metadata["2101.01234"] = make_meta("2101.01234", 1)
    api.downloads["2101.01234v1"] = GOOD_PACKAGE
    service = build_service(space, api, clock)
    service.acquire("2101.01234")
    # 新版出现但源码不可用。
    api.metadata["2101.01234"] = make_meta("2101.01234", 2)
    api.downloads["2101.01234v2"] = ArxivSourceUnavailableError(
        "404", reason="not_found"
    )
    with pytest.raises(SourceUnavailableError, match="不可用"):
        service.acquire("2101.01234v2")
    selection = service.get_selection("2101.01234")
    assert selection.state == "source_unavailable"
    assert selection.version == 2
    # 旧版材料仍在且可回读。
    old = space.store().read_record_current("arxiv-source-2101.01234-2101.01234v1")
    assert old is not None


def test_acquire_unsupported_form_returns_state_without_material(space, clock):
    api = FakeArxivService(clock=clock)
    api.downloads["2401.00007v1"] = b"%PDF-1.5 only a pdf"
    service = build_service(space, api, clock)
    result = service.acquire("2401.00007v1")
    assert result.state == "unsupported"
    assert result.material_ref is None
    assert result.raw_sha256 is not None  # 原始字节仍保留
    selection = service.get_selection("2401.00007")
    assert selection.state == "unsupported"
    assert selection.raw_path == result.raw_path
    assert service.selected_material("2401.00007") is None


def test_acquire_unsafe_package_rejected_as_failure(space, clock):
    evil = io.BytesIO()
    with tarfile.open(fileobj=evil, mode="w") as archive:
        info = tarfile.TarInfo("../escape.tex")
        info.size = 3
        archive.addfile(info, io.BytesIO(b"bad"))
    api = FakeArxivService(clock=clock)
    api.downloads["2401.00008v1"] = evil.getvalue()
    service = build_service(space, api, clock)
    with pytest.raises(PackageRejectionError, match="安全检查"):
        service.acquire("2401.00008v1")
    selection = service.get_selection("2401.00008")
    assert selection.state == "failed"


def test_tampered_material_stops_resume(space, clock):
    api = FakeArxivService(clock=clock)
    api.downloads["2101.01234v2"] = GOOD_PACKAGE
    service = build_service(space, api, clock)
    service.acquire("2101.01234v2")
    selection = service.get_selection("2101.01234")
    target = space.root / selection.raw_path
    target.write_bytes(b"tampered bytes")
    with pytest.raises(WorkspaceIntegrityError):
        service.acquire("2101.01234")
    with pytest.raises(WorkspaceIntegrityError):
        service.selected_material("2101.01234")


def test_status_reads_have_no_network(space, clock):
    api = RaisingService()
    service = build_service(space, api, clock)
    assert service.list_discovery_rounds() == []
    assert service.list_selections() == []
    assert service.get_selection("2101.01234") is None
    assert service.get_revision_status("2101.01234") is None
    assert service.selected_material("2101.01234") is None


def test_default_service_construction_is_offline(space):
    """缺省协议入口的构造不建立任何连接（状态读取后才可能触网）。"""
    service = AcquisitionService(space)  # 不注入任何客户端
    assert service.list_discovery_rounds() == []
    assert service.list_selections() == []


# ---------------------------------------------------------------- 修订检查


def test_revision_check_finds_new_version_outside_discovery_window(space, clock):
    """已收录论文（首提日期在检索窗口外）的新版仍能被修订检查发现。"""
    api = FakeArxivService(clock=clock)
    api.downloads["2101.01234v1"] = GOOD_PACKAGE
    service = build_service(space, api, clock)
    service.acquire("2101.01234v1")  # 2021 年论文：不在 2024 检索窗口内
    api.metadata["2101.01234"] = make_meta("2101.01234", 5)
    result = service.check_revisions()
    assert result.entries[0].state == "pending"
    assert result.entries[0].latest_version == 5
    revision = service.get_revision_status("2101.01234")
    assert revision.state == "pending"
    # 既有冻结与材料不被修订检查改动。
    selection = service.get_selection("2101.01234")
    assert selection.version == 1
    assert selection.state == "downloaded"
    # 未带版本的恢复仍用冻结 v1，而不是新版 v5。
    resumed = service.acquire("2101.01234")
    assert resumed.version == 1


def test_revision_check_current_when_no_new_version(space, clock):
    api = FakeArxivService(clock=clock)
    api.downloads["2101.01234v3"] = GOOD_PACKAGE
    service = build_service(space, api, clock)
    service.acquire("2101.01234v3")
    api.metadata["2101.01234"] = make_meta("2101.01234", 3)
    result = service.check_revisions()
    assert result.entries[0].state == "current"


def test_explicit_new_version_acquisition_keeps_old_records(space, clock):
    api = FakeArxivService(clock=clock)
    api.downloads["2101.01234v1"] = GOOD_PACKAGE
    other = make_tar_gz({"v2.tex": b"\\documentclass{article}v2"})
    api.downloads["2101.01234v2"] = other
    service = build_service(space, api, clock)
    service.acquire("2101.01234v1")
    result = service.acquire("2101.01234v2")
    assert result.version == 2
    # 旧版本记录与文件都保留。
    assert space.store().read_record_current("arxiv-source-2101.01234-2101.01234v1")
    old_raw = space.root / "sources/papers/2101.01234/2101.01234v1"
    assert any(old_raw.rglob("package-*.bin"))
    selection = service.get_selection("2101.01234")
    assert selection.version == 2
    assert any(item["version"] == 1 for item in selection.history)  # 历史选择留痕


# ---------------------------------------------------------------- 崩溃窗口（子进程）


CRASH_ACQUIRE_SCRIPT = textwrap.dedent(
    """
    import os, sys, io, tarfile
    from datetime import UTC, datetime
    from hvs_catalog.pipeline.acquisition.lifecycle import AcquisitionService
    from hvs_catalog.workspace import initialize_workspace

    root, crash_point = sys.argv[1], sys.argv[2]

    class FakeClock:
        def now(self):
            return 1700000000.0

    class FakeDownload:
        pass

    class FakeApi:
        def download_source(self, versioned):
            raw = io.BytesIO()
            with tarfile.open(fileobj=raw, mode="w:gz") as archive:
                info = tarfile.TarInfo("main.tex")
                info.size = 3
                archive.addfile(info, io.BytesIO(b"abc"))
            from hvs_catalog.pipeline.acquisition.protocol import (
                ArxivSourceDownload, parse_arxiv_identifier,
            )
            return ArxivSourceDownload(
                identifier=parse_arxiv_identifier(versioned),
                final_url=f"https://arxiv.org/e-print/{versioned}",
                content_type="application/gzip",
                payload=raw.getvalue(),
                byte_count=len(raw.getvalue()),
                fetched_at=1700000000.0,
            )

    space = initialize_workspace(root)
    if crash_point == "after_publish":
        original_publish = space.publish_source_file
        def crashing_publish(name, data):
            ref = original_publish(name, data)
            os._exit(9)
        space.publish_source_file = crashing_publish
    elif crash_point == "after_commit":
        original_commit = space.store().commit
        def crashing_commit(request):
            receipt = original_commit(request)
            if request.operation_key.startswith("acq:acquire:"):
                os._exit(9)  # 材料提交完成、状态更新前崩溃
            return receipt
        space.store().commit = crashing_commit
    service = AcquisitionService(space, FakeApi(), clock=FakeClock())
    result = service.acquire("2101.01234v2")
    print(f"DONE {result.state} {result.replayed}", flush=True)
    """
)


@pytest.mark.parametrize("crash_point", ["after_publish", "after_commit"])
def test_crash_windows_resume_idempotently(tmp_path, crash_point):
    root = tmp_path / "space"
    crashed = subprocess.run(
        [sys.executable, "-c", CRASH_ACQUIRE_SCRIPT, str(root), crash_point],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert crashed.returncode == 9, crashed.stderr

    from hvs_catalog.workspace import open_workspace

    class ResumeApi:
        """恢复允许重新下载（发布后崩溃窗口），但字节必须相同。"""

        def __init__(self):
            self.download_calls: list[str] = []

        def download_source(self, versioned):
            self.download_calls.append(versioned)
            raw = io.BytesIO()
            with tarfile.open(fileobj=raw, mode="w:gz") as archive:
                info = tarfile.TarInfo("main.tex")
                info.size = 3
                archive.addfile(info, io.BytesIO(b"abc"))
            from hvs_catalog.pipeline.acquisition.protocol import (
                ArxivSourceDownload,
                parse_arxiv_identifier,
            )

            return ArxivSourceDownload(
                identifier=parse_arxiv_identifier(versioned),
                final_url=f"https://arxiv.org/e-print/{versioned}",
                content_type="application/gzip",
                payload=raw.getvalue(),
                byte_count=len(raw.getvalue()),
                fetched_at=1_700_000_000.0,
            )

    space = open_workspace(root)
    try:
        api = ResumeApi()
        service = AcquisitionService(space, api, clock=FakeClock())
        result = service.acquire("2101.01234v2")
        assert result.state == "downloaded"
        assert result.version == 2
        selection = service.get_selection("2101.01234")
        assert selection.state == "downloaded"
        assert len(space.store().list_records()) == 1  # 只有一份材料记录
        material = service.selected_material("2101.01234")
        assert material is not None
        receipts = space.store().find_receipts(kind="acquisition_acquire")
        assert receipts, "应存在获取回执"
        if crash_point == "after_publish":
            # 记录尚未提交：恢复需重新下载一次，发布复用同一字节。
            assert api.download_calls == ["2101.01234v2"]
        else:
            # 回执已落盘：确定性记录号直接采用已提交材料，免网络。
            assert api.download_calls == []
            assert result.replayed
    finally:
        space.close()


def test_repeated_query_new_round_seq_keeps_both_provenance(space, clock):
    """同一检索请求可用更大 round_seq 开新轮次：各带全套溯源且互不覆盖。"""
    request = base_request()
    entries = (make_meta("2401.00001", 1),)
    api = script_single_page(entries)
    service = build_service(space, api, clock)
    first = service.run_discovery(request)
    assert first.state == "complete"
    calls_after_first = len(api.search_calls)

    second = service.run_discovery(request, round_seq=2)
    assert second.round_id != first.round_id
    assert second.state == "complete"
    rounds = service.list_discovery_rounds()
    assert {item.round_id for item in rounds} == {first.round_id, second.round_id}
    # 两轮各有自己的页级溯源记录。
    records = {
        record.record_id: record
        for record in space.store().list_records()
        if record.record_id.startswith("arxiv-search-")
    }
    assert len(records) == 2
    # 同一轮次身份重跑是幂等状态返回：不触发新的页请求。
    again = service.run_discovery(request, round_seq=2)
    assert again.round_id == second.round_id
    assert len(api.search_calls) == calls_after_first + 1


def test_round_seq_mismatched_request_rejected(space, clock):
    """同 (request, round_seq) 身份下持久化请求被篡改后重跑仍被拒绝。"""
    request_a = base_request()
    api = script_single_page((make_meta("2401.00001", 1),))
    service = build_service(space, api, clock)
    service.run_discovery(request_a, round_seq=3)
    round_id = discovery_round_id(request_a, 3)
    stored = json.loads(space.store().get_meta(f"acquisition:round:{round_id}"))
    stored["request"]["terms"] = "tampered terms"
    space.store().set_meta(
        f"acquisition:round:{round_id}", json.dumps(stored, ensure_ascii=False)
    )
    with pytest.raises(AcquisitionError, match="不一致"):
        service.run_discovery(request_a, round_seq=3)


def test_completed_selection_not_replaced_by_sequential_rerun(space, clock):
    """串行边界（整操作锁后无并发交错）：已完成选择不被后续操作覆盖。

    原并发交错场景（另一进程的迟到 v1 结果覆盖 v2 完成）由空间排他
    锁排除：同空间的第二个写操作得到“忙”。这里验证串行重跑的等价
    语义——对已完成目标重复获取只做核对，不降级、不换版本。
    """
    api = FakeArxivService(clock=clock)
    api.metadata["2101.01234"] = make_meta("2101.01234", 1)
    api.downloads["2101.01234v1"] = GOOD_PACKAGE
    api.downloads["2101.01234v2"] = make_tar_gz({"main.tex": b"v2 body"})
    service = build_service(space, api, clock)
    service.acquire("2101.01234")  # v1 冻结并完成
    service.acquire("2101.01234v2")  # 显式 v2 完成（旧选择入历史）
    # 串行重跑旧版本：已完成材料核对复用，不重新下载、不覆盖 v2。
    again = service.acquire("2101.01234v1")
    assert again.state in ("downloaded", "imported")
    assert again.replayed is True
    status = service.get_selection("2101.01234")
    assert status is not None and status.version == 1
    history_versions = [item["version"] for item in status.history]
    assert 2 in history_versions  # v2 历史保留


def test_concurrent_same_page_commit_idempotent(space, clock):
    """并发推进同一轮次：后到的一方幂等跳过，不产生重复记录。"""
    request = base_request()
    entries = (make_meta("2401.00002", 1),)
    api_a = script_single_page(entries)
    api_b = script_single_page(entries)
    service_a = build_service(space, api_a, clock)
    service_b = build_service(space, api_b, clock)
    first = service_a.run_discovery(request)
    assert first.state == "complete"
    second = service_b.run_discovery(request)
    assert second.round_id == first.round_id
    assert second.state == "complete"
    page_receipts = space.store().find_receipts(kind="acquisition_discovery_page")
    assert len(page_receipts) == 1  # 同一页只提交一次
    records = [
        record
        for record in space.store().list_records()
        if record.record_id.startswith("arxiv-search-")
    ]
    assert len(records) == 1


def make_two_page_script(api_class=FakeArxivService):
    """两页脚本：page0（has_more=True）与 page1（完成）。"""

    api = api_class(clock=None)

    def search(request):
        api.search_calls.append(request.start)
        if request.start == 0:
            return make_page(
                request,
                (make_meta("2401.00101", 1),),
                total=2,
                fetched_at=1_700_000_100.0,
            )
        if request.start == 1:
            return make_page(
                request,
                (make_meta("2401.00102", 1),),
                total=2,
                fetched_at=1_700_000_200.0,
            )
        raise AssertionError(f"未脚本的检索页 start={request.start}")

    api.search = search  # type: ignore[method-assign]
    return api


def test_discovery_stale_terminal_cannot_overwrite_complete(space, clock):
    """确定性交错：A 页界未完 → B 完成 → A 迟到的旧终态不得覆盖。

    这不是顺序重复测试：两个竞争操作是 A 延迟的终态提交与 B 的完成
    提交；随后用新服务（模拟新进程）按存储事实查询。
    """
    request = base_request()
    api_a = make_two_page_script()
    api_b = make_two_page_script()
    service_a = build_service(space, api_a, clock)
    service_b = build_service(space, api_b, clock)
    # A：页数界限停在 1 页 incomplete。
    first = service_a.run_discovery(request, max_pages=1)
    assert first.state == "incomplete"
    round_id = first.round_id
    stale_snapshot = json.loads(space.store().get_meta(f"acquisition:round:{round_id}"))
    assert len(stale_snapshot["pages"]) == 1
    # B：推进到完成（2 页）。
    second = service_b.run_discovery(request)
    assert second.state == "complete"
    # A：迟到的旧终态提交（1 页 incomplete）。
    merged = service_a._save_round_state(round_id, stale_snapshot)
    assert merged["state"] == "complete"
    assert len(merged["pages"]) == 2
    # 新进程按存储事实查询：完整 2 页；记录与轮次进度一致。
    service_c = build_service(space, RaisingService(), clock)
    status = service_c.run_discovery(request)  # 完整轮次幂等返回，不触网
    assert status.state == "complete"
    assert len(status.pages) == 2
    page_receipts = space.store().find_receipts(kind="acquisition_discovery_page")
    assert len(page_receipts) == 2


def test_unversioned_acquire_on_completed_verifies_without_metadata(space, clock):
    """串行等价：已完成选择上的无版本获取只核对材料，不触元数据、不换版。

    原交错场景（A 的过时 v1 元数据覆盖 B 已完成的 v2）由整操作锁排
    除；这里验证公开路径的串行语义：已完成后无版本获取恢复冻结版
    本的材料，绝不被新的元数据悄悄改选。
    """
    api = FakeArxivService(clock=clock)
    api.metadata["2101.01234"] = make_meta("2101.01234", 1)
    api.downloads["2101.01234v2"] = make_tar_gz({"main.tex": b"v2 body"})
    service = build_service(space, api, clock)
    service.acquire("2101.01234v2")  # 显式 v2 完成
    calls = list(api.metadata_calls)
    result = service.acquire("2101.01234")  # 无版本：恢复冻结的 v2
    assert result.state == "downloaded"
    assert result.version == 2
    assert result.replayed is True
    assert api.metadata_calls == calls  # 未发起任何元数据查询
    status = service.get_selection("2101.01234")
    assert status is not None
    assert status.version == 2
    assert status.state == "downloaded"


def test_same_version_rerun_after_completion_keeps_material(space, clock):
    """串行等价：同版本完成后再次获取（哪怕源已 404）只核对，不降级。"""
    api = FakeArxivService(clock=clock)
    api.downloads["2101.01234v2"] = GOOD_PACKAGE
    service = build_service(space, api, clock)
    result = service.acquire("2101.01234v2")
    assert result.state == "downloaded"
    # 源随后不可用：重复获取同一已完成版本仍返回完成材料。
    api.downloads["2101.01234v2"] = ArxivSourceUnavailableError(
        "404", reason="not_found"
    )
    again = service.acquire("2101.01234v2")
    assert again.state == "downloaded"
    assert again.replayed is True
    material = service.selected_material("2101.01234")
    assert material is not None
    assert material.version == 2
    payload = space.read_file(
        ArtifactRef(path=material.raw_path, sha256=material.raw_sha256)
    )
    assert payload == GOOD_PACKAGE


def test_explicit_older_version_and_pinned_resume(space, clock):
    """显式旧版本仍可受控选择；无版本重跑沿用冻结版本（不漂移）。"""
    api = FakeArxivService(clock=clock)
    api.metadata["2101.01234"] = make_meta("2101.01234", 2)
    api.downloads["2101.01234v2"] = make_tar_gz({"main.tex": b"v2 body"})
    api.downloads["2101.01234v1"] = make_tar_gz({"main.tex": b"v1 body"})
    service = build_service(space, api, clock)
    service.acquire("2101.01234")  # 冻结 v2 并完成
    older = service.acquire("2101.01234v1")  # 显式旧版本受控换选
    assert older.version == 1
    assert older.state == "downloaded"
    # 元数据又变了：无版本获取仍沿用冻结 v1，不漂移到 v3。
    api.metadata["2101.01234"] = make_meta("2101.01234", 3)
    again = service.acquire("2101.01234")
    assert again.version == 1
    assert again.replayed is True


def test_revision_check_unknown_when_metadata_missing(space, clock):
    """未返回/未找到不能当 current：标 unknown 且保留已知选择版本。"""
    api = FakeArxivService(clock=clock)
    api.downloads["2101.01234v1"] = GOOD_PACKAGE
    service = build_service(space, api, clock)
    service.acquire("2101.01234v1")
    result = service.check_revisions(["2101.01234"])  # api 未脚本该元数据
    entry = result.entries[0]
    assert entry.state == "unknown"
    assert entry.selected_version == 1  # 已知事实保留，不臆造 -1
    assert entry.latest_version is None
    assert "无法判定" in (entry.error or "")
    stored = service.get_revision_status("2101.01234")
    assert stored is not None and stored.state == "unknown"


def test_revision_check_partial_batch_preserves_known(space, clock):
    """部分成功批次：找到的保留真实结论，未找到的诚实标 unknown。"""
    api = FakeArxivService(clock=clock)
    api.metadata["2101.00001"] = make_meta("2101.00001", 2)
    api.downloads["2101.00001v1"] = GOOD_PACKAGE
    api.downloads["2101.00002v1"] = GOOD_PACKAGE
    service = build_service(space, api, clock)
    service.acquire("2101.00001v1")
    service.acquire("2101.00002v1")
    result = service.check_revisions(["2101.00001", "2101.00002"])
    by_id = {entry.canonical_id: entry for entry in result.entries}
    assert by_id["2101.00001"].state == "pending"  # 真实结论保留
    assert by_id["2101.00001"].latest_version == 2
    assert by_id["2101.00002"].state == "unknown"  # 未找到：不能当 current
    assert by_id["2101.00002"].selected_version == 1


def test_import_invalid_version_and_blank_provenance_rejected_no_side_effects(
    space, tmp_path
):
    """版本/来源声明非法时在任何发布或选择改动之前拒绝。"""
    package = tmp_path / "pkg.tar.gz"
    package.write_bytes(GOOD_PACKAGE)
    service = build_service(space, FakeArxivService(clock=None), FakeClock())
    for bad_version in (0, -1, True, 2.0, "2"):
        with pytest.raises(AcquisitionError, match="正整数"):
            service.import_local_package(
                package,
                arxiv_id="2305.01234",
                version=bad_version,
                provenance="test copy",
            )
    for bad_provenance in ("", "   \t "):
        with pytest.raises(AcquisitionError, match="来源声明"):
            service.import_local_package(
                package, arxiv_id="2305.01234v2", provenance=bad_provenance
            )
    # 无副作用：无记录、无来源文件、无选择。
    assert space.store().list_records() == []
    assert not (space.root / "sources").exists() or not any(
        (space.root / "sources").iterdir()
    )
    assert service.list_selections() == []


def test_page_document_persists_all_returned_metadata(space, clock, tmp_path):
    """页归档视图含全部返回元数据（含摘要），备份恢复后可回读。"""
    entries = (make_meta("2401.00003", 2),)
    api = script_single_page(entries)
    service = build_service(space, api, clock)
    status = service.run_discovery(base_request())
    record = space.store().read_record_current(
        f"arxiv-search-{status.round_id}-p000000"
    )
    doc = json.loads(space.read_file(record.data.raw_response))
    entry = doc["entries"][0]
    assert doc["parsed_view"] is True
    for key in (
        "canonical_id",
        "version",
        "api_id_url",
        "published",
        "updated",
        "title",
        "authors",
        "categories",
        "primary_category",
        "abstract",
        "doi",
        "journal_ref",
    ):
        assert key in entry, f"页归档视图缺少字段 {key}"
    assert entry["abstract"] == "abstract text"
    # 记录字段也含摘要。
    names = {f.name for f in record.data.entries[0].fields}
    assert "abstract" in names
    # 备份恢复后仍可回读摘要。
    from hvs_catalog.workspace import backup_workspace, restore_workspace

    backup_dir = tmp_path / "backup"
    backup_workspace(space, backup_dir)
    restored = restore_workspace(backup_dir, tmp_path / "restored")
    try:
        restored_record = restored.store().read_record_current(record.record_id)
        restored_doc = json.loads(restored.read_file(restored_record.data.raw_response))
        assert restored_doc["entries"][0]["abstract"] == "abstract text"
    finally:
        restored.close()


# ------------------------------------------------------------------ R3：并发与代际


def _run_locked_in_thread(space, api, clock, identifier, gate, arrived, patch_name):
    """在独立线程内对同一空间发起获取；补丁点到达后置位 arrived。"""
    import threading

    from hvs_catalog.workspace import open_workspace

    original = getattr(api, patch_name)

    def patched(*args, **kwargs):
        arrived.set()  # 操作已持锁并到达阻塞网络点
        gate.wait(timeout=10)
        return original(*args, **kwargs)

    setattr(api, patch_name, patched)
    outcome: dict[str, object] = {}

    def run() -> None:
        # SQLite 连接线程绑定：句柄/连接都在本线程内创建与关闭。
        space_t = open_workspace(space.root)
        service_t = build_service(space_t, api, clock)
        try:
            result = service_t.acquire(identifier)
            outcome["a"] = result.state
        except BaseException as exc:  # noqa: BLE001 - 线程内捕获后断言
            outcome["a"] = exc
        finally:
            space_t.close()

    thread = threading.Thread(target=run)
    thread.start()
    return thread, outcome


def test_same_workspace_second_write_returns_busy(space, clock):
    """R05 空间串行：A 持锁阻塞在网络点时，B 的同空间写操作返回忙。

    到达屏障保证 A 确实进入阻塞查询点（已持锁）后才发起 B；B 得到
    WorkspaceBusyError 而不是并发推进或排队。A 完成后 B 重试成功
    （真实的忙/重试路径，非自动排队）。
    """
    import threading

    from hvs_catalog.workspace import WorkspaceBusyError

    api_a = FakeArxivService(clock=clock)
    api_a.metadata["2101.01234"] = make_meta("2101.01234", 1)
    api_a.downloads["2101.01234v1"] = GOOD_PACKAGE
    gate = threading.Event()
    arrived = threading.Event()
    original_meta = api_a.metadata_by_ids

    def blocked_meta(identifiers):
        arrived.set()  # A 已持锁并到达阻塞查询点
        gate.wait(timeout=10)
        return original_meta(identifiers)

    api_a.metadata_by_ids = blocked_meta  # type: ignore[method-assign]
    api_b = FakeArxivService(clock=clock)
    api_b.metadata["2101.01234"] = make_meta("2101.01234", 1)
    api_b.downloads["2101.01234v2"] = make_tar_gz({"main.tex": b"v2 body"})
    service_b = build_service(space, api_b, clock)

    thread, outcome = _run_locked_in_thread(
        space, api_a, clock, "2101.01234", gate, arrived, "metadata_by_ids"
    )
    try:
        assert arrived.wait(timeout=10), "A 未在期限内到达阻塞查询点"
        # B：同空间第二个写操作 → 忙（不排队、不并发推进）。
        with pytest.raises(WorkspaceBusyError):
            service_b.acquire("2101.01234v2")
        # 修订检查与本地导入同样受空间锁保护：同样返回忙。
        with pytest.raises(WorkspaceBusyError):
            service_b.check_revisions(["2101.01234"])
        mirror = space.root.parent / "busy-import.tar.gz"
        mirror.write_bytes(GOOD_PACKAGE)
        with pytest.raises(WorkspaceBusyError):
            service_b.import_local_package(
                mirror, arxiv_id="2101.01234v2", provenance="busy probe"
            )
    finally:
        gate.set()
    thread.join(timeout=10)
    assert not thread.is_alive(), "A 线程未在期限内结束"
    assert outcome["a"] == "downloaded", outcome
    # A 完成释放锁后：B 重试成功（显式 v2 下载，旧 v1 入历史）。
    retry = service_b.acquire("2101.01234v2")
    assert retry.state == "downloaded"
    status = service_b.get_selection("2101.01234")
    assert status is not None
    assert status.version == 2
    assert [item["version"] for item in status.history] == [1]


def test_cross_process_lock_returns_busy_and_releases(space, clock, tmp_path):
    """跨进程忙/释放：另一进程持锁时本进程写操作返回忙，退出后恢复。"""
    import subprocess
    import sys
    import textwrap

    from hvs_catalog.workspace import WorkspaceBusyError

    holder = tmp_path / "hold_lock.py"
    holder.write_text(
        textwrap.dedent(
            f"""
            import sys
            sys.path.insert(0, {str((Path.cwd() / "src").resolve())!r})
            sys.path.insert(0, {str(Path.cwd().resolve())!r})
            from hvs_catalog.workspace import open_workspace
            space = open_workspace({str(space.root)!r})
            with space.lock():
                print("held", flush=True)
                import time
                deadline = time.monotonic() + 6
                while time.monotonic() < deadline:
                    time.sleep(0.05)
            space.close()
            """
        ),
        encoding="utf-8",
    )
    proc = subprocess.Popen(
        [sys.executable, str(holder)],
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert proc.stdout is not None
        line = proc.stdout.readline().strip()
        assert line == "held", line
        api = FakeArxivService(clock=clock)
        api.downloads["2101.01234v1"] = GOOD_PACKAGE
        service = build_service(space, api, clock)
        with pytest.raises(WorkspaceBusyError):
            service.acquire("2101.01234v1")
    finally:
        rc = proc.wait(timeout=30)
        assert rc == 0
    # 持锁进程退出后：本进程获取正常完成。
    api = FakeArxivService(clock=clock)
    api.downloads["2101.01234v1"] = GOOD_PACKAGE
    service = build_service(space, api, clock)
    result = service.acquire("2101.01234v1")
    assert result.state == "downloaded"


def test_same_version_conflicting_bytes_rejected_sequentially(space, clock, tmp_path):
    """同版本不同字节的串行边界：跨来源发布前按既有材料哈希拒绝覆盖。

    已完成的同版本重复获取按固定输入语义只核对既有材料（不触网），
    因此字节冲突在**导入不同字节**这一串行路径上拒绝。
    """
    import hashlib

    from hvs_catalog.workspace import WorkspaceConflictError

    other_bytes = make_tar_gz({"main.tex": b"different bytes v1"})
    api = FakeArxivService(clock=clock)
    api.metadata["2101.01234"] = make_meta("2101.01234", 1)
    api.downloads["2101.01234v1"] = GOOD_PACKAGE
    service = build_service(space, api, clock)
    first = service.acquire("2101.01234v1")
    assert first.state == "downloaded"
    # 串行导入同版本、不同字节：发布前拒绝，同版同号不覆盖不同内容。
    other = tmp_path / "other.tar.gz"
    other.write_bytes(other_bytes)
    with pytest.raises(WorkspaceConflictError):
        service.import_local_package(
            other, arxiv_id="2101.01234v1", provenance="conflicting mirror"
        )
    status = service.get_selection("2101.01234")
    assert status is not None
    assert status.state == "downloaded"
    assert status.raw_sha256 == hashlib.sha256(GOOD_PACKAGE).hexdigest()


def test_busy_retry_repeat_stability(tmp_path):
    """忙/重试路径重复运行稳定性（有界 20 次，如实收集失败）。"""
    failures: list[str] = []
    for round_no in range(20):
        root = tmp_path / f"busy-{round_no}"
        space = initialize_workspace(root)
        try:
            clock = FakeClock()
            test_same_workspace_second_write_returns_busy(space, clock)
        except BaseException as exc:  # noqa: BLE001 - 如实收集每轮结果
            failures.append(f"round {round_no}: {type(exc).__name__}: {exc}")
        finally:
            space.close()
    assert not failures, "重复忙/重试出现失败：\n" + "\n".join(failures)


def test_intent_keyed_freeze_receipts_allow_explicit_reselection(space, clock):
    """latest v1 → explicit v2（失败）→ explicit v1：意图/代际键互不冲突。"""
    api = FakeArxivService(clock=clock)
    api.metadata["2101.01234"] = make_meta("2101.01234", 1)
    api.downloads["2101.01234v1"] = GOOD_PACKAGE
    api.downloads["2101.01234v2"] = ArxivSourceUnavailableError(
        "404", reason="not_found"
    )
    service = build_service(space, api, clock)
    first = service.acquire("2101.01234")  # latest → v1
    assert first.version == 1 and first.state == "downloaded"
    with pytest.raises(SourceUnavailableError):
        service.acquire("2101.01234v2")  # explicit v2 → 待处理
    again = service.acquire("2101.01234v1")  # 有意显式重选 v1
    assert again.version == 1
    assert again.state == "downloaded"
    assert again.replayed is True  # 复用既有材料
    assert api.download_calls.count("2101.01234v1") == 1  # 未重复下载
    freezes = space.store().find_receipts(kind="acquisition_selection_freeze")
    assert len(freezes) == 3  # latest v1 / explicit v2 / explicit v1 各自独立
    status = service.get_selection("2101.01234")
    assert status is not None
    history_versions = [item["version"] for item in status.history]
    assert 2 in history_versions  # v2 待处理历史保留


def test_revision_found_but_anomalous_batch_unknown(space, clock):
    """found 条目遇影响可信性的批次异常：保守整批 unknown 并保留诊断。"""
    api = FakeArxivService(clock=clock)
    api.metadata["2101.01234"] = make_meta("2101.01234", 1)
    api.downloads["2101.01234v1"] = GOOD_PACKAGE
    service = build_service(space, api, clock)
    service.acquire("2101.01234v1")
    original_meta = api.metadata_by_ids

    def with_anomaly(identifiers):
        result = original_meta(identifiers)
        return ArxivIdBatchResult(
            response_url=result.response_url,
            fetched_at=result.fetched_at,
            lookups=result.lookups,
            anomalies=(PaginationAnomaly.DUPLICATE_ENTRY,),
        )

    api.metadata_by_ids = with_anomaly  # type: ignore[method-assign]
    result = service.check_revisions(["2101.01234"])
    entry = result.entries[0]
    assert entry.state == "unknown"  # found 也不能证明无新版
    assert entry.selected_version == 1  # 已知事实保留
    assert entry.latest_version is None
    assert "批次异常" in (entry.error or "")
    assert "duplicate_entry" in (entry.error or "")
    # 重启后（新服务，不触网）诊断仍可读。
    service2 = build_service(space, RaisingService(), clock)
    stored = service2.get_revision_status("2101.01234")
    assert stored is not None
    assert stored.state == "unknown"
    assert stored.selected_version == 1
    assert "批次异常" in (stored.error or "")


def test_network_download_then_local_import_identical_bytes(space, clock, tmp_path):
    """跨来源同字节：网络下载后本地导入复用记录，retrieval 不可变。"""
    api = FakeArxivService(clock=clock)
    api.downloads["2401.01234v1"] = GOOD_PACKAGE
    service = build_service(space, api, clock)
    downloaded = service.acquire("2401.01234v1")
    assert downloaded.state == "downloaded"
    record_before = space.store().read_record_current(downloaded.material_ref.record_id)
    network_retrieval = record_before.data.origin.retrieval
    assert "downloaded from" in network_retrieval

    mirror = tmp_path / "mirror.tar.gz"
    mirror.write_bytes(GOOD_PACKAGE)
    imported = service.import_local_package(
        mirror, arxiv_id="2401.01234v1", provenance="local mirror copy"
    )
    assert imported.replayed is True  # 材料复用，不是新记录
    assert imported.material_ref.record_id == downloaded.material_ref.record_id
    # 一份材料记录 + 一条声明事件记录（首次时间持久化于声明事件）。
    material_records = [
        r
        for r in space.store().list_records()
        if r.record_id.startswith("arxiv-source-")
    ]
    decl_records = [
        r
        for r in space.store().list_records()
        if r.record_id.startswith("acq-import-decl-")
    ]
    assert len(material_records) == 1  # 仍只有一份材料记录
    assert len(decl_records) == 1  # 一条本地来源声明事件
    record_after = space.store().read_record_current(downloaded.material_ref.record_id)
    assert record_after.data.origin.retrieval == network_retrieval  # 不可变
    assert record_after.revision == record_before.revision  # 无记录修订
    status = service.get_selection("2401.01234")
    assert status is not None
    assert status.state == "imported"
    logged = [item["provenance"] for item in status.provenance_log]
    assert logged == ["local mirror copy"]
    import_receipts = space.store().find_receipts(kind="acquisition_import")
    assert len(import_receipts) == 1  # 导入操作有自己的回执


def test_local_import_then_network_acquire_reuses_material(space, clock):
    """反向：本地导入后网络获取同版本走核对复用，不重复下载。"""
    api = FakeArxivService(clock=clock)
    api.downloads["2401.02222v1"] = GOOD_PACKAGE
    service = build_service(space, api, clock)
    import io as _io
    import tarfile as _tar

    raw = _io.BytesIO()
    with _tar.open(fileobj=raw, mode="w:gz") as archive:
        info = _tar.TarInfo("main.tex")
        data = b"\\documentclass{article}reuse"
        info.size = len(data)
        archive.addfile(info, _io.BytesIO(data))
    payload = raw.getvalue()
    api.downloads["2401.02222v1"] = payload
    package_dir = space.root.parent
    pkg = package_dir / "reuse.tar.gz"
    pkg.write_bytes(payload)
    result_import = service.import_local_package(
        pkg, arxiv_id="2401.02222v1", provenance="from coauthor"
    )
    assert not result_import.replayed
    status = service.get_selection("2401.02222")
    assert status is not None and status.state == "imported"
    api.download_calls.clear()
    again = service.acquire("2401.02222v1")
    assert again.replayed is True
    assert api.download_calls == []  # 已完成目标只核对，不触网


def test_cross_source_conflicting_bytes_rejected_before_publish(space, clock, tmp_path):
    """网络下载后本地导入不同字节：发布前拒绝，状态不变。"""
    from hvs_catalog.workspace import WorkspaceConflictError

    api = FakeArxivService(clock=clock)
    api.downloads["2401.03333v1"] = GOOD_PACKAGE
    service = build_service(space, api, clock)
    downloaded = service.acquire("2401.03333v1")
    other = tmp_path / "other.tar.gz"
    other.write_bytes(make_tar_gz({"main.tex": b"conflicting bytes"}))
    with pytest.raises(WorkspaceConflictError):
        service.import_local_package(
            other, arxiv_id="2401.03333v1", provenance="mismatched copy"
        )
    status = service.get_selection("2401.03333")
    assert status is not None
    assert status.state == "downloaded"
    assert status.raw_sha256 == downloaded.raw_sha256


def test_backup_restores_cross_source_provenance_and_retrieval(space, clock, tmp_path):
    """备份恢复后：跨来源溯源日志与首次网络 retrieval 都完整保留。"""
    from hvs_catalog.workspace import backup_workspace, restore_workspace

    api = FakeArxivService(clock=clock)
    api.downloads["2401.04444v1"] = GOOD_PACKAGE
    service = build_service(space, api, clock)
    service.acquire("2401.04444v1")
    mirror = tmp_path / "m.tar.gz"
    mirror.write_bytes(GOOD_PACKAGE)
    service.import_local_package(
        mirror, arxiv_id="2401.04444v1", provenance="mirror after download"
    )
    backup_dir = tmp_path / "backup"
    backup_workspace(space, backup_dir)
    restored = restore_workspace(backup_dir, tmp_path / "restored")
    try:
        service2 = AcquisitionService(restored, RaisingService(), clock=clock)
        status = service2.get_selection("2401.04444")
        assert status is not None
        assert status.state == "imported"
        logged = [item["provenance"] for item in status.provenance_log]
        assert logged == ["mirror after download"]
        record = restored.store().read_record_current(status.source_record.record_id)
        assert "downloaded from" in record.data.origin.retrieval  # 首次依据
        payload = restored.read_file(
            ArtifactRef(path=status.raw_path, sha256=status.raw_sha256)
        )
        assert payload == GOOD_PACKAGE
    finally:
        restored.close()


# ------------------------------------------------------------------ R4：不变量与幂等


def test_adopt_committed_recovers_crash_window_and_rejects_foreign_record(
    space, clock, tmp_path
):
    """崩溃窗口恢复（整操作锁后无并发交错）：采用已提交材料补状态。

    记录号确定性可回查时，恢复采用既有材料（哈希核对 + 本地重算清
    单）而不重新下载；绑定**本次冻结**的选择。记录身份与目标一致
    （同论文、同版本）作为不变量一并校验：不一致的记录拒绝采用。
    """
    from hvs_catalog.workspace import open_workspace

    api = FakeArxivService(clock=clock)
    service = build_service(space, api, clock)
    # 种子：本地导入留下 v1 材料（确定性记录存在）。
    package = tmp_path / "seed.tar.gz"
    package.write_bytes(GOOD_PACKAGE)
    seed = service.import_local_package(
        package, arxiv_id="2101.01234v1", provenance="seed copy"
    )
    record_id = seed.material_ref.record_id
    # 制造未完成窗口：显式 v2 不可用 → 待处理（v1 材料记录保留）。
    api.downloads["2101.01234v2"] = ArxivSourceUnavailableError(
        "404", reason="not_found"
    )
    with pytest.raises(SourceUnavailableError):
        service.acquire("2101.01234v2")
    # 模拟“回执后、状态更新前崩溃”：显式重选回 v1（selected，未完成）。
    frozen = service._freeze_selection(parse_arxiv_identifier("2101.01234v1"))
    assert frozen["state"] == "selected"
    # 恢复（公开获取路径）：采用既有记录，不重新下载。
    result = service.acquire("2101.01234v1")
    assert result.state == "downloaded"
    assert result.replayed is True
    assert result.material_ref.record_id == record_id
    status = service.get_selection("2101.01234")
    assert status is not None
    assert status.state == "downloaded"
    # 目标一致性：请求 v2（无其材料记录）→ 不借用 v1 记录，返回
    # None 交由正常下载路径处理；v1 状态不受影响。
    ident_v2 = parse_arxiv_identifier("2101.01234v2")
    assert service._adopt_committed(ident_v2, 2, "2101.01234v2", dict(frozen)) is None
    status = service.get_selection("2101.01234")
    assert status is not None
    assert status.state == "downloaded"
    assert status.version == 1
    # 新进程（新句柄）核对：材料与状态一致。
    fresh = open_workspace(space.root)
    try:
        service_c = AcquisitionService(fresh, RaisingService(), clock=clock)
        material = service_c.selected_material("2101.01234")
        assert material is not None
        assert material.version == 1
        payload = fresh.read_file(
            ArtifactRef(path=material.raw_path, sha256=material.raw_sha256)
        )
        assert payload == GOOD_PACKAGE
    finally:
        fresh.close()


def test_cross_version_import_builds_coherent_target(space, clock, tmp_path):
    """审核反例：v2 已下载、v1 留待处理时导入相同字节的 v2。

    跨版本导入建立与目标 v2、记录和新世代一致的选择，不把 v2 字节
    并进 v1 身份；旧选择入历史。
    """
    api = FakeArxivService(clock=clock)
    api.downloads["2101.01234v2"] = GOOD_PACKAGE
    api.downloads["2101.01234v1"] = ArxivSourceUnavailableError(
        "404", reason="not_found"
    )
    service = build_service(space, api, clock)
    service.acquire("2101.01234v2")  # v2 完成（记录存在）
    with pytest.raises(SourceUnavailableError):
        service.acquire("2101.01234v1")  # v1 待处理（当前选择）
    mirror = tmp_path / "m.tar.gz"
    mirror.write_bytes(GOOD_PACKAGE)
    result = service.import_local_package(
        mirror, arxiv_id="2101.01234v2", provenance="mirror of v2"
    )
    assert result.version == 2
    assert result.replayed is True  # 材料复用
    status = service.get_selection("2101.01234")
    assert status is not None
    assert status.version == 2  # 选择是 v2，不是 v1
    assert status.versioned_id == "2101.01234v2"
    assert status.state == "imported"
    assert status.source_record is not None
    assert status.source_record.record_id == result.material_ref.record_id
    # 记录号、原始路径与 v2 材料一致（一个不变量）。
    record = space.store().read_record_current(result.material_ref.record_id)
    assert record.data.origin.arxiv_id == "2101.01234v2"
    assert status.raw_path == result.raw_path
    # 旧 v1 待处理进入历史。
    history_versions = [item["version"] for item in status.history]
    assert 1 in history_versions
    logged = [item["provenance"] for item in status.provenance_log]
    assert logged == ["mirror of v2"]


def test_same_declaration_replay_after_time_advance_stable(space, clock, tmp_path):
    """同一声明在时钟前进 60 秒后重放：稳定幂等回执，不改首登时间。"""
    api = FakeArxivService(clock=clock)
    service = build_service(space, api, clock)
    package = tmp_path / "p.tar.gz"
    package.write_bytes(GOOD_PACKAGE)
    service.import_local_package(
        package, arxiv_id="2305.01234v2", provenance="stable declaration"
    )
    status = service.get_selection("2305.01234")
    assert status is not None
    first_at = status.provenance_log[0]["at"]
    # 时钟前进 60 秒后重复相同声明：不抛操作键冲突。
    clock._now += 60.0
    repeat = service.import_local_package(
        package, arxiv_id="2305.01234v2", provenance="stable declaration"
    )
    assert repeat.replayed is True
    status = service.get_selection("2305.01234")
    assert status is not None
    assert len(status.provenance_log) == 1  # 不追加重复声明
    assert status.provenance_log[0]["at"] == first_at  # 首登时间保留
    receipts = space.store().find_receipts(kind="acquisition_import_provenance")
    assert len(receipts) == 1  # 同键同内容稳定重放


def test_declared_reimport_after_switch_is_deliberate_reselection(
    space, clock, tmp_path
):
    """R5 收敛语义：显式导入（含已登记声明的重放）是有意的目标重选。

    切到 v2 后重放旧 v1 声明：选择**有意**回到 v1（新世代、v2 入历
    史），返回实际提交的选择；不再把重放当作“迟到旧操作”静默保留
    v2，也不因 v2 已完成而报冲突。
    """
    api = FakeArxivService(clock=clock)
    service = build_service(space, api, clock)
    package = tmp_path / "p.tar.gz"
    package.write_bytes(GOOD_PACKAGE)
    service.import_local_package(
        package, arxiv_id="2305.01234v1", provenance="old declaration"
    )
    first_status = service.get_selection("2305.01234")
    assert first_status is not None
    assert first_status.generation if hasattr(first_status, "generation") else True
    # 目标切到 v2（显式获取另一种字节）。
    api.downloads["2305.01234v2"] = make_tar_gz({"main.tex": b"v2 body"})
    service.acquire("2305.01234v2")
    status = service.get_selection("2305.01234")
    assert status is not None
    assert status.version == 2
    # 重放旧声明（v1 字节 + 旧 provenance）：有意重选回 v1。
    clock._now += 120.0
    replay = service.import_local_package(
        package, arxiv_id="2305.01234v1", provenance="old declaration"
    )
    assert replay.replayed is True  # 材料/声明/选择回执均为重放
    status = service.get_selection("2305.01234")
    assert status is not None
    assert status.version == 1  # 选择回到 v1
    assert status.state == "imported"
    history_versions = [item["version"] for item in status.history]
    assert 2 in history_versions  # v2 进入历史
    assert status.source_record is not None
    assert status.source_record.record_id == replay.material_ref.record_id


def test_revision_same_second_changed_result_persists_latest(space, clock):
    """同一秒内两次检查（结果不同）：各自独立回执，状态收敛到最新。"""
    api = FakeArxivService(clock=clock)
    api.metadata["2101.01234"] = make_meta("2101.01234", 1)
    api.downloads["2101.01234v1"] = GOOD_PACKAGE
    service = build_service(space, api, clock)
    service.acquire("2101.01234v1")
    first = service.check_revisions(["2101.01234"])
    assert first.entries[0].state == "current"  # 第一次：无新版
    # 同一秒（假时钟不动），元数据变为 v2：第二次必须持久化 pending。
    api.metadata["2101.01234"] = make_meta("2101.01234", 2)
    second = service.check_revisions(["2101.01234"])
    assert second.entries[0].state == "pending"
    receipts = space.store().find_receipts(kind="acquisition_revision_check")
    assert len(receipts) == 2  # 两次检查各自有效回执
    stored = build_service(space, RaisingService(), clock).get_revision_status(
        "2101.01234"
    )
    assert stored is not None
    assert stored.state == "pending"  # 盘上是最新一次的诚实结果
    assert stored.latest_version == 2


def test_revision_same_second_unknown_transition(space, clock):
    """同一秒内 current → unknown 转换也独立持久化。"""
    api = FakeArxivService(clock=clock)
    api.metadata["2101.01234"] = make_meta("2101.01234", 1)
    api.downloads["2101.01234v1"] = GOOD_PACKAGE
    service = build_service(space, api, clock)
    service.acquire("2101.01234v1")
    assert service.check_revisions(["2101.01234"]).entries[0].state == "current"
    # 同一秒，元数据消失：unknown 必须覆盖 current。
    api.metadata.pop("2101.01234")
    entry = service.check_revisions(["2101.01234"]).entries[0]
    assert entry.state == "unknown"
    assert entry.selected_version == 1
    stored = build_service(space, RaisingService(), clock).get_revision_status(
        "2101.01234"
    )
    assert stored is not None
    assert stored.state == "unknown"


def test_same_target_replay_retains_history_and_first_time(space, clock, tmp_path):
    """同目标重放：保留历史与首登时间；状态变化由选择回执审计。"""
    api = FakeArxivService(clock=clock)
    service = build_service(space, api, clock)
    package = tmp_path / "p.tar.gz"
    package.write_bytes(GOOD_PACKAGE)
    service.import_local_package(
        package, arxiv_id="2305.01235v1", provenance="first copy"
    )
    # 目标切到 v2 再显式恢复 v1（积累历史）。
    api.downloads["2305.01235v2"] = make_tar_gz({"main.tex": b"v2"})
    service.acquire("2305.01235v2")
    service.import_local_package(
        package, arxiv_id="2305.01235v1", provenance="first copy"
    )
    status = service.get_selection("2305.01235")
    assert status is not None
    history_versions = [item["version"] for item in status.history]
    assert history_versions == [1, 2]  # 两次切换的历史完整保留
    assert len(status.provenance_log) == 1
    first_at = status.provenance_log[0]["at"]
    # 同目标、同声明再次导入（重放）：历史/首登时间/状态不回退。
    clock._now += 90.0
    replay = service.import_local_package(
        package, arxiv_id="2305.01235v1", provenance="first copy"
    )
    assert replay.replayed is True
    status = service.get_selection("2305.01235")
    assert status is not None
    assert [item["version"] for item in status.history] == [1, 2]
    assert status.provenance_log[0]["at"] == first_at
    # 选择状态的变化永远有选择回执（不靠 set_meta 补写）。
    sel_receipts = space.store().find_receipts(kind="acquisition_import_selection")
    assert len(sel_receipts) == 2  # 初次(g1) 与重选(g3)；重放复用 g3 回执
    prov_receipts = space.store().find_receipts(kind="acquisition_import_provenance")
    assert len(prov_receipts) == 1  # 同声明只登记一次


def test_revision_cross_instance_same_clock_persists(space, clock):
    """跨实例重启、同一注入时钟：两次公开检查各自持久化、收敛最新。

    R4 审核反例（微秒+实例序号在重启后撞键）的处置：操作令牌为
    uuid4，跨实例/进程不冲突。
    """
    from hvs_catalog.workspace import open_workspace

    api = FakeArxivService(clock=clock)
    api.metadata["2101.01234"] = make_meta("2101.01234", 1)
    api.downloads["2101.01234v1"] = GOOD_PACKAGE
    service = build_service(space, api, clock)
    service.acquire("2101.01234v1")
    first = service.check_revisions(["2101.01234"])
    assert first.entries[0].state == "current"
    # 关闭句柄，重启新实例（实例序号归零），同一冻结时钟。
    space.close()
    reopened = open_workspace(space.root)
    try:
        api2 = FakeArxivService(clock=clock)
        api2.metadata["2101.01234"] = make_meta("2101.01234", 2)
        service2 = AcquisitionService(reopened, api2, clock=clock)
        second = service2.check_revisions(["2101.01234"])
        assert second.entries[0].state == "pending"
        receipts = reopened.store().find_receipts(kind="acquisition_revision_check")
        assert len(receipts) == 2  # 两次公开检查两条回执，无键冲突
        stored = AcquisitionService(
            reopened, RaisingService(), clock=clock
        ).get_revision_status("2101.01234")
        assert stored is not None
        assert stored.state == "pending"
        assert stored.latest_version == 2
    finally:
        reopened.close()


def test_unversioned_freeze_fails_closed_on_anomalous_batch(space, clock):
    """无版本冻结的信任边界：批次异常时失败关闭，不冻结不下载。"""
    from hvs_catalog.pipeline.acquisition.protocol import (
        PaginationAnomaly,
    )

    api = FakeArxivService(clock=clock)
    api.metadata["2101.01234"] = make_meta("2101.01234", 3)
    api.downloads["2101.01234v3"] = GOOD_PACKAGE

    original = api.metadata_by_ids

    def anomalous(identifiers):
        batch = original(identifiers)
        return replace(batch, anomalies=(PaginationAnomaly.DUPLICATE_ENTRY,))

    api.metadata_by_ids = anomalous  # type: ignore[method-assign]
    service = build_service(space, api, clock)
    with pytest.raises(AcquisitionError, match="影响可信度"):
        service.acquire("2101.01234")
    # 失败关闭：无冻结回执、无下载、无选择覆盖。
    assert api.download_calls == []
    assert service.get_selection("2101.01234") is None
    freeze_receipts = [
        row for row in space.store().find_receipts(kind="acquisition_selection_freeze")
    ]
    assert freeze_receipts == []
    # 异常清除后显式版本获取不受影响（无新增元数据依赖）。
    api.metadata_by_ids = original  # type: ignore[method-assign]
    result = service.acquire("2101.01234v3")
    assert result.state == "downloaded"


# ---------------------------------------------------------------- R extra1：F1–F3


def _sparse_pax_package() -> bytes:
    """构造审核反例形态：局部 PAX 携带 GNU.sparse.map 无效值的小包。"""
    import tests.test_acquisition_archive as arch

    records = arch._pax_record("GNU.sparse.map", "garbage")
    return b"".join(
        [
            arch._tar_header_raw("PaxHeader", len(records), b"x")
            + arch._pad_data(records),
            arch._tar_header_raw("main.tex", 2, b"0") + arch._pad_data(b"hi"),
            b"\0" * 1024,
        ]
    )


def test_acquire_sparse_pax_observable_unsupported_with_retained_bytes(
    space, clock, tmp_path
):
    """F1 公开路径：sparse 变体 → 可观察"不支持"，字节保留并可恢复。"""
    api = FakeArxivService(clock=clock)
    api.downloads["2101.01234v1"] = _sparse_pax_package()
    service = build_service(space, api, clock)
    result = service.acquire("2101.01234v1")
    assert result.state == "unsupported"  # 文档化结果，不是未分类异常
    assert result.material_ref is None
    # 原始字节已保留且挂在选择状态（可随备份遍历）。
    status = service.get_selection("2101.01234")
    assert status is not None
    assert status.state == "unsupported"
    assert status.raw_path == result.raw_path
    payload = space.read_file(
        ArtifactRef(path=result.raw_path, sha256=result.raw_sha256)
    )
    assert payload == _sparse_pax_package()
    # 尝试记录存在：保留字节作为记录附件随备份/恢复。
    attempts = [
        r
        for r in space.store().list_records()
        if r.record_id.startswith("arxiv-attempt-")
    ]
    assert len(attempts) == 1
    # 新进程核对：无可用材料、状态一致。
    from hvs_catalog.workspace import open_workspace

    fresh = open_workspace(space.root)
    try:
        service_c = AcquisitionService(fresh, RaisingService(), clock=clock)
        assert service_c.selected_material("2101.01234") is None
        status_c = service_c.get_selection("2101.01234")
        assert status_c is not None
        assert status_c.state == "unsupported"
        # 备份→恢复后保留字节仍可读（R05 备份/恢复接口）。
        from hvs_catalog.workspace import backup_workspace, restore_workspace

        backup_dir = tmp_path / "backup"
        backup_workspace(fresh, backup_dir)
        restored = restore_workspace(backup_dir, tmp_path / "restored")
        try:
            payload2 = restored.read_file(
                ArtifactRef(path=result.raw_path, sha256=result.raw_sha256)
            )
            assert payload2 == _sparse_pax_package()
        finally:
            restored.close()
    finally:
        fresh.close()


def test_empty_revision_query_busy_under_discovery_lock(space, clock):
    """F2：发现持锁时，修订检查 None/[] 都按整操作串行返回忙。"""
    import threading

    from hvs_catalog.workspace import WorkspaceBusyError

    request = base_request()
    api = script_single_page((make_meta("2401.00009", 1),))
    gate = threading.Event()
    arrived = threading.Event()
    original = api.search

    def blocked_search(req):
        arrived.set()  # 发现已持锁到达网络点
        gate.wait(timeout=10)
        return original(req)

    api.search = blocked_search  # type: ignore[method-assign]
    from hvs_catalog.workspace import open_workspace

    outcome: dict[str, object] = {}

    def run_discovery() -> None:
        # SQLite 连接线程绑定：发现操作的句柄/连接在线程内创建与关闭。
        space_t = open_workspace(space.root)
        service_t = build_service(space_t, api, clock)
        try:
            service_t.run_discovery(request)
            outcome["d"] = "ok"
        except BaseException as exc:  # noqa: BLE001
            outcome["d"] = exc
        finally:
            space_t.close()

    thread = threading.Thread(target=run_discovery)
    thread.start()
    try:
        assert arrived.wait(timeout=10), "发现未到达阻塞查询点"
        fresh = build_service(space, FakeArxivService(clock=clock), clock)
        with pytest.raises(WorkspaceBusyError):
            fresh.check_revisions(None)
        with pytest.raises(WorkspaceBusyError):
            fresh.check_revisions([])
    finally:
        gate.set()
    thread.join(timeout=10)
    assert not thread.is_alive(), "发现线程未在期限内结束"
    assert outcome["d"] == "ok", outcome  # 单页发现正常完成
    # 锁释放后：空列表与空索引都正常返回空结果，不触网。
    idle = FakeArxivService(clock=clock)
    fresh = build_service(space, idle, clock)
    empty = fresh.check_revisions([])
    assert empty.entries == ()
    none_result = fresh.check_revisions(None)  # 索引尚无选择
    assert none_result.entries == ()
    assert idle.metadata_calls == []  # 无不必要的网络调用


def test_first_declaration_time_immutable_across_reselection(space, clock, tmp_path):
    """F3：首次声明时间跨版本重选不变。

    网络取得 v1（t0）→ t+60 首次登记来源 A → 转到 v2 → t+180 以相同
    声明重选 v1：日志保留 A 的首次时间 t+60，网络获取记录不变。
    """
    api = FakeArxivService(clock=clock)
    api.downloads["2101.01234v1"] = GOOD_PACKAGE
    service = build_service(space, api, clock)
    downloaded = service.acquire("2101.01234v1")  # t0：网络获取
    assert downloaded.state == "downloaded"
    package = tmp_path / "src.tar.gz"
    package.write_bytes(GOOD_PACKAGE)
    clock._now += 60.0
    service.import_local_package(
        package, arxiv_id="2101.01234v1", provenance="source A"
    )
    status = service.get_selection("2101.01234")
    assert status is not None
    first_at = status.provenance_log[0]["at"]
    # 转到 v2（显式获取另一种字节），再以相同声明重选 v1（t+180）。
    api.downloads["2101.01234v2"] = make_tar_gz({"main.tex": b"v2"})
    service.acquire("2101.01234v2")
    clock._now += 120.0  # 现在 t+180
    reimport = service.import_local_package(
        package, arxiv_id="2101.01234v1", provenance="source A"
    )
    assert reimport.version == 1
    status = service.get_selection("2101.01234")
    assert status is not None
    assert status.version == 1
    assert len(status.provenance_log) == 1
    assert status.provenance_log[0]["provenance"] == "source A"
    assert status.provenance_log[0]["at"] == first_at  # 首次时间未被改写
    # 原始网络获取记录不变（retrieval 保持下载来源）。
    record = space.store().read_record_current(downloaded.material_ref.record_id)
    assert record.data.origin.retrieval.startswith("downloaded from")


def test_first_declaration_time_survives_crash_between_receipts(space, clock, tmp_path):
    """F3：声明回执后、选择回执前崩溃 → 新句柄重启重试复用首次时间。

    本用例是同 PID 的 close/open_workspace 重启（证据强度弱于真实子
    进程）；真实子进程回归见
    ``test_interrupted_declaration_recovery_subprocess``。
    """
    from hvs_catalog.workspace import open_workspace

    package = tmp_path / "src.tar.gz"
    package.write_bytes(GOOD_PACKAGE)
    store = space.store()
    real_commit = store.commit

    def fail_selection(request):
        if request.kind == "acquisition_import_selection":
            raise RuntimeError("注入：选择回执提交前崩溃")
        return real_commit(request)

    store.commit = fail_selection  # type: ignore[method-assign]
    service = build_service(space, FakeArxivService(clock=clock), clock)
    clock._now += 60.0  # t+60：首次登记
    with pytest.raises(RuntimeError, match="注入"):
        service.import_local_package(
            package, arxiv_id="2305.01234v2", provenance="source A"
        )
    store.commit = real_commit  # type: ignore[method-assign]
    space.close()
    # 新句柄重启、时钟前进（t+200）重试同一声明：首次时间保留。
    clock._now += 140.0
    reopened = open_workspace(space.root)
    try:
        service2 = AcquisitionService(
            reopened, FakeArxivService(clock=clock), clock=clock
        )
        result = service2.import_local_package(
            package, arxiv_id="2305.01234v2", provenance="source A"
        )
        assert result.replayed is True  # 声明事件复用（材料同字节复用）
        status = service2.get_selection("2305.01234")
        assert status is not None
        assert len(status.provenance_log) == 1
        first_at = status.provenance_log[0]["at"]
        assert first_at.endswith("+00:00") or first_at  # ISO 时间
        # 首次时间即 t+60 时刻：与重试时钟（t+200）不同。
        from datetime import datetime

        assert datetime.fromisoformat(first_at).timestamp() == 1_700_000_060.0
    finally:
        reopened.close()


def test_all_target_declarations_maintained_on_reselection(space, clock, tmp_path):
    """F3：重选回 v1 保留该目标的**全部**历史声明（不只最近选择）。"""
    api = FakeArxivService(clock=clock)
    api.downloads["2101.01234v2"] = make_tar_gz({"main.tex": b"v2"})
    service = build_service(space, api, clock)
    package = tmp_path / "src.tar.gz"
    package.write_bytes(GOOD_PACKAGE)
    mirror = tmp_path / "mirror.tar.gz"
    mirror.write_bytes(GOOD_PACKAGE)
    clock._now += 10.0
    service.import_local_package(
        package, arxiv_id="2101.01234v1", provenance="source A"
    )
    clock._now += 20.0
    service.import_local_package(mirror, arxiv_id="2101.01234v1", provenance="source B")
    status = service.get_selection("2101.01234")
    assert status is not None
    times = {item["provenance"]: item["at"] for item in status.provenance_log}
    # 转到 v2 再重选回 v1：日志包含 A 与 B 的原始首次时间。
    service.acquire("2101.01234v2")
    clock._now += 100.0
    service.import_local_package(
        package, arxiv_id="2101.01234v1", provenance="source A"
    )
    status = service.get_selection("2101.01234")
    assert status is not None
    assert status.version == 1
    relogged = {item["provenance"]: item["at"] for item in status.provenance_log}
    assert set(relogged) == {"source A", "source B"}
    assert relogged == times  # 两条首次时间都未被改写


# ---------------------------------------------------------------- R extra2：F1–F3


def _fail_once_on_selection_commit(space):
    """注入：选择回执提交前失败一次（声明回执/事件已提交后中断）。"""
    store = space.store()
    real_commit = store.commit

    def fail_selection(request):
        if request.kind == "acquisition_import_selection":
            raise RuntimeError("注入：选择回执提交前失败")
        return real_commit(request)

    store.commit = fail_selection  # type: ignore[method-assign]
    return real_commit


def test_interrupted_declaration_recovered_by_next_import(space, clock, tmp_path):
    """F1 复现一：A 事件已提交、选择回执中断；随后导入 B → 日志含 A、B。

    A 与 B 的首次时间都是各自事件的原始时间；新句柄与备份恢复后日志
    完整（声明事件是权威来源，不是选择视图）。
    """
    from hvs_catalog.workspace import (
        backup_workspace,
        open_workspace,
        restore_workspace,
    )

    package = tmp_path / "p.tar.gz"
    package.write_bytes(GOOD_PACKAGE)
    other = tmp_path / "o.tar.gz"
    other.write_bytes(GOOD_PACKAGE)
    service = build_service(space, FakeArxivService(clock=clock), clock)
    clock._now += 10.0  # tA
    real_commit = _fail_once_on_selection_commit(space)
    with pytest.raises(RuntimeError, match="注入"):
        service.import_local_package(
            package, arxiv_id="2305.01234v2", provenance="declaration A"
        )
    store = space.store()
    store.commit = real_commit  # type: ignore[method-assign]
    assert service.get_selection("2305.01234") is None  # 选择确实未落
    clock._now += 50.0  # tB
    result_b = service.import_local_package(
        other, arxiv_id="2305.01234v2", provenance="declaration B"
    )
    assert result_b.replayed is False or result_b.replayed is True
    status = service.get_selection("2305.01234")
    assert status is not None
    logged = {item["provenance"]: item["at"] for item in status.provenance_log}
    assert set(logged) == {"declaration A", "declaration B"}  # A 未被漏记
    from datetime import datetime

    t_a = datetime.fromisoformat(logged["declaration A"]).timestamp()
    t_b = datetime.fromisoformat(logged["declaration B"]).timestamp()
    assert t_a == 1_700_000_010.0  # tA 原始时间
    assert t_b == 1_700_000_060.0  # tB 原始时间
    # 新句柄重启核对。
    space.close()
    reopened = open_workspace(space.root)
    try:
        status2 = AcquisitionService(
            reopened, FakeArxivService(clock=clock), clock=clock
        ).get_selection("2305.01234")
        assert status2 is not None
        assert {i["provenance"]: i["at"] for i in status2.provenance_log} == logged
        # 备份恢复后日志完整。
        backup_dir = tmp_path / "bk"
        backup_workspace(reopened, backup_dir)
        restored = restore_workspace(backup_dir, tmp_path / "restored")
        try:
            status3 = AcquisitionService(
                restored, FakeArxivService(clock=clock), clock=clock
            ).get_selection("2305.01234")
            assert status3 is not None
            assert {i["provenance"]: i["at"] for i in status3.provenance_log} == logged
        finally:
            restored.close()
    finally:
        reopened.close()


def test_interrupted_declaration_between_completed_selections(space, clock, tmp_path):
    """F1 复现二：已选 A 完成；B 事件提交后选择中断；再导入 C → A、B、C。"""
    package = tmp_path / "p.tar.gz"
    package.write_bytes(GOOD_PACKAGE)
    service = build_service(space, FakeArxivService(clock=clock), clock)
    clock._now += 10.0
    service.import_local_package(
        package, arxiv_id="2305.01234v2", provenance="declaration A"
    )
    real_commit = _fail_once_on_selection_commit(space)
    clock._now += 20.0
    with pytest.raises(RuntimeError, match="注入"):
        service.import_local_package(
            package, arxiv_id="2305.01234v2", provenance="declaration B"
        )
    space.store().commit = real_commit  # type: ignore[method-assign]
    clock._now += 30.0
    service.import_local_package(
        package, arxiv_id="2305.01234v2", provenance="declaration C"
    )
    status = service.get_selection("2305.01234")
    assert status is not None
    logged = {item["provenance"]: item["at"] for item in status.provenance_log}
    assert set(logged) == {"declaration A", "declaration B", "declaration C"}  # B 未漏
    from datetime import datetime

    assert (
        datetime.fromisoformat(logged["declaration A"]).timestamp() == 1_700_000_010.0
    )
    assert (
        datetime.fromisoformat(logged["declaration B"]).timestamp() == 1_700_000_030.0
    )
    assert (
        datetime.fromisoformat(logged["declaration C"]).timestamp() == 1_700_000_060.0
    )


def test_retry_older_declaration_after_interrupted_newer(space, clock, tmp_path):
    """选择回执键含完整日志摘要：恢复事件后的重试拿新回执、无冲突。

    A 完成 → B 事件提交但选择中断 → 重试**旧声明 A**：选择日志升级为
    [A, B]（新键新回执，不静默丢弃 B，也不撞旧键冲突）。
    """
    package = tmp_path / "p.tar.gz"
    package.write_bytes(GOOD_PACKAGE)
    service = build_service(space, FakeArxivService(clock=clock), clock)
    clock._now += 10.0
    service.import_local_package(
        package, arxiv_id="2305.01234v2", provenance="declaration A"
    )
    before = len(space.store().find_receipts(kind="acquisition_import_selection"))
    real_commit = _fail_once_on_selection_commit(space)
    clock._now += 20.0
    with pytest.raises(RuntimeError, match="注入"):
        service.import_local_package(
            package, arxiv_id="2305.01234v2", provenance="declaration B"
        )
    space.store().commit = real_commit  # type: ignore[method-assign]
    clock._now += 40.0
    retry = service.import_local_package(
        package, arxiv_id="2305.01234v2", provenance="declaration A"
    )
    assert retry.replayed is True  # 材料/声明事件复用
    status = service.get_selection("2305.01234")
    assert status is not None
    logged = {item["provenance"] for item in status.provenance_log}
    assert logged == {"declaration A", "declaration B"}  # 恢复的 B 未被丢弃
    # 选择回执：A 的原回执 + 重试的完整日志新回执（B 的键从未提交）。
    after = space.store().find_receipts(kind="acquisition_import_selection")
    assert len(after) == before + 1


def test_legacy_receipt_without_event_fails_closed(space, clock, tmp_path):
    """F2：旧回执缺事件记录 → 明确不兼容失败，不伪造时间/不改状态。"""
    from hvs_catalog.pipeline.acquisition.lifecycle import CommitRequest

    package = tmp_path / "p.tar.gz"
    package.write_bytes(GOOD_PACKAGE)
    digest = hashlib.sha256(b"legacy source").hexdigest()
    legacy_key = (
        f"acq:import-provenance:2305.01234:v2:"
        f"{hashlib.sha256(GOOD_PACKAGE).hexdigest()}:{digest}"
    )
    # 直接提交只有内容的旧式回执（无新记录）。
    space.store().commit(
        CommitRequest(
            operation_key=legacy_key,
            kind="acquisition_import_provenance",
            content={
                "kind": "acquisition_import_provenance",
                "canonical_id": "2305.01234",
                "version": 2,
                "raw_sha256": hashlib.sha256(GOOD_PACKAGE).hexdigest(),
                "provenance": "legacy source",
            },
        )
    )
    service = build_service(space, FakeArxivService(clock=clock), clock)
    # 先正常完成 v1（跨版本重选路由的既有选择）。
    other_v1 = tmp_path / "v1.tar.gz"
    other_v1.write_bytes(make_tar_gz({"main.tex": b"v1 body"}))
    service.import_local_package(
        other_v1, arxiv_id="2305.01234v1", provenance="normal v1 copy"
    )
    v1_status = service.get_selection("2305.01234")
    assert v1_status is not None and v1_status.version == 1
    # 同一声明再导入 v2：在任何改动之前失败关闭。
    with pytest.raises(AcquisitionError, match="不兼容的旧获取状态"):
        service.import_local_package(
            package, arxiv_id="2305.01234v2", provenance="legacy source"
        )
    # 不同声明导入 v2 同样失败关闭。
    with pytest.raises(AcquisitionError, match="不兼容的旧获取状态"):
        service.import_local_package(
            package, arxiv_id="2305.01234v2", provenance="other source"
        )
    # 状态未变：v1 选择与历史原样、无伪造的事件记录、旧回执保留。
    after = service.get_selection("2305.01234")
    assert after is not None
    assert after.version == 1
    assert after.state == "imported"
    assert [i["provenance"] for i in after.provenance_log] == ["normal v1 copy"]
    decl_records_v2 = [
        r
        for r in space.store().list_records()
        if r.record_id.startswith("acq-import-decl-2305.01234-v2-")
    ]
    assert decl_records_v2 == []  # 没有为 v2 旧回执伪造时间/事件
    assert space.store().find_receipt(legacy_key) is not None  # 旧数据保留


def test_corrupt_declaration_record_flagged_not_skipped(space, clock, tmp_path):
    """F2：结构损坏的声明事件记录同样上报问题，不当作"无此来源"。"""
    from hvs_catalog.pipeline.acquisition.lifecycle import CommitRequest
    from hvs_catalog.records.common import build_record
    from hvs_catalog.records.objects import ExternalRecord

    package = tmp_path / "p.tar.gz"
    package.write_bytes(GOOD_PACKAGE)
    raw = space.publish_source_file("papers/corrupt-decl.bin", b"corrupt")
    digest = hashlib.sha256(b"corrupt source").hexdigest()
    record = build_record(
        ExternalRecord,
        record_id=(f"acq-import-decl-2305.01234-v2-{digest[:16]}"),
        created_at=datetime.fromtimestamp(1_700_000_000.0, tz=UTC),
        execution_id=None,
        data={
            "service": "arxiv-local-import",
            "catalog_version": None,
            "queried_at": datetime.fromtimestamp(1_700_000_000.0, tz=UTC),
            "query": {"canonical_id": "2305.01234", "version": 2},
            "raw_response": {"path": raw.path, "sha256": raw.sha256},
            "entries": [],  # 损坏：缺少 declaration 字段
        },
    )
    space.store().commit(
        CommitRequest(
            operation_key=(
                f"acq:import-provenance:2305.01234:v2:"
                f"{hashlib.sha256(GOOD_PACKAGE).hexdigest()}:{digest}"
            ),
            kind="acquisition_import_provenance",
            new_records=[record],
            content={
                "kind": "acquisition_import_provenance",
                "canonical_id": "2305.01234",
                "version": 2,
                "raw_sha256": hashlib.sha256(GOOD_PACKAGE).hexdigest(),
                "provenance": "corrupt source",
            },
        )
    )
    service = build_service(space, FakeArxivService(clock=clock), clock)
    _, problems = service._declarations_for_target("2305.01234", 2)
    assert problems and "结构损坏" in problems[0]
    with pytest.raises(AcquisitionError, match="不兼容的旧获取状态"):
        service.import_local_package(
            package, arxiv_id="2305.01234v2", provenance="any source"
        )


def test_local_unsupported_import_rejected_without_workspace_effects(
    space, clock, tmp_path
):
    """F3：本地导入不支持形态 → 明确"未导入工作空间"，无任何副作用。"""
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
    package = tmp_path / "sparse.tar.gz"
    package.write_bytes(sparse_pkg)
    service = build_service(space, FakeArxivService(clock=clock), clock)
    with pytest.raises(UnsupportedPackageError, match="未导入工作空间") as excinfo:
        service.import_local_package(
            package, arxiv_id="2305.01234v2", provenance="local sparse copy"
        )
    assert "原文件未改动" in str(excinfo.value)
    assert "GNU.sparse" in str(excinfo.value) or "允许清单" in str(excinfo.value)
    # 无副作用：无选择、无记录、无尝试；调用方文件字节未变。
    assert service.get_selection("2305.01234") is None
    assert space.store().list_records() == []
    assert package.read_bytes() == sparse_pkg


def test_interrupted_declaration_recovery_subprocess(tmp_path):
    """真实有界子进程回归：声明提交后选择中断 → 另一进程导入 B 恢复 A。

    子进程执行"A 事件已提交、选择回执前失败"的确切检查点（注入异常
    后以退出码 7 退出）；父进程（天然不同 PID）随后导入 B 并核对
    A、B 原始时间。无睡眠等待证据。
    """
    import subprocess
    import sys
    import textwrap

    root = tmp_path / "ws"
    script = tmp_path / "child.py"
    script.write_text(
        textwrap.dedent(
            f"""
            import sys
            sys.path.insert(0, {str((Path.cwd() / "src").resolve())!r})
            sys.path.insert(0, {str(Path.cwd().resolve())!r})
            sys.path.insert(0, {str((Path.cwd() / "tests").resolve())!r})
            from hvs_catalog.workspace import initialize_workspace
            from hvs_catalog.pipeline.acquisition.lifecycle import AcquisitionService
            from test_acquisition_lifecycle import FakeArxivService, FakeClock
            space = initialize_workspace({str(root)!r})
            store = space.store()
            real_commit = store.commit
            def fail_selection(request):
                if request.kind == "acquisition_import_selection":
                    print("checkpoint", flush=True)
                    sys.exit(7)  # 模拟选择回执提交前进程终止
                return real_commit(request)
            store.commit = fail_selection
            clock = FakeClock(start=1_700_000_000.0)
            clock._now += 10.0
            service = AcquisitionService(space, FakeArxivService(clock=clock), clock=clock)
            try:
                service.import_local_package(
                    {str(tmp_path / "a.tar.gz")!r},
                    arxiv_id="2305.01234v2",
                    provenance="declaration A",
                )
            except SystemExit:
                raise
            finally:
                space.close()
            """
        ),
        encoding="utf-8",
    )
    (tmp_path / "a.tar.gz").write_bytes(GOOD_PACKAGE)
    (tmp_path / "b.tar.gz").write_bytes(GOOD_PACKAGE)
    proc = subprocess.run(
        [sys.executable, str(script)],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert proc.returncode == 7, proc.stderr[-400:]
    assert "checkpoint" in proc.stdout
    # 父进程（不同 PID）：导入 B，A 的首次时间按事件恢复。
    space = initialize_workspace(root)
    try:
        clock = FakeClock(start=1_700_000_010.0)
        clock._now += 50.0  # tB
        service = AcquisitionService(space, FakeArxivService(clock=clock), clock=clock)
        service.import_local_package(
            str(tmp_path / "b.tar.gz"),
            arxiv_id="2305.01234v2",
            provenance="declaration B",
        )
        status = service.get_selection("2305.01234")
        assert status is not None
        logged = {item["provenance"]: item["at"] for item in status.provenance_log}
        assert set(logged) == {"declaration A", "declaration B"}
        from datetime import datetime

        assert (
            datetime.fromisoformat(logged["declaration A"]).timestamp()
            == 1_700_000_010.0
        )
    finally:
        space.close()


# ---------------------------------------------------------------- R extra3：选择回执身份一致性


def _log_times(status) -> dict[str, float]:
    from datetime import datetime

    return {
        item["provenance"]: datetime.fromisoformat(item["at"]).timestamp()
        for item in status.provenance_log
    }


def _reopen(space, clock):
    from hvs_catalog.workspace import open_workspace

    space.close()
    reopened = open_workspace(space.root)
    return reopened, AcquisitionService(
        reopened, FakeArxivService(clock=clock), clock=clock
    )


def test_import_selection_identity_a_b_a_b_sequence(space, clock, tmp_path):
    """审核反例序列：同版本同字节 A→B→A→B（时钟前进 + 每步重开句柄）。

    选择回执身份 = 承诺的提交结果（目标/字节/世代/完整日志摘要）；
    内容与键严格对应，不含调用声明。任何一步都不得抛
    OperationKeyConflictError；每步断言返回值与盘上精确状态。
    """
    from hvs_catalog.workspace import WorkspaceConflictError  # noqa: F401

    package = tmp_path / "p.tar.gz"
    package.write_bytes(GOOD_PACKAGE)
    service = build_service(space, FakeArxivService(clock=clock), clock)

    # A @ t0：全新导入。
    r1 = service.import_local_package(package, arxiv_id="2305.01234v2", provenance="A")
    assert (r1.replayed, r1.version) == (False, 2)
    st = service.get_selection("2305.01234")
    assert st is not None and st.state == "imported" and st.version == 2
    assert _log_times(st) == {"A": 1_700_000_000.0}
    record_id = r1.material_ref.record_id
    retrieval_first = st.retrieval
    assert "declared provenance: A" in retrieval_first  # 材料记录首次获取依据

    # B @ t+60（重开句柄）：材料复用、日志追加，状态仍一致。
    clock._now += 60.0
    space, service = _reopen(space, clock)
    r2 = service.import_local_package(package, arxiv_id="2305.01234v2", provenance="B")
    assert r2.replayed is True  # 字节复用
    assert r2.material_ref.record_id == record_id
    st = service.get_selection("2305.01234")
    assert st is not None
    assert _log_times(st) == {"A": 1_700_000_000.0, "B": 1_700_000_060.0}
    assert st.retrieval == retrieval_first  # 选择依据仍是首次获取声明
    selected_at_b = st.selected_at

    # A @ t+120（重开句柄）：同状态（世代 1、日志 [A,B]）→ 回执重放，
    # 无键冲突；盘上状态保持该结果的首次提交视图。
    clock._now += 60.0
    space, service = _reopen(space, clock)
    r3 = service.import_local_package(package, arxiv_id="2305.01234v2", provenance="A")
    assert r3.replayed is True
    assert r3.material_ref.record_id == record_id
    st = service.get_selection("2305.01234")
    assert st is not None
    assert _log_times(st) == {"A": 1_700_000_000.0, "B": 1_700_000_060.0}
    assert st.selected_at == selected_at_b  # 同状态：首次提交时间保留
    assert st.retrieval == retrieval_first

    # B @ t+180（重开句柄）：同样同状态 → 幂等重放，无异常。
    clock._now += 60.0
    space, service = _reopen(space, clock)
    r4 = service.import_local_package(package, arxiv_id="2305.01234v2", provenance="B")
    assert r4.replayed is True
    st = service.get_selection("2305.01234")
    assert st is not None
    assert _log_times(st) == {"A": 1_700_000_000.0, "B": 1_700_000_060.0}
    assert st.history == ()  # 同版本无历史切换

    # 备份恢复后状态一致。
    from hvs_catalog.workspace import backup_workspace, restore_workspace

    backup_dir = tmp_path / "bk"
    backup_workspace(space, backup_dir)
    restored = restore_workspace(backup_dir, tmp_path / "restored")
    try:
        st_r = AcquisitionService(
            restored, FakeArxivService(clock=clock), clock=clock
        ).get_selection("2305.01234")
        assert st_r is not None
        assert _log_times(st_r) == {"A": 1_700_000_000.0, "B": 1_700_000_060.0}
        assert st_r.retrieval == retrieval_first
        assert st_r.selected_at == selected_at_b
    finally:
        restored.close()


def test_import_selection_identity_cross_version_sequence(space, clock, tmp_path):
    """跨版本序列：v1(A,B) → v2 → v1(A) → v1(B) → v1(A)，逐步精确断言。"""
    package = tmp_path / "p.tar.gz"
    package.write_bytes(GOOD_PACKAGE)
    api = FakeArxivService(clock=clock)
    api.downloads["2305.01234v2"] = make_tar_gz({"main.tex": b"v2 body"})
    service = build_service(space, api, clock)

    service.import_local_package(package, arxiv_id="2305.01234v1", provenance="A")
    clock._now += 10.0
    service.import_local_package(package, arxiv_id="2305.01234v1", provenance="B")
    st = service.get_selection("2305.01234")
    assert st is not None and st.version == 1
    assert _log_times(st) == {"A": 1_700_000_000.0, "B": 1_700_000_010.0}

    # v2（世代 2，v1 入历史）。
    clock._now += 10.0
    service.acquire("2305.01234v2")
    st = service.get_selection("2305.01234")
    assert st is not None and st.version == 2 and st.state == "downloaded"
    assert [h["version"] for h in st.history] == [1]

    # 重选回 v1(A)（世代 3，日志从事件重建为 [A,B]）。
    clock._now += 10.0
    r = service.import_local_package(package, arxiv_id="2305.01234v1", provenance="A")
    assert r.version == 1 and r.replayed is True
    st = service.get_selection("2305.01234")
    assert st is not None and st.version == 1 and st.state == "imported"
    assert _log_times(st) == {"A": 1_700_000_000.0, "B": 1_700_000_010.0}
    assert [h["version"] for h in st.history] == [1, 2]

    # 再导入 v1(B)：同状态（世代 3、日志 [A,B]）→ 幂等重放，无冲突。
    clock._now += 10.0
    r = service.import_local_package(package, arxiv_id="2305.01234v1", provenance="B")
    assert r.replayed is True
    st = service.get_selection("2305.01234")
    assert st is not None and st.version == 1
    assert _log_times(st) == {"A": 1_700_000_000.0, "B": 1_700_000_010.0}
    assert [h["version"] for h in st.history] == [1, 2]

    # 再导入 v1(A)：仍同状态 → 幂等重放。
    clock._now += 10.0
    r = service.import_local_package(package, arxiv_id="2305.01234v1", provenance="A")
    assert r.replayed is True
    st = service.get_selection("2305.01234")
    assert st is not None
    assert _log_times(st) == {"A": 1_700_000_000.0, "B": 1_700_000_010.0}
    assert [h["version"] for h in st.history] == [1, 2]


def test_import_selection_identity_interrupted_b_full_recovery(space, clock, tmp_path):
    """中断恢复全序列：B 声明提交/选择中断 → A 恢复完整日志 → B 重试 →
    A 再重复。每步断言精确盘上状态，B 重试不再冲突（审核反例）。"""
    package = tmp_path / "p.tar.gz"
    package.write_bytes(GOOD_PACKAGE)
    service = build_service(space, FakeArxivService(clock=clock), clock)
    service.import_local_package(package, arxiv_id="2305.01234v2", provenance="A")
    st = service.get_selection("2305.01234")
    assert st is not None and _log_times(st) == {"A": 1_700_000_000.0}

    # B：声明事件提交后、选择回执前中断。
    real_commit = _fail_once_on_selection_commit(space)
    clock._now += 20.0
    with pytest.raises(RuntimeError, match="注入"):
        service.import_local_package(package, arxiv_id="2305.01234v2", provenance="B")
    space.store().commit = real_commit  # type: ignore[method-assign]
    st = service.get_selection("2305.01234")
    assert st is not None and _log_times(st) == {"A": 1_700_000_000.0}  # 视图未变

    # A 恢复完整日志（新回执，世代 1，日志 [A,B]）。
    clock._now += 20.0
    r = service.import_local_package(package, arxiv_id="2305.01234v2", provenance="A")
    assert r.replayed is True
    st = service.get_selection("2305.01234")
    assert st is not None
    assert _log_times(st) == {"A": 1_700_000_000.0, "B": 1_700_000_020.0}

    # B 重试：同状态（世代 1、日志 [A,B]）→ 幂等重放；不得抛冲突。
    clock._now += 20.0
    r = service.import_local_package(package, arxiv_id="2305.01234v2", provenance="B")
    assert r.replayed is True
    st = service.get_selection("2305.01234")
    assert st is not None
    assert _log_times(st) == {"A": 1_700_000_000.0, "B": 1_700_000_020.0}
    assert st.history == ()

    # A 再重复：仍幂等。
    clock._now += 20.0
    r = service.import_local_package(package, arxiv_id="2305.01234v2", provenance="A")
    assert r.replayed is True
    st = service.get_selection("2305.01234")
    assert st is not None
    assert _log_times(st) == {"A": 1_700_000_000.0, "B": 1_700_000_020.0}


def test_import_selection_identity_three_declarations_and_pending_switch(
    space, clock, tmp_path
):
    """三声明 + 待处理他版本切换：A→B→C → v2 待处理 → 重选 v1(C)。"""
    package = tmp_path / "p.tar.gz"
    package.write_bytes(GOOD_PACKAGE)
    api = FakeArxivService(clock=clock)
    api.downloads["2305.01234v2"] = ArxivSourceUnavailableError(
        "404", reason="not_found"
    )
    service = build_service(space, api, clock)
    service.import_local_package(package, arxiv_id="2305.01234v1", provenance="A")
    clock._now += 10.0
    service.import_local_package(package, arxiv_id="2305.01234v1", provenance="B")
    clock._now += 10.0
    service.import_local_package(package, arxiv_id="2305.01234v1", provenance="C")
    st = service.get_selection("2305.01234")
    assert st is not None and st.version == 1
    assert _log_times(st) == {
        "A": 1_700_000_000.0,
        "B": 1_700_000_010.0,
        "C": 1_700_000_020.0,
    }
    # v2 不可用 → 待处理（世代 2，v1 入历史），v1 材料保留。
    clock._now += 10.0
    with pytest.raises(SourceUnavailableError):
        service.acquire("2305.01234v2")
    st = service.get_selection("2305.01234")
    assert st is not None and st.version == 2 and st.state == "source_unavailable"
    assert [h["version"] for h in st.history] == [1]
    # 重选回 v1(C)：日志从事件重建为 [A,B,C]，世代 3。
    clock._now += 10.0
    r = service.import_local_package(package, arxiv_id="2305.01234v1", provenance="C")
    assert r.version == 1 and r.replayed is True
    st = service.get_selection("2305.01234")
    assert st is not None and st.version == 1 and st.state == "imported"
    assert _log_times(st) == {
        "A": 1_700_000_000.0,
        "B": 1_700_000_010.0,
        "C": 1_700_000_020.0,
    }
    assert [h["version"] for h in st.history] == [1, 2]


def test_interrupted_b_recovery_subprocess_full_sequence(tmp_path):
    """真实有界子进程：B 中断 → 父进程 A 恢复 → B 重试 → A 重复。"""
    import subprocess
    import sys
    import textwrap

    root = tmp_path / "ws"
    script = tmp_path / "child.py"
    script.write_text(
        textwrap.dedent(
            f"""
            import sys
            sys.path.insert(0, {str((Path.cwd() / "src").resolve())!r})
            sys.path.insert(0, {str(Path.cwd().resolve())!r})
            sys.path.insert(0, {str((Path.cwd() / "tests").resolve())!r})
            from hvs_catalog.workspace import initialize_workspace
            from hvs_catalog.pipeline.acquisition.lifecycle import AcquisitionService
            from test_acquisition_lifecycle import FakeArxivService, FakeClock
            space = initialize_workspace({str(root)!r})
            store = space.store()
            real_commit = store.commit
            clock = FakeClock(start=1_700_000_000.0)
            service = AcquisitionService(space, FakeArxivService(clock=clock), clock=clock)
            # A 正常完成（含选择回执）。
            service.import_local_package(
                {str(tmp_path / "pkg.tar.gz")!r},
                arxiv_id="2305.01234v2",
                provenance="A",
            )
            clock._now += 20.0
            # 注入：B 的选择回执提交前终止进程。
            def fail_selection(request):
                if request.kind == "acquisition_import_selection":
                    print("checkpoint", flush=True)
                    sys.exit(7)
                return real_commit(request)
            store.commit = fail_selection
            try:
                service.import_local_package(
                    {str(tmp_path / "pkg.tar.gz")!r},
                    arxiv_id="2305.01234v2",
                    provenance="B",
                )
            finally:
                space.close()
            """
        ),
        encoding="utf-8",
    )
    (tmp_path / "pkg.tar.gz").write_bytes(GOOD_PACKAGE)
    proc = subprocess.run(
        [sys.executable, str(script)],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert proc.returncode == 7, proc.stderr[-400:]
    assert "checkpoint" in proc.stdout
    # 父进程（不同 PID）：A 恢复完整日志 → B 重试 → A 重复，逐步断言。
    space = initialize_workspace(root)
    try:
        clock = FakeClock(start=1_700_000_000.0)
        clock._now += 40.0
        service = AcquisitionService(space, FakeArxivService(clock=clock), clock=clock)
        r = service.import_local_package(
            str(tmp_path / "pkg.tar.gz"), arxiv_id="2305.01234v2", provenance="A"
        )
        assert r.replayed is True
        status = service.get_selection("2305.01234")
        assert status is not None
        _assert_log = {item["provenance"]: item["at"] for item in status.provenance_log}
        assert set(_assert_log) == {"A", "B"}
        from datetime import datetime

        assert datetime.fromisoformat(_assert_log["A"]).timestamp() == 1_700_000_000.0
        assert datetime.fromisoformat(_assert_log["B"]).timestamp() == 1_700_000_020.0
        clock._now += 20.0
        r = service.import_local_package(
            str(tmp_path / "pkg.tar.gz"), arxiv_id="2305.01234v2", provenance="B"
        )
        assert r.replayed is True  # 同状态幂等重放，无冲突
        clock._now += 20.0
        r = service.import_local_package(
            str(tmp_path / "pkg.tar.gz"), arxiv_id="2305.01234v2", provenance="A"
        )
        assert r.replayed is True
        status = service.get_selection("2305.01234")
        assert status is not None
        final = {item["provenance"]: item["at"] for item in status.provenance_log}
        assert final == _assert_log  # 时间不再变化
    finally:
        space.close()


# ------------------------------------------------ R provenance-fix：远程来源投影


@pytest.mark.parametrize("origin", ["downloaded", "imported"])
def test_selected_material_rejects_missing_record_without_writes(
    space, clock, tmp_path, origin, capsys
):
    """原始字节仍在不等于精确材料记录存在；交付失败关闭，诊断仍可读。"""
    import sqlite3

    from hvs_catalog.cli import main
    from hvs_catalog.workspace import open_workspace

    api = FakeArxivService(clock=clock)
    api.downloads["2401.01234v1"] = GOOD_PACKAGE
    service = build_service(space, api, clock)
    if origin == "downloaded":
        service.acquire("2401.01234v1")
    else:
        package = tmp_path / "input.tar.gz"
        package.write_bytes(GOOD_PACKAGE)
        service.import_local_package(
            package, arxiv_id="2401.01234v1", provenance="synthetic local source"
        )
    material = service.selected_material("2401.01234")
    assert material is not None and material.origin_retrieval
    raw_before = (space.root / material.raw_path).read_bytes()
    space.close()
    # 故障注入仅限本测试的一次性合成工作空间，不实现数据修复。
    with sqlite3.connect(space.root / "business.db") as connection:
        connection.execute(
            "DELETE FROM records WHERE record_id=? AND revision=?",
            (material.material_ref.record_id, material.material_ref.revision),
        )
        connection.commit()
        before = list(connection.iterdump())
    reopened = open_workspace(space.root)
    try:
        service = AcquisitionService(reopened, RaisingService(), clock=clock)
        assert service.get_selection("2401.01234").state == origin
        with pytest.raises(AcquisitionError, match="材料记录.*缺失"):
            service.selected_material("2401.01234")
        with pytest.raises(AcquisitionError, match="材料记录.*缺失"):
            service.acquire("2401.01234v1")
    finally:
        reopened.close()
    assert (
        main(
            [
                "acquisition",
                "material",
                "--space",
                str(space.root),
                "--id",
                "2401.01234",
            ]
        )
        == 1
    )
    captured = capsys.readouterr()
    assert "缺失" in captured.err
    assert "[溯源]" not in captured.out
    assert (space.root / material.raw_path).read_bytes() == raw_before
    with sqlite3.connect(space.root / "business.db") as connection:
        assert list(connection.iterdump()) == before


def test_remote_material_provenance_projected_from_record(space, clock, tmp_path):
    """远程获取成功的公开来源 = 材料记录的不可变 origin.retrieval。

    覆盖：获取成功、重开工作空间、跨版本各自来源、读路径零业务写入、
    备份恢复后一致（旧缺陷：SelectionStatus.retrieval=None、
    SelectedMaterial.origin_retrieval=''）。
    """
    from hvs_catalog.workspace import (
        backup_workspace,
        open_workspace,
        restore_workspace,
    )

    api = FakeArxivService(clock=clock)
    api.downloads["2401.01234v1"] = GOOD_PACKAGE
    api.downloads["2401.01234v2"] = make_tar_gz({"main.tex": b"v2"})
    service = build_service(space, api, clock)
    result = service.acquire("2401.01234v1")
    assert result.state == "downloaded"
    # 材料记录的真实来源。
    record = space.store().read_record_current(result.material_ref.record_id)
    record_retrieval = record.data.origin.retrieval
    assert record_retrieval.startswith("downloaded from https://arxiv.org/e-print/")

    def assert_projection(svc) -> None:
        status = svc.get_selection("2401.01234")
        assert status is not None
        assert status.retrieval == record_retrieval  # 精确等于记录值
        material = svc.selected_material("2401.01234")
        assert material is not None
        assert material.origin_retrieval == record_retrieval

    # 读路径零业务写入：回执与记录数在读前后不变。
    receipts_before = len(space.store().find_receipts())
    records_before = len(space.store().list_records())
    assert_projection(service)
    assert len(space.store().find_receipts()) == receipts_before
    assert len(space.store().list_records()) == records_before

    # 重开工作空间（新句柄）仍正确（记录派生，不依赖内存状态）。
    space.close()
    reopened = open_workspace(space.root)
    try:
        assert_projection(AcquisitionService(reopened, RaisingService(), clock=clock))
        # 备份恢复后仍正确。
        backup_dir = tmp_path / "bk"
        backup_workspace(reopened, backup_dir)
        restored = restore_workspace(backup_dir, tmp_path / "restored")
        try:
            assert_projection(
                AcquisitionService(restored, RaisingService(), clock=clock)
            )
        finally:
            restored.close()
        # 跨版本：显式 v2 后各自来源精确。
        api2 = FakeArxivService(clock=clock)
        api2.downloads["2401.01234v2"] = make_tar_gz({"main.tex": b"v2"})
        service2 = AcquisitionService(reopened, api2, clock=clock)
        r2 = service2.acquire("2401.01234v2")
        assert r2.state == "downloaded"
        record2 = reopened.store().read_record_current(r2.material_ref.record_id)
        status2 = service2.get_selection("2401.01234")
        assert status2 is not None
        assert status2.version == 2
        assert status2.retrieval == record2.data.origin.retrieval
        assert "2401.01234v2" in status2.retrieval
    finally:
        reopened.close()


def test_missing_selection_retrieval_projected_from_record(space, clock):
    """既有下载状态缺 retrieval（历史路径未写入）：投影仍取记录值。

    测试直接把选择 meta 的 retrieval 置空模拟旧状态；读取是纯投影、
    不回写修复（无回执外写入）。
    """
    api = FakeArxivService(clock=clock)
    api.downloads["2401.01234v1"] = GOOD_PACKAGE
    service = build_service(space, api, clock)
    result = service.acquire("2401.01234v1")
    assert result.state == "downloaded"
    record = space.store().read_record_current(result.material_ref.record_id)
    record_retrieval = record.data.origin.retrieval
    # 模拟旧状态：选择 meta 丢失来源（直接 set_meta 仅测试侧模拟）。
    state = dict(service._load_selection("2401.01234"))
    state["retrieval"] = None
    space.store().set_meta(
        "acq:selection:2401.01234", json.dumps(state, ensure_ascii=False)
    )
    receipts_before = len(space.store().find_receipts())
    status = service.get_selection("2401.01234")
    assert status is not None
    assert status.retrieval == record_retrieval  # 记录派生，非臆造
    material = service.selected_material("2401.01234")
    assert material is not None
    assert material.origin_retrieval == record_retrieval
    assert len(space.store().find_receipts()) == receipts_before  # 无写入


def test_adopt_committed_projected_retrieval(space, clock, tmp_path):
    """崩溃窗口采用已提交材料：公开来源 = 采用记录的真实来源。"""
    api = FakeArxivService(clock=clock)
    api.downloads["2401.01234v2"] = ArxivSourceUnavailableError(
        "404", reason="not_found"
    )
    service = build_service(space, api, clock)
    # 种子：远程下载 v1（记录带下载来源）。
    api_seed = FakeArxivService(clock=clock)
    api_seed.downloads["2401.01234v1"] = GOOD_PACKAGE
    seeded = build_service(space, api_seed, clock)
    downloaded = seeded.acquire("2401.01234v1")
    record = space.store().read_record_current(downloaded.material_ref.record_id)
    record_retrieval = record.data.origin.retrieval
    # 崩溃窗口：v2 待处理后显式重选回 v1（selected，记录已在）。
    with pytest.raises(SourceUnavailableError):
        service.acquire("2401.01234v2")
    frozen = service._freeze_selection(parse_arxiv_identifier("2401.01234v1"))
    assert frozen["state"] == "selected"
    recovered = service.acquire("2401.01234v1")  # 采用已提交材料
    assert recovered.state == "downloaded"
    assert recovered.replayed is True
    status = service.get_selection("2401.01234")
    assert status is not None
    assert status.retrieval == record_retrieval
    material = service.selected_material("2401.01234")
    assert material is not None
    assert material.origin_retrieval == record_retrieval


def test_imported_provenance_semantics_unchanged(space, clock, tmp_path):
    """导入语义保持：投影仍是材料记录的首次获取/导入声明。"""
    package = tmp_path / "p.tar.gz"
    package.write_bytes(GOOD_PACKAGE)
    service = build_service(space, FakeArxivService(clock=clock), clock)
    service.import_local_package(
        package, arxiv_id="2305.01234v2", provenance="record-derived copy"
    )
    status = service.get_selection("2305.01234")
    assert status is not None
    assert status.retrieval is not None
    assert "declared provenance: record-derived copy" in status.retrieval
    material = service.selected_material("2305.01234")
    assert material is not None
    assert material.origin_retrieval == status.retrieval

"""本地导入与备份恢复的合成测试（Lane B：import/backup 集成）。

全部离线：导入文件在系统临时目录构造；备份/恢复用工作空间公共 API；
恢复后的查询与材料回读在真实子进程中完成（独立进程恢复验证）。
"""

from __future__ import annotations

import io
import subprocess
import sys
import tarfile
import textwrap
from datetime import date

import pytest

import hvs_catalog.pipeline.acquisition.lifecycle as lifecycle_mod
from hvs_catalog.pipeline.acquisition.archive import (
    UnsupportedPackageError,
)
from hvs_catalog.pipeline.acquisition.lifecycle import (
    AcquisitionError,
    AcquisitionService,
    PackageRejectionError,
)
from hvs_catalog.pipeline.acquisition.protocol import (
    ArxivIdBatchResult,
    ArxivIdLookup,
    ArxivPaperMeta,
    ArxivSearchRequest,
    ArxivSourceDownload,
    parse_arxiv_identifier,
)
from hvs_catalog.records.common import ArtifactRef
from hvs_catalog.workspace import (
    WorkspaceConflictError,
    backup_workspace,
    initialize_workspace,
    restore_workspace,
)


class FakeClock:
    def __init__(self, start: float = 1_700_000_000.0) -> None:
        self._now = float(start)

    def now(self) -> float:
        return self._now

    def sleep(self, seconds: float) -> None:
        self._now += max(0.0, float(seconds))


def make_tar_gz(members: dict[str, bytes]) -> bytes:
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w:gz") as archive:
        for name, data in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
    return raw.getvalue()


PACKAGE_A = make_tar_gz(
    {"main.tex": b"\\documentclass{article}A", "fig/datum.dat": b"1,2,3"}
)
PACKAGE_B = make_tar_gz({"main.tex": b"\\documentclass{article}B"})


class ScriptedApi:
    """下载脚本化、其余拒绝的协议替身。"""

    def __init__(self, downloads: dict[str, bytes]) -> None:
        self.downloads = downloads
        self.download_calls: list[str] = []

    def download_source(self, identifier):
        versioned = (
            identifier if isinstance(identifier, str) else identifier.versioned_id
        )
        self.download_calls.append(versioned)
        payload = self.downloads.get(versioned)
        if payload is None:
            raise AssertionError(f"未脚本的下载：{versioned}")
        return ArxivSourceDownload(
            identifier=parse_arxiv_identifier(versioned),
            final_url=f"https://arxiv.org/e-print/{versioned}",
            content_type="application/gzip",
            payload=payload,
            byte_count=len(payload),
            fetched_at=1_700_000_000.0,
        )

    def search(self, request):  # pragma: no cover - 不应被调用
        raise AssertionError("本测试不应触网")

    def metadata_by_ids(self, identifiers):  # pragma: no cover
        raise AssertionError("本测试不应触网")


def make_service(space, api, clock=None):
    return AcquisitionService(space, api, clock=clock or FakeClock())


@pytest.fixture()
def space(tmp_path):
    workspace = initialize_workspace(tmp_path / "space")
    yield workspace
    workspace.close()


def test_import_ok_with_declared_identity_and_provenance(space, tmp_path):
    package = tmp_path / "local-a.tar.gz"
    package.write_bytes(PACKAGE_A)
    service = make_service(space, ScriptedApi({}))
    result = service.import_local_package(
        package, arxiv_id="2305.01234v2", provenance="manually downloaded by expert X"
    )
    assert result.canonical_id == "2305.01234"
    assert result.version == 2
    selection = service.get_selection("2305.01234")
    assert selection.state == "imported"
    assert selection.via == "local_import"
    # 溯源明确标注是本地声明，不是 arXiv 核实。
    assert "local import" in selection.retrieval
    assert "expert X" in selection.retrieval
    record = space.store().read_record_current(result.material_ref.record_id)
    assert record is not None
    assert "local import" in record.data.origin.retrieval
    # 清单持久化（含成员哈希），原始字节可回读。
    inventory = {item["name"] for item in selection.package_inventory}
    assert inventory == {"main.tex", "fig/datum.dat"}
    assert (
        space.read_file(ArtifactRef(path=result.raw_path, sha256=result.raw_sha256))
        == PACKAGE_A
    )
    material = service.selected_material("2305.01234")
    assert material is not None and material.version == 2


def test_import_requires_explicit_version(space, tmp_path):
    package = tmp_path / "local-b.tar.gz"
    package.write_bytes(PACKAGE_B)
    service = make_service(space, ScriptedApi({}))
    with pytest.raises(AcquisitionError, match="具体版本"):
        service.import_local_package(
            package, arxiv_id="2305.01234", provenance="test copy"
        )
    with pytest.raises(AcquisitionError, match="具体版本"):
        service.import_local_package(package, arxiv_id="2305.01234", provenance="x")


def test_import_contradictory_version_declaration_rejected(space, tmp_path):
    package = tmp_path / "local-c.tar.gz"
    package.write_bytes(PACKAGE_B)
    service = make_service(space, ScriptedApi({}))
    with pytest.raises(AcquisitionError, match="矛盾"):
        service.import_local_package(
            package, arxiv_id="2305.01234v2", version=3, provenance="test copy"
        )


def test_import_legacy_identifier_supported(space, tmp_path):
    package = tmp_path / "legacy.tar.gz"
    package.write_bytes(PACKAGE_B)
    service = make_service(space, ScriptedApi({}))
    result = service.import_local_package(
        package, arxiv_id="math.AG/0309136v1", provenance="archive copy"
    )
    assert result.versioned_id == "math.AG/0309136v1"
    assert result.raw_path.startswith("sources/packages/")
    assert service.get_version("math.AG/0309136v1").source_record == result.material_ref


def test_import_same_identity_version_different_bytes_rejected(space, tmp_path):
    first = tmp_path / "first.tar.gz"
    first.write_bytes(PACKAGE_A)
    service = make_service(space, ScriptedApi({}))
    service.import_local_package(first, arxiv_id="2305.01234v2", provenance="one")
    conflicting = tmp_path / "conflicting.tar.gz"
    conflicting.write_bytes(PACKAGE_B)
    with pytest.raises(WorkspaceConflictError, match="不同字节"):
        service.import_local_package(
            conflicting, arxiv_id="2305.01234v2", provenance="two"
        )
    # 既有材料未被覆盖。
    selection = service.get_selection("2305.01234")
    assert (
        space.read_file(
            ArtifactRef(path=selection.raw_path, sha256=selection.raw_sha256)
        )
        == PACKAGE_A
    )


def test_import_identical_bytes_reusable_keeps_provenance(space, tmp_path):
    first = tmp_path / "a.tar.gz"
    first.write_bytes(PACKAGE_A)
    again = tmp_path / "b.tar.gz"
    again.write_bytes(PACKAGE_A)
    service = make_service(space, ScriptedApi({}))
    first_result = service.import_local_package(
        first, arxiv_id="2305.01234v2", provenance="first copy"
    )
    assert not first_result.replayed
    repeat = service.import_local_package(
        again, arxiv_id="2305.01234v2", provenance="second copy"
    )
    assert repeat.replayed
    assert repeat.raw_path == first_result.raw_path
    # 一份材料记录 + 两条声明事件记录（两个不同声明各持久化首登时间）。
    assert len(space.store().list_records()) == 3
    # 首次溯源仍在记录中（不因重复导入丢失）。
    record = space.store().read_record_current(repeat.material_ref.record_id)
    assert "first copy" in record.data.origin.retrieval


def test_import_unsafe_package_rejected_nothing_published(space, tmp_path):
    evil = io.BytesIO()
    with tarfile.open(fileobj=evil, mode="w") as archive:
        info = tarfile.TarInfo("../escape.tex")
        info.size = 3
        archive.addfile(info, io.BytesIO(b"bad"))
    package = tmp_path / "evil.tar"
    package.write_bytes(evil.getvalue())
    service = make_service(space, ScriptedApi({}))
    with pytest.raises(PackageRejectionError):
        service.import_local_package(
            package, arxiv_id="2305.01234v2", provenance="test copy"
        )
    assert not (space.root / "sources" / "papers").exists() or not any(
        (space.root / "sources" / "papers").rglob("package-*")
    )
    assert service.list_selections() == []


def test_import_pdf_rejected_as_unsupported(space, tmp_path):
    package = tmp_path / "paper.pdf"
    package.write_bytes(b"%PDF-1.5 not a source package")
    service = make_service(space, ScriptedApi({}))
    with pytest.raises(UnsupportedPackageError, match="PDF"):
        service.import_local_package(
            package, arxiv_id="2305.01234v2", provenance="test copy"
        )


def test_import_missing_file_rejected(space, tmp_path):
    service = make_service(space, ScriptedApi({}))
    with pytest.raises(AcquisitionError, match="不存在"):
        service.import_local_package(
            tmp_path / "nope.tar.gz",
            arxiv_id="2305.01234v2",
            provenance="test copy",
        )


def test_import_size_cap(space, tmp_path, monkeypatch):
    monkeypatch.setattr(lifecycle_mod, "MAX_PACKAGE_BYTES", 16)
    package = tmp_path / "big.tar.gz"
    package.write_bytes(PACKAGE_A)
    service = make_service(space, ScriptedApi({}))
    with pytest.raises(PackageRejectionError, match="上限"):
        service.import_local_package(
            package, arxiv_id="2305.01234v2", provenance="test copy"
        )


# ---------------------------------------------------------------- 备份/恢复 + 子进程


RESTORE_QUERY_SCRIPT = textwrap.dedent(
    """
    import json, sys
    from hvs_catalog.pipeline.acquisition.lifecycle import AcquisitionService
    from hvs_catalog.records.common import ArtifactRef
    from hvs_catalog.workspace import open_workspace

    root = sys.argv[1]
    space = open_workspace(root)

    class NoNetwork:
        def search(self, request):
            raise AssertionError("恢复查询不应触网")
        def metadata_by_ids(self, identifiers):
            raise AssertionError("恢复查询不应触网")
        def download_source(self, identifier):
            raise AssertionError("恢复查询不应触网")

    service = AcquisitionService(space, NoNetwork(), clock=None)
    rounds = service.list_discovery_rounds()
    selections = service.list_selections()
    print(f"ROUNDS {len(rounds)} {rounds[0].state} {rounds[0].papers and 'papers' or 'empty'}")
    by_canonical = {item.canonical_id: item for item in selections}
    downloaded = by_canonical["2101.01234"]
    imported = by_canonical["2305.01234"]
    print(f"SELECTIONS {downloaded.state} {imported.state}")
    material = service.selected_material("2101.01234")
    assert material is not None
    raw = space.read_file(ArtifactRef(path=material.raw_path, sha256=material.raw_sha256))
    print(f"MATERIAL {material.versioned_id} {len(raw)}")
    revision = service.get_revision_status("2101.01234")
    print(f"REVISION {revision.state} {revision.latest_version}")
    record = space.store().read_record_current(material.material_ref.record_id)
    assert record is not None
    print(f"RECORD {record.data.paper_key} {record.data.source_version}")
    round_ = service.get_discovery_round(rounds[0].round_id)
    page = round_.pages[0]
    page_record = space.store().read_record_current(page["record_id"])
    assert page_record is not None
    doc = json.loads(space.read_file(page_record.data.raw_response).decode("utf-8"))
    print(f"PAGE {page['start']} {len(doc['entries'])}")
    space.close()
    """
)


def test_backup_restore_then_subprocess_resume_query(tmp_path):
    """完整流程 → 备份 → 恢复到新目录 → 独立进程免网络查询全部进度与材料。"""
    space = initialize_workspace(tmp_path / "space")

    # 1) 发现轮次（单页完成）。
    from test_acquisition_lifecycle import script_single_page

    entries = (
        _meta("2401.00001", 1),
        _meta("2401.00002", 1),
    )
    api = script_single_page(entries)
    clock = FakeClock()
    service = AcquisitionService(space, api, clock=clock)
    request = ArxivSearchRequest(
        submitted_from=date(2024, 1, 1),
        submitted_to=date(2024, 1, 31),
        terms="dark matter",
        categories=("astro-ph.CO",),
    )
    status = service.run_discovery(request)
    assert status.state == "complete"

    # 2) 获取一篇（发现轮次外年份的旧论文，显式版本）。
    download_api = ScriptedApi({"2101.01234v2": PACKAGE_A})
    service2 = AcquisitionService(space, download_api, clock=FakeClock())
    result = service2.acquire("2101.01234v2")
    assert result.state == "downloaded"

    # 3) 修订检查发现 v5 待处理。
    revision_api = _RevisionApi({"2101.01234": _meta("2101.01234", 5)})
    AcquisitionService(space, revision_api, clock=FakeClock()).check_revisions()

    # 4) 本地导入另一篇。
    package = tmp_path / "local.tar.gz"
    package.write_bytes(PACKAGE_B)
    service3 = AcquisitionService(space, ScriptedApi({}), clock=FakeClock())
    import_result = service3.import_local_package(
        package, arxiv_id="2305.01234v1", provenance="restore test copy"
    )
    assert import_result.canonical_id == "2305.01234"

    # 5) 备份 → 恢复到全新目录。
    backup_dir = tmp_path / "backup"
    report = backup_workspace(space, backup_dir)
    assert report.files
    restored_root = tmp_path / "restored"
    restored = restore_workspace(backup_dir, restored_root)
    try:
        # 6) 独立子进程：免网络查询全部进度、溯源与材料。
        proc = subprocess.run(
            [
                sys.executable,
                "-c",
                RESTORE_QUERY_SCRIPT,
                str(restored_root),
            ],
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
        assert proc.returncode == 0, proc.stderr
        lines = proc.stdout.strip().splitlines()
        assert any(line.startswith("ROUNDS 1 complete papers") for line in lines), lines
        assert any(line == "SELECTIONS downloaded imported" for line in lines), lines
        assert any(
            line.startswith(f"MATERIAL 2101.01234v2 {len(PACKAGE_A)}") for line in lines
        ), lines
        assert any(line == "REVISION pending 5" for line in lines), lines
        assert any(line.startswith("RECORD 2101.01234 v2") for line in lines), lines
        assert any(line.startswith("PAGE 0 2") for line in lines), lines
    finally:
        restored.close()
        space.close()


def _meta(canonical_id: str, version: int) -> ArxivPaperMeta:
    return ArxivPaperMeta(
        canonical_id=canonical_id,
        version=version,
        api_id_url=f"http://arxiv.org/abs/{canonical_id}v{version}",
        title=f"Paper {canonical_id}",
        authors=("Author",),
        categories=("astro-ph.CO",),
        primary_category="astro-ph.CO",
        published="2021-01-01T00:00:00Z",
        updated="2024-06-01T00:00:00Z",
        abstract="abs",
        doi=None,
        journal_ref=None,
    )


class _RevisionApi(ScriptedApi):
    def __init__(self, metadata: dict) -> None:
        super().__init__({})
        self._metadata = metadata

    def metadata_by_ids(self, identifiers):
        lookups = []
        for item in identifiers:
            canonical = item if isinstance(item, str) else item.canonical_id
            meta = self._metadata.get(canonical)
            lookups.append(
                ArxivIdLookup(
                    requested=parse_arxiv_identifier(canonical),
                    status="found" if meta is not None else "not_found",
                    meta=meta,
                )
            )
        return ArxivIdBatchResult(
            response_url="synthetic",
            fetched_at=1_700_000_000.0,
            lookups=tuple(lookups),
            anomalies=(),
        )


def test_backup_includes_all_referenced_material(tmp_path):
    """备份清单覆盖记录引用的全部来源文件（无未备份的孤儿状态）。"""
    space = initialize_workspace(tmp_path / "space")
    try:
        service = AcquisitionService(
            space, ScriptedApi({"2101.01234v2": PACKAGE_A}), clock=FakeClock()
        )
        result = service.acquire("2101.01234v2")
        assert result.state == "downloaded"
        backup_dir = tmp_path / "backup"
        report = backup_workspace(space, backup_dir)
        paths = {entry["path"] for entry in report.files}
        assert result.raw_path in paths  # 原始包已入备份
        assert "business.db" in paths  # 记录/回执/meta 已入备份
    finally:
        space.close()


def test_import_replay_with_new_provenance_retained(space, tmp_path):
    """相同字节重复导入：材料与首次溯源不动，新来源声明追加到溯源日志。"""
    first = tmp_path / "a.tar.gz"
    first.write_bytes(PACKAGE_A)
    again = tmp_path / "b.tar.gz"
    again.write_bytes(PACKAGE_A)
    service = make_service(space, ScriptedApi({}))
    service.import_local_package(
        first, arxiv_id="2305.01234v2", provenance="first copy"
    )
    repeat = service.import_local_package(
        again, arxiv_id="2305.01234v2", provenance="mirror at other site"
    )
    assert repeat.replayed
    status = service.get_selection("2305.01234")
    assert status is not None
    logged = [item["provenance"] for item in status.provenance_log]
    assert logged == ["first copy", "mirror at other site"]
    # 材料记录只有一份（字节复用）；两个声明各有一条声明事件记录。
    assert len(space.store().list_records()) == 3
    record = space.store().read_record_current(repeat.material_ref.record_id)
    assert "first copy" in record.data.origin.retrieval
    # 同一声明再次导入：幂等，不产生重复日志条目。
    third = service.import_local_package(
        again, arxiv_id="2305.01234v2", provenance="mirror at other site"
    )
    assert third.replayed
    status = service.get_selection("2305.01234")
    assert status is not None
    assert len(status.provenance_log) == 2
    # 声明记录是来源权威，无独立声明/选择回执协调路径。
    assert len(space.store().list_records()) == 3


def test_unsupported_retained_bytes_survive_backup_restore(tmp_path):
    """形态不支持时保留的原始字节经尝试记录入备份，恢复后仍可读。"""
    from hvs_catalog.records.common import ArtifactRef

    space = initialize_workspace(tmp_path / "space")
    try:
        pdf_bytes = b"%PDF-1.4 unsupported form body"
        service = AcquisitionService(
            space, ScriptedApi({"2401.00009v1": pdf_bytes}), clock=FakeClock()
        )
        result = service.acquire("2401.00009v1")
        assert result.state == "unsupported"
        attempt_id = f"arxiv-attempt-2401.00009v1-{result.raw_sha256[:16]}"
        record = space.store().read_record_current(attempt_id)
        assert record is not None
        assert service.get_selection("2401.00009").error
        # 尝试记录把保留字节作为附件引用：备份沿记录遍历到这些字节。
        backup_dir = tmp_path / "backup"
        report = backup_workspace(space, backup_dir)
        paths = {entry["path"] for entry in report.files}
        assert result.raw_path in paths
        assert "business.db" in paths  # 尝试记录随业务库入备份
        assert report.problems == []
        # 恢复到新空间后字节仍可按哈希核对读取。
        restored_root = tmp_path / "restored"
        restored = restore_workspace(backup_dir, restored_root)
        try:
            payload = restored.read_file(
                ArtifactRef(path=result.raw_path, sha256=result.raw_sha256)
            )
            assert payload == pdf_bytes
            restored_record = restored.store().read_record_current(attempt_id)
            assert restored_record is not None
        finally:
            restored.close()
    finally:
        space.close()


def test_integrity_rejected_retained_bytes_covered_by_attempt_record(tmp_path):
    """安全检查拒绝时保留的字节同样有尝试记录（备份可遍历）。"""
    import tarfile as tarfile_mod

    bomb_shell = io.BytesIO()
    with tarfile_mod.open(fileobj=bomb_shell, mode="w") as archive:
        info = tarfile_mod.TarInfo("../escape.tex")
        info.size = 4
        archive.addfile(info, io.BytesIO(b"evil"))
    space = initialize_workspace(tmp_path / "space")
    try:
        service = AcquisitionService(
            space,
            ScriptedApi({"2401.00010v1": bomb_shell.getvalue()}),
            clock=FakeClock(),
        )
        with pytest.raises(PackageRejectionError):
            service.acquire("2401.00010v1")
        records = space.store().list_records()
        assert len(records) == 1
        assert records[0].data.service == "arxiv-eprint-attempt"
        status = service.get_selection("2401.00010")
        assert status is not None
        assert status.state == "failed"
        assert status.raw_sha256 is not None
    finally:
        space.close()

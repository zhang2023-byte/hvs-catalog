"""Synthetic local-import and backup/restore tests (Lane B integration).

All tests are offline: build imported files in temporary directories and use public
workspace backup/restore APIs. Query restored progress and reread materials in real
subprocesses to verify independent-process recovery.
"""

from __future__ import annotations

import io
import subprocess
import sys
import tarfile
import textwrap
from datetime import date

import pytest
from materials_helpers import make_pdf

import hvs_catalog.pipeline.acquisition.lifecycle as lifecycle_mod
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


PACKAGE_A = make_pdf("Synthetic A")
PACKAGE_B = make_pdf("Synthetic B")


class ScriptedApi:
    """Protocol fake with scripted downloads that rejects other operations."""

    def __init__(self, downloads: dict[str, bytes]) -> None:
        self.downloads = downloads
        self.download_calls: list[str] = []

    def download_pdf(self, identifier):
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

    def search(self, request):  # pragma: no cover - must not be called
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
    result = service.import_local_pdf(
        package, arxiv_id="2305.01234v2", provenance="manually downloaded by expert X"
    )
    assert result.canonical_id == "2305.01234"
    assert result.version == 2
    selection = service.get_selection("2305.01234")
    assert selection.state == "imported"
    assert selection.via == "local_import"
    # Identify provenance as a local declaration, not arXiv verification.
    assert "local import" in selection.retrieval
    assert "expert X" in selection.retrieval
    record = space.store().read_record_current(result.material_ref.record_id)
    assert record is not None
    assert "local import" in record.data.origin.retrieval
    # Persist the manifest and member hashes; raw bytes remain readable.
    assert selection.package_inventory == (
        {"media_type": "application/pdf", "pages": 1},
    )
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
        service.import_local_pdf(package, arxiv_id="2305.01234", provenance="test copy")
    with pytest.raises(AcquisitionError, match="具体版本"):
        service.import_local_pdf(package, arxiv_id="2305.01234", provenance="x")


def test_import_contradictory_version_declaration_rejected(space, tmp_path):
    package = tmp_path / "local-c.tar.gz"
    package.write_bytes(PACKAGE_B)
    service = make_service(space, ScriptedApi({}))
    with pytest.raises(AcquisitionError, match="矛盾"):
        service.import_local_pdf(
            package, arxiv_id="2305.01234v2", version=3, provenance="test copy"
        )


def test_import_legacy_identifier_supported(space, tmp_path):
    package = tmp_path / "legacy.tar.gz"
    package.write_bytes(PACKAGE_B)
    service = make_service(space, ScriptedApi({}))
    result = service.import_local_pdf(
        package, arxiv_id="math.AG/0309136v1", provenance="archive copy"
    )
    assert result.versioned_id == "math.AG/0309136v1"
    assert result.raw_path.startswith("sources/packages/")
    assert service.get_version("math.AG/0309136v1").source_record == result.material_ref


def test_import_same_identity_version_different_bytes_rejected(space, tmp_path):
    first = tmp_path / "first.tar.gz"
    first.write_bytes(PACKAGE_A)
    service = make_service(space, ScriptedApi({}))
    service.import_local_pdf(first, arxiv_id="2305.01234v2", provenance="one")
    conflicting = tmp_path / "conflicting.tar.gz"
    conflicting.write_bytes(PACKAGE_B)
    with pytest.raises(WorkspaceConflictError, match="不同字节"):
        service.import_local_pdf(conflicting, arxiv_id="2305.01234v2", provenance="two")
    # Existing material is not overwritten.
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
    first_result = service.import_local_pdf(
        first, arxiv_id="2305.01234v2", provenance="first copy"
    )
    assert not first_result.replayed
    repeat = service.import_local_pdf(
        again, arxiv_id="2305.01234v2", provenance="second copy"
    )
    assert repeat.replayed
    assert repeat.raw_path == first_result.raw_path
    # One material record plus two declaration events, each retaining its first registration time.
    assert len(space.store().list_records()) == 3
    # Retain original provenance across repeated imports.
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
        service.import_local_pdf(
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
    with pytest.raises(PackageRejectionError, match="PDF"):
        service.import_local_pdf(
            package, arxiv_id="2305.01234v2", provenance="test copy"
        )


def test_import_missing_file_rejected(space, tmp_path):
    service = make_service(space, ScriptedApi({}))
    with pytest.raises(AcquisitionError, match="不存在"):
        service.import_local_pdf(
            tmp_path / "nope.tar.gz",
            arxiv_id="2305.01234v2",
            provenance="test copy",
        )


def test_import_size_cap(space, tmp_path, monkeypatch):
    monkeypatch.setattr(lifecycle_mod, "MAX_PDF_BYTES", 16)
    package = tmp_path / "big.tar.gz"
    package.write_bytes(PACKAGE_A)
    service = make_service(space, ScriptedApi({}))
    with pytest.raises(PackageRejectionError, match="上限"):
        service.import_local_pdf(
            package, arxiv_id="2305.01234v2", provenance="test copy"
        )


# ---------------------------------------------------------------- Backup/restore and subprocesses


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
        def download_pdf(self, identifier):
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


@pytest.mark.integration
def test_backup_restore_then_subprocess_resume_query(tmp_path):
    """Run the full flow, back up, restore to a fresh directory, then query all progress
    and materials offline in an independent process.
    """
    space = initialize_workspace(tmp_path / "space")

    # 1) Complete a single-page discovery round.
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

    # 2) Fetch an older paper outside the discovery year range, with an explicit version.
    download_api = ScriptedApi({"2101.01234v2": PACKAGE_A})
    service2 = AcquisitionService(space, download_api, clock=FakeClock())
    result = service2.acquire("2101.01234v2")
    assert result.state == "downloaded"

    # 3) Find pending v5 through revision checking.
    revision_api = _RevisionApi({"2101.01234": _meta("2101.01234", 5)})
    AcquisitionService(space, revision_api, clock=FakeClock()).check_revisions()

    # 4) Import another paper locally.
    package = tmp_path / "local.tar.gz"
    package.write_bytes(PACKAGE_B)
    service3 = AcquisitionService(space, ScriptedApi({}), clock=FakeClock())
    import_result = service3.import_local_pdf(
        package, arxiv_id="2305.01234v1", provenance="restore test copy"
    )
    assert import_result.canonical_id == "2305.01234"

    # 5) Back up and restore to a fresh directory.
    backup_dir = tmp_path / "backup"
    report = backup_workspace(space, backup_dir)
    assert report.files
    restored_root = tmp_path / "restored"
    restored = restore_workspace(backup_dir, restored_root)
    try:
        # 6) Query all progress, provenance, and materials offline in an independent subprocess.
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
    """Include every source file referenced by records in the backup manifest, leaving no
    unbacked orphan state.
    """
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
        assert result.raw_path in paths  # Raw package bytes are backed up.
        assert "business.db" in paths  # Records, receipts, and metadata are backed up.
    finally:
        space.close()


def test_import_replay_with_new_provenance_retained(space, tmp_path):
    """Repeated imports of identical bytes preserve material and original provenance while
    appending new source declarations to the log.
    """
    first = tmp_path / "a.tar.gz"
    first.write_bytes(PACKAGE_A)
    again = tmp_path / "b.tar.gz"
    again.write_bytes(PACKAGE_A)
    service = make_service(space, ScriptedApi({}))
    service.import_local_pdf(first, arxiv_id="2305.01234v2", provenance="first copy")
    repeat = service.import_local_pdf(
        again, arxiv_id="2305.01234v2", provenance="mirror at other site"
    )
    assert repeat.replayed
    status = service.get_selection("2305.01234")
    assert status is not None
    logged = [item["provenance"] for item in status.provenance_log]
    assert logged == ["first copy", "mirror at other site"]
    # One material record reuses the bytes; two declarations have separate event records.
    assert len(space.store().list_records()) == 3
    record = space.store().read_record_current(repeat.material_ref.record_id)
    assert "first copy" in record.data.origin.retrieval
    # Reimporting the same declaration is idempotent and adds no duplicate log entries.
    third = service.import_local_pdf(
        again, arxiv_id="2305.01234v2", provenance="mirror at other site"
    )
    assert third.replayed
    status = service.get_selection("2305.01234")
    assert status is not None
    assert len(status.provenance_log) == 2
    # Declaration records are authoritative; no separate declaration/selection receipt coordination exists.
    assert len(space.store().list_records()) == 3


def test_unsupported_retained_bytes_survive_backup_restore(tmp_path):
    """Back up raw bytes retained for unsupported formats through attempt records; they
    remain readable after restore.
    """
    from hvs_catalog.records.common import ArtifactRef

    space = initialize_workspace(tmp_path / "space")
    try:
        pdf_bytes = b"%PDF-1.4 unsupported form body"
        service = AcquisitionService(
            space, ScriptedApi({"2401.00009v1": pdf_bytes}), clock=FakeClock()
        )
        with pytest.raises(PackageRejectionError):
            service.acquire("2401.00009v1")
        result = service.get_selection("2401.00009")
        assert result.state == "failed"
        attempt_id = f"arxiv-attempt-2401.00009v1-{result.raw_sha256[:16]}"
        record = space.store().read_record_current(attempt_id)
        assert record is not None
        assert service.get_selection("2401.00009").error
        # Attempt records reference retained bytes as attachments, which backup traverses.
        backup_dir = tmp_path / "backup"
        report = backup_workspace(space, backup_dir)
        paths = {entry["path"] for entry in report.files}
        assert result.raw_path in paths
        assert (
            "business.db" in paths
        )  # Back up attempt records with the business database.
        assert report.problems == []
        # After restoring to a fresh workspace, verify and read bytes by hash.
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
    """Retain an attempt record for safety-rejected bytes too, allowing backup traversal."""
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

"""Offline acquisition-format v2 tests using fresh temporary synthetic workspaces.

Atomic commits, public restart, and version lookups replace legacy
generation/index/per-item compensation tests. Inject precise failures at
commit/publication points; use pipe handshakes rather than sleeps for process
contention.
"""

from __future__ import annotations

import gzip
import io
import json
import sqlite3
import subprocess
import sys
import tarfile
from dataclasses import dataclass, field, replace
from datetime import date
from pathlib import Path

import pytest
from materials_helpers import make_pdf

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
from hvs_catalog.records.objects import ExternalRecord
from hvs_catalog.records.source import SourcePackageRecord
from hvs_catalog.workspace import (
    WorkspaceBusyError,
    WorkspaceConflictError,
    WorkspaceIntegrityError,
    backup_workspace,
    initialize_workspace,
    open_workspace,
    restore_workspace,
)


class FakeClock:
    def __init__(self, start=1_700_000_000.0):
        self._now = start

    def now(self):
        return self._now


def make_meta(canonical_id, version, *, published="2021-01-01T00:00:00Z"):
    return ArxivPaperMeta(
        canonical_id=canonical_id,
        version=version,
        api_id_url=f"http://arxiv.org/abs/{canonical_id}v{version}",
        title=f"Paper {canonical_id}",
        authors=("Author One", "Author Two"),
        categories=("astro-ph.CO",),
        primary_category="astro-ph.CO",
        published=published,
        updated="2024-06-01T00:00:00Z",
        abstract="abstract text",
        doi="10.0000/synthetic",
        journal_ref="Synthetic journal",
    )


def base_request(**overrides):
    return ArxivSearchRequest(
        **{
            "submitted_from": date(2024, 1, 1),
            "submitted_to": date(2024, 1, 31),
            "terms": "dark matter",
            "categories": ("astro-ph.CO",),
            **overrides,
        }
    )


def make_page(request, entries, *, total, fetched_at=1_700_000_100.0, anomalies=()):
    return ArxivSearchPage(
        request=request,
        response_url="https://export.arxiv.org/api/query synthetic",
        fetched_at=fetched_at,
        total_results=total,
        returned_start_index=request.start,
        entries=tuple(entries),
        anomalies=tuple(anomalies),
    )


@dataclass
class FakeArxivService:
    pages: dict = field(default_factory=dict)
    metadata: dict = field(default_factory=dict)
    downloads: dict = field(default_factory=dict)
    search_calls: list = field(default_factory=list)
    metadata_calls: list = field(default_factory=list)
    download_calls: list = field(default_factory=list)
    clock: FakeClock | None = None
    anomalies: tuple = ()

    def search(self, request):
        self.search_calls.append(request.start)
        outcome = self.pages[request.start]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    def metadata_by_ids(self, identifiers):
        self.metadata_calls.append(list(identifiers))
        lookups = []
        for key in identifiers:
            ident = parse_arxiv_identifier(key)
            meta = self.metadata.get(ident.canonical_id)
            lookups.append(
                ArxivIdLookup(
                    requested=ident, status="found" if meta else "not_found", meta=meta
                )
            )
        return ArxivIdBatchResult(
            response_url="synthetic",
            fetched_at=1_700_000_000.0,
            lookups=tuple(lookups),
            anomalies=self.anomalies,
        )

    def download_pdf(self, identifier):
        self.download_calls.append(identifier)
        outcome = self.downloads[identifier]
        if isinstance(outcome, Exception):
            raise outcome
        return ArxivSourceDownload(
            identifier=parse_arxiv_identifier(identifier),
            final_url=f"https://arxiv.org/e-print/{identifier}",
            content_type="application/gzip",
            payload=outcome,
            byte_count=len(outcome),
            fetched_at=1_700_000_000.0,
        )


class RaisingService:
    def search(self, request):
        raise AssertionError("不应触网")

    def metadata_by_ids(self, identifiers):
        raise AssertionError("不应触网")

    def download_pdf(self, identifier):
        raise AssertionError("不应触网")


def script_single_page(entries, *, total=None, anomalies=()):
    request = base_request()
    return FakeArxivService(
        pages={
            0: make_page(
                request,
                entries,
                total=len(entries) if total is None else total,
                anomalies=anomalies,
            )
        }
    )


def make_tar_gz(members):
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w") as archive:
        for name, data in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
    return gzip.compress(raw.getvalue(), mtime=0)


GOOD_PACKAGE = make_pdf("Synthetic v1")
OTHER_PACKAGE = make_pdf("Synthetic v2")


@pytest.fixture
def space(tmp_path):
    workspace = initialize_workspace(tmp_path / "space")
    yield workspace
    workspace.close()


@pytest.fixture
def clock():
    return FakeClock()


def build_service(space, api, clock):
    return AcquisitionService(space, api, clock=clock)


def two_pages():
    request = base_request(page_size=2)
    return request, FakeArxivService(
        pages={
            0: make_page(
                request,
                [make_meta("2401.00001", 1), make_meta("2401.00002", 1)],
                total=3,
            ),
            2: make_page(
                replace(request, start=2), [make_meta("2401.00001", 2)], total=3
            ),
        }
    )


def snapshot(space):
    return space.store().transaction_read(lambda tx: tuple(tx.conn.iterdump()))


def source_records(space):
    return [
        r for r in space.store().list_records() if isinstance(r, SourcePackageRecord)
    ]


def test_discovery_page_bound_resume_and_dedup(space, clock):
    request, api = two_pages()
    service = build_service(space, api, clock)
    first = service.run_discovery(request, max_pages=1)
    assert first.state == "incomplete" and first.reason == "page_bound"
    assert len(first.pages) == 1
    space.close()
    reopened = open_workspace(space.root)
    try:
        second = build_service(reopened, api, clock).run_discovery(request)
        assert second.state == "complete"
        assert api.search_calls == [0, 2]
        assert second.papers["2401.00001"]["pages"] == [0, 2]
        assert second.papers["2401.00001"]["version"] == 2
        before = snapshot(reopened)
        third = AcquisitionService(reopened, RaisingService()).run_discovery(request)
        assert third == second
        assert snapshot(reopened) == before
    finally:
        reopened.close()


@pytest.mark.parametrize("anomaly", list(PaginationAnomaly))
def test_discovery_anomaly_not_complete(space, anomaly):
    service = AcquisitionService(
        space, script_single_page([make_meta("2401.00001", 1)], anomalies=(anomaly,))
    )
    status = service.run_discovery(base_request())
    assert status.state == "incomplete" and status.reason == "anomalies"
    with pytest.raises(AcquisitionError, match="异常"):
        service.run_discovery(base_request())


def test_discovery_true_empty_and_new_round(space):
    service = AcquisitionService(space, script_single_page([]))
    first = service.run_discovery(base_request())
    second = service.run_discovery(base_request(), round_seq=2)
    assert first.state == second.state == "complete"
    assert first.papers == {}
    assert first.round_id != second.round_id
    assert len(service.list_discovery_rounds()) == 2


def test_discovery_error_resumes_failed_page_not_empty_success(space):
    request, api = two_pages()
    page = api.pages[2]
    api.pages[2] = RuntimeError("synthetic network failure")
    service = AcquisitionService(space, api)
    with pytest.raises(RuntimeError):
        service.run_discovery(request)
    status = service.get_discovery_round(discovery_round_id(request))
    assert status.state == "failed" and len(status.pages) == 1
    api.pages[2] = page
    assert service.run_discovery(request).state == "complete"
    assert api.search_calls == [0, 2, 2]


def test_discovery_response_provenance_and_binding(space):
    meta = make_meta("2401.00001", 2)
    service = AcquisitionService(space, script_single_page([meta]))
    status = service.run_discovery(base_request())
    page = status.pages[0]
    record = space.store().read_record_current(page["record_id"])
    document = json.loads(space.read_file(record.data.raw_response))
    assert document["parsed_view"] is True
    assert document["entries"][0]["abstract"] == meta.abstract
    assert document["entries"][0]["authors"] == list(meta.authors)
    assert document["entries"][0]["doi"] == meta.doi
    assert record.data.query["submitted_from"] == "2024-01-01"
    assert record.data.query["categories"] == ["astro-ph.CO"]
    state = json.loads(space.store().get_meta("acquisition:state"))
    # No duplicate papers, page-byte/metadata snapshots, or indexes.
    assert set(state["rounds"][status.round_id]["pages"][0]) == {
        "record_id",
        "revision",
    }
    assert "papers" not in state["rounds"][status.round_id]
    state["rounds"][status.round_id]["request"]["terms"] = "changed"
    space.store().set_meta("acquisition:state", json.dumps(state))
    with pytest.raises(AcquisitionError, match="请求.*不一致"):
        service.run_discovery(base_request())


def test_latest_frozen_before_download_and_restart(space, clock):
    api = FakeArxivService(
        metadata={"2101.01234": make_meta("2101.01234", 2)},
        downloads={"2101.01234v2": RuntimeError("download failure")},
    )
    service = build_service(space, api, clock)
    with pytest.raises(RuntimeError):
        service.acquire("2101.01234")
    assert service.get_selection("2101.01234").version == 2
    assert service.get_selection("2101.01234").state == "failed"
    space.close()
    reopened = open_workspace(space.root)
    try:
        api.metadata["2101.01234"] = make_meta("2101.01234", 9)
        api.downloads["2101.01234v2"] = GOOD_PACKAGE
        result = build_service(reopened, api, clock).acquire("2101.01234")
        assert result.version == 2
        assert len(api.metadata_calls) == 1
        assert api.download_calls == ["2101.01234v2", "2101.01234v2"]
    finally:
        reopened.close()


@pytest.mark.parametrize("anomaly", list(PaginationAnomaly))
def test_latest_anomalous_response_not_frozen(space, anomaly):
    api = FakeArxivService(
        metadata={"2101.01234": make_meta("2101.01234", 2)}, anomalies=(anomaly,)
    )
    service = AcquisitionService(space, api)
    with pytest.raises(AcquisitionError, match="批次异常"):
        service.acquire("2101.01234")
    assert service.list_selections() == []
    assert api.download_calls == []
    assert space.store().list_records() == []


def test_latest_missing_or_foreign_metadata_not_frozen(space):
    api = FakeArxivService()
    service = AcquisitionService(space, api)
    for meta in (None, make_meta("2101.99999", 2)):
        if meta:
            api.metadata["2101.01234"] = meta
        with pytest.raises(AcquisitionError):
            service.acquire("2101.01234")
        assert service.get_selection("2101.01234") is None


@pytest.mark.parametrize("identifier", ["2101.01234v1", "astro-ph/0507001v3"])
def test_explicit_and_legacy_versions_and_repeat(space, identifier):
    api = FakeArxivService(downloads={identifier: GOOD_PACKAGE})
    service = AcquisitionService(space, api)
    first = service.acquire(identifier)
    second = service.acquire(identifier)
    third = service.acquire(first.canonical_id)
    assert first.versioned_id == identifier
    assert second.replayed and third.replayed
    assert api.download_calls == [identifier]
    assert api.metadata_calls == []
    assert len(source_records(space)) == 1
    assert service.get_version(identifier).source_record == first.material_ref


def test_explicit_reselection_and_failed_new_version_keep_old(space):
    api = FakeArxivService(
        downloads={
            "2101.01234v1": GOOD_PACKAGE,
            "2101.01234v2": ArxivSourceUnavailableError("404", reason="not_found"),
        }
    )
    service = AcquisitionService(space, api)
    old = service.acquire("2101.01234v1")
    for _ in range(3):
        with pytest.raises(SourceUnavailableError):
            service.acquire("2101.01234v2")
        assert service.selected_material("2101.01234") is None
        assert service.get_version("2101.01234v1").source_record == old.material_ref
        again = service.acquire("2101.01234v1")
        assert again.replayed
    assert api.download_calls.count("2101.01234v1") == 1
    assert [h["version"] for h in service.get_selection("2101.01234").history] == [2]
    assert len(service.list_versions("2101.01234")) == 2


def test_multiple_versions_reuse_same_raw_by_content(space):
    api = FakeArxivService(
        downloads={"2101.01234v1": GOOD_PACKAGE, "2101.01234v2": GOOD_PACKAGE}
    )
    service = AcquisitionService(space, api)
    one = service.acquire("2101.01234v1")
    two = service.acquire("2101.01234v2")
    assert one.raw_path == two.raw_path
    assert one.material_ref != two.material_ref
    assert len(source_records(space)) == 2
    assert len(list((space.root / "sources/packages").iterdir())) == 1


@pytest.mark.parametrize(
    "payload, expected",
    [
        (b"%PDF-1.4 test", "failed"),
        (make_tar_gz({"../escape.tex": b"evil"}), "failed"),
    ],
)
def test_remote_rejected_raw_retained_and_backup(space, tmp_path, payload, expected):
    api = FakeArxivService(downloads={"2401.00010v1": payload})
    service = AcquisitionService(space, api)
    if expected == "failed":
        with pytest.raises(PackageRejectionError):
            service.acquire("2401.00010v1")
    else:
        assert service.acquire("2401.00010v1").state == "unsupported"
        assert service.acquire("2401.00010v1").replayed
    status = service.get_selection("2401.00010")
    assert status.state == expected and status.error
    assert status.source_record is None
    assert service.selected_material("2401.00010") is None
    backup_workspace(space, tmp_path / "backup")
    restored = restore_workspace(tmp_path / "backup", tmp_path / "restored")
    try:
        restored_status = AcquisitionService(restored, RaisingService()).get_selection(
            "2401.00010"
        )
        assert restored_status == status
        assert (
            restored.read_file(
                ArtifactRef(path=status.raw_path, sha256=status.raw_sha256)
            )
            == payload
        )
    finally:
        restored.close()


def test_revisions_outside_discovery_window_and_honest_unknown(space, clock):
    request = base_request()
    api = script_single_page([make_meta("2401.00001", 1)])
    api.downloads = {"2101.01234v1": GOOD_PACKAGE}
    api.metadata = {"2101.01234": make_meta("2101.01234", 5)}
    service = build_service(space, api, clock)
    service.run_discovery(request)
    old = service.acquire("2101.01234v1")
    result = service.check_revisions()
    assert result.entries[0].state == "pending"
    assert result.entries[0].latest_version == 5
    assert service.selected_material("2101.01234").material_ref == old.material_ref
    # Same-second and cross-handle checks need no unique operation key; later actual results replace earlier results.
    other = build_service(space, api, clock)
    api.metadata.clear()
    unknown = other.check_revisions(["2101.01234", "2101.99999"])
    assert all(e.state == "unknown" for e in unknown.entries)
    assert unknown.entries[0].selected_version == 1
    assert unknown.entries[1].selected_version is None
    api.metadata["2101.01234"] = make_meta("2101.01234", 1)
    assert other.check_revisions().entries[0].state == "current"
    api.anomalies = (PaginationAnomaly.DUPLICATE_ENTRY,)
    assert other.check_revisions().entries[0].state == "unknown"


def test_import_alternating_versions_declarations_first_time_and_retrieval(
    space, clock, tmp_path
):
    path = tmp_path / "input.tar.gz"
    path.write_bytes(GOOD_PACKAGE)
    service = build_service(space, RaisingService(), clock)
    first = service.import_local_pdf(path, arxiv_id="2305.01234v1", provenance="A")
    first_status = service.get_selection("2305.01234")
    first_time = first_status.provenance_log[0]["at"]
    for version, provenance in [
        (1, "B"),
        (1, "A"),
        (2, "C"),
        (1, "B"),
        (2, "C"),
        (1, "A"),
    ]:
        clock._now += 60
        service.import_local_pdf(
            path, arxiv_id=f"2305.01234v{version}", provenance=provenance
        )
        assert service.get_selection("2305.01234").version == version
    final = service.get_selection("2305.01234")
    assert final.source_record == first.material_ref
    assert final.retrieval == first_status.retrieval
    assert {p["provenance"] for p in final.provenance_log} == {"A", "B"}
    assert (
        next(p["at"] for p in final.provenance_log if p["provenance"] == "A")
        == first_time
    )
    assert [h["version"] for h in final.history] == [2]
    assert len(source_records(space)) == 2
    assert (
        len([r for r in space.store().list_records() if isinstance(r, ExternalRecord)])
        == 3
    )
    state = json.loads(space.store().get_meta("acquisition:state"))
    assert set(state["selections"]["2305.01234"]) == {
        "versioned_id",
        "via",
        "selected_at",
    }
    assert all(
        "raw" not in f and "retrieval" not in f and "provenance_log" not in f
        for f in state["versions"].values()
    )
    assert not any(
        "generation" in f or "history" in f for f in state["versions"].values()
    )


def test_cross_source_same_bytes_keeps_first_origin(space, clock, tmp_path):
    api = FakeArxivService(downloads={"2401.01234v1": GOOD_PACKAGE})
    service = build_service(space, api, clock)
    downloaded = service.acquire("2401.01234v1")
    original = service.get_selection("2401.01234").retrieval
    path = tmp_path / "mirror.tar.gz"
    path.write_bytes(GOOD_PACKAGE)
    clock._now += 60
    imported = service.import_local_pdf(
        path, arxiv_id="2401.01234v1", provenance="mirror"
    )
    assert imported.replayed and imported.material_ref == downloaded.material_ref
    assert service.get_selection("2401.01234").retrieval == original
    assert (
        service.get_selection("2401.01234").provenance_log[0]["provenance"] == "mirror"
    )
    assert service.acquire("2401.01234").replayed
    assert len(api.download_calls) == 1


def test_conflict_old_unselected_version_rejected_before_publish(space, tmp_path):
    api = FakeArxivService(
        downloads={"2401.01234v1": GOOD_PACKAGE, "2401.01234v2": OTHER_PACKAGE}
    )
    service = AcquisitionService(space, api)
    service.acquire("2401.01234v1")
    service.acquire("2401.01234v2")
    before = snapshot(space)
    files = set(space.root.rglob("*.bin"))
    path = tmp_path / "conflict.tar.gz"
    path.write_bytes(OTHER_PACKAGE)
    with pytest.raises(WorkspaceConflictError, match="不同字节"):
        service.import_local_pdf(path, arxiv_id="2401.01234v1", provenance="conflict")
    assert snapshot(space) == before and set(space.root.rglob("*.bin")) == files
    assert service.get_selection("2401.01234").version == 2


@pytest.mark.parametrize(
    "version, provenance",
    [(True, "x"), (0, "x"), (-1, "x"), (1.5, "x"), (1, " "), (1, "")],
)
def test_invalid_import_declaration_no_changes(space, tmp_path, version, provenance):
    path = tmp_path / "input.tar.gz"
    path.write_bytes(GOOD_PACKAGE)
    before = snapshot(space)
    with pytest.raises(AcquisitionError):
        AcquisitionService(space, RaisingService()).import_local_pdf(
            path, arxiv_id="2305.01234", version=version, provenance=provenance
        )
    assert snapshot(space) == before
    assert path.read_bytes() == GOOD_PACKAGE


@pytest.mark.parametrize(
    "payload",
    [b"%PDF-1.4 test", b"<html>error</html>", make_tar_gz({"../escape.tex": b"bad"})],
)
def test_rejected_local_input_unchanged_not_imported(space, tmp_path, payload):
    path = tmp_path / "rejected"
    path.write_bytes(payload)
    before = snapshot(space)
    with pytest.raises(PackageRejectionError, match="未导入"):
        AcquisitionService(space, RaisingService()).import_local_pdf(
            path, arxiv_id="2305.01234v1", provenance="test"
        )
    assert snapshot(space) == before
    assert path.read_bytes() == payload
    assert list((space.root / "sources").rglob("*.bin")) == []


@pytest.mark.parametrize(
    "damage", ["missing-record", "foreign-record", "missing-file", "hash"]
)
def test_material_delivery_refuses_missing_conflicting_data(space, damage):
    service = AcquisitionService(
        space, FakeArxivService(downloads={"2401.01234v1": GOOD_PACKAGE})
    )
    result = service.acquire("2401.01234v1")
    if damage in ("missing-record", "foreign-record"):
        with sqlite3.connect(space.root / "business.db") as conn:
            if damage == "missing-record":
                conn.execute(
                    "DELETE FROM records WHERE record_id=?",
                    (result.material_ref.record_id,),
                )
            else:
                # Failure windows need no automatic repair, but exact identity must be verified.
                conn.execute(
                    "UPDATE records SET payload=replace(payload, '2401.01234v1', '2401.99999v1') WHERE record_id=?",
                    (result.material_ref.record_id,),
                )
    elif damage == "missing-file":
        (space.root / result.raw_path).unlink()
    else:
        (space.root / result.raw_path).write_bytes(b"tampered")
    before = snapshot(space)
    with pytest.raises((AcquisitionError, WorkspaceIntegrityError)):
        service.selected_material("2401.01234")
    # Do not modify or fabricate missing materials.
    assert snapshot(space) == before


def test_status_projections_no_network_no_writes_and_backup(space, tmp_path):
    api = FakeArxivService(downloads={"2401.01234v1": GOOD_PACKAGE})
    service = AcquisitionService(space, api)
    result = service.acquire("2401.01234v1")
    origin = space.store().read_record(result.material_ref).data.origin.retrieval
    before = snapshot(space)
    readonly = AcquisitionService(space, RaisingService())
    assert readonly.get_selection("2401.01234").retrieval == origin
    assert readonly.get_version("2401.01234v1").retrieval == origin
    assert readonly.selected_material("2401.01234").origin_retrieval == origin
    assert snapshot(space) == before
    backup_workspace(space, tmp_path / "bk")
    restored = restore_workspace(tmp_path / "bk", tmp_path / "restored")
    try:
        assert (
            AcquisitionService(restored, RaisingService())
            .get_selection("2401.01234")
            .retrieval
            == origin
        )
    finally:
        restored.close()


@pytest.mark.integration
@pytest.mark.parametrize(
    "operation", ["discover", "fetch", "import", "revisions", "empty-revisions"]
)
def test_all_write_operations_busy_for_other_process(space, tmp_path, operation):
    code = """
import sys
from hvs_catalog.workspace import open_workspace
space = open_workspace(sys.argv[1])
with space.lock():
    print('LOCKED', flush=True)
    sys.stdin.readline()
space.close()
"""
    process = subprocess.Popen(
        [sys.executable, "-c", code, str(space.root)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert process.stdout.readline().strip() == "LOCKED"
        service = AcquisitionService(space, RaisingService())
        path = tmp_path / "input.tar.gz"
        path.write_bytes(GOOD_PACKAGE)
        with pytest.raises(WorkspaceBusyError):
            if operation == "discover":
                service.run_discovery(base_request())
            elif operation == "fetch":
                service.acquire("2401.01234v1")
            elif operation == "import":
                service.import_local_pdf(
                    path, arxiv_id="2401.01234v1", provenance="test"
                )
            else:
                service.check_revisions([] if operation == "empty-revisions" else None)
        assert service.list_selections() == []  # Committed facts remain queryable.
    finally:
        process.communicate("release\n", timeout=30)
    assert process.returncode == 0
    assert AcquisitionService(space, RaisingService()).check_revisions().entries == ()


CRASH_SCRIPT = """
import os, sys
sys.path.insert(0, sys.argv[1])
from test_acquisition_lifecycle import (FakeArxivService, GOOD_PACKAGE, AcquisitionService,
    initialize_workspace, script_single_page, base_request, make_meta)
space = initialize_workspace(sys.argv[2])
operation, window = sys.argv[3:5]
api = FakeArxivService(downloads={'2401.01234v1': GOOD_PACKAGE})
if operation == 'discover':
    api = script_single_page([make_meta('2401.00001', 1)])
service = AcquisitionService(space, api)
store = space.store()
real_commit = store.commit
real_publish = space.publish_source_file
if window == 'after_publish':
    def publish(*args, **kwargs):
        result = real_publish(*args, **kwargs)
        os._exit(70)
    space.publish_source_file = publish
else:
    def commit(request):
        target = operation != 'fetch' or any(r.record_id.startswith('arxiv-source-') for r in request.new_records)
        if target and window == 'before_commit':
            os._exit(70)
        result = real_commit(request)
        if (target and window == 'after_commit') or (operation == 'fetch' and window == 'after_raw_commit'):
            os._exit(70)
        return result
    store.commit = commit
if operation == 'fetch':
    service.acquire('2401.01234v1')
elif operation == 'import':
    service.import_local_pdf(sys.argv[5], arxiv_id='2401.01234v1', provenance='A')
else:
    service.run_discovery(base_request())
"""


@pytest.mark.integration
@pytest.mark.parametrize(
    "operation,window",
    [
        (op, win)
        for op in ("fetch", "import", "discover")
        for win in ("after_publish", "before_commit", "after_commit")
    ]
    + [("fetch", "after_raw_commit")],
)
def test_precise_crash_windows_new_process_resume(tmp_path, operation, window):
    root = tmp_path / "space"
    path = tmp_path / "input.tar.gz"
    path.write_bytes(GOOD_PACKAGE)
    child = subprocess.run(
        [
            sys.executable,
            "-c",
            CRASH_SCRIPT,
            str(Path(__file__).parent),
            str(root),
            operation,
            window,
            str(path),
        ],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert child.returncode == 70, child.stderr
    space = open_workspace(root)
    try:
        api = FakeArxivService(downloads={"2401.01234v1": GOOD_PACKAGE})
        if operation == "discover":
            api = script_single_page([make_meta("2401.00001", 1)])
        service = AcquisitionService(space, api)
        if operation == "fetch":
            if window in ("before_commit", "after_raw_commit"):
                assert service.selected_material("2401.01234") is None
                backup_workspace(space, tmp_path / "partial-backup")
                restored = restore_workspace(
                    tmp_path / "partial-backup", tmp_path / "partial-restored"
                )
                try:
                    result = AcquisitionService(restored, RaisingService()).acquire(
                        "2401.01234"
                    )
                    assert result.state == "downloaded"
                finally:
                    restored.close()
            result = service.acquire("2401.01234")
            assert result.state == "downloaded"
            if window != "after_publish":
                assert api.download_calls == []
            assert len(source_records(space)) == 1
            assert service.acquire("2401.01234v1").replayed
        elif operation == "import":
            if window != "after_commit":
                assert service.list_selections() == []
                assert space.store().list_records() == []
            result = service.import_local_pdf(
                path, arxiv_id="2401.01234v1", provenance="A"
            )
            first = service.get_selection("2401.01234")
            assert service.import_local_pdf(
                path, arxiv_id="2401.01234v1", provenance="A"
            ).replayed
            assert (
                service.get_selection("2401.01234").provenance_log
                == first.provenance_log
            )
            assert len(source_records(space)) == 1
            assert len(space.store().list_records()) == 2
        else:
            result = service.run_discovery(base_request())
            assert result.state == "complete" and len(result.pages) == 1
            assert (
                len(
                    [
                        r
                        for r in space.store().list_records()
                        if r.data.service == "arxiv-api"
                    ]
                )
                == 1
            )
            if window == "after_commit":
                assert api.search_calls == []
    finally:
        space.close()


@pytest.mark.integration
@pytest.mark.parametrize(
    "stage", ["discover", "metadata", "download", "revisions", "import"]
)
def test_operation_holds_space_lock_not_database_transaction(
    space, tmp_path, monkeypatch, stage
):
    probe = """
import sqlite3, sys
from hvs_catalog.workspace import open_workspace, WorkspaceBusyError
space = open_workspace(sys.argv[1])
try:
    with space.lock():
        raise AssertionError('操作期间空间锁被释放')
except WorkspaceBusyError:
    pass
with sqlite3.connect(space.root / 'business.db', timeout=0.1) as conn:
    conn.execute('BEGIN IMMEDIATE')
    conn.rollback()
space.close()
"""

    def check():
        child = subprocess.run(
            [sys.executable, "-c", probe, str(space.root)],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        assert child.returncode == 0, child.stderr

    api = FakeArxivService(
        metadata={"2401.01234": make_meta("2401.01234", 1)},
        downloads={"2401.01234v1": GOOD_PACKAGE},
    )
    service = AcquisitionService(space, api)
    if stage == "discover":
        api = script_single_page([make_meta("2401.00001", 1)])
        real = api.search

        def search(request):
            check()
            return real(request)

        api.search = search
        AcquisitionService(space, api).run_discovery(base_request())
    elif stage in ("metadata", "revisions"):
        if stage == "revisions":
            service.acquire("2401.01234v1")
        real = api.metadata_by_ids

        def metadata(ids):
            check()
            return real(ids)

        api.metadata_by_ids = metadata
        if stage == "metadata":
            service.acquire("2401.01234")
        else:
            service.check_revisions()
    elif stage == "download":
        real = api.download_pdf

        def download(identifier):
            check()
            return real(identifier)

        api.download_pdf = download
        service.acquire("2401.01234v1")
    else:
        from hvs_catalog.pipeline.acquisition import lifecycle

        real = lifecycle.pdf_page_count

        def inspect(payload):
            check()
            return real(payload)

        monkeypatch.setattr(lifecycle, "pdf_page_count", inspect)
        path = tmp_path / "input.tar.gz"
        path.write_bytes(GOOD_PACKAGE)
        service.import_local_pdf(path, arxiv_id="2401.01234v1", provenance="test")


def test_new_declaration_failed_save_is_atomic(space, tmp_path, monkeypatch):
    path = tmp_path / "input.tar.gz"
    path.write_bytes(GOOD_PACKAGE)
    service = AcquisitionService(space, RaisingService(), clock=FakeClock())
    service.import_local_pdf(path, arxiv_id="2401.01234v1", provenance="A")
    before = snapshot(space)
    real = space.store().commit

    def fail(request):
        raise RuntimeError("injected before atomic record/state commit")

    monkeypatch.setattr(space.store(), "commit", fail)
    with pytest.raises(RuntimeError, match="injected"):
        service.import_local_pdf(path, arxiv_id="2401.01234v2", provenance="B")
    assert snapshot(space) == before
    assert service.get_version("2401.01234v2") is None
    monkeypatch.setattr(space.store(), "commit", real)
    service.import_local_pdf(path, arxiv_id="2401.01234v2", provenance="B")
    assert service.get_selection("2401.01234").version == 2
    assert service.get_version("2401.01234v1").provenance_log[0]["provenance"] == "A"


def test_missing_declaration_basis_not_silently_skipped(space, tmp_path):
    path = tmp_path / "input.tar.gz"
    path.write_bytes(GOOD_PACKAGE)
    service = AcquisitionService(space, RaisingService())
    service.import_local_pdf(path, arxiv_id="2401.01234v1", provenance="A")
    declaration = next(
        r for r in space.store().list_records() if isinstance(r, ExternalRecord)
    )
    payload = declaration.model_dump(mode="json", by_alias=True)
    payload["data"]["entries"][0]["fields"] = [
        f
        for f in payload["data"]["entries"][0]["fields"]
        if f["name"] != "first_declared_at"
    ]
    with sqlite3.connect(space.root / "business.db") as conn:
        conn.execute(
            "UPDATE records SET payload=? WHERE record_id=?",
            (json.dumps(payload), declaration.record_id),
        )
    before = snapshot(space)
    with pytest.raises(AcquisitionError, match="声明记录损坏"):
        service.get_version("2401.01234v1")
    with pytest.raises(AcquisitionError, match="声明记录损坏"):
        service.import_local_pdf(path, arxiv_id="2401.01234v1", provenance="B")
    assert snapshot(space) == before


@pytest.mark.parametrize("legacy", [{"format": 1}, None])
def test_legacy_format_rejected_preserves_old_data(space, legacy):
    if legacy is None:
        space.store().set_meta("acquisition:index", '{"selections":["synthetic"]}')
    else:
        space.store().set_meta("acquisition:state", json.dumps(legacy))
    before = snapshot(space)
    service = AcquisitionService(space, RaisingService())
    with pytest.raises(AcquisitionError, match="新建工作空间"):
        service.list_selections()
    with pytest.raises(AcquisitionError, match="新建工作空间"):
        service.acquire("2401.01234v1")
    assert snapshot(space) == before

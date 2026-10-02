"""Serial acquisition: version facts, current pointers, and round progress;
records/attachments are authoritative.

Format v2 does not migrate old development state. Writers hold workspace locks for whole
operations but never database transactions during network waits. Save associated
records/state through existing atomic commit interfaces, without per-step compensation
receipts. External responses and local commits are not one transaction; exits before
commit may repeat requests.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from hvs_catalog.records.common import (
    ArtifactRef,
    RecordRef,
    build_record,
    canonical_json,
)
from hvs_catalog.records.objects import ExternalRecord
from hvs_catalog.records.source import (
    ArxivOrigin,
    SourceFile,
    SourcePackageData,
    SourcePackageRecord,
)
from hvs_catalog.workspace import CommitRequest, Workspace, WorkspaceConflictError

from .archive import (
    MAX_PACKAGE_BYTES,
    PackageIntegrityError,
    UnsupportedPackageError,
    inspect_package,
)
from .protocol import (
    MAX_ID_LIST_LENGTH,
    ArxivApiService,
    ArxivIdentifier,
    ArxivSearchRequest,
    ArxivSourceUnavailableError,
    parse_arxiv_identifier,
)

__all__ = [
    "AcquisitionError",
    "AcquisitionResult",
    "AcquisitionService",
    "DiscoveryRoundStatus",
    "ImportResult",
    "PackageRejectionError",
    "RevisionCheckResult",
    "RevisionStatus",
    "SelectedMaterial",
    "SelectionStatus",
    "SourceUnavailableError",
]

_STATE_KEY = "acquisition:state"
_STATE_VERSION = 2
ROUND_IN_PROGRESS = "in_progress"
ROUND_COMPLETE = "complete"
ROUND_INCOMPLETE = "incomplete"
ROUND_FAILED = "failed"


class AcquisitionError(Exception):
    """Acquisition engineering failure; retain committed facts for queries/retries."""


class SourceUnavailableError(AcquisitionError):
    """The server has no usable source; this is not scientific exclusion."""


class PackageRejectionError(AcquisitionError):
    """The source package failed safety/integrity checks."""


@dataclass(frozen=True)
class DiscoveryRoundStatus:
    round_id: str
    state: str
    request: dict[str, Any]
    pages: tuple[dict[str, Any], ...] = ()
    papers: dict[str, dict[str, Any]] = field(default_factory=dict)
    reason: str | None = None


@dataclass(frozen=True)
class SelectionStatus:
    canonical_id: str
    version: int
    versioned_id: str
    state: str
    via: str
    selected_at: str
    source_record: RecordRef | None = None
    raw_path: str | None = None
    raw_sha256: str | None = None
    retrieval: str | None = None
    package_inventory: tuple[dict[str, Any], ...] = ()
    provenance_log: tuple[dict[str, Any], ...] = ()
    error: str | None = None
    # One item per other version, derived from version facts rather than selection snapshots.
    history: tuple[dict[str, Any], ...] = ()


@dataclass(frozen=True)
class AcquisitionResult:
    canonical_id: str
    version: int
    versioned_id: str
    state: str
    material_ref: RecordRef | None
    raw_path: str | None
    raw_sha256: str | None
    replayed: bool


@dataclass(frozen=True)
class ImportResult:
    canonical_id: str
    version: int
    versioned_id: str
    material_ref: RecordRef
    raw_path: str
    raw_sha256: str
    replayed: bool


@dataclass(frozen=True)
class RevisionStatus:
    canonical_id: str
    selected_version: int | None
    latest_version: int | None
    latest_updated: str | None
    checked_at: str
    state: str
    error: str | None = None


@dataclass(frozen=True)
class RevisionCheckResult:
    checked_at: str
    entries: tuple[RevisionStatus, ...]


@dataclass(frozen=True)
class SelectedMaterial:
    canonical_id: str
    version: int
    versioned_id: str
    material_ref: RecordRef
    raw_path: str
    raw_sha256: str
    origin_retrieval: str


def _now_iso(clock: Any) -> str:
    timestamp = clock.now() if clock is not None else datetime.now(UTC).timestamp()
    return datetime.fromtimestamp(float(timestamp), tz=UTC).isoformat()


def _safe_segment(text: str) -> str:
    return text.replace("/", "_")


def _dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _inventory_payload(inspection: Any) -> list[dict[str, Any]]:
    return [
        {"name": m.name, "size": m.size, "sha256": m.sha256, "media_type": m.media_type}
        for m in inspection.members
    ]


def _request_basis(request: ArxivSearchRequest) -> dict:
    return {
        "submitted_from": request.submitted_from.isoformat(),
        "submitted_to": request.submitted_to.isoformat(),
        "terms": request.terms,
        "categories": list(request.categories),
        "sort_by": request.sort_by,
        "sort_order": request.sort_order,
        "page_size": request.page_size,
    }


def discovery_round_id(request: ArxivSearchRequest, round_seq: int = 1) -> str:
    basis = {**_request_basis(request), "round_seq": int(round_seq)}
    digest = hashlib.sha256(canonical_json(basis).encode()).hexdigest()
    return f"d{digest[:16]}"


def _page_document(page: Any) -> bytes:
    """Complete parsed protocol view without claiming it is a raw Atom response."""
    document = {
        "kind": "arxiv_discovery_page",
        "parsed_view": True,
        "response_url": page.response_url,
        "fetched_at": page.fetched_at,
        "request": {
            "search_query": page.request.search_query(),
            "start": page.request.start,
            "page_size": page.request.page_size,
            "sort_by": page.request.sort_by,
            "sort_order": page.request.sort_order,
        },
        "total_results": page.total_results,
        "returned_start_index": page.returned_start_index,
        "anomalies": [a.value for a in page.anomalies],
        "entries": [
            {
                "canonical_id": m.canonical_id,
                "version": m.version,
                "api_id_url": m.api_id_url,
                "published": m.published,
                "updated": m.updated,
                "title": m.title,
                "authors": list(m.authors),
                "categories": list(m.categories),
                "primary_category": m.primary_category,
                "abstract": m.abstract,
                "doi": m.doi,
                "journal_ref": m.journal_ref,
            }
            for m in page.entries
        ],
    }
    return (
        json.dumps(document, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    ).encode()


class AcquisitionService:
    """Acquisition service for initialized workspaces; queries neither access networks nor
    repair data.
    """

    def __init__(
        self, space: Workspace, api: ArxivApiService | None = None, *, clock: Any = None
    ) -> None:
        self._space = space
        self._api = api if api is not None else ArxivApiService()
        self._clock = clock

    def _load(self) -> dict:
        store = self._space.store()
        raw = store.get_meta(_STATE_KEY)
        if raw is None:
            # Reject old formats explicitly without silent resets, legacy content scans, or migration.
            old = store.transaction_read(
                lambda tx: tx.conn.execute(
                    "SELECT 1 FROM meta WHERE key LIKE 'acquisition:%' LIMIT 1"
                ).fetchone()
            )
            if old is not None:
                raise AcquisitionError(
                    "不兼容的旧获取状态；请新建工作空间（不自动迁移或删除旧文件）"
                )
            return {
                "format": _STATE_VERSION,
                "rounds": {},
                "selections": {},
                "versions": {},
                "revisions": {},
            }
        try:
            state = json.loads(raw)
            if state["format"] != _STATE_VERSION or any(
                not isinstance(state[key], dict)
                for key in ("rounds", "selections", "versions", "revisions")
            ):
                raise ValueError("format")
        except (ValueError, KeyError, TypeError) as exc:
            raise AcquisitionError(
                "获取状态损坏或格式不兼容；保留现场，请人工检查或新建工作空间"
            ) from exc
        return state

    def _save(self, state: dict, records: list | None = None) -> None:
        updates = {_STATE_KEY: _dump(state)}
        if records:
            # Shared-record receipts prove commits only; they do not coordinate/replay acquisition.
            self._space.store().commit(
                CommitRequest(
                    operation_key=f"acq:records:{uuid.uuid4().hex}",
                    kind="acquisition_records",
                    new_records=records,
                    result_refs=[r.ref() for r in records],
                    meta_updates=updates,
                )
            )
        else:
            self._space.store().set_meta_many(updates)

    def list_discovery_rounds(self) -> list[DiscoveryRoundStatus]:
        return [self._round_status(r) for r in self._load()["rounds"].values()]

    def get_discovery_round(self, round_id: str) -> DiscoveryRoundStatus | None:
        value = self._load()["rounds"].get(round_id)
        return self._round_status(value) if value is not None else None

    def list_selections(self) -> list[SelectionStatus]:
        state = self._load()
        return [self._selection_status(state, key) for key in state["selections"]]

    def get_selection(self, canonical_id: str) -> SelectionStatus | None:
        state = self._load()
        return (
            self._selection_status(state, canonical_id)
            if canonical_id in state["selections"]
            else None
        )

    def get_version(self, identifier: str) -> SelectionStatus | None:
        """Look up a specific version with materials/provenance without changing current
        selection.
        """
        ident = parse_arxiv_identifier(identifier)
        if ident.version is None:
            raise AcquisitionError("回查版本必须提供带版本号的 arXiv 标识")
        state = self._load()
        return (
            self._version_status(state, ident.versioned_id)
            if ident.versioned_id in state["versions"]
            else None
        )

    def list_versions(self, canonical_id: str | None = None) -> list[SelectionStatus]:
        state = self._load()
        return [
            self._version_status(state, key)
            for key in state["versions"]
            if canonical_id is None
            or parse_arxiv_identifier(key).canonical_id == canonical_id
        ]

    def get_revision_status(self, canonical_id: str) -> RevisionStatus | None:
        value = self._load()["revisions"].get(canonical_id)
        return RevisionStatus(**value) if value is not None else None

    def _source_record(self, fact: dict) -> SourcePackageRecord | None:
        ref = fact.get("source_record")
        if ref is None:
            return None
        record = self._space.store().read_record(RecordRef(**ref))
        ident = parse_arxiv_identifier(fact["versioned_id"])
        if not isinstance(record, SourcePackageRecord) or (
            record.data.paper_key != ident.canonical_id
            or record.data.origin.kind != "arxiv"
            or record.data.origin.arxiv_id != ident.versioned_id
            or record.data.source_version != f"v{ident.version}"
            or len(record.data.files) != 1
            or record.data.files[0].role != "raw_package"
        ):
            raise AcquisitionError(
                f"{ident.versioned_id} 材料记录缺失或身份不符；不自动修复"
            )
        return record

    def _download_record(self, fact: dict) -> ExternalRecord | None:
        ref = fact.get("attempt_record")
        if ref is None:
            return None
        record = self._space.store().read_record(RecordRef(**ref))
        if (
            not isinstance(record, ExternalRecord)
            or record.data.service != "arxiv-eprint-attempt"
            or record.data.query.get("versioned_id") != fact["versioned_id"]
            or record.data.raw_response is None
        ):
            raise AcquisitionError("原包获取依据记录缺失或身份不符；不补造来源")
        return record

    def _declarations(self, versioned: str) -> tuple[dict, ...]:
        declarations = []
        for record in self._space.store().list_records():
            if (
                not isinstance(record, ExternalRecord)
                or record.data.service != "arxiv-local-import"
            ):
                continue
            if record.data.query.get("versioned_id") != versioned:
                continue
            fields = {
                f.name: f.raw_value
                for entry in record.data.entries
                for f in entry.fields
            }
            if not {"provenance", "first_declared_at"} <= fields.keys():
                raise AcquisitionError(f"{versioned} 来源声明记录损坏；请人工检查")
            declarations.append(
                {"provenance": fields["provenance"], "at": fields["first_declared_at"]}
            )
        return tuple(
            sorted(declarations, key=lambda item: (item["at"], item["provenance"]))
        )

    def _version_status(self, state: dict, versioned: str) -> SelectionStatus:
        fact = state["versions"][versioned]
        ident = parse_arxiv_identifier(versioned)
        record = self._source_record(fact)
        inventory = ()
        raw = None
        retrieval = None
        if record is not None:
            file = record.data.files[0]
            raw = {"path": file.path, "sha256": file.sha256}
            retrieval = record.data.origin.retrieval
            inventory = tuple(
                _inventory_payload(
                    inspect_package(self._space.read_file(ArtifactRef(**raw)))
                )
            )
        else:
            attempt = self._download_record(fact)
            if attempt is not None:
                raw = attempt.data.raw_response.model_dump(mode="json")
                retrieval = f"downloaded from {attempt.data.query['final_url']} at {attempt.data.query['fetched_at']}"
        return SelectionStatus(
            canonical_id=ident.canonical_id,
            version=ident.version,
            versioned_id=versioned,
            state=fact["state"],
            via=fact["via"],
            selected_at=fact["registered_at"],
            source_record=record.ref() if record is not None else None,
            raw_path=raw["path"] if raw else None,
            raw_sha256=raw["sha256"] if raw else None,
            retrieval=retrieval,
            package_inventory=inventory,
            provenance_log=self._declarations(versioned),
            error=fact.get("error"),
        )

    def _selection_status(self, state: dict, canonical_id: str) -> SelectionStatus:
        pointer = state["selections"][canonical_id]
        status = self._version_status(state, pointer["versioned_id"])
        history = []
        for key, fact in state["versions"].items():
            ident = parse_arxiv_identifier(key)
            if ident.canonical_id == canonical_id and key != status.versioned_id:
                history.append(
                    {
                        "version": ident.version,
                        "state": fact["state"],
                        "via": fact["via"],
                        "at": fact["registered_at"],
                    }
                )
        return SelectionStatus(
            **{
                **status.__dict__,
                "via": pointer["via"],
                "selected_at": pointer["selected_at"],
                "history": tuple(sorted(history, key=lambda item: item["version"])),
            }
        )

    def selected_material(self, canonical_id: str) -> SelectedMaterial | None:
        state = self._load()
        pointer = state["selections"].get(canonical_id)
        if pointer is None:
            return None
        fact = state["versions"][pointer["versioned_id"]]
        if fact["state"] not in ("downloaded", "imported"):
            return None
        record = self._source_record(fact)
        if record is None:
            raise AcquisitionError(f"{fact['versioned_id']} 已完成但缺少材料记录引用")
        file = record.data.files[0]
        self._space.read_file(ArtifactRef(path=file.path, sha256=file.sha256))
        ident = parse_arxiv_identifier(fact["versioned_id"])
        return SelectedMaterial(
            ident.canonical_id,
            ident.version,
            ident.versioned_id,
            record.ref(),
            file.path,
            file.sha256,
            record.data.origin.retrieval,
        )

    def _select(self, state: dict, ident: ArxivIdentifier, via: str) -> dict:
        versioned = ident.versioned_id
        if versioned not in state["versions"]:
            state["versions"][versioned] = {
                "versioned_id": versioned,
                "registered_at": _now_iso(self._clock),
                "via": via,
                "state": "selected",
                "error": None,
                "source_record": None,
            }
        current = state["selections"].get(ident.canonical_id)
        if current is None or current["versioned_id"] != versioned:
            state["selections"][ident.canonical_id] = {
                "versioned_id": versioned,
                "via": via,
                "selected_at": _now_iso(self._clock),
            }
        return state["versions"][versioned]

    def acquire(self, identifier: ArxivIdentifier | str) -> AcquisitionResult:
        ident = (
            parse_arxiv_identifier(identifier)
            if isinstance(identifier, str)
            else identifier
        )
        with self._space.lock():
            state = self._load()
            if ident.version is None:
                current = state["selections"].get(ident.canonical_id)
                if current is not None:
                    ident = parse_arxiv_identifier(current["versioned_id"])
                    via = current["via"]
                else:
                    batch = self._api.metadata_by_ids([ident.canonical_id])
                    if batch.anomalies:
                        raise AcquisitionError(
                            f"{ident.canonical_id} 元数据批次异常；不能据此选定最新版"
                        )
                    lookup = next(
                        (
                            x
                            for x in batch.lookups
                            if x.requested.canonical_id == ident.canonical_id
                        ),
                        None,
                    )
                    if (
                        lookup is None
                        or lookup.status != "found"
                        or lookup.meta is None
                        or lookup.meta.canonical_id != ident.canonical_id
                    ):
                        raise AcquisitionError(
                            f"元数据查询未返回 {ident.canonical_id}；不能猜测版本，未创建冻结"
                        )
                    ident = parse_arxiv_identifier(
                        f"{ident.canonical_id}v{lookup.meta.version}"
                    )
                    via = "latest_at_selection"
            else:
                via = "explicit"
            fact = state["versions"].get(ident.versioned_id)
            if fact is not None and fact["state"] in (
                "downloaded",
                "imported",
                "unsupported",
            ):
                result = self._acquisition_result(state, ident, replayed=True)
                current = state["selections"].get(ident.canonical_id)
                if current is None or current["versioned_id"] != ident.versioned_id:
                    self._select(state, ident, via)
                    self._save(state)
                return result
            fact = self._select(state, ident, via)
            self._save(
                state
            )  # Freeze latest before download; explicit old-version selection does not rely on provenance replay.
            # Reuse registered raw packages after pre-commit failure/retry instead of requesting again.
            attempt = self._download_record(fact)
            if attempt is None:
                try:
                    download = self._api.download_source(ident.versioned_id)
                except ArxivSourceUnavailableError as exc:
                    fact.update(state="source_unavailable", error=str(exc))
                    self._save(state)
                    raise SourceUnavailableError(
                        f"{ident.versioned_id} 源码包不可用：{exc}；已保留待处理记录"
                    ) from exc
                except Exception as exc:
                    fact.update(state="failed", error=str(exc))
                    self._save(state)
                    raise
                self._reject_conflict(state, ident, download.payload)
                ref = self._publish_raw(download.payload)
                attempt = self._attempt_record(
                    ident, ref, download.fetched_at, download.final_url
                )
                fact["attempt_record"] = attempt.ref().model_dump(mode="json")
                self._save(
                    state, [attempt]
                )  # Raw packages can be backed up; source usability is not yet claimed.
            raw = attempt.data.raw_response
            payload = self._space.read_file(raw)
            try:
                inspect_package(payload)
            except UnsupportedPackageError as exc:
                fact.update(state="unsupported", error=str(exc))
                self._save(state)
                return self._acquisition_result(state, ident, replayed=False)
            except PackageIntegrityError as exc:
                fact.update(state="failed", error=str(exc))
                self._save(state)
                raise PackageRejectionError(
                    f"{ident.versioned_id} 源码包未通过安全检查：{exc}"
                ) from exc
            retrieval = f"downloaded from {attempt.data.query['final_url']} at {attempt.data.query['fetched_at']}"
            record = self._new_source(ident, raw, retrieval)
            fact.update(
                state="downloaded",
                error=None,
                source_record=record.ref().model_dump(mode="json"),
            )
            self._save(state, [record])
            return self._acquisition_result(state, ident, replayed=False)

    def _acquisition_result(
        self, state: dict, ident: ArxivIdentifier, *, replayed: bool
    ) -> AcquisitionResult:
        status = self._version_status(state, ident.versioned_id)
        if status.raw_path:
            self._space.read_file(
                ArtifactRef(path=status.raw_path, sha256=status.raw_sha256)
            )
        if status.state in ("downloaded", "imported") and status.source_record is None:
            raise AcquisitionError("已完成状态缺少材料记录；不补造")
        return AcquisitionResult(
            ident.canonical_id,
            ident.version,
            ident.versioned_id,
            status.state,
            status.source_record,
            status.raw_path,
            status.raw_sha256,
            replayed,
        )

    def _publish_raw(self, payload: bytes) -> ArtifactRef:
        digest = hashlib.sha256(payload).hexdigest()
        return self._space.publish_source_file(f"packages/{digest}.bin", payload)

    def _reject_conflict(
        self, state: dict, ident: ArxivIdentifier, payload: bytes
    ) -> None:
        fact = state["versions"].get(ident.versioned_id)
        if fact is None:
            return
        record = self._source_record(fact)
        attempt = self._download_record(fact) if record is None else None
        digest = (
            record.data.files[0].sha256
            if record
            else (attempt.data.raw_response.sha256 if attempt else None)
        )
        if digest and digest != hashlib.sha256(payload).hexdigest():
            raise WorkspaceConflictError(
                f"{ident.versioned_id} 已有不同字节的材料；同论文同版本不能覆盖不同内容"
            )

    def _new_source(
        self, ident: ArxivIdentifier, raw: ArtifactRef, retrieval: str
    ) -> SourcePackageRecord:
        return build_record(
            SourcePackageRecord,
            record_id=f"arxiv-source-{_safe_segment(ident.versioned_id)}",
            created_at=datetime.fromisoformat(_now_iso(self._clock)),
            execution_id=None,
            data=SourcePackageData(
                paper_key=ident.canonical_id,
                origin=ArxivOrigin(
                    kind="arxiv", arxiv_id=ident.versioned_id, retrieval=retrieval
                ),
                source_version=f"v{ident.version}",
                files=[
                    SourceFile(
                        file_id="raw",
                        path=raw.path,
                        sha256=raw.sha256,
                        media_type="application/octet-stream",
                        role="raw_package",
                        read_state="unparsed",
                        derived_from=[],
                    )
                ],
                gaps=[],
            ),
        )

    def _attempt_record(
        self,
        ident: ArxivIdentifier,
        raw: ArtifactRef,
        fetched_at: float,
        final_url: str,
    ) -> ExternalRecord:
        dt = datetime.fromtimestamp(float(fetched_at), tz=UTC)
        return build_record(
            ExternalRecord,
            record_id=f"arxiv-attempt-{_safe_segment(ident.versioned_id)}-{raw.sha256[:16]}",
            created_at=dt,
            execution_id=None,
            data={
                "service": "arxiv-eprint-attempt",
                "catalog_version": None,
                "queried_at": dt,
                "query": {
                    "versioned_id": ident.versioned_id,
                    "fetched_at": fetched_at,
                    "final_url": final_url,
                },
                "raw_response": {"path": raw.path, "sha256": raw.sha256},
                "entries": [],
            },
        )

    def import_local_package(
        self,
        path: str | Path,
        *,
        arxiv_id: str,
        version: int | None = None,
        provenance: str = "",
    ) -> ImportResult:
        if not provenance or not provenance.strip():
            raise AcquisitionError("本地导入必须提供非空来源声明（provenance）")
        if version is not None and (
            isinstance(version, bool) or not isinstance(version, int) or version <= 0
        ):
            raise AcquisitionError(f"声明的版本必须是正整数：{version!r}")
        ident = parse_arxiv_identifier(arxiv_id)
        if version is None:
            version = ident.version
        if version is None:
            raise AcquisitionError("本地导入必须声明具体版本")
        if ident.version is not None and ident.version != version:
            raise AcquisitionError("声明的版本互相矛盾")
        ident = parse_arxiv_identifier(f"{ident.canonical_id}v{version}")
        with self._space.lock():
            state = self._load()
            file = Path(path)
            if not file.is_file():
                raise AcquisitionError(f"导入文件不存在或不是常规文件：{file}")
            # Bounded reading covers growing files too; preserve the caller's original file.
            with file.open("rb") as stream:
                payload = stream.read(MAX_PACKAGE_BYTES + 1)
            if len(payload) > MAX_PACKAGE_BYTES:
                raise PackageRejectionError(
                    f"导入文件超过工程上限 {MAX_PACKAGE_BYTES}；未导入、未备份，原文件未改动"
                )
            try:
                inspect_package(payload)
            except PackageIntegrityError as exc:
                raise PackageRejectionError(
                    f"导入文件未通过安全检查：{exc}；未导入、未备份，原文件未改动"
                ) from exc
            except UnsupportedPackageError as exc:
                raise UnsupportedPackageError(
                    f"本地导入文件是不支持的归档形态：{exc}；未导入工作空间（无选择、尝试记录或备份引用），原文件未改动"
                ) from exc
            self._reject_conflict(state, ident, payload)
            self._declarations(
                ident.versioned_id
            )  # Never silently skip gaps in saved provenance.
            raw = self._publish_raw(payload)
            fact = self._select(state, ident, "local_import")
            record = self._source_record(fact)
            reused = record is not None
            records = []
            if record is None:
                record = self._new_source(
                    ident,
                    raw,
                    f"local import; declared provenance: {provenance}; imported at {_now_iso(self._clock)}",
                )
                records.append(record)
            digest = hashlib.sha256(canonical_json(provenance).encode()).hexdigest()
            declaration_id = (
                f"acq-import-decl-{_safe_segment(ident.versioned_id)}-{digest[:16]}"
            )
            declaration = self._space.store().read_record_current(declaration_id)
            if declaration is None:
                first_at = _now_iso(self._clock)
                dt = datetime.fromisoformat(first_at)
                records.append(
                    build_record(
                        ExternalRecord,
                        record_id=declaration_id,
                        created_at=dt,
                        execution_id=None,
                        data={
                            "service": "arxiv-local-import",
                            "catalog_version": None,
                            "queried_at": dt,
                            "query": {"versioned_id": ident.versioned_id},
                            "raw_response": {"path": raw.path, "sha256": raw.sha256},
                            "entries": [
                                {
                                    "entry_id": "declaration",
                                    "fields": [
                                        {
                                            "name": "provenance",
                                            "raw_value": provenance,
                                            "unit": None,
                                            "response_selector": "import_declaration",
                                        },
                                        {
                                            "name": "first_declared_at",
                                            "raw_value": first_at,
                                            "unit": None,
                                            "response_selector": "import_declaration",
                                        },
                                    ],
                                }
                            ],
                        },
                    )
                )
            fact.update(
                state="imported",
                error=None,
                source_record=record.ref().model_dump(mode="json"),
            )
            self._save(
                state, records
            )  # Commit materials, new declarations, and explicit selection together.
            return ImportResult(
                ident.canonical_id,
                version,
                ident.versioned_id,
                record.ref(),
                raw.path,
                raw.sha256,
                reused,
            )

    def run_discovery(
        self,
        request: ArxivSearchRequest,
        *,
        max_pages: int | None = None,
        round_seq: int = 1,
    ) -> DiscoveryRoundStatus:
        with self._space.lock():
            state = self._load()
            round_id = discovery_round_id(request, round_seq)
            basis = _request_basis(request)
            round_state = state["rounds"].get(round_id)
            if round_state is None:
                round_state = {
                    "round_id": round_id,
                    "request": basis,
                    "state": ROUND_IN_PROGRESS,
                    "reason": None,
                    "pages": [],
                    "next_start": 0,
                }
                state["rounds"][round_id] = round_state
            elif round_state["request"] != basis:
                raise AcquisitionError(f"轮次 {round_id} 请求与既有进度不一致")
            elif round_state["state"] == ROUND_COMPLETE:
                return self._round_status(round_state)
            elif round_state.get("reason") == "anomalies":
                raise AcquisitionError(
                    f"轮次 {round_id} 因分页异常未完；请缩小范围，不能靠重跑标记完整"
                )
            round_state.update(state=ROUND_IN_PROGRESS, reason=None)
            self._save(state)
            pages_fetched = 0
            while True:
                start = round_state["next_start"]
                try:
                    page = self._api.search(
                        ArxivSearchRequest(
                            **{
                                **request.__dict__,
                                "start": start,
                            }
                        )
                    )
                except Exception as exc:
                    round_state.update(
                        state=ROUND_FAILED,
                        reason="service_error",
                        detail=f"{type(exc).__name__}: {exc}",
                    )
                    self._save(state)
                    raise
                document = _page_document(page)
                digest = hashlib.sha256(document).hexdigest()
                ref = self._space.publish_source_file(
                    f"discovery/{round_id}/page-{start:06d}-{digest}.json", document
                )
                dt = datetime.fromtimestamp(float(page.fetched_at), tz=UTC)
                record = build_record(
                    ExternalRecord,
                    record_id=f"arxiv-search-{round_id}-p{start:06d}",
                    created_at=dt,
                    execution_id=None,
                    data={
                        "service": "arxiv-api",
                        "catalog_version": None,
                        "queried_at": dt,
                        "query": {
                            **basis,
                            "round_id": round_id,
                            "start": start,
                            "search_query": page.request.search_query(),
                        },
                        "raw_response": {"path": ref.path, "sha256": ref.sha256},
                        "entries": [
                            {
                                "entry_id": m.canonical_id,
                                "fields": [
                                    {
                                        "name": name,
                                        "raw_value": str(value) or " ",
                                        "unit": None,
                                        "response_selector": selector,
                                    }
                                    for name, value, selector in (
                                        ("arxiv_id", m.canonical_id, "atom:entry/id"),
                                        ("version", m.version, "atom:entry/id"),
                                        (
                                            "published",
                                            m.published,
                                            "atom:entry/published",
                                        ),
                                        ("updated", m.updated, "atom:entry/updated"),
                                        ("title", m.title, "atom:entry/title"),
                                        ("abstract", m.abstract, "atom:entry/summary"),
                                        (
                                            "primary_category",
                                            m.primary_category,
                                            "atom:entry/arxiv:primary_category",
                                        ),
                                    )
                                ],
                            }
                            for m in page.entries
                        ],
                    },
                )
                round_state["pages"].append(record.ref().model_dump(mode="json"))
                round_state["next_start"] = start + len(page.entries)
                pages_fetched += 1
                if page.has_more is False:
                    round_state.update(state=ROUND_COMPLETE, reason=None)
                elif page.has_more is None:
                    round_state.update(state=ROUND_INCOMPLETE, reason="anomalies")
                elif max_pages is not None and pages_fetched >= max_pages:
                    round_state.update(state=ROUND_INCOMPLETE, reason="page_bound")
                self._save(
                    state, [record]
                )  # Commit page records, progress, and actual terminal state once.
                if round_state["state"] != ROUND_IN_PROGRESS:
                    return self._round_status(round_state)

    def _round_status(self, value: dict) -> DiscoveryRoundStatus:
        pages = []
        papers = {}
        for ref in value["pages"]:
            record = self._space.store().read_record(RecordRef(**ref))
            if not isinstance(record, ExternalRecord):
                raise AcquisitionError("发现页记录缺失；不补造进度")
            raw = record.data.raw_response
            document = json.loads(self._space.read_file(raw))
            start = document["request"]["start"]
            pages.append(
                {
                    "start": start,
                    "record_id": ref["record_id"],
                    "revision": ref["revision"],
                    "fetched_at": document["fetched_at"],
                    "entries": len(document["entries"]),
                    "total_results": document["total_results"],
                    "anomalies": document["anomalies"],
                    "raw_path": raw.path,
                    "raw_sha256": raw.sha256,
                    "response_url": document["response_url"],
                }
            )
            for meta in document["entries"]:
                info = papers.setdefault(meta["canonical_id"], {"pages": []})
                info.update(
                    version=meta["version"],
                    updated=meta["updated"],
                    published=meta["published"],
                )
                if start not in info["pages"]:
                    info["pages"].append(start)
        return DiscoveryRoundStatus(
            value["round_id"],
            value["state"],
            dict(value["request"]),
            tuple(pages),
            papers,
            value.get("reason"),
        )

    def check_revisions(
        self, canonical_ids: list[str] | None = None
    ) -> RevisionCheckResult:
        with self._space.lock():
            state = self._load()
            ids = list(state["selections"]) if canonical_ids is None else canonical_ids
            checked_at = _now_iso(self._clock)
            entries = []
            for offset in range(0, len(ids), MAX_ID_LIST_LENGTH):
                batch = ids[offset : offset + MAX_ID_LIST_LENGTH]
                result = self._api.metadata_by_ids(batch)
                found = {
                    x.meta.canonical_id: x.meta
                    for x in result.lookups
                    if x.status == "found" and x.meta is not None
                }
                for key in batch:
                    pointer = state["selections"].get(key)
                    selected = (
                        parse_arxiv_identifier(pointer["versioned_id"]).version
                        if pointer
                        else None
                    )
                    meta = found.get(key)
                    if result.anomalies or meta is None or selected is None:
                        error = (
                            "批次异常：" + ",".join(a.value for a in result.anomalies)
                            if result.anomalies
                            else "无冻结选择或元数据未返回或未找到"
                        )
                        entry = RevisionStatus(
                            key,
                            selected,
                            None,
                            None,
                            checked_at,
                            "unknown",
                            error + "：无法判定是否有新版",
                        )
                    else:
                        entry = RevisionStatus(
                            key,
                            selected,
                            meta.version,
                            meta.updated,
                            checked_at,
                            "pending" if meta.version > selected else "current",
                        )
                    entries.append(entry)
                    state["revisions"][key] = entry.__dict__
                self._save(
                    state
                )  # Completed batches remain queryable; later failures do not erase them.
            return RevisionCheckResult(checked_at, tuple(entries))

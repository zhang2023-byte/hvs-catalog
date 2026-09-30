"""arXiv 获取生命周期：发现、版本冻结、获取、本地导入与状态恢复（Lane B）。

在 Lane A 协议入口（:class:`ArxivApiService`，无状态）之上提供有持久
化的工程生命周期：

- **发现轮次**：显式受限检索的分页推进、每页溯源（ExternalRecord +
  归档页文档）、按论文去重而保留每页出处；只有 Lane A 给出的唯一
  ``has_more is False`` 信号能把轮次标记为完整，异常/截断/重复/上限/
  页数界限都保持"不完整"，可恢复。
- **版本冻结**：未带版本的获取先查元数据选定当时最新版本并在下载
  **之前**持久化冻结；恢复绝不悄悄改选更新版本。显式版本始终被尊
  重（含旧式标识）。已知论文的修订检查独立于新论文的首提日期窗口；
  新版源码不可用时保留待处理记录与旧材料。
- **获取与发布**：固定版本下载 → 有界安全检查（archive 模块）→ 内
  容寻址发布原始字节（同字节复用、不同字节拒绝覆盖）→ 再提交
  SourcePackageRecord（引用在发布之后）。崩溃窗口（发布后未提交、
  回执后未返回）经操作键幂等恢复。包成员清单不落盘解压，持久化在
  提交内容与选择状态中；记录内的文件条目只有未解析的原始包。
- **本地导入**：显式文件 + 声明的标识/版本/来源；不从内容猜身份，
  同论文同版本不同字节拒绝覆盖，重复相同字节复用且不丢失溯源；来
  源记录明确标注是本地声明而非 arXiv 核实。
- **状态/恢复**：全部进度与溯源持久化在 R05 业务库（记录 + 回执 +
  meta），随备份/恢复可见；状态读取与列表不触网、不隐式初始化；
  失败/中断不变成空成功，恢复先核对冻结输入与哈希，不一致即停。

进度状态存放在业务库 ``meta`` 表（``acquisition:*`` 键，JSON 值），
与同批记录在**同一事务**内更新；不建独立数据库，不写仓库外散落
JSON。网络请求一律在空间锁与数据库事务之外进行。
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, ClassVar

from hvs_catalog.records.common import (
    ArtifactRef,
    RecordRef,
    build_record,
    canonical_json,
)
from hvs_catalog.records.objects import ExternalField, ExternalRecord
from hvs_catalog.records.source import (
    ArxivOrigin,
    SourceFile,
    SourcePackageData,
    SourcePackageRecord,
)
from hvs_catalog.workspace import (
    CommitRequest,
    Receipt,
    Workspace,
    WorkspaceConflictError,
    WorkspaceIntegrityError,
)

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

_STATE_VERSION = 1
_META_INDEX = "acquisition:index"
_META_ROUND_PREFIX = "acquisition:round:"
_META_SELECTION_PREFIX = "acquisition:selection:"
_META_REVISION_PREFIX = "acquisition:revision:"

ROUND_IN_PROGRESS = "in_progress"
ROUND_COMPLETE = "complete"
ROUND_INCOMPLETE = "incomplete"
ROUND_FAILED = "failed"


class AcquisitionError(Exception):
    """获取生命周期的工程失败（可操作：读取持久化状态后重试/恢复）。"""


class SourceUnavailableError(AcquisitionError):
    """固定版本的源码包在服务端不可用（404/HTML 错误页/PDF-only）。

    不是获取流程缺陷：待处理记录已持久化，旧材料与发现进度不受影响。
    """


class PackageRejectionError(AcquisitionError):
    """源码包未通过工程安全检查（路径越界、链接/设备、超限、损坏）。"""


@dataclass(frozen=True)
class DiscoveryRoundStatus:
    """一个发现轮次的持久化状态（读取自业务库，不触网）。"""

    round_id: str
    state: str
    request: dict[str, Any]
    pages: tuple[dict[str, Any], ...] = ()
    papers: dict[str, dict[str, Any]] = field(default_factory=dict)
    reason: str | None = None


@dataclass(frozen=True)
class SelectionStatus:
    """一篇论文的版本冻结与获取状态。"""

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
    history: tuple[dict[str, Any], ...] = ()


@dataclass(frozen=True)
class AcquisitionResult:
    """一次获取/恢复的结果。"""

    canonical_id: str
    version: int
    versioned_id: str
    state: str  # downloaded | unsupported | imported（恢复已导入的目标）
    material_ref: RecordRef | None
    raw_path: str | None
    raw_sha256: str | None
    replayed: bool


@dataclass(frozen=True)
class ImportResult:
    """一次本地导入的结果。"""

    canonical_id: str
    version: int
    versioned_id: str
    material_ref: RecordRef
    raw_path: str
    raw_sha256: str
    replayed: bool


@dataclass(frozen=True)
class RevisionStatus:
    """一篇已知论文的修订检查结果。

    ``state`` 为 ``pending``（有新版待处理）、``current``（已核实无
    新版）或 ``unknown``（未能核实：未返回/未找到/标识不符/批次异
    常，不能当成无新版）。版本号为 ``None`` 表示该身份未知，不用臆
    造的 -1 冒充。
    """

    canonical_id: str
    selected_version: int | None
    latest_version: int | None
    latest_updated: str | None
    checked_at: str
    state: str  # pending | current | unknown
    error: str | None = None


@dataclass(frozen=True)
class RevisionCheckResult:
    """一次修订检查的整体结果。"""

    checked_at: str
    entries: tuple[RevisionStatus, ...]


@dataclass(frozen=True)
class SelectedMaterial:
    """交付给 R11 的选定版本材料引用（只有选定版本，不混入其他版本）。"""

    canonical_id: str
    version: int
    versioned_id: str
    material_ref: RecordRef
    raw_path: str
    raw_sha256: str
    origin_retrieval: str


def _now_timestamp(clock: Any) -> float:
    return float(clock.now() if clock is not None else datetime.now(UTC).timestamp())


def _now_dt(clock: Any) -> datetime:
    return datetime.fromtimestamp(_now_timestamp(clock), tz=UTC)


def _now_iso(clock: Any) -> str:
    return _now_dt(clock).isoformat()


def _safe_segment(text: str) -> str:
    """把标识符转换为文件路径安全段（旧式标识含 ``/``）。"""
    return text.replace("/", "_")


def _dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _inventory_payload(inspection: Any) -> list[dict[str, Any]]:
    return [
        {
            "name": member.name,
            "size": member.size,
            "sha256": member.sha256,
            "media_type": member.media_type,
        }
        for member in inspection.members
    ]


def _page_document(page: Any) -> bytes:
    """把一页检索结果序列化为确定性的归档页文档（JSON 字节）。

    Lane A 的公共接口返回解析后的页对象而不暴露原始线路字节；本文
    档是该页的**解析视图**（``parsed_view=true``，非原始 Atom 线路响
    应），保存页对象的全部返回元数据（含摘要）、总数、异常信号与
    响应 URL，作为持久化的页级溯源材料，随备份/恢复可回读。
    """
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
        "anomalies": [anomaly.value for anomaly in page.anomalies],
        "entries": [
            {
                "canonical_id": meta.canonical_id,
                "version": meta.version,
                "api_id_url": meta.api_id_url,
                "published": meta.published,
                "updated": meta.updated,
                "title": meta.title,
                "authors": list(meta.authors),
                "categories": list(meta.categories),
                "primary_category": meta.primary_category,
                "abstract": meta.abstract,
                "doi": meta.doi,
                "journal_ref": meta.journal_ref,
            }
            for meta in page.entries
        ],
    }
    return (
        json.dumps(document, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    ).encode("utf-8")


def _entry_fields(meta: Any) -> list[ExternalField]:
    def field(name: str, raw_value: Any, selector: str) -> ExternalField:
        text = str(raw_value)
        return ExternalField(
            name=name,
            raw_value=text if text else " ",
            unit=None,
            response_selector=f"atom:entry/{selector}",
        )

    return [
        field("arxiv_id", meta.canonical_id, "id"),
        field("version", meta.version, "id"),
        field("published", meta.published, "published"),
        field("updated", meta.updated, "updated"),
        field("title", meta.title, "title"),
        field("abstract", meta.abstract, "summary"),
        field("primary_category", meta.primary_category, "arxiv:primary_category"),
    ]


def discovery_round_id(request: ArxivSearchRequest, round_seq: int = 1) -> str:
    """轮次身份：规范化检索请求 + 显式轮次序号的稳定摘要。

    同一检索请求可用不同 ``round_seq`` 在不同时间重复开新轮次，各自
    带全套页级溯源；同一 ``(request, round_seq)`` 身份幂等（重复执行
    只做恢复/状态返回），同身份下请求条件变化则拒绝。
    """
    basis = {
        "submitted_from": request.submitted_from.isoformat(),
        "submitted_to": request.submitted_to.isoformat(),
        "terms": request.terms,
        "categories": list(request.categories),
        "sort_by": request.sort_by,
        "sort_order": request.sort_order,
        "page_size": request.page_size,
        "round_seq": int(round_seq),
    }
    digest = hashlib.sha256(canonical_json(basis).encode("utf-8")).hexdigest()
    return f"d{digest[:16]}"


class AcquisitionService:
    """某个工作空间上的 arXiv 获取生命周期服务。

    参数：
        space: 已初始化的 R05 工作空间（写操作在空间锁内进行）。
        api: Lane A 协议入口；缺省构造生产默认服务（构造不触网）。
        clock: 提供时间戳的时钟，测试可注入假时钟。

    所有进度/溯源状态持久化在业务库（记录 + 回执 + meta）；状态读取
    与列表操作不触网、不隐式初始化。写操作从开始到结束持有空间锁；
    网络调用在数据库事务之外，但不释放空间锁。
    """

    def __init__(
        self,
        space: Workspace,
        api: ArxivApiService | None = None,
        *,
        clock: Any = None,
    ) -> None:
        self._space = space
        self._api = api if api is not None else ArxivApiService()
        self._clock = clock

    # ---------------------------------------------------------------- 状态读取

    def list_discovery_rounds(self) -> list[DiscoveryRoundStatus]:
        index = self._load_json(_META_INDEX) or {}
        statuses = []
        for round_id in index.get("rounds", []):
            state = self._load_json(_META_ROUND_PREFIX + round_id)
            if state is not None:
                statuses.append(self._round_status(state))
        return statuses

    def get_discovery_round(self, round_id: str) -> DiscoveryRoundStatus | None:
        state = self._load_json(_META_ROUND_PREFIX + round_id)
        return None if state is None else self._round_status(state)

    def list_selections(self) -> list[SelectionStatus]:
        index = self._load_json(_META_INDEX) or {}
        statuses = []
        for canonical_id in index.get("selections", []):
            state = self._load_json(_META_SELECTION_PREFIX + canonical_id)
            if state is not None:
                statuses.append(self._selection_status(state))
        return statuses

    def get_selection(self, canonical_id: str) -> SelectionStatus | None:
        state = self._load_json(_META_SELECTION_PREFIX + canonical_id)
        return None if state is None else self._selection_status(state)

    def get_revision_status(self, canonical_id: str) -> RevisionStatus | None:
        state = self._load_json(_META_REVISION_PREFIX + canonical_id)
        return None if state is None else self._revision_status(state)

    def selected_material(self, canonical_id: str) -> SelectedMaterial | None:
        """返回该论文当前选定版本的材料引用（仅选定版本，供 R11 使用）。

        交付前核对精确材料记录与原始包哈希；缺失或不一致即报错停止，
        不静默重选、覆盖或修复持久化状态。
        """
        selection = self.get_selection(canonical_id)
        if selection is None or selection.state not in ("downloaded", "imported"):
            return None
        if selection.source_record is None:
            raise AcquisitionError(
                f"{selection.versioned_id} 状态为已完成但缺少材料记录引用；"
                "请检查持久化状态"
            )
        if self._space.store().read_record(selection.source_record) is None:
            raise AcquisitionError(
                f"材料记录 {selection.source_record.record_id} "
                f"v{selection.source_record.revision} 缺失；已完成状态与记录不一致"
            )
        assert selection.raw_path and selection.raw_sha256
        self._space.read_file(
            ArtifactRef(path=selection.raw_path, sha256=selection.raw_sha256)
        )
        return SelectedMaterial(
            canonical_id=selection.canonical_id,
            version=selection.version,
            versioned_id=selection.versioned_id,
            material_ref=selection.source_record,
            raw_path=selection.raw_path,
            raw_sha256=selection.raw_sha256,
            origin_retrieval=selection.retrieval or "",
        )

    # ---------------------------------------------------------------- 发现

    def run_discovery(
        self,
        request: ArxivSearchRequest,
        *,
        max_pages: int | None = None,
        round_seq: int = 1,
    ) -> DiscoveryRoundStatus:
        """执行（或恢复）一个显式受限的发现轮次。

        幂等：同一 ``(request, round_seq)`` 已完整时直接返回状态，不触
        网；同一检索请求要用新溯源再跑一次，用更大的 ``round_seq`` 开
        新轮次（旧轮次与全部溯源保留）。进行中/因页数界限未完的轮次
        从持久化进度继续；因分页异常未完的轮次重复执行会抛出可操作
        错误（先处理异常，不能靠重跑抹掉）。
        """
        with self._space.lock():
            return self._run_discovery_locked(request, max_pages, round_seq)

    def _run_discovery_locked(
        self,
        request: ArxivSearchRequest,
        max_pages: int | None,
        round_seq: int,
    ) -> DiscoveryRoundStatus:
        """发现轮次主体（调用方已持有空间排他锁）。"""
        round_id = discovery_round_id(request, round_seq)
        current_request = {
            "submitted_from": request.submitted_from.isoformat(),
            "submitted_to": request.submitted_to.isoformat(),
            "terms": request.terms,
            "categories": list(request.categories),
            "sort_by": request.sort_by,
            "sort_order": request.sort_order,
            "page_size": request.page_size,
        }
        state = self._load_json(_META_ROUND_PREFIX + round_id)
        if state is None:
            state = {
                "version": _STATE_VERSION,
                "round_id": round_id,
                "round_seq": int(round_seq),
                "request": current_request,
                "state": ROUND_IN_PROGRESS,
                "reason": None,
                "pages": [],
                "papers": {},
            }
        else:
            if dict(state["request"]) != current_request:
                raise AcquisitionError(
                    f"轮次 {round_id} 的请求与既有进度不一致；同一轮次身份"
                    "必须携带相同检索条件"
                )
            if state["state"] == ROUND_COMPLETE:
                return self._round_status(state)
            if state["state"] == ROUND_INCOMPLETE and state.get("reason") == (
                "anomalies"
            ):
                raise AcquisitionError(
                    f"轮次 {round_id} 因分页异常未完（{state.get('detail')}）；"
                    "请先处理异常（缩小日期范围/调整分页），不能靠重跑标记完整"
                )
            if state["state"] == ROUND_FAILED:
                raise AcquisitionError(
                    f"轮次 {round_id} 之前失败：{state.get('detail')}"
                )
            state["state"] = ROUND_IN_PROGRESS
            state["reason"] = None

        try:
            pages_fetched = 0
            while True:
                start = self._next_start(state)
                page = self._api.search(
                    ArxivSearchRequest(
                        submitted_from=request.submitted_from,
                        submitted_to=request.submitted_to,
                        terms=request.terms,
                        categories=request.categories,
                        sort_by=request.sort_by,
                        sort_order=request.sort_order,
                        start=start,
                        page_size=request.page_size,
                    )
                )
                pages_fetched += 1
                state = self._commit_page(round_id, request, start, page, state)
                has_more = page.has_more
                if has_more is False:
                    state["state"] = ROUND_COMPLETE
                    state["reason"] = None
                    state = self._save_round_state(round_id, state)
                    break
                if has_more is None:
                    state["state"] = ROUND_INCOMPLETE
                    state["reason"] = "anomalies"
                    state["detail"] = ",".join(
                        anomaly.value for anomaly in page.anomalies
                    )
                    state = self._save_round_state(round_id, state)
                    break
                if max_pages is not None and pages_fetched >= max_pages:
                    state["state"] = ROUND_INCOMPLETE
                    state["reason"] = "page_bound"
                    state = self._save_round_state(round_id, state)
                    break
        except AcquisitionError:
            raise
        except Exception as exc:  # 服务/传输失败：持久化失败态后原样上抛
            state["state"] = ROUND_FAILED
            state["reason"] = "service_error"
            state["detail"] = f"{type(exc).__name__}: {exc}"
            try:
                state = self._save_round_state(round_id, state)
            except Exception:  # noqa: BLE001, S110 - 状态写入失败不掩盖原始错误
                pass
            raise
        return self._round_status(state)

    # ---------------------------------------------------------------- 获取

    def acquire(self, identifier: ArxivIdentifier | str) -> AcquisitionResult:
        """按标识获取（或恢复）固定版本的源码包材料。

        未带版本且尚无冻结时：先查元数据选定当时最新版本并在下载前持
        久化冻结（``via=latest_at_selection``）；已有冻结则恢复使用冻
        结版本，绝不悄悄改选更新版本。显式版本（含旧式标识）始终按
        声明选定。已完成的目标只做哈希核对（不触网）。源码不可用时
        持久化待处理状态并抛 :class:`SourceUnavailableError`。
        """
        ident = (
            parse_arxiv_identifier(identifier)
            if isinstance(identifier, str)
            else identifier
        )
        with self._space.lock():
            return self._acquire_locked(ident)

    def _acquire_locked(self, ident: ArxivIdentifier) -> AcquisitionResult:
        """获取主体（调用方已持有空间排他锁）。"""
        selection = self._load_selection(ident.canonical_id)
        if (
            selection is not None
            and selection["state"] in ("downloaded", "imported")
            and (ident.version is None or ident.version == selection["version"])
        ):
            return self._verify_material(selection)
        if selection is None or ident.version is not None:
            selection = self._freeze_selection(ident)
        version = selection["version"]
        versioned = selection["versioned_id"]

        # 回执后、状态更新前崩溃的恢复：确定性记录号已可回查时，直接
        # 采用已提交材料（哈希核对 + 本地重算清单），不重新下载。
        adopted = self._adopt_committed(ident, version, versioned, selection)
        if adopted is not None:
            return adopted

        try:
            download = self._api.download_source(versioned)
        except ArxivSourceUnavailableError as exc:
            self._persist_selection_state(
                selection, "source_unavailable", error=str(exc)
            )
            raise SourceUnavailableError(
                f"{versioned} 的源码包不可用：{exc}；已保留待处理记录，"
                "旧材料与发现进度不受影响"
            ) from exc
        except Exception as exc:
            self._persist_selection_state(selection, "failed", error=str(exc))
            raise

        self._reject_conflicting_material(ident.canonical_id, version, download.payload)
        raw_ref = self._publish_package_raw(
            ident.canonical_id, versioned, download.payload
        )
        try:
            inspection = inspect_package(download.payload)
        except UnsupportedPackageError as exc:
            # 形态不支持：原始字节已保留，不产生可用材料记录；补一条
            # 尝试记录使保留字节随备份/恢复可遍历（不只挂在 meta）。
            self._commit_attempt_record(
                ident=ident,
                version=version,
                versioned=versioned,
                raw_ref=raw_ref,
                reason="unsupported_form",
                error=str(exc),
                fetched_at=download.fetched_at,
            )
            self._persist_selection_state(
                selection,
                "unsupported",
                error=str(exc),
                raw_path=raw_ref.path,
                raw_sha256=raw_ref.sha256,
                retrieval=(
                    f"downloaded from {download.final_url} at "
                    f"{download.fetched_at}; unsupported form: {exc}"
                ),
            )
            return AcquisitionResult(
                canonical_id=ident.canonical_id,
                version=version,
                versioned_id=versioned,
                state="unsupported",
                material_ref=None,
                raw_path=raw_ref.path,
                raw_sha256=raw_ref.sha256,
                replayed=False,
            )
        except PackageIntegrityError as exc:
            self._commit_attempt_record(
                ident=ident,
                version=version,
                versioned=versioned,
                raw_ref=raw_ref,
                reason="integrity_rejected",
                error=str(exc),
                fetched_at=download.fetched_at,
            )
            self._persist_selection_state(
                selection,
                "failed",
                error=str(exc),
                raw_path=raw_ref.path,
                raw_sha256=raw_ref.sha256,
            )
            raise PackageRejectionError(
                f"{versioned} 的源码包未通过安全检查：{exc}"
            ) from exc

        record, receipt, reused = self._commit_source_package(
            ident=ident,
            version=version,
            versioned=versioned,
            raw_ref=raw_ref,
            inspection=inspection,
            retrieval=(
                f"downloaded from {download.final_url} at {download.fetched_at}"
            ),
            operation_prefix="acquire",
        )
        self._persist_selection_state(
            selection,
            "downloaded",
            record=record,
            raw_path=raw_ref.path,
            raw_sha256=raw_ref.sha256,
            inventory=_inventory_payload(inspection),
        )
        return AcquisitionResult(
            canonical_id=ident.canonical_id,
            version=version,
            versioned_id=versioned,
            state="downloaded",
            material_ref=record.ref(),
            raw_path=raw_ref.path,
            raw_sha256=raw_ref.sha256,
            replayed=receipt.replayed or reused,
        )

    # ---------------------------------------------------------------- 修订检查

    def check_revisions(
        self, canonical_ids: list[str] | None = None
    ) -> RevisionCheckResult:
        """检查已收录论文是否有新版（独立于新论文的首提日期窗口）。

        只查询元数据并持久化检查结果：发现新版时记录待处理状态，不
        改动既有冻结选择与材料；获取新版由调用方以显式版本发起
        :meth:`acquire`。未能核实（未返回/未找到/标识不符/批次异常）
        的论文标 ``unknown`` 并保留已知事实，不冒充“无新版”。
        """
        # 操作身份：跨实例/进程不冲突（uuid4），同一操作内的提交复用同
        # 一令牌；每次公开调用即有意开始一次新检查。
        token = uuid.uuid4().hex
        with self._space.lock():
            # 索引读取与空列表提前返回都在空间锁内（四入口整操作串行
            # 规则）：其他执行者持锁时，空查询同样返回忙，而不是绕过
            # 锁给出"成功"的空结果。
            if canonical_ids is None:
                index = self._load_json(_META_INDEX) or {}
                canonical_ids = list(index.get("selections", []))
            if not canonical_ids:
                return RevisionCheckResult(checked_at=_now_iso(self._clock), entries=())
            return self._check_revisions_locked(canonical_ids, token)

    def _check_revisions_locked(
        self, canonical_ids: list[str], token: str
    ) -> RevisionCheckResult:
        """修订检查主体（调用方已持有空间排他锁）。"""
        checked_at = _now_iso(self._clock)
        entries: list[RevisionStatus] = []
        for offset in range(0, len(canonical_ids), MAX_ID_LIST_LENGTH):
            batch = canonical_ids[offset : offset + MAX_ID_LIST_LENGTH]
            result = self._api.metadata_by_ids(batch)
            found = {
                lookup.meta.canonical_id: lookup.meta
                for lookup in result.lookups
                if lookup.status == "found" and lookup.meta is not None
            }
            batch_anomalies = [
                anomaly.value for anomaly in getattr(result, "anomalies", ())
            ]
            if batch_anomalies:
                # 保守处理：影响批次可信性的异常（重复/截断等）意味着响
                # 应整体不可完全信任——即使某些条目 found，也不能证明
                # “无新版”；整批标 unknown 并保留诊断与已知选择版本。
                reason = "批次异常：" + ",".join(batch_anomalies)
                for canonical_id in batch:
                    selection = self._load_selection(canonical_id)
                    entries.append(
                        RevisionStatus(
                            canonical_id=canonical_id,
                            selected_version=(
                                int(selection["version"]) if selection else None
                            ),
                            latest_version=None,
                            latest_updated=None,
                            checked_at=checked_at,
                            state="unknown",
                            error=reason + "：响应可信性不足，无法判定是否有新版",
                        )
                    )
                continue
            for canonical_id in batch:
                selection = self._load_selection(canonical_id)
                meta = found.get(canonical_id)
                if selection is None or meta is None:
                    # 未返回/未找到/无冻结选择：不能证明无新版，标
                    # unknown 并保留已知事实（有冻结则版本号真实）。
                    reasons = []
                    if selection is None:
                        reasons.append("无冻结选择")
                    else:
                        reasons.append("元数据未返回或未找到")
                    if batch_anomalies:
                        reasons.append("批次异常：" + ",".join(batch_anomalies))
                    entries.append(
                        RevisionStatus(
                            canonical_id=canonical_id,
                            selected_version=(
                                int(selection["version"]) if selection else None
                            ),
                            latest_version=None,
                            latest_updated=None,
                            checked_at=checked_at,
                            state="unknown",
                            error="；".join(reasons) + "：无法判定是否有新版",
                        )
                    )
                    continue
                state = {
                    "version": _STATE_VERSION,
                    "canonical_id": canonical_id,
                    "selected_version": selection["version"],
                    "latest_version": meta.version,
                    "latest_updated": meta.updated,
                    "checked_at": checked_at,
                    "state": (
                        "pending" if meta.version > selection["version"] else "current"
                    ),
                    "error": None,
                }
                entries.append(self._revision_status(state))
        # 单一原子事务：回执（跨实例唯一令牌）+ 全部条目的状态 meta。
        # 秒级 checked_at 只是展示性元数据；跨进程重启的新公开调用拿
        # 新令牌，同秒也能各自持久化并收敛到最新一次的诚实结果。
        self._space.store().commit(
            CommitRequest(
                operation_key=f"acq:revision-check:{token}",
                kind="acquisition_revision_check",
                content={
                    "kind": "acquisition_revision_check",
                    "token": token,
                    "checked": [
                        {
                            "canonical_id": entry.canonical_id,
                            "latest_version": entry.latest_version,
                        }
                        for entry in entries
                    ],
                },
                meta_updates={
                    _META_REVISION_PREFIX + entry.canonical_id: _dump(
                        {
                            "version": _STATE_VERSION,
                            "canonical_id": entry.canonical_id,
                            "selected_version": entry.selected_version,
                            "latest_version": entry.latest_version,
                            "latest_updated": entry.latest_updated,
                            "checked_at": entry.checked_at,
                            "state": entry.state,
                            "error": entry.error,
                        }
                    )
                    for entry in entries
                },
            )
        )
        return RevisionCheckResult(checked_at=checked_at, entries=tuple(entries))

    # ---------------------------------------------------------------- 本地导入

    def import_local_package(
        self,
        path: str | Path,
        *,
        arxiv_id: str,
        version: int | None = None,
        provenance: str = "",
    ) -> ImportResult:
        """导入本地已下载的同类源码包；身份与版本必须显式声明。

        不从归档内容猜测论文身份；声明的标识经 Lane A 严格解析（含旧
        式），版本必须是正整数（拒绝 bool、零、负数与非整数），最终
        组装的版本化标识再过一次严格解析；来源声明必须非空非纯空白
        ——本地声明不是服务核实，但必须有可审计的来源依据。同论文
        同版本不同字节拒绝覆盖；相同字节重复导入幂等复用且不丢失溯
        源。所有校验都在发布字节或改动选择之前完成。
        """
        if provenance is None or not provenance.strip():
            raise AcquisitionError(
                "本地导入必须提供非空、非纯空白的来源声明（provenance）；"
                "本地声明不是服务核实，但需要可审计的来源依据"
            )
        if version is not None and (
            isinstance(version, bool) or not isinstance(version, int) or version <= 0
        ):
            raise AcquisitionError(
                f"声明的版本必须是正整数（v1 起），拒绝：{version!r}"
            )
        ident = parse_arxiv_identifier(arxiv_id)
        if version is None:
            if ident.version is None:
                raise AcquisitionError(
                    "本地导入必须声明具体版本（arxiv_id 带版本号或显式 version 参数）"
                )
            version = ident.version
        if ident.version is not None and ident.version != version:
            raise AcquisitionError(
                f"声明的版本互相矛盾：arxiv_id={ident.versioned_id}，version={version}"
            )
        versioned = f"{ident.canonical_id}v{version}"
        recheck = parse_arxiv_identifier(versioned)
        if recheck.canonical_id != ident.canonical_id or recheck.version != version:
            raise AcquisitionError(
                f"组装的版本化标识 {versioned} 未通过严格解析，拒绝导入"
            )
        with self._space.lock():
            # 整操作持锁（R05 空间串行）：前置状态检查、文件读取、发
            # 布、回执与选择全部在同一把锁内完成，同空间其他写操作返
            # 回忙。旧工程声明状态（回执无事件记录）在任何改动前失败
            # 关闭（不迁移、不伪造时间）。
            self._check_declaration_state_compat(ident.canonical_id, version)
            file_path = Path(path)
            if not file_path.is_file():
                raise AcquisitionError(f"导入文件不存在或不是常规文件：{file_path}")
            size = file_path.stat().st_size
            if size > MAX_PACKAGE_BYTES:
                raise PackageRejectionError(
                    f"导入文件 {size} 字节超过工程上限 {MAX_PACKAGE_BYTES}"
                )
            payload = file_path.read_bytes()
            try:
                inspection = inspect_package(payload)
            except PackageIntegrityError as exc:
                raise PackageRejectionError(f"导入文件未通过安全检查：{exc}") from exc
            except UnsupportedPackageError as exc:
                # 本地导入的拒绝语义与远程获取不同：文件是调用方的，
                # **未导入工作空间**（无选择/尝试记录/附件引用，不在工
                # 作空间备份范围内），原文件未改动；远程获取路径收到
                # 的字节才由获取流程在限额内保留并随备份恢复。
                raise UnsupportedPackageError(
                    f"本地导入文件是不支持的归档形态：{exc}；"
                    "未导入工作空间（无选择、尝试记录或备份引用），"
                    "原文件未改动"
                ) from exc
            self._reject_conflicting_material(ident.canonical_id, version, payload)
            raw_ref = self._publish_package_raw(ident.canonical_id, versioned, payload)
            record, _material_receipt, reused = self._commit_source_package(
                ident=ident,
                version=version,
                versioned=versioned,
                raw_ref=raw_ref,
                inspection=inspection,
                retrieval=(
                    f"local import; declared provenance: {provenance or '(未说明)'};"
                    f" imported at {_now_iso(self._clock)}"
                ),
                operation_prefix="import",
            )
            declared = provenance.strip() and provenance or "(未说明)"
            prov_digest = hashlib.sha256(
                canonical_json(declared).encode("utf-8")
            ).hexdigest()
            # 首次声明事件：首次时间随确定性记录持久化；重复出现（跨版
            # 本重选、崩溃重试）复用原始时间，不改写、不重算。
            first_at, declared_now = self._commit_declaration_event(
                ident, version, raw_ref, declared, prov_digest
            )
            # 目标日志**总是**从该版本全部已持久化声明事件重建（权威来
            # 源是事件，不是选择视图里的陈旧日志）：覆盖首次导入、同目
            # 标与跨版本重选——被中断提交的选择回执不再造成事件漏记。
            prior_log, _ = self._declarations_for_target(ident.canonical_id, version)
            seen = {
                hashlib.sha256(
                    canonical_json(item.get("provenance", "")).encode("utf-8")
                ).hexdigest()
                for item in prior_log
            }
            if prov_digest not in seen:
                prior_log.append({"provenance": declared, "at": first_at})
            existing = self._load_selection(ident.canonical_id)
            same_target = existing is not None and int(existing["version"]) == version
            history: list[dict[str, Any]] = []
            generation = 1
            if same_target:
                generation = int(existing.get("generation", 0))
                history = list(existing.get("history", []))
            elif existing is not None:
                # 跨版本导入是一次**有意的目标切换**（与声明是否重放无
                # 关）：新世代、旧选择入历史、新目标自己的溯源日志；
                # 绝不把新字节并进旧版本身份，也不静默保留旧选择。
                generation = int(existing.get("generation", 0)) + 1
                history = list(existing.get("history", []))
                history.append(
                    {
                        "version": existing["version"],
                        "state": existing["state"],
                        "via": existing.get("via"),
                        "at": existing.get("selected_at"),
                    }
                )
            selection = {
                "state_version": _STATE_VERSION,
                "canonical_id": ident.canonical_id,
                "version": version,
                "versioned_id": versioned,
                "generation": generation,
                "selected_at": _now_iso(self._clock),
                "via": "local_import",
                "state": "imported",
                "error": None,
                "source_record_id": record.record_id,
                "source_record_revision": record.revision,
                "raw_path": raw_ref.path,
                "raw_sha256": raw_ref.sha256,
                # 选择状态的获取依据取自**材料记录的不可变 retrieval**
                # （首次获取/导入声明）：选择结果不区分由哪条已登记声
                # 明发起——调用方声明身份只活在声明事件与溯源日志里，
                # 不渗入选择状态（否则同状态不同调用方的回执重放会留
                # 下与公开结果不一致的状态视图）。
                "retrieval": record.data.origin.retrieval,
                "package_inventory": _inventory_payload(inspection),
                "provenance_log": prior_log,
                "history": history,
            }
            # 选择动作回执：与声明事件分离、独立原子提交（回执 + 选择
            # meta + 索引同事务）。
            #
            # **操作身份 = 承诺的提交结果**：(论文, 版本, 字节, 世代,
            # 完整日志摘要)。回执内容与键严格一一对应（内容只是键各
            # 分量的明细，不含任何随调用方变化的字段——调用声明身份
            # 由声明事件回执承载）。因此：
            # - 同一选择状态的任何重试/换声明重入（A→B→A→B）都是同键
            #   同内容的幂等重放，不产生键冲突；
            # - 日志或世代变化（新声明、恢复出事件、跨版本切换）得到
            #   新键新回执，其 meta 原子携带完整日志，不静默丢弃事件。
            # 选择状态的变化永远由这类可审计回执承载，不做回执重放后
            # 的直接 meta 补写；重放未应用 meta 时采纳存储事实（同一
            # 键意味着同一状态，selected_at 即该状态首次提交时间）。
            log_digest = hashlib.sha256(
                canonical_json(prior_log).encode("utf-8")
            ).hexdigest()
            sel_key = (
                f"acq:import-selection:{ident.canonical_id}:"
                f"v{version}:{raw_ref.sha256}:g{generation}:{log_digest}"
            )
            # 存储对无新记录的回执统一标 replayed（无待插入内容）；导
            # 入的“重放”按**提交前键是否已存在**如实判定。
            store = self._space.store()
            sel_existed = store.find_receipt(sel_key) is not None
            index = self._load_json(_META_INDEX) or {
                "rounds": [],
                "selections": [],
            }
            if ident.canonical_id not in index["selections"]:
                index["selections"].append(ident.canonical_id)
            store.commit(
                CommitRequest(
                    operation_key=sel_key,
                    kind="acquisition_import_selection",
                    content={
                        "kind": "acquisition_import_selection",
                        "canonical_id": ident.canonical_id,
                        "version": version,
                        "generation": generation,
                        "state": "imported",
                        "raw": {"path": raw_ref.path, "sha256": raw_ref.sha256},
                        "record_id": record.record_id,
                        "record_revision": record.revision,
                        "log_digest": log_digest,
                    },
                    meta_updates={
                        _META_SELECTION_PREFIX + ident.canonical_id: _dump(selection),
                        _META_INDEX: _dump(index),
                    },
                )
            )
            persisted = self._load_selection(ident.canonical_id)
            if persisted is not None and persisted != selection:
                # 回执重放未应用 meta：唯一可能是既有事实不劣于本次
                # 快照。采纳存储事实，不做任何回执外的直接修复写。
                selection = persisted
            return ImportResult(
                canonical_id=ident.canonical_id,
                version=version,
                versioned_id=versioned,
                material_ref=record.ref(),
                raw_path=raw_ref.path,
                raw_sha256=raw_ref.sha256,
                replayed=reused or not declared_now or sel_existed,
            )

    # ---------------------------------------------------------------- 内部：持久化

    def _load_json(self, key: str) -> Any:
        raw = self._space.store().get_meta(key)
        return None if raw is None else json.loads(raw)

    def _save_json(self, key: str, value: Any) -> None:
        self._space.store().set_meta(key, _dump(value))

    def _round_status(self, state: dict) -> DiscoveryRoundStatus:
        return DiscoveryRoundStatus(
            round_id=state["round_id"],
            state=state["state"],
            request=dict(state["request"]),
            pages=tuple(state.get("pages", [])),
            papers=dict(state.get("papers", {})),
            reason=state.get("reason"),
        )

    def _record_origin_retrieval(self, state: dict) -> str | None:
        """从持久化材料记录读取真实 ``origin.retrieval``（只读投影）。

        公开状态/材料投影的来源字段以**材料记录**为权威：远程获取的
        URL 与时间只存在于记录的不可变 origin 中；选择 meta 可能缺失
        （历史路径未写入）。记录不可读或字段为空时返回 None（调用方
        回退到选择自身携带的值），绝不臆造来源文本；本方法不做任何
        写入——读路径无业务副作用。
        """
        record_id = state.get("source_record_id")
        if not record_id:
            return None
        revision = int(state.get("source_record_revision") or 1)
        record = self._space.store().read_record(
            RecordRef(record_id=record_id, revision=revision)
        )
        if record is None:
            return None
        origin = getattr(record.data, "origin", None)
        retrieval = getattr(origin, "retrieval", None)
        return retrieval if retrieval else None

    def _selection_status(self, state: dict) -> SelectionStatus:
        record = None
        if state.get("source_record_id"):
            record = RecordRef(
                record_id=state["source_record_id"],
                revision=int(state.get("source_record_revision", 1)),
            )
        return SelectionStatus(
            canonical_id=state["canonical_id"],
            version=int(state["version"]),
            versioned_id=state["versioned_id"],
            state=state["state"],
            via=state.get("via", ""),
            selected_at=state.get("selected_at", ""),
            source_record=record,
            raw_path=state.get("raw_path"),
            raw_sha256=state.get("raw_sha256"),
            # 来源以材料记录为权威（只读投影）；记录不可读时回退到
            # 选择自身携带的值（导入语义不变），不臆造。
            retrieval=(self._record_origin_retrieval(state) or state.get("retrieval")),
            package_inventory=tuple(state.get("package_inventory", [])),
            provenance_log=tuple(state.get("provenance_log", [])),
            error=state.get("error"),
            history=tuple(state.get("history", [])),
        )

    def _revision_status(self, state: dict) -> RevisionStatus:
        selected = state.get("selected_version")
        latest = state.get("latest_version")
        return RevisionStatus(
            canonical_id=state["canonical_id"],
            selected_version=None if selected is None else int(selected),
            latest_version=None if latest is None else int(latest),
            latest_updated=state.get("latest_updated"),
            checked_at=state["checked_at"],
            state=state["state"],
            error=state.get("error"),
        )

    def _load_selection(self, canonical_id: str) -> dict | None:
        return self._load_json(_META_SELECTION_PREFIX + canonical_id)

    def _index_with(self, *, round_id: str | None, canonical_id: str | None) -> dict:
        index = self._load_json(_META_INDEX) or {"rounds": [], "selections": []}
        changed = False
        if round_id is not None and round_id not in index["rounds"]:
            index["rounds"].append(round_id)
            changed = True
        if canonical_id is not None and canonical_id not in index["selections"]:
            index["selections"].append(canonical_id)
            changed = True
        return index if changed else {**index, "_unchanged": True}

    def _upsert_selection(self, selection: dict) -> None:
        """写选择 meta 与索引；历史由版本切换点（冻结/导入）维护。

        同一选择内的状态推进（selected → downloaded 等）不产生新的历
        史条目；历史记录的是**既往版本选择**，在切换时一次性追加。
        """
        self._save_json(_META_SELECTION_PREFIX + selection["canonical_id"], selection)
        index = self._load_json(_META_INDEX) or {"rounds": [], "selections": []}
        if selection["canonical_id"] not in index["selections"]:
            index["selections"].append(selection["canonical_id"])
            self._save_json(_META_INDEX, index)

    def _freeze_selection(self, ident: ArxivIdentifier) -> dict:
        """选定并持久化冻结版本（下载之前）；单一原子事务。

        调用方（公开获取入口）已持有空间排他锁，冻结、下载与回写全程
        串行，不存在需要 CAS 拒绝的并发交错。无版本选定的元数据查询
        遵循**信任边界**：批次带有任何影响可信度的异常（重复条目、截
        断等）时失败关闭——给出可观察诊断，不写成功冻结回执、不下载、
        不覆盖既有选择；显式版本路径不触发新的元数据查询。

        冻结回执与选择 meta 在**同一事务**内提交；操作键含意图
        （via）与代际：同一请求重试幂等（同键同内容），显式重选产生
        新键（latest v1 → explicit v2 → explicit v1 各自独立），同键
        不同内容由存储按操作键冲突如实上抛。
        """
        if ident.version is not None:
            version, via = ident.version, "explicit"
        else:
            batch = self._api.metadata_by_ids([ident.canonical_id])
            batch_anomalies = [
                anomaly.value for anomaly in getattr(batch, "anomalies", ())
            ]
            if batch_anomalies:
                # 失败关闭：无版本选定依赖该响应推断“最新”；批次可信
                # 度存疑时不能冻结（与修订检查的保守边界一致）。
                raise AcquisitionError(
                    f"{ident.canonical_id} 的元数据批次存在影响可信度的异常："
                    + ",".join(batch_anomalies)
                    + "；不能据此选定最新版，未创建任何冻结或下载"
                )
            lookup = next(
                (
                    item
                    for item in batch.lookups
                    if item.requested.canonical_id == ident.canonical_id
                ),
                None,
            )
            if lookup is None or lookup.status != "found" or lookup.meta is None:
                status = lookup.status if lookup is not None else "missing"
                raise AcquisitionError(
                    f"元数据查询未返回 {ident.canonical_id}（status={status}）；"
                    "不能猜测版本，未创建任何冻结"
                )
            version, via = lookup.meta.version, "latest_at_selection"
        versioned = f"{ident.canonical_id}v{version}"
        frozen_at = _now_iso(self._clock)
        current = self._load_selection(ident.canonical_id)
        generation = 1
        history: list[dict[str, Any]] = []
        if current is not None:
            history = list(current.get("history", []))
            if int(current["version"]) == version:
                generation = int(current.get("generation", 0))  # 同版本幂等
                if current["state"] in ("downloaded", "imported"):
                    # 同版本已完成：冻结是幂等重入，不覆盖完成状态。
                    return current
            else:
                generation = int(current.get("generation", 0)) + 1  # 显式换版
                history.append(
                    {
                        "version": current["version"],
                        "state": current["state"],
                        "via": current.get("via"),
                        "at": current.get("selected_at"),
                    }
                )
        selection = {
            "state_version": _STATE_VERSION,
            "canonical_id": ident.canonical_id,
            "version": version,
            "versioned_id": versioned,
            "generation": generation,
            "selected_at": frozen_at,
            "via": via,
            "state": "selected",
            "error": None,
            "source_record_id": None,
            "source_record_revision": None,
            "raw_path": None,
            "raw_sha256": None,
            "retrieval": None,
            "package_inventory": [],
            "history": history,
        }
        content = {
            "kind": "acquisition_selection_freeze",
            "canonical_id": ident.canonical_id,
            "version": version,
            "via": via,
            "generation": generation,
        }
        index = self._load_json(_META_INDEX) or {"rounds": [], "selections": []}
        if ident.canonical_id not in index["selections"]:
            index["selections"].append(ident.canonical_id)
        self._space.store().commit(
            CommitRequest(
                operation_key=(
                    f"acq:select:{ident.canonical_id}:v{version}:{via}:g{generation}"
                ),
                kind="acquisition_selection_freeze",
                content=content,
                meta_updates={
                    _META_SELECTION_PREFIX + ident.canonical_id: _dump(selection),
                    _META_INDEX: _dump(index),
                },
            )
        )
        return selection

    def _persist_selection_state(
        self,
        selection: dict,
        state: str,
        *,
        error: str | None = None,
        record: Any = None,
        raw_path: str | None = None,
        raw_sha256: str | None = None,
        retrieval: str | None = None,
        inventory: list[dict[str, Any]] | None = None,
    ) -> dict:
        """同一操作内推进选择状态（调用方已持锁，串行无并发交错）。

        状态推进仍由操作内已审计的回执链承载（冻结回执 + 材料回执/
        尝试回执），这里只落选择 meta 与索引；历史由
        :meth:`_upsert_selection` 追加。
        """
        updated = dict(selection)
        updated["state"] = state
        updated["error"] = error
        if record is not None:
            updated["source_record_id"] = record.record_id
            updated["source_record_revision"] = record.revision
        if raw_path is not None:
            updated["raw_path"] = raw_path
        if raw_sha256 is not None:
            updated["raw_sha256"] = raw_sha256
        if retrieval is not None:
            updated["retrieval"] = retrieval
        if inventory is not None:
            updated["package_inventory"] = inventory
        self._upsert_selection(updated)
        return updated

    def _next_start(self, state: dict) -> int:
        pages = state.get("pages", [])
        if not pages:
            return 0
        last = pages[-1]
        return int(last["start"]) + int(last.get("entries", 0))

    def _commit_page(
        self,
        round_id: str,
        request: ArxivSearchRequest,
        start: int,
        page: Any,
        state: dict,
    ) -> dict:
        """提交一页：发布页文档 → 记录 + 轮次进度 + 索引在同一事务落盘。"""
        document = _page_document(page)
        raw_ref = self._space.publish_source_file(
            f"discovery/{round_id}/page-{start:06d}.json", document
        )
        fetched_dt = datetime.fromtimestamp(float(page.fetched_at), tz=UTC)
        record_id = f"arxiv-search-{round_id}-p{start:06d}"
        record = build_record(
            ExternalRecord,
            record_id=record_id,
            created_at=fetched_dt,
            execution_id=None,
            data={
                "service": "arxiv-api",
                "catalog_version": None,
                "queried_at": fetched_dt,
                "query": {
                    "round_id": round_id,
                    "search_query": page.request.search_query(),
                    "start": start,
                    "page_size": page.request.page_size,
                    "sort_by": page.request.sort_by,
                    "sort_order": page.request.sort_order,
                    "submitted_from": request.submitted_from.isoformat(),
                    "submitted_to": request.submitted_to.isoformat(),
                    "terms": request.terms,
                    "categories": list(request.categories),
                },
                "raw_response": {
                    "path": raw_ref.path,
                    "sha256": raw_ref.sha256,
                },
                "entries": [
                    {
                        "entry_id": meta.canonical_id,
                        "fields": [
                            {
                                "name": f.name,
                                "raw_value": f.raw_value,
                                "unit": None,
                                "response_selector": f.response_selector,
                            }
                            for f in _entry_fields(meta)
                        ],
                    }
                    for meta in page.entries
                ],
            },
        )
        pages = list(state.get("pages", []))
        pages.append(
            {
                "start": start,
                "record_id": record_id,
                "revision": record.revision,
                "fetched_at": page.fetched_at,
                "entries": len(page.entries),
                "total_results": page.total_results,
                "anomalies": [anomaly.value for anomaly in page.anomalies],
                "raw_path": raw_ref.path,
                "raw_sha256": raw_ref.sha256,
                "response_url": page.response_url,
            }
        )
        papers = dict(state.get("papers", {}))
        for meta in page.entries:
            entry = dict(papers.get(meta.canonical_id, {"pages": []}))
            entry["version"] = meta.version
            entry["updated"] = meta.updated
            entry["published"] = meta.published
            page_list = list(entry.get("pages", []))
            if start not in page_list:
                page_list.append(start)
            entry["pages"] = page_list
            papers[meta.canonical_id] = entry
        state = {
            **state,
            "pages": pages,
            "papers": papers,
            "state": ROUND_IN_PROGRESS,
        }
        index = self._load_json(_META_INDEX) or {"rounds": [], "selections": []}
        if round_id not in index["rounds"]:
            index["rounds"].append(round_id)
        self._space.store().commit(
            CommitRequest(
                operation_key=f"acq:discovery:{round_id}:page:{start}",
                kind="acquisition_discovery_page",
                new_records=[record],
                content={
                    "kind": "acquisition_discovery_page",
                    "round_id": round_id,
                    "start": start,
                    "fetched_at": page.fetched_at,
                    "response_url": page.response_url,
                    "total_results": page.total_results,
                    "anomalies": [anomaly.value for anomaly in page.anomalies],
                    "entries": [
                        {
                            "canonical_id": meta.canonical_id,
                            "version": meta.version,
                            "updated": meta.updated,
                        }
                        for meta in page.entries
                    ],
                    "raw": {"path": raw_ref.path, "sha256": raw_ref.sha256},
                },
                meta_updates={
                    _META_ROUND_PREFIX + round_id: _dump(state),
                    _META_INDEX: _dump(index),
                },
            )
        )
        # 读回核对（调用方整操作持锁，串行无并发交错）：回执提交与
        # meta 更新同事务，正常必然一致；不一致按完整性错误如实上抛
        # （记录与轮次进度脱节是存储异常），不做回执外的补写修复。
        persisted = self._load_json(_META_ROUND_PREFIX + round_id)
        if persisted is None or start not in [
            int(item["start"]) for item in persisted.get("pages", [])
        ]:
            raise WorkspaceIntegrityError(
                f"页 {start} 的回执已提交但轮次 {round_id} 的进度缺失；"
                "记录与轮次进度不一致，请人工检查"
            )
        return state

    _ROUND_STATE_RANK: ClassVar[dict[str, int]] = {
        ROUND_FAILED: 0,
        ROUND_IN_PROGRESS: 1,
        ROUND_INCOMPLETE: 2,
        ROUND_COMPLETE: 3,
    }

    def _merge_round_state(self, stored: dict, incoming: dict) -> dict:
        """单调合并：旧快照的终态不得覆盖更新/更完整的存储进度。

        进度（页数）优先；页数相同取状态秩更高者。页数更多的快照即使
        带低秩状态也保留其页集，但状态取存储与提交中更高者。
        """
        stored_pages = len(stored.get("pages", []))
        incoming_pages = len(incoming.get("pages", []))
        stored_rank = self._ROUND_STATE_RANK.get(stored.get("state"), 0)
        incoming_rank = self._ROUND_STATE_RANK.get(incoming.get("state"), 0)
        if stored_pages > incoming_pages:
            base = dict(stored)
            if incoming_rank > stored_rank:
                base["state"] = incoming["state"]
                base["reason"] = incoming.get("reason")
                base["detail"] = incoming.get("detail")
            return base
        if stored_pages == incoming_pages:
            if incoming_rank >= stored_rank:
                return dict(incoming)
            return dict(stored)
        return dict(incoming)

    def _save_round_state(self, round_id: str, state: dict) -> dict:
        """轮次终态/进度提交（调用方整操作持锁），读回核对后返回。

        与存储进度做单调合并（旧快照不覆盖更完整状态——覆盖崩溃后
        重跑等场景）；回执与 meta 同事务提交，读回不一致按完整性错
        误如实上抛，不做回执外的补写修复。
        """
        stored = self._load_json(_META_ROUND_PREFIX + round_id)
        merged = self._merge_round_state(stored, state) if stored else dict(state)
        index = self._load_json(_META_INDEX) or {"rounds": [], "selections": []}
        if round_id not in index["rounds"]:
            index["rounds"].append(round_id)
        self._space.store().commit(
            CommitRequest(
                operation_key=(
                    f"acq:discovery:{round_id}:state:{merged['state']}:"
                    f"{len(merged.get('pages', []))}"
                ),
                kind="acquisition_discovery_state",
                content={
                    "kind": "acquisition_discovery_state",
                    "round_id": round_id,
                    "state": merged["state"],
                    "pages": len(merged.get("pages", [])),
                },
                meta_updates={
                    _META_ROUND_PREFIX + round_id: _dump(merged),
                    _META_INDEX: _dump(index),
                },
            )
        )
        persisted = self._load_json(_META_ROUND_PREFIX + round_id)
        if persisted != merged:
            raise WorkspaceIntegrityError(
                f"轮次 {round_id} 的状态回执已提交但读回不一致；"
                "存储进度异常，请人工检查"
            )
        return merged

    def _commit_attempt_record(
        self,
        *,
        ident: ArxivIdentifier,
        version: int,
        versioned: str,
        raw_ref: ArtifactRef,
        reason: str,
        error: str,
        fetched_at: float,
    ) -> None:
        """为"已保留但不可用"的原始字节补一条工程记录，使备份可遍历。

        保留字节只挂在选择状态 meta 时不会被 R05 备份遍历（备份沿记录
        的附件引用走）；这条 ExternalRecord 把字节作为记录附件引用，
        随业务库整体备份/恢复。幂等键含哈希与原因，重复下载相同字节
        时幂等复用。
        """
        record_id = (
            f"arxiv-attempt-{_safe_segment(ident.canonical_id)}-"
            f"{_safe_segment(versioned)}-{raw_ref.sha256[:8]}"
        )
        fetched_dt = datetime.fromtimestamp(float(fetched_at), tz=UTC)
        record = build_record(
            ExternalRecord,
            record_id=record_id,
            created_at=fetched_dt,
            execution_id=None,
            data={
                "service": "arxiv-eprint-attempt",
                "catalog_version": None,
                "queried_at": fetched_dt,
                "query": {
                    "canonical_id": ident.canonical_id,
                    "version": version,
                    "versioned_id": versioned,
                    "reason": reason,
                    "error": error,
                },
                "raw_response": {"path": raw_ref.path, "sha256": raw_ref.sha256},
                "entries": [],
            },
        )
        self._space.store().commit(
            CommitRequest(
                operation_key=(
                    f"acq:attempt:{ident.canonical_id}:v{version}:"
                    f"{raw_ref.sha256}:{reason}"
                ),
                kind="acquisition_attempt",
                new_records=[record],
                result_refs=[record.ref()],
                content={
                    "kind": "acquisition_attempt",
                    "canonical_id": ident.canonical_id,
                    "version": version,
                    "reason": reason,
                    "raw": {"path": raw_ref.path, "sha256": raw_ref.sha256},
                },
            )
        )

    def _declaration_record_id(
        self, canonical_id: str, version: int, prov_digest: str
    ) -> str:
        return (
            f"acq-import-decl-{_safe_segment(canonical_id)}-"
            f"v{version}-{prov_digest[:16]}"
        )

    def _commit_declaration_event(
        self,
        ident: ArxivIdentifier,
        version: int,
        raw_ref: ArtifactRef,
        declared: str,
        prov_digest: str,
    ) -> tuple[str, bool]:
        """首次声明事件的持久化：首次时间随事件记录可靠保存。

        声明回执只存内容摘要（R05 存储格式），**首次登记时间**作为
        业务记录的字段持久化：记录号/操作键确定性派生自（论文、版
        本、字节、声明摘要）。已存在时**复用原始时间**——跨版本重
        选、声明回执已提交而选择回执未提交时的崩溃重试都不改写首登
        时间；不存在时用当前时钟创建。声明回执内容保持不可变（无时
        间戳），同键重试幂等重放。返回 (首次时间, 是否本次新建)。
        """
        record_id = self._declaration_record_id(
            ident.canonical_id, version, prov_digest
        )
        existing = self._space.store().read_record_current(record_id)
        if existing is not None:
            for entry in existing.data.entries:
                for item in entry.fields:
                    if item.name == "first_declared_at":
                        return item.raw_value, False
            raise WorkspaceIntegrityError(
                f"声明记录 {record_id} 缺少 first_declared_at 字段；请人工检查"
            )
        first_at = _now_iso(self._clock)
        first_dt = datetime.fromisoformat(first_at)
        record = build_record(
            ExternalRecord,
            record_id=record_id,
            created_at=first_dt,
            execution_id=None,
            data={
                "service": "arxiv-local-import",
                "catalog_version": None,
                "queried_at": first_dt,
                "query": {
                    "canonical_id": ident.canonical_id,
                    "version": version,
                    "raw_sha256": raw_ref.sha256,
                    "provenance_digest": prov_digest,
                },
                "raw_response": {"path": raw_ref.path, "sha256": raw_ref.sha256},
                "entries": [
                    {
                        "entry_id": "declaration",
                        "fields": [
                            {
                                "name": "provenance",
                                "raw_value": declared,
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
        self._space.store().commit(
            CommitRequest(
                operation_key=(
                    f"acq:import-provenance:{ident.canonical_id}:"
                    f"v{version}:{raw_ref.sha256}:{prov_digest}"
                ),
                kind="acquisition_import_provenance",
                new_records=[record],
                result_refs=[record.ref()],
                content={
                    "kind": "acquisition_import_provenance",
                    "canonical_id": ident.canonical_id,
                    "version": version,
                    "raw_sha256": raw_ref.sha256,
                    "provenance": declared,
                },
            )
        )
        return first_at, True

    def _declarations_for_target(
        self, canonical_id: str, version: int
    ) -> tuple[list[dict[str, Any]], list[str]]:
        """从持久化声明事件重建目标版本的完整声明日志。

        返回 (声明列表按首次时间排序, 问题回执键列表)。声明事件记录是
        目标日志的**权威来源**（不是选择视图里的陈旧日志）。发现既有
        声明回执缺少事件记录（未升级的旧工程状态）或记录结构损坏时，
        不静默跳过、不猜测时间：作为问题如实上报，由调用方在改动任何
        选择/历史之前失败关闭。
        """
        rows = self._space.store().find_receipts(kind="acquisition_import_provenance")
        prefix = f"acq:import-provenance:{canonical_id}:v{version}:"
        declarations: list[dict[str, Any]] = []
        problems: list[str] = []
        for row in rows:
            if not row.operation_key.startswith(prefix):
                continue
            digest = row.operation_key.rsplit(":", 1)[-1]
            record = self._space.store().read_record_current(
                self._declaration_record_id(canonical_id, version, digest)
            )
            if record is None:
                problems.append(f"{row.operation_key}（缺少声明事件记录）")
                continue
            fields = {
                item.name: item.raw_value
                for entry in record.data.entries
                for item in entry.fields
            }
            if "first_declared_at" not in fields or "provenance" not in fields:
                problems.append(f"{row.operation_key}（声明事件记录结构损坏）")
                continue
            declarations.append(
                {
                    "provenance": fields["provenance"],
                    "at": fields["first_declared_at"],
                }
            )
        declarations.sort(key=lambda item: item["at"])
        return declarations, problems

    def _check_declaration_state_compat(self, canonical_id: str, version: int) -> None:
        """导入前置检查：目标版本的全部既有声明事件必须完整可读。

        旧工程状态（存在 ``acquisition_import_provenance`` 回执但缺少
        对应声明事件记录，或记录结构损坏）不受支持也不迁移：在任何
        选择/历史改动或首次时间声明之前**明确失败关闭**，保留原始数
        据不动、不伪造时间、不把回执重放当作记录已插入的证据。建议
        新建工作空间。
        """
        _, problems = self._declarations_for_target(canonical_id, version)
        if problems:
            raise AcquisitionError(
                f"{canonical_id}v{version} 存在不兼容的旧获取状态："
                + "；".join(problems)
                + "。该状态缺少可验证的声明事件记录，无法可靠重建来源"
                "日志；已拒绝本次导入且未改动任何选择、历史或时间。"
                "请人工检查或新建工作空间后重新导入（不自动迁移）"
            )

    def _reject_conflicting_material(
        self, canonical_id: str, version: int, payload: bytes
    ) -> None:
        """同论文同版本已存在不同字节时在发布前拒绝（不留孤儿文件）。"""
        selection = self._load_selection(canonical_id)
        if (
            selection is not None
            and selection.get("raw_sha256")
            and int(selection["version"]) == version
        ):
            digest = hashlib.sha256(payload).hexdigest()
            if selection["raw_sha256"] != digest:
                raise WorkspaceConflictError(
                    f"{canonical_id}v{version} 已有不同字节的材料（既有 "
                    f"{selection['raw_sha256'][:12]}…，本次 {digest[:12]}…）；"
                    "同论文同版本不能覆盖不同内容"
                )

    def _publish_package_raw(
        self, canonical_id: str, versioned_id: str, payload: bytes
    ) -> ArtifactRef:
        """内容寻址发布原始包字节：同字节复用，不同字节拒绝覆盖。"""
        digest = hashlib.sha256(payload).hexdigest()
        return self._space.publish_source_file(
            f"papers/{_safe_segment(canonical_id)}/{_safe_segment(versioned_id)}"
            f"/package-{digest}.bin",
            payload,
        )

    def _commit_source_package(
        self,
        *,
        ident: ArxivIdentifier,
        version: int,
        versioned: str,
        raw_ref: ArtifactRef,
        inspection: Any,
        retrieval: str,
        operation_prefix: str,
    ) -> tuple[SourcePackageRecord, Receipt]:
        """提交来源包记录（引用在发布之后）；幂等键含论文/版本/哈希。

        记录内的文件条目只有未解析的原始包（盘上有对应字节并按哈希
        核验）；成员清单不落盘解压，持久化在提交内容与选择状态中，
        由 R11 按原始包自行解包。
        """
        record_id = (
            f"arxiv-source-{_safe_segment(ident.canonical_id)}-"
            f"{_safe_segment(versioned)}"
        )
        record = build_record(
            SourcePackageRecord,
            record_id=record_id,
            created_at=_now_dt(self._clock),
            execution_id=None,
            data=SourcePackageData(
                paper_key=ident.canonical_id,
                origin=ArxivOrigin(
                    kind="arxiv",
                    arxiv_id=versioned,
                    retrieval=retrieval,
                ),
                source_version=f"v{version}",
                files=[
                    SourceFile(
                        file_id="raw",
                        path=raw_ref.path,
                        sha256=raw_ref.sha256,
                        media_type="application/octet-stream",
                        role="raw_package",
                        read_state="unparsed",
                        derived_from=[],
                    )
                ],
                gaps=[],
            ),
        )
        content = {
            "kind": f"acquisition_{operation_prefix}",
            "canonical_id": ident.canonical_id,
            "version": version,
            "raw": {"path": raw_ref.path, "sha256": raw_ref.sha256},
            "members": _inventory_payload(inspection),
        }
        record_id = (
            f"arxiv-source-{_safe_segment(ident.canonical_id)}-"
            f"{_safe_segment(versioned)}"
        )
        existing = self._space.store().read_record_current(record_id)
        if existing is not None:
            files = existing.data.files
            if not files:
                raise AcquisitionError(
                    f"既有材料记录 {record_id} 没有原始包文件条目；请人工检查"
                )
            if files[0].sha256 != raw_ref.sha256:
                raise WorkspaceConflictError(
                    f"{ident.canonical_id}v{version} 已有不同字节的材料"
                    f"（既有 {files[0].sha256[:12]}…，本次 "
                    f"{raw_ref.sha256[:12]}…）；同版同号不能覆盖不同内容"
                )
            # 跨来源同字节（如网络下载后本地镜像导入相同字节）：
            # 复用既有材料记录与附件引用，首次获取依据（retrieval）
            # 不可变；只提交本次操作的幂等回执（无记录修订，内容与
            # 首次提交完全一致以保证同键重放），新来源声明由调用方
            # 追加到溯源日志。
            receipt = self._space.store().commit(
                CommitRequest(
                    operation_key=(
                        f"acq:{operation_prefix}:{ident.canonical_id}:"
                        f"v{version}:{raw_ref.sha256}"
                    ),
                    kind=f"acquisition_{operation_prefix}",
                    result_refs=[existing.ref()],
                    content=content,
                )
            )
            return existing, receipt, True
        receipt = self._space.store().commit(
            CommitRequest(
                operation_key=(
                    f"acq:{operation_prefix}:{ident.canonical_id}:v{version}:"
                    f"{raw_ref.sha256}"
                ),
                kind=f"acquisition_{operation_prefix}",
                new_records=[record],
                result_refs=[record.ref()],
                content=content,
            )
        )
        return record, receipt, False

    def _adopt_committed(
        self,
        ident: ArxivIdentifier,
        version: int,
        versioned: str,
        selection: dict,
    ) -> AcquisitionResult | None:
        """采用已提交但状态未更新的材料（回执后崩溃窗口）。

        记录号是确定性的；存在则核对盘上字节哈希并在本地重算清单，
        然后补写选择状态。回写绑定**本次冻结**的选择（含世代），在锁
        内对当前选择做代际重验：期间被其他进程推进（不同世代/版本）
        则拒绝迟到回写，绝不借用当前选择包装旧记录。字节缺失/哈希不
        符抛工作空间完整性错误（恢复停止），不重新下载、不覆盖。记
        录身份与目标一致（同论文、同版本、同记录号）作为一个不变量
        一并校验。
        """
        record_id = (
            f"arxiv-source-{_safe_segment(ident.canonical_id)}-"
            f"{_safe_segment(versioned)}"
        )
        record = self._space.store().read_record_current(record_id)
        if record is None:
            return None
        files = record.data.files
        if not files:
            raise AcquisitionError(f"材料记录 {record_id} 没有原始包文件条目")
        # 目标一致性不变量：记录必须属于本次请求的论文与版本。
        origin = getattr(record.data, "origin", None)
        if (
            record.data.paper_key != ident.canonical_id
            or getattr(origin, "arxiv_id", None) != versioned
        ):
            raise WorkspaceIntegrityError(
                f"材料记录 {record_id} 与获取目标 {versioned} 不一致："
                f"paper_key={record.data.paper_key!r}，"
                f"arxiv_id={getattr(origin, 'arxiv_id', None)!r}；"
                "拒绝采用，请人工检查"
            )
        raw = files[0]
        payload = self._space.read_file(ArtifactRef(path=raw.path, sha256=raw.sha256))
        try:
            inspection = inspect_package(payload)
        except (UnsupportedPackageError, PackageIntegrityError) as exc:
            raise AcquisitionError(
                f"已提交材料与安全检查结果不一致（{record_id}）：{exc}；"
                "恢复停止，请人工检查"
            ) from exc
        bound = {
            **selection,
            "canonical_id": ident.canonical_id,
            "version": version,
            "versioned_id": versioned,
        }
        self._persist_selection_state(
            bound,
            "downloaded",
            record=record,
            raw_path=raw.path,
            raw_sha256=raw.sha256,
            inventory=_inventory_payload(inspection),
        )
        return AcquisitionResult(
            canonical_id=ident.canonical_id,
            version=version,
            versioned_id=versioned,
            state="downloaded",
            material_ref=record.ref(),
            raw_path=raw.path,
            raw_sha256=raw.sha256,
            replayed=True,
        )

    def _verify_material(self, selection: dict) -> AcquisitionResult:
        """已完成目标的恢复核对：只验哈希与记录，不触网。"""
        record_id = selection.get("source_record_id")
        revision = int(selection.get("source_record_revision") or 1)
        if not record_id:
            raise AcquisitionError(
                f"{selection['versioned_id']} 状态为已完成但缺少材料记录引用；"
                "请检查持久化状态"
            )
        record = self._space.store().read_record(
            RecordRef(record_id=record_id, revision=revision)
        )
        if record is None:
            raise AcquisitionError(
                f"材料记录 {record_id} v{revision} 缺失；已完成状态与记录不一致"
            )
        raw_path = selection.get("raw_path")
        raw_sha256 = selection.get("raw_sha256")
        if not raw_path or not raw_sha256:
            raise AcquisitionError(
                f"{selection['versioned_id']} 缺少原始包引用，无法核对完整性"
            )
        # 哈希不符时 read_file 抛工作空间完整性错误：恢复立即停止。
        self._space.read_file(ArtifactRef(path=raw_path, sha256=raw_sha256))
        return AcquisitionResult(
            canonical_id=selection["canonical_id"],
            version=int(selection["version"]),
            versioned_id=selection["versioned_id"],
            state=selection["state"],
            material_ref=RecordRef(record_id=record_id, revision=revision),
            raw_path=raw_path,
            raw_sha256=raw_sha256,
            replayed=True,
        )

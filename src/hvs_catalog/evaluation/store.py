"""Gold 正式答案的受控保存：准备、获准保存与查询/恢复。

正式目标由不可变内容文件和最后更新的版本入口组成：先在暂存区写好并
核对答案、来源与批准文件，再发布到确定路径，最后在锁内原子更新版本
入口；入口引用完整文件清单及哈希，读取者只接受文件全部存在且一致的
入口。部分写出的文件在入口发布前不算正式答案。

操作日志在任何正式写入前持久化，记录操作标识、准备清单哈希、预期旧
入口与新入口。相同操作标识、相同内容重试时核对并返回原结果；相同标
识异内容拒绝。入口已更新而回执未返回时，恢复据入口与操作记录确认已
保存，不发布第二个版本。不能解释的文件/日志不一致保留现场并停止。

批准绑定具体内容、材料与规则版本、目标位置和预期旧版本；批准后改动
草稿内容须重新准备与审定。接口保存不执行 Git 提交或推送。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from pydantic import AwareDatetime, Field

from ..records.common import Config, canonical_json
from ..workspace.errors import WorkspaceIntegrityError
from ..workspace.layout import resolve_within
from ..workspace.locking import SpaceLock
from .checking import GoldCheckReport, check_gold_annotation
from .errors import (
    GoldApprovalError,
    GoldCheckError,
    GoldIntegrityError,
    GoldOperationConflictError,
    GoldTargetError,
)
from .models import GoldAnnotation, read_gold_annotation
from .pdfcheck import inspect_pdf

TARGET_FORMAT = "hvs-catalog-gold-target"
TARGET_FORMAT_VERSION = 1
TARGET_MARKER = "gold-target.json"
ANNOTATIONS_DIR = "annotations"
VERSIONS_DIR = "versions"
OPERATIONS_DIR = "operations"
STAGING_DIR = "staging"
LOCK_FILENAME = ".lock"
ANSWER_FILENAME = "answer.json"
METADATA_FILENAME = "metadata.json"
PROVENANCE_FILENAME = "provenance.json"
APPROVAL_FILENAME = "approval.json"
MANIFEST_FILENAME = "manifest.json"
MANIFEST_SHA_FILENAME = "manifest.sha256"
CHECK_REPORT_FILENAME = "check-report.json"
PREPARATIONS_DIRNAME = "preparations"

TARGET_MANAGED_NAMES = frozenset(
    {
        TARGET_MARKER,
        ANNOTATIONS_DIR,
        VERSIONS_DIR,
        OPERATIONS_DIR,
        STAGING_DIR,
        LOCK_FILENAME,
    }
)

_SAFE_SEGMENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")

FaultHooks = dict[str, Callable[[], None]]


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_json(value: object) -> str:
    return _sha256_bytes(canonical_json(value).encode("utf-8"))


def _fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _atomic_write(path: Path, data: bytes) -> None:
    """同目录临时文件、fsync、原子改名，再同步目录。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.parent / f".{path.name}.tmp-{os.getpid()}-{uuid.uuid4().hex[:8]}"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    try:
        os.write(fd, data)
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(tmp, path)
    _fsync_dir(path.parent)


def _atomic_write_json(path: Path, payload: object) -> None:
    _atomic_write(
        path,
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True).encode()
        + b"\n",
    )


def _require_safe_segment(value: str, what: str) -> str:
    if not _SAFE_SEGMENT_RE.fullmatch(value):
        raise GoldTargetError(f"{what} 只能含字母、数字、点、下划线或连字符：{value!r}")
    return value


def _resolve_in_target(root: Path, relative: str | Path) -> Path:
    """目标内相对路径逐段解析；任何一段符号链接或越界都按完整性错误停止。"""
    try:
        return resolve_within(root, relative)
    except WorkspaceIntegrityError as exc:
        raise GoldIntegrityError(
            f"目标内路径不安全（{relative}）：{exc}；保留现场并停止"
        ) from exc


def _reject_git_revision(target: GoldTarget, document_id: str) -> None:
    path = target.entry_path(document_id)
    if path.is_file():
        entry = VersionEntry.model_validate_json(path.read_bytes())
        if entry.storage_mode == "git":
            raise GoldTargetError(
                "Gold was corrected in place under Git history; prepare/save/recovery "
                "cannot replace it. Edit the same files and use gold git-refresh."
            )


class FileEntry(Config):
    path: str
    sha256: str


class PreviousRef(Config):
    version: int
    manifest_sha256: str


class VersionEntry(Config):
    storage_mode: Literal["versioned", "git"] = "versioned"
    document_id: str
    version: int
    manifest_sha256: str
    operation_id: str
    created_at: str
    files: list[FileEntry]
    previous: PreviousRef | None = None


class OperationRecord(Config):
    operation_id: str
    document_id: str
    manifest_sha256: str
    approval_sha256: str
    answer_sha256: str
    provenance_sha256: str | None = None
    metadata_sha256: str | None = None
    expected_old: PreviousRef | None
    new_version: int
    files: list[FileEntry]
    state: str
    updated_at: str


class SaveApproval(Config):
    """专家对具体准备内容的科学审定与保存批准；工具只验证内容绑定。

    ``target_path`` 固定批准指向的正式目标目录；同一份资料在另一个目标
    不能串用该批准，须重新准备与审定。
    """

    approved_by: str
    approver_kind: Literal["human"]
    approved_at: AwareDatetime
    document_id: str
    target_path: str
    manifest_sha256: str
    expected_old: PreviousRef | None
    rules_version: str
    rules_sha256: str
    basis: str
    scientific_review: bool = False
    accepted_pending: list[str] = Field(default_factory=list)


class PreparationManifest(Config):
    """准备阶段固定的完整内容清单；批准与保存都绑定这份清单。

    ``target_path`` 是准备时绑定的正式目标目录（绝对路径），保存时在锁
    内复核；清单标识也由它参与派生，不同目标得到不同清单。
    """

    prep_id: str
    document_id: str
    target_path: str
    new_version: int
    expected_old: PreviousRef | None
    draft_sha256: str
    answer_path: str
    answer_sha256: str
    provenance_path: str | None = None
    provenance_sha256: str | None = None
    metadata_path: str | None = None
    metadata_sha256: str | None = None
    format_version: int = 1
    rules_version: str
    rules_sha256: str
    materials: list[FileEntry]
    science_pending: list[str]
    source_materials: list[FileEntry] = Field(default_factory=list)


class SaveReceipt(Config):
    operation_id: str
    document_id: str
    version: int
    manifest_sha256: str
    files: list[FileEntry]
    created_at: str
    replayed: bool = False
    recovered: bool = False


class GoldTarget:
    """正式答案目标目录：初始化保护已有资料，读写都限制在根内。"""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).resolve()

    # ---- 初始化与读取 ----

    def _verify_layout(self) -> None:
        """核对管理目录、标记与锁的归属；只接受真实目录与普通文件。

        管理名称不能是符号链接（防止越界写入或读取），标记必须可解析且
        版本已知；任何不符都整体拒绝，目标外与已有资料保持不变。
        """
        if not self.root.is_dir():
            raise GoldTargetError(f"目标路径 {self.root} 不是目录")
        problems: list[str] = []
        for name in (ANNOTATIONS_DIR, VERSIONS_DIR, OPERATIONS_DIR, STAGING_DIR):
            path = self.root / name
            if not path.exists() and not path.is_symlink():
                continue
            if path.is_symlink():
                problems.append(f"{name} 是符号链接，不能证明归属")
            elif not path.is_dir():
                problems.append(f"{name} 不是目录")
        lock_path = self.root / LOCK_FILENAME
        if (lock_path.exists() or lock_path.is_symlink()) and (
            lock_path.is_symlink() or not lock_path.is_file()
        ):
            problems.append(f"{LOCK_FILENAME} 不是普通文件")
        marker = self.root / TARGET_MARKER
        if marker.exists() or marker.is_symlink():
            if marker.is_symlink() or not marker.is_file():
                problems.append(f"{TARGET_MARKER} 不是普通文件")
            else:
                try:
                    payload = json.loads(marker.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError) as exc:
                    problems.append(f"目标标记无法读取：{exc}")
                else:
                    if (
                        payload.get("format") != TARGET_FORMAT
                        or payload.get("format_version") != TARGET_FORMAT_VERSION
                    ):
                        problems.append(
                            "未知或损坏的目标标记版本："
                            f"{payload.get('format')!r} v{payload.get('format_version')!r}"
                            f"（当前支持 {TARGET_FORMAT} v{TARGET_FORMAT_VERSION}）"
                        )
        if problems:
            raise GoldTargetError(
                f"目标目录 {self.root} 布局不满足受控保存前提："
                + "；".join(problems)
                + "；拒绝操作，目标内已有资料与目标外内容保持不变"
            )

    def initialize(self) -> None:
        """初始化目标目录；已有非托管资料或归属不明内容时整体拒绝。"""
        self.root.mkdir(parents=True, exist_ok=True)
        self._verify_layout()
        marker = self.root / TARGET_MARKER
        if marker.is_file():
            return  # 已初始化；幂等
        unknown = sorted(
            entry.name
            for entry in self.root.iterdir()
            if entry.name not in TARGET_MANAGED_NAMES
        )
        if unknown:
            raise GoldTargetError(
                f"目标目录 {self.root} 内已有非托管内容：{unknown}；"
                "初始化整体拒绝，已有资料保持不变"
            )
        for name in (ANNOTATIONS_DIR, VERSIONS_DIR, OPERATIONS_DIR, STAGING_DIR):
            (self.root / name).mkdir(exist_ok=True)
        lock_path = self.root / LOCK_FILENAME
        if not lock_path.exists():
            fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o644)
            os.close(fd)
        _atomic_write_json(
            marker,
            {
                "format": TARGET_FORMAT,
                "format_version": TARGET_FORMAT_VERSION,
                "created_at": _now(),
            },
        )

    def lock(self) -> SpaceLock:
        self.initialize()
        return SpaceLock(self.root, lock_filename=LOCK_FILENAME)

    def _entry_path(self, document_id: str) -> Path:
        _require_safe_segment(document_id, "document_id")
        return _resolve_in_target(self.root, f"{VERSIONS_DIR}/{document_id}.json")

    def _operation_path(self, operation_id: str) -> Path:
        _require_safe_segment(operation_id, "operation_id")
        return _resolve_in_target(self.root, f"{OPERATIONS_DIR}/{operation_id}.json")

    def entry_path(self, document_id: str) -> Path:
        """版本入口文件的确定路径。"""
        return self._entry_path(document_id)

    def operation_path(self, operation_id: str) -> Path:
        """操作日志文件的确定路径。"""
        return self._operation_path(operation_id)

    def read_entry(self, document_id: str) -> VersionEntry | None:
        """读取版本入口；入口存在但文件缺失或不一致时明确报错。"""
        self._verify_layout()
        path = self._entry_path(document_id)
        if not path.is_file():
            return None
        entry = VersionEntry.model_validate(
            json.loads(path.read_text(encoding="utf-8"))
        )
        problems = self.verify_entry_files(entry)
        if problems:
            raise GoldIntegrityError(
                f"版本入口 v{entry.version} 引用的文件不完整或不一致："
                + "；".join(problems)
            )
        if entry.document_id != document_id:
            raise GoldIntegrityError(
                "Version entry identity differs from requested document"
            )
        if entry.storage_mode == "git":
            from .git_revision import validate_git_entry

            validate_git_entry(self, entry)
            return entry
        if any(item.path.endswith(METADATA_FILENAME) for item in entry.files):
            record = self.read_operation(entry.operation_id)
            if (
                record is None
                or record.document_id != entry.document_id
                or record.operation_id != entry.operation_id
                or record.new_version != entry.version
                or record.manifest_sha256 != entry.manifest_sha256
                or record.expected_old != entry.previous
                or record.metadata_sha256 is None
                or record.state not in {"planned", "entry_published"}
                or (record.files and record.files != entry.files)
            ):
                raise GoldIntegrityError(
                    "Compact Gold version entry is not bound to an operation record"
                )
            expected = {
                _answer_target_path(
                    entry.document_id, entry.version
                ): record.answer_sha256,
                _metadata_target_path(
                    entry.document_id, entry.version
                ): record.metadata_sha256,
            }
            if {item.path: item.sha256 for item in entry.files} != expected:
                raise GoldIntegrityError(
                    "Compact Gold version files differ from the operation record"
                )
            approval_sidecar = _resolve_in_target(
                self.root, _compact_approval_path(entry.operation_id)
            )
            if (
                not approval_sidecar.is_file()
                or _sha256_bytes(approval_sidecar.read_bytes())
                != record.approval_sha256
            ):
                raise GoldIntegrityError(
                    "Compact Gold human approval record is missing or changed"
                )
            try:
                approval = SaveApproval.model_validate(
                    json.loads(approval_sidecar.read_text(encoding="utf-8"))
                )
            except (OSError, ValueError) as exc:
                raise GoldIntegrityError(
                    "Compact Gold human approval record is invalid"
                ) from exc
            if (
                approval.scientific_review is not True
                or approval.approver_kind != "human"
                or approval.document_id != entry.document_id
                or approval.manifest_sha256 != entry.manifest_sha256
                or approval.target_path != str(self.root)
            ):
                raise GoldIntegrityError(
                    "Compact Gold human approval does not bind the version"
                )
        return entry

    def verify_entry_files(self, entry: VersionEntry) -> list[str]:
        return self.verify_files(entry.files)

    def verify_files(self, files: list[FileEntry]) -> list[str]:
        """核对一组入口文件：存在、哈希一致且路径不越出目标根。"""
        problems: list[str] = []
        for item in files:
            try:
                target = resolve_within(self.root, item.path)
            except WorkspaceIntegrityError as exc:
                problems.append(str(exc))
                continue
            if not target.is_file():
                problems.append(f"缺失 {item.path}")
            elif _sha256_bytes(target.read_bytes()) != item.sha256:
                problems.append(f"哈希不符 {item.path}")
        return problems

    def read_operation(self, operation_id: str) -> OperationRecord | None:
        self._verify_layout()
        path = self._operation_path(operation_id)
        if not path.is_file():
            return None
        return OperationRecord.model_validate(
            json.loads(path.read_text(encoding="utf-8"))
        )

    def read_current_answer(
        self, document_id: str
    ) -> tuple[VersionEntry, GoldAnnotation | object]:
        """读取当前正式答案；入口与文件核对通过才返回。"""
        entry = self.read_entry(document_id)
        if entry is None:
            raise GoldTargetError(f"目标中没有文档 {document_id} 的正式答案")
        answer_entry = next(
            item for item in entry.files if item.path.endswith(ANSWER_FILENAME)
        )
        answer_path = _resolve_in_target(self.root, answer_entry.path)
        if any(item.path.endswith(METADATA_FILENAME) for item in entry.files):
            from .compact import read_compact_gold_pair

            pair = read_compact_gold_pair(answer_path)
            if document_id != f"gold-{pair.answer.arxiv_id}":
                raise GoldIntegrityError("Compact answer identity differs from entry")
            return entry, pair.answer
        annotation = read_gold_annotation(answer_path)
        if annotation.document_id != document_id:
            raise GoldIntegrityError(
                f"答案文件的 document_id（{annotation.document_id}）与入口"
                f"（{document_id}）不符"
            )
        return entry, annotation


@dataclass
class Preparation:
    manifest: PreparationManifest
    directory: Path
    answer_bytes: bytes
    provenance_bytes: bytes | None
    check_report: GoldCheckReport
    metadata_bytes: bytes | None = None

    @property
    def manifest_sha256(self) -> str:
        return manifest_sha256(self.manifest)


def _formalize_draft(draft: GoldAnnotation) -> GoldAnnotation:
    """确定性的格式整理：草稿转为正式答案形态，不补任何科学内容。"""
    data = draft.model_dump(mode="json", by_alias=True)
    data["kind"] = "answer"
    data.pop("pending", None)
    if draft.provenance.origin == "new_annotation":
        data["provenance"]["original_approval_version"] = 1
    return GoldAnnotation.model_validate(data)


def _answer_target_path(document_id: str, version: int) -> str:
    return f"{ANNOTATIONS_DIR}/{document_id}/v{version}/{ANSWER_FILENAME}"


def _provenance_target_path(document_id: str, version: int) -> str:
    return f"{ANNOTATIONS_DIR}/{document_id}/v{version}/{PROVENANCE_FILENAME}"


def _metadata_target_path(document_id: str, version: int) -> str:
    return f"{ANNOTATIONS_DIR}/{document_id}/v{version}/{METADATA_FILENAME}"


def _compact_approval_path(operation_id: str) -> str:
    return f"{OPERATIONS_DIR}/{operation_id}.approval.json"


def _derive_compact_preparation(
    *,
    pair,
    target: GoldTarget,
    expected_old_version: int | None,
    material_root: Path,
) -> tuple[PreparationManifest, bytes, bytes, GoldCheckReport]:
    from .compact import (
        check_compact_gold_pair,
        compact_answer_to_json,
        compact_metadata_to_json,
        read_compact_gold_pair,
    )

    report = check_compact_gold_pair(pair, material_root=material_root)
    if report.blocking_count:
        details = "; ".join(
            f"[{item.category}] {item.subject}: {item.message}"
            for item in report.problems
            if item.category in ("structure", "material")
        )
        raise GoldCheckError(
            f"Compact Gold has {report.blocking_count} blocking problems: {details}"
        )
    document_id = f"gold-{pair.answer.arxiv_id}"
    _reject_git_revision(target, document_id)
    _require_safe_segment(document_id, "document_id")
    entry = target.read_entry(document_id)
    if expected_old_version is None:
        if entry is not None:
            raise GoldTargetError(
                f"Document {document_id} already has v{entry.version}"
            )
        new_version = 1
        expected_old = None
    else:
        if entry is None or entry.version != expected_old_version:
            raise GoldTargetError("Expected old Gold version does not match target")
        if not any(item.path.endswith(METADATA_FILENAME) for item in entry.files):
            raise GoldTargetError(
                "A v1 answer cannot be revised as v2 without explicit migration"
            )
        previous = read_compact_gold_pair(
            _resolve_in_target(
                target.root, _answer_target_path(document_id, entry.version)
            )
        )
        if (
            previous.metadata.annotation.original_annotator
            != pair.metadata.annotation.original_annotator
            or previous.metadata.annotation.original_annotated_at
            != pair.metadata.annotation.original_annotated_at
        ):
            raise GoldTargetError(
                "Revision cannot replace the original human annotation identity"
            )
        new_version = entry.version + 1
        expected_old = PreviousRef(
            version=entry.version, manifest_sha256=entry.manifest_sha256
        )
    answer_bytes = compact_answer_to_json(pair.answer).encode("utf-8")
    metadata_bytes = compact_metadata_to_json(pair.metadata).encode("utf-8")
    manifest_core = {
        "format_version": 2,
        "source_materials": [
            {"path": pair.metadata.source.name, "sha256": pair.metadata.source.sha256}
        ]
        if pair.metadata.source
        else [],
        "document_id": document_id,
        "target_path": str(target.root),
        "new_version": new_version,
        "expected_old": None
        if expected_old is None
        else expected_old.model_dump(mode="json"),
        "draft_sha256": _sha256_bytes(answer_bytes + b"\n" + metadata_bytes),
        "answer_path": _answer_target_path(document_id, new_version),
        "answer_sha256": _sha256_bytes(answer_bytes),
        "metadata_path": _metadata_target_path(document_id, new_version),
        "metadata_sha256": _sha256_bytes(metadata_bytes),
        "rules_version": pair.metadata.rules.version,
        "rules_sha256": pair.metadata.rules.sha256,
        "materials": [
            {"path": item.name, "sha256": item.sha256} for item in pair.metadata.files
        ],
        "science_pending": sorted(set(report.pending_ids())),
    }
    prep_id = f"prep-{_sha256_json(manifest_core)[:16]}"
    manifest = PreparationManifest(prep_id=prep_id, **manifest_core)
    return manifest, answer_bytes, metadata_bytes, report


def _build_provenance_payload(draft: GoldAnnotation, report: GoldCheckReport) -> dict:
    return {
        "origin": draft.provenance.origin,
        "legacy": (
            draft.provenance.legacy.model_dump(mode="json")
            if draft.provenance.legacy is not None
            else None
        ),
        "note": draft.provenance.note,
        "prepared_by": draft.provenance.prepared_by.model_dump(mode="json")
        if draft.provenance.prepared_by
        else None,
        "original_expert": draft.provenance.original_expert.model_dump(mode="json")
        if draft.provenance.original_expert
        else None,
        "original_expert_approval": (
            f"annotations/{draft.document_id}/v1/approval.json"
            if draft.provenance.origin == "new_annotation"
            else None
        ),
        "check_summary": {
            "document_sha256": report.document_sha256,
            "problems_by_category": {
                category: len(report.problems_of(category))
                for category in ("structure", "material", "science_pending")
            },
            "science_pending": sorted(set(report.pending_ids())),
            "accepted_draft_pending": [
                item.model_dump(mode="json")
                for item in draft.pending
                if item.category == "science_review"
            ],
            "science_pending_details": [
                item.model_dump(mode="json")
                for item in report.problems_of("science_pending")
            ],
        },
    }


def _derive_preparation(
    *,
    draft: GoldAnnotation,
    target: GoldTarget,
    expected_old_version: int | None,
    material_root: Path,
) -> tuple[PreparationManifest, GoldAnnotation, bytes, bytes, GoldCheckReport]:
    _reject_git_revision(target, draft.document_id)
    if draft.kind != "draft":
        raise GoldTargetError(
            f"准备保存只接受草稿文档（kind=draft），当前 kind={draft.kind!r}；"
            "修订正式答案须先导出为草稿再修改"
        )
    # 检查完整草稿（含待办）：结构/材料缺口阻断，科学待办进入待核查清单，
    # 不在转为正式答案时静默删除
    report = check_gold_annotation(draft, material_root=material_root)
    if report.blocking_count:
        details = "; ".join(
            f"[{item.category}] {item.subject}: {item.message}"
            for item in report.problems
            if item.category in ("structure", "material")
        )
        raise GoldCheckError(
            f"草稿存在阻断正式保存的问题（{report.blocking_count} 项），"
            f"不能进入准备：{details}"
        )
    answer = _formalize_draft(draft)
    _require_safe_segment(draft.document_id, "document_id")
    entry = target.read_entry(draft.document_id)
    if expected_old_version is None:
        if entry is not None:
            raise GoldTargetError(
                f"文档 {draft.document_id} 在目标中已有 v{entry.version}，"
                "新建保存要求目标尚不存在；修订须提供预期旧版本"
            )
        new_version = 1
        expected_old = None
    else:
        if entry is None:
            raise GoldTargetError(
                f"预期修订文档 {draft.document_id} v{expected_old_version}，"
                "但目标中尚无任何版本"
            )
        if entry.version != expected_old_version:
            raise GoldTargetError(
                f"目标当前为 v{entry.version}，与预期旧版本 "
                f"v{expected_old_version} 不符"
            )
        previous = target.read_current_answer(draft.document_id)[1].provenance
        current = draft.provenance
        if previous.origin != current.origin or previous.legacy != current.legacy:
            raise GoldTargetError("修订必须保留最初来源及原始标注记录")
        if (
            previous.original_expert is not None
            and previous.original_expert.expert is not None
            and (
                current.original_expert is None
                or previous.original_expert.expert != current.original_expert.expert
                or (
                    previous.original_expert.annotated_at is not None
                    and previous.original_expert.annotated_at
                    != current.original_expert.annotated_at
                )
            )
        ):
            raise GoldTargetError("修订不能覆盖已确认的原始人类专家；后续审批另行记录")
        new_version = entry.version + 1
        expected_old = PreviousRef(
            version=entry.version, manifest_sha256=entry.manifest_sha256
        )
    answer_bytes = (
        json.dumps(
            answer.model_dump(mode="json", by_alias=True),
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")
    provenance_bytes = (
        json.dumps(
            _build_provenance_payload(draft, report),
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")
    draft_bytes = (
        json.dumps(
            draft.model_dump(mode="json", by_alias=True),
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")
    manifest_core = {
        "source_materials": [
            {
                "path": draft.material_correspondence.latex.name,
                "sha256": draft.material_correspondence.latex.sha256,
            }
        ]
        if draft.material_correspondence.latex
        else [],
        "document_id": draft.document_id,
        "target_path": str(target.root),
        "new_version": new_version,
        "expected_old": (
            None if expected_old is None else expected_old.model_dump(mode="json")
        ),
        "draft_sha256": _sha256_bytes(draft_bytes),
        "answer_path": _answer_target_path(draft.document_id, new_version),
        "answer_sha256": _sha256_bytes(answer_bytes),
        "provenance_path": _provenance_target_path(draft.document_id, new_version),
        "provenance_sha256": _sha256_bytes(provenance_bytes),
        "rules_version": answer.rules.version,
        "rules_sha256": answer.rules.sha256,
        "materials": [
            {"path": item.name, "sha256": item.sha256}
            for item in answer.files
            if item.sha256 is not None
        ],
        "science_pending": sorted(set(report.pending_ids())),
    }
    prep_id = f"prep-{_sha256_json(manifest_core)[:16]}"
    manifest = PreparationManifest(prep_id=prep_id, **manifest_core)
    return manifest, answer, answer_bytes, provenance_bytes, report


def prepare_save(
    *,
    draft_path: str | Path,
    material_root: str | Path,
    work_dir: str | Path,
    target: GoldTarget,
    expected_old_version: int | None = None,
) -> Preparation:
    """完成确定性整理并固定待批准的完整内容清单；不更新正式答案。

    准备材料写入草稿工作目录的 ``preparations/<prep_id>/``，同一草稿与
    同一前提得到同一清单；修改内容后重新准备得到新清单。
    """
    target.initialize()
    draft_path = Path(draft_path)
    if (
        draft_path.name == ANSWER_FILENAME
        and (draft_path.parent / METADATA_FILENAME).is_file()
    ):
        from .compact import read_compact_gold_pair

        pair = read_compact_gold_pair(draft_path)
        manifest, answer_bytes, metadata_bytes, report = _derive_compact_preparation(
            pair=pair,
            target=target,
            expected_old_version=expected_old_version,
            material_root=Path(material_root),
        )
        directory = Path(work_dir) / PREPARATIONS_DIRNAME / manifest.prep_id
        directory.mkdir(parents=True, exist_ok=True)
        _atomic_write(directory / ANSWER_FILENAME, answer_bytes)
        _atomic_write(directory / METADATA_FILENAME, metadata_bytes)
        _atomic_write_json(
            directory / MANIFEST_FILENAME,
            manifest.model_dump(mode="json", exclude_unset=True),
        )
        _atomic_write(
            directory / MANIFEST_SHA_FILENAME,
            (_manifest_sha256(manifest) + "\n").encode(),
        )
        _atomic_write_json(
            directory / CHECK_REPORT_FILENAME, report.model_dump(mode="json")
        )
        return Preparation(
            manifest=manifest,
            directory=directory,
            answer_bytes=answer_bytes,
            provenance_bytes=None,
            metadata_bytes=metadata_bytes,
            check_report=report,
        )
    draft = read_gold_annotation(draft_path)
    manifest, _answer, answer_bytes, provenance_bytes, report = _derive_preparation(
        draft=draft,
        target=target,
        expected_old_version=expected_old_version,
        material_root=Path(material_root),
    )
    directory = Path(work_dir) / PREPARATIONS_DIRNAME / manifest.prep_id
    directory.mkdir(parents=True, exist_ok=True)
    _atomic_write(directory / ANSWER_FILENAME, answer_bytes)
    _atomic_write(directory / PROVENANCE_FILENAME, provenance_bytes)
    _atomic_write_json(
        directory / MANIFEST_FILENAME,
        manifest.model_dump(mode="json", exclude_unset=True),
    )
    _atomic_write(
        directory / MANIFEST_SHA_FILENAME, (_manifest_sha256(manifest) + "\n").encode()
    )
    _atomic_write_json(
        directory / CHECK_REPORT_FILENAME,
        report.model_dump(mode="json"),
    )
    return Preparation(
        manifest=manifest,
        directory=directory,
        answer_bytes=answer_bytes,
        provenance_bytes=provenance_bytes,
        check_report=report,
    )


def load_preparation(preparation_dir: str | Path) -> Preparation:
    """读取已保存的准备材料并核对内容与清单一致。"""
    directory = Path(preparation_dir)
    manifest = PreparationManifest.model_validate(
        json.loads((directory / MANIFEST_FILENAME).read_text(encoding="utf-8"))
    )
    answer_bytes = (directory / ANSWER_FILENAME).read_bytes()
    provenance_bytes = (
        (directory / PROVENANCE_FILENAME).read_bytes()
        if manifest.format_version == 1
        else None
    )
    metadata_bytes = (
        (directory / METADATA_FILENAME).read_bytes()
        if manifest.format_version == 2
        else None
    )
    report = GoldCheckReport.model_validate(
        json.loads((directory / CHECK_REPORT_FILENAME).read_text(encoding="utf-8"))
    )
    problems: list[str] = []
    if _sha256_bytes(answer_bytes) != manifest.answer_sha256:
        problems.append("答案文件与清单哈希不符")
    if manifest.format_version == 1:
        if (
            provenance_bytes is None
            or _sha256_bytes(provenance_bytes) != manifest.provenance_sha256
        ):
            problems.append("来源记录与清单哈希不符")
    elif manifest.format_version == 2:
        if (
            metadata_bytes is None
            or _sha256_bytes(metadata_bytes) != manifest.metadata_sha256
        ):
            problems.append("元数据文件与清单哈希不符")
        else:
            from .compact import read_compact_gold_pair

            try:
                pair = read_compact_gold_pair(directory / ANSWER_FILENAME)
                if pair.metadata.review.answer_sha256 != manifest.answer_sha256:
                    problems.append("元数据与答案文件绑定不符")
            except (GoldIntegrityError, ValueError) as exc:
                problems.append(f"Compact Gold 结构或绑定不符：{exc}")
    else:
        problems.append(f"未知 Gold 准备格式版本 {manifest.format_version}")
    if sorted(set(report.pending_ids())) != sorted(manifest.science_pending):
        problems.append("检查报告与清单的待核查项不一致")
    sidecar = directory / MANIFEST_SHA_FILENAME
    if sidecar.is_file() and (
        sidecar.read_text(encoding="utf-8").strip() != _manifest_sha256(manifest)
    ):
        problems.append("清单哈希旁车文件与清单内容不符")
    if problems:
        raise GoldIntegrityError(
            f"准备材料 {directory} 与清单不符（{'；'.join(problems)}）；"
            "保留现场，重新准备后再试"
        )
    return Preparation(
        manifest=manifest,
        directory=directory,
        answer_bytes=answer_bytes,
        provenance_bytes=provenance_bytes,
        metadata_bytes=metadata_bytes,
        check_report=report,
    )


def _verify_approval_binding(approval: SaveApproval, preparation: Preparation) -> None:
    if not approval.approved_by.strip() or not approval.basis.strip():
        raise GoldApprovalError(
            "批准记录必须写明操作者与审定依据；工具不自动生成 approved=true"
        )
    manifest = preparation.manifest
    if manifest.format_version == 2 and approval.scientific_review is not True:
        raise GoldApprovalError(
            "Compact Gold requires explicit human scientific_review=true for this exact answer and metadata manifest"
        )
    if approval.document_id != manifest.document_id:
        raise GoldApprovalError(
            f"批准的文档（{approval.document_id}）与准备内容"
            f"（{manifest.document_id}）不符"
        )
    if approval.target_path != manifest.target_path:
        raise GoldApprovalError(
            f"批准绑定的保存目标（{approval.target_path}）与准备内容绑定的目标"
            f"（{manifest.target_path}）不符；相同资料在不同目标不能串用批准"
        )
    if approval.manifest_sha256 != _manifest_sha256(manifest):
        raise GoldApprovalError(
            "批准绑定的内容清单与准备材料不符；批准后改动内容须重新审定"
        )
    expected = manifest.expected_old
    approved_old = approval.expected_old
    if (expected is None) != (approved_old is None) or (
        expected is not None
        and approved_old is not None
        and (
            expected.version != approved_old.version
            or expected.manifest_sha256 != approved_old.manifest_sha256
        )
    ):
        raise GoldApprovalError("批准的目标前提（预期旧版本）与准备内容不符")
    if (
        approval.rules_version != manifest.rules_version
        or approval.rules_sha256 != manifest.rules_sha256
    ):
        raise GoldApprovalError("批准的规则版本与准备内容不符")
    if sorted(approval.accepted_pending) != sorted(set(manifest.science_pending)):
        raise GoldApprovalError(
            "批准接受的待核查项与准备清单不一致；按规则保留的科学未决状态"
            "须在批准中明确列出后才能保存"
        )


def _manifest_sha256(manifest: PreparationManifest) -> str:
    # Preserve the exact v1 preparation hash across schema extensions: historical
    # manifests did not contain v2-only default fields.
    return _sha256_json(manifest.model_dump(mode="json", exclude_unset=True))


def manifest_sha256(manifest: PreparationManifest) -> str:
    """准备清单的内容哈希；批准记录以此绑定具体待保存内容。"""
    return _manifest_sha256(manifest)


def _verify_materials(manifest: PreparationManifest, material_root: Path) -> None:
    """锁内复核固定材料：文件存在、哈希一致且可打开。"""
    for item in manifest.source_materials:
        path = resolve_within(material_root, item.path)
        if not path.is_file() or _sha256_bytes(path.read_bytes()) != item.sha256:
            raise GoldCheckError(f"LaTeX 材料 {item.path} 缺失或哈希不符")
    for item in manifest.materials:
        path = resolve_within(material_root, item.path)
        inspection = inspect_pdf(path, expected_sha256=item.sha256)
        if inspection.problems:
            raise GoldCheckError(
                f"材料 {item.path} 复核未通过：{'；'.join(inspection.problems)}"
            )


def _publish_files(
    *,
    target: GoldTarget,
    staging: Path,
    document_id: str,
    version: int,
    files: list[tuple[str, bytes, str]],
    hooks: FaultHooks | None,
) -> list[FileEntry]:
    """从暂存区发布内容文件；每个文件原子改名到位后同步目录。

    目标路径自目标根逐段解析（拒绝任何一段符号链接），不信任已存在的
    版本目录本身。
    """
    final_dir = _resolve_in_target(
        target.root, f"{ANNOTATIONS_DIR}/{document_id}/v{version}"
    )
    final_dir.mkdir(parents=True, exist_ok=True)
    published: list[FileEntry] = []
    for name, data, digest in files:
        staged = _resolve_in_target(
            target.root, staging.relative_to(target.root) / name
        )
        _atomic_write(staged, data)
        if _sha256_bytes(staged.read_bytes()) != digest:
            raise GoldIntegrityError(f"暂存文件 {name} 与清单哈希不符")
    hook = (hooks or {}).get("after_stage")
    if hook is not None:
        hook()
    for name, data, digest in files:
        staged = _resolve_in_target(
            target.root, staging.relative_to(target.root) / name
        )
        relative = f"{ANNOTATIONS_DIR}/{document_id}/v{version}/{name}"
        final = _resolve_in_target(target.root, relative)
        if final.exists() and _sha256_bytes(final.read_bytes()) != digest:
            raise GoldIntegrityError(
                f"目标文件 {final} 已存在且内容与清单不符；保留现场并停止"
            )
        os.replace(staged, final)
        _fsync_dir(final.parent)
        published.append(
            FileEntry(path=str(final.relative_to(target.root)), sha256=digest)
        )
        hook = (hooks or {}).get(f"after_file:{name}")
        if hook is not None:
            hook()
    hook = (hooks or {}).get("after_files")
    if hook is not None:
        hook()
    return published


def approved_save(
    *,
    draft_path: str | Path,
    material_root: str | Path,
    target: GoldTarget,
    preparation_dir: str | Path,
    approval: SaveApproval | str | Path,
    operation_id: str,
    fault_hooks: FaultHooks | None = None,
) -> SaveReceipt:
    """锁内复核后保存获准内容；重试按操作标识返回或恢复原结果。

    ``fault_hooks`` 仅供独立进程的中断验证定位故障窗口，不属于正常用户
    流程。
    """
    _require_safe_segment(operation_id, "operation_id")
    preparation = load_preparation(preparation_dir)
    manifest = preparation.manifest

    if not isinstance(approval, SaveApproval):
        approval = SaveApproval.model_validate(
            json.loads(Path(approval).read_text(encoding="utf-8"))
        )
    else:
        # model_copy/model_construct may bypass validation; revalidate at the write boundary.
        approval = SaveApproval.model_validate(approval.model_dump(mode="python"))
    _verify_approval_binding(approval, preparation)
    approval_bytes = (
        json.dumps(
            approval.model_dump(mode="json"),
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")

    with target.lock():
        _reject_git_revision(target, preparation.manifest.document_id)
        # 锁内复核目标绑定：批准与清单固定的目标必须是当前目标
        if manifest.target_path != str(target.root):
            raise GoldTargetError(
                f"准备清单绑定的目标目录（{manifest.target_path}）与当前目标"
                f"（{target.root}）不符；相同资料在不同目标不能串用批准与准备"
            )
        _verify_materials(manifest, Path(material_root))
        approval_sha = _sha256_bytes(approval_bytes)
        existing = target.read_operation(operation_id)
        if existing is not None:
            # 已有操作记录：重放只认原内容与原批准，先核对实际结果
            if existing.manifest_sha256 != _manifest_sha256(manifest):
                raise GoldOperationConflictError(
                    f"操作标识 {operation_id!r} 已用于不同内容；重试须携带相同"
                    "内容，或为新内容更换操作标识"
                )
            if existing.approval_sha256 != approval_sha:
                raise GoldOperationConflictError(
                    f"操作标识 {operation_id!r} 已绑定不同的批准记录"
                )
            return _reconcile_operation(
                target=target,
                record=existing,
                approval_bytes=approval_bytes,
                preparation=preparation,
                fault_hooks=fault_hooks,
                recovered=True,
            )

        # 新操作：以当前草稿重新推导，必须与获准清单一致（批准后改动检测）
        if manifest.format_version == 2:
            from .compact import read_compact_gold_pair

            pair = read_compact_gold_pair(draft_path)
            current = _derive_compact_preparation(
                pair=pair,
                target=target,
                expected_old_version=(
                    manifest.expected_old.version if manifest.expected_old else None
                ),
                material_root=Path(material_root),
            )[0]
        else:
            draft = read_gold_annotation(draft_path)
            current = _derive_preparation(
                draft=draft,
                target=target,
                expected_old_version=(
                    manifest.expected_old.version if manifest.expected_old else None
                ),
                material_root=Path(material_root),
            )[0]
        if current.model_dump(mode="json") != manifest.model_dump(mode="json"):
            raise GoldApprovalError(
                "当前草稿与获准的准备清单不一致（内容在批准后发生了变化）；"
                "原批准不再适用，须重新准备并重新审定"
            )

        entry = target.read_entry(manifest.document_id)
        if manifest.expected_old is None:
            if entry is not None:
                raise GoldTargetError(
                    f"新建保存要求目标尚无文档 {manifest.document_id}，"
                    f"当前已有 v{entry.version}"
                )
        else:
            if (
                entry is None
                or entry.version != manifest.expected_old.version
                or (entry.manifest_sha256 != manifest.expected_old.manifest_sha256)
            ):
                raise GoldTargetError(
                    "目标版本与批准前提不符：预期 "
                    f"{_describe_old(manifest.expected_old)}，实际 "
                    f"{_describe_old_from_entry(entry)}；修订须按当前版本"
                    "重新准备与审定"
                )

        record = OperationRecord(
            operation_id=operation_id,
            document_id=manifest.document_id,
            manifest_sha256=_manifest_sha256(manifest),
            approval_sha256=approval_sha,
            answer_sha256=manifest.answer_sha256,
            provenance_sha256=manifest.provenance_sha256,
            metadata_sha256=manifest.metadata_sha256,
            expected_old=manifest.expected_old,
            new_version=manifest.new_version,
            files=[],
            state="planned",
            updated_at=_now(),
        )
        _atomic_write_json(
            target.operation_path(operation_id), record.model_dump(mode="json")
        )
        hook = (fault_hooks or {}).get("after_log")
        if hook is not None:
            hook()
        return _reconcile_operation(
            target=target,
            record=record,
            approval_bytes=approval_bytes,
            preparation=preparation,
            fault_hooks=fault_hooks,
            recovered=False,
        )


def _describe_old(expected: PreviousRef | None) -> str:
    if expected is None:
        return "尚无版本"
    return f"v{expected.version}（清单 {_short(expected.manifest_sha256)}）"


def _short(value: str) -> str:
    return f"{value[:12]}…"


def _describe_old_from_entry(entry: VersionEntry | None) -> str:
    if entry is None:
        return "尚无版本"
    return f"v{entry.version}（清单 {_short(entry.manifest_sha256)}）"


def _complete_entry_operation(target: GoldTarget, entry: VersionEntry) -> None:
    """锁内核对已发布入口，补全中断留下的日志；保留稳定的原回执。"""
    record = target.read_operation(entry.operation_id)
    if record is None:
        raise GoldIntegrityError("已发布入口缺少对应操作日志；保留现场并停止")
    expected = {
        _answer_target_path(entry.document_id, entry.version): record.answer_sha256
    }
    if record.metadata_sha256 is not None:
        expected[_metadata_target_path(entry.document_id, entry.version)] = (
            record.metadata_sha256
        )
        approval_sidecar = _resolve_in_target(
            target.root, _compact_approval_path(record.operation_id)
        )
        if (
            not approval_sidecar.is_file()
            or _sha256_bytes(approval_sidecar.read_bytes()) != record.approval_sha256
        ):
            raise GoldIntegrityError(
                "Compact Gold approval sidecar is missing or changed"
            )
    else:
        expected[_provenance_target_path(entry.document_id, entry.version)] = (
            record.provenance_sha256
        )
        expected[
            f"{ANNOTATIONS_DIR}/{entry.document_id}/v{entry.version}/{APPROVAL_FILENAME}"
        ] = record.approval_sha256
    if (
        record.document_id != entry.document_id
        or record.operation_id != entry.operation_id
        or record.new_version != entry.version
        or record.manifest_sha256 != entry.manifest_sha256
        or record.expected_old != entry.previous
        or record.state not in {"planned", "entry_published"}
        or len(entry.files) != len(expected)
        or {item.path: item.sha256 for item in entry.files} != expected
        or target.verify_entry_files(entry)
        or (record.files and record.files != entry.files)
    ):
        raise GoldIntegrityError("已发布入口与操作日志不一致；保留现场并停止")
    if record.state == "entry_published" and record.files:
        return
    completed = record.model_copy(
        update={
            "state": "entry_published",
            "files": entry.files,
            "updated_at": entry.created_at,
        }
    )
    _atomic_write_json(
        target.operation_path(record.operation_id), completed.model_dump(mode="json")
    )


def _reconcile_operation(
    *,
    target: GoldTarget,
    record: OperationRecord,
    approval_bytes: bytes,
    preparation: Preparation,
    fault_hooks: FaultHooks | None,
    recovered: bool,
) -> SaveReceipt:
    """按操作记录核对现场：已保存则返回原结果，未完成则继续发布。

    这是保存与恢复的公共路径：入口已发布时不再发布第二个版本；操作已
    完整发布并被后续版本取代时，按操作记录核对历史文件后返回原回执；
    修订要求前驱入口存在且匹配，入口缺失不能当作新建。任何不能解释的
    不一致保留现场并抛出 GoldIntegrityError。
    """
    manifest = preparation.manifest
    if manifest.format_version == 2:
        approval_sidecar = _resolve_in_target(
            target.root, _compact_approval_path(record.operation_id)
        )
        if (
            approval_sidecar.exists()
            and _sha256_bytes(approval_sidecar.read_bytes()) != record.approval_sha256
        ):
            raise GoldIntegrityError(
                "Compact Gold approval sidecar differs from operation record"
            )
    entry = target.read_entry(manifest.document_id)
    if (
        entry is not None
        and entry.operation_id == record.operation_id
        and entry.manifest_sha256 == record.manifest_sha256
        and entry.version == record.new_version
    ):
        _complete_entry_operation(target, entry)
        return SaveReceipt(
            operation_id=record.operation_id,
            document_id=manifest.document_id,
            version=entry.version,
            manifest_sha256=record.manifest_sha256,
            files=entry.files,
            created_at=entry.created_at,
            replayed=True,
            recovered=True,
        )
    if entry is not None and entry.version == record.new_version:
        raise GoldIntegrityError(
            f"目标已有 v{entry.version} 入口但内容与操作 "
            f"{record.operation_id!r} 的记录不一致；保留现场并停止"
        )
    if record.state == "entry_published" and record.files:
        # 操作已完成发布（可能已被后续版本取代）：核对历史文件后返回原回执
        problems = target.verify_files(record.files)
        if problems:
            raise GoldIntegrityError(
                f"操作 {record.operation_id!r} 已发布的历史文件不完整或不一致："
                + "；".join(problems)
                + "；保留现场，请人工核对"
            )
        if manifest.format_version == 2 and not approval_sidecar.is_file():
            raise GoldIntegrityError(
                "Published compact Gold approval sidecar is missing"
            )
        return SaveReceipt(
            operation_id=record.operation_id,
            document_id=manifest.document_id,
            version=record.new_version,
            manifest_sha256=record.manifest_sha256,
            files=record.files,
            created_at=record.updated_at,
            replayed=True,
            recovered=True,
        )
    if entry is not None and entry.version > record.new_version:
        raise GoldIntegrityError(
            f"目标入口 v{entry.version} 领先于操作 {record.operation_id!r} 的"
            f" v{record.new_version}，而该操作未完成发布；"
            "保留现场并停止，不覆盖已发布内容"
        )
    expected = record.expected_old
    if expected is not None:
        # 修订要求前驱入口存在且匹配；入口丢失按资料损坏处理，不当新建
        if entry is None:
            raise GoldIntegrityError(
                f"修订操作 {record.operation_id!r} 的前驱入口缺失（预期 "
                f"{_describe_old(expected)}）；不能把入口缺失当作新建，"
                "保留现场并停止"
            )
        if (
            entry.version != expected.version
            or entry.manifest_sha256 != expected.manifest_sha256
        ):
            raise GoldIntegrityError(
                f"目标入口 v{entry.version} 与操作 {record.operation_id!r} 的"
                f"修订前提（预期 {_describe_old(expected)}）不符；"
                "保留现场并停止"
            )
    elif entry is not None:
        raise GoldIntegrityError(
            f"新建操作 {record.operation_id!r} 要求目标尚无入口，"
            f"实际已有 v{entry.version}；保留现场并停止"
        )

    # 入口尚未发布：核对已发布文件是否与清单一致（部分写出不算正式答案）
    answer_final = _resolve_in_target(target.root, manifest.answer_path)
    compact = manifest.format_version == 2
    companion_path = manifest.metadata_path if compact else manifest.provenance_path
    companion_hash = manifest.metadata_sha256 if compact else manifest.provenance_sha256
    if companion_path is None or companion_hash is None:
        raise GoldIntegrityError("Preparation companion path/hash is missing")
    companion_final = _resolve_in_target(target.root, companion_path)
    approval_final = (
        _resolve_in_target(target.root, _compact_approval_path(record.operation_id))
        if compact
        else answer_final.parent / APPROVAL_FILENAME
    )
    for final, digest in (
        (answer_final, manifest.answer_sha256),
        (companion_final, companion_hash),
    ):
        if final.exists():
            if _sha256_bytes(final.read_bytes()) != digest:
                raise GoldIntegrityError(
                    f"已发布文件 {final} 与清单不符；保留现场并停止"
                )
        elif final.parent.exists():
            stray = [
                item.name
                for item in final.parent.iterdir()
                if item.name
                not in (
                    {ANSWER_FILENAME, METADATA_FILENAME}
                    if compact
                    else {ANSWER_FILENAME, PROVENANCE_FILENAME, APPROVAL_FILENAME}
                )
            ]
            if stray:
                raise GoldIntegrityError(
                    f"版本目录 {final.parent} 内出现不能解释的内容：{stray}；"
                    "保留现场并停止"
                )
    if approval_final.exists() and _sha256_bytes(
        approval_final.read_bytes()
    ) != _sha256_bytes(approval_bytes):
        raise GoldIntegrityError(
            f"已发布的批准文件与本次批准记录不符；操作 {record.operation_id!r} "
            "不能以不同批准内容重放"
        )

    staging = _resolve_in_target(target.root, f"{STAGING_DIR}/{record.operation_id}")
    # Validate all leaves before creating directories or writing the first file.
    for name in (
        (ANSWER_FILENAME, METADATA_FILENAME)
        if compact
        else (ANSWER_FILENAME, PROVENANCE_FILENAME, APPROVAL_FILENAME)
    ):
        _resolve_in_target(target.root, staging.relative_to(target.root) / name)
    if entry is not None:
        # A caller may only have queried a completed-but-unacknowledged predecessor.
        # Preserve its receipt before replacing the sole current-version entry.
        _complete_entry_operation(target, entry)
    staging.mkdir(parents=True, exist_ok=True)
    if compact:
        if preparation.metadata_bytes is None:
            raise GoldIntegrityError("Compact Gold metadata was not prepared")
        if not approval_final.exists():
            _atomic_write(approval_final, approval_bytes)
        files: list[tuple[str, bytes, str]] = [
            (ANSWER_FILENAME, preparation.answer_bytes, manifest.answer_sha256),
            (METADATA_FILENAME, preparation.metadata_bytes, companion_hash),
        ]
    else:
        if preparation.provenance_bytes is None:
            raise GoldIntegrityError("v1 provenance was not prepared")
        files = [
            (ANSWER_FILENAME, preparation.answer_bytes, manifest.answer_sha256),
            (PROVENANCE_FILENAME, preparation.provenance_bytes, companion_hash),
            (APPROVAL_FILENAME, approval_bytes, _sha256_bytes(approval_bytes)),
        ]
    published = _publish_files(
        target=target,
        staging=staging,
        document_id=manifest.document_id,
        version=manifest.new_version,
        files=files,
        hooks=fault_hooks,
    )
    entry_payload = VersionEntry(
        document_id=manifest.document_id,
        version=manifest.new_version,
        manifest_sha256=record.manifest_sha256,
        operation_id=record.operation_id,
        created_at=_now(),
        files=published,
        previous=record.expected_old,
    )
    _atomic_write_json(
        target.entry_path(manifest.document_id), entry_payload.model_dump(mode="json")
    )
    hook = (fault_hooks or {}).get("after_entry")
    if hook is not None:
        hook()
    record = record.model_copy(
        update={
            "files": published,
            "state": "entry_published",
            "updated_at": entry_payload.created_at,
        }
    )
    _atomic_write_json(
        target.operation_path(record.operation_id), record.model_dump(mode="json")
    )
    shutil.rmtree(staging, ignore_errors=True)
    return SaveReceipt(
        operation_id=record.operation_id,
        document_id=manifest.document_id,
        version=manifest.new_version,
        manifest_sha256=record.manifest_sha256,
        files=published,
        created_at=entry_payload.created_at,
        recovered=recovered,
    )


class OperationStatus(Config):
    operation_id: str
    state: str
    detail: str
    receipt: SaveReceipt | None = None


def query_operation(
    *,
    operation_id: str,
    target: GoldTarget,
) -> OperationStatus:
    """查询操作结果：先核对实际入口与文件，再报告状态；只读。

    入口已发布且回执未曾返回时，据入口与操作记录确认已保存，不发布第
    二个版本；已成功保存并被后续版本修订的历史操作同样可查原回执，查
    询时核对相应历史文件的完整性。不能解释的不一致如实报告并保留现
    场；修订操作的前驱入口缺失按不一致报告。原路径已用 Git 修订时返回
    historical：历史操作与批准不能证明当前答案，原内容须由 Git 历史核查，
    此查询不验证原内容或当前修订内容。
    """
    record = target.read_operation(operation_id)
    if record is None:
        return OperationStatus(
            operation_id=operation_id,
            state="unknown",
            detail=f"目标中没有操作 {operation_id!r} 的记录",
        )
    if record.metadata_sha256 is not None:
        approval_sidecar = _resolve_in_target(
            target.root, _compact_approval_path(operation_id)
        )
        if (
            approval_sidecar.exists()
            and _sha256_bytes(approval_sidecar.read_bytes()) != record.approval_sha256
        ):
            return OperationStatus(
                operation_id=operation_id,
                state="inconsistent",
                detail="Compact Gold approval sidecar hash mismatch",
            )
        if record.state == "entry_published" and not approval_sidecar.is_file():
            return OperationStatus(
                operation_id=operation_id,
                state="inconsistent",
                detail="Compact Gold approval sidecar missing",
            )
    raw_path = target.entry_path(record.document_id)
    if raw_path.is_file():
        raw_entry = VersionEntry.model_validate_json(raw_path.read_bytes())
        if raw_entry.storage_mode == "git" and record.new_version == raw_entry.version:
            return OperationStatus(
                operation_id=operation_id,
                state="historical",
                detail=(
                    "Original save and approval are historical evidence only. The same "
                    "Gold files were corrected in place; inspect Git history for the "
                    "original bytes. This query does not verify historical content or "
                    "the current corrected answer."
                ),
            )
    try:
        entry = target.read_entry(record.document_id)
    except GoldIntegrityError as exc:
        return OperationStatus(
            operation_id=operation_id,
            state="inconsistent",
            detail=f"版本入口、文件或批准记录不一致：{exc}",
        )
    if (
        entry is not None
        and entry.operation_id == operation_id
        and entry.manifest_sha256 == record.manifest_sha256
    ):
        return OperationStatus(
            operation_id=operation_id,
            state="saved",
            detail=(
                f"已保存为 v{entry.version}；入口与文件核对一致，"
                "回执未返回时据入口确认，不重复发布"
            ),
            receipt=SaveReceipt(
                operation_id=operation_id,
                document_id=record.document_id,
                version=entry.version,
                manifest_sha256=record.manifest_sha256,
                files=entry.files,
                created_at=entry.created_at,
                replayed=True,
                recovered=True,
            ),
        )
    if entry is not None and entry.version == record.new_version:
        return OperationStatus(
            operation_id=operation_id,
            state="inconsistent",
            detail=(
                f"目标入口 v{entry.version} 与操作记录（v{record.new_version}）"
                "不能对应；保留现场，请人工核对"
            ),
        )
    if record.state == "entry_published" and record.files:
        # 历史操作：已成功保存（可能已被修订），核对历史文件后返回原回执
        problems = target.verify_files(record.files)
        if problems:
            return OperationStatus(
                operation_id=operation_id,
                state="inconsistent",
                detail=(
                    f"操作记录称已保存 v{record.new_version}，但历史文件不完整"
                    f"或不一致：{'；'.join(problems)}；保留现场，请人工核对"
                ),
            )
        superseded = (
            f"，随后被 v{entry.version} 修订"
            if entry is not None and entry.version > record.new_version
            else "；历史文件核对一致"
        )
        return OperationStatus(
            operation_id=operation_id,
            state="saved",
            detail=f"已保存为 v{record.new_version}（历史操作）{superseded}",
            receipt=SaveReceipt(
                operation_id=operation_id,
                document_id=record.document_id,
                version=record.new_version,
                manifest_sha256=record.manifest_sha256,
                files=record.files,
                created_at=record.updated_at,
                replayed=True,
                recovered=True,
            ),
        )
    if entry is not None:
        return OperationStatus(
            operation_id=operation_id,
            state="inconsistent",
            detail=(
                f"目标入口 v{entry.version} 与操作记录（v{record.new_version}）"
                "不能对应；保留现场，请人工核对"
            ),
        )
    if record.expected_old is not None:
        return OperationStatus(
            operation_id=operation_id,
            state="inconsistent",
            detail=(
                "修订操作的前驱入口缺失（预期 "
                f"{_describe_old(record.expected_old)}）；不能把入口缺失当作"
                "新建，保留现场，请人工核对"
            ),
        )
    answer_final = _resolve_in_target(
        target.root, _answer_target_path(record.document_id, record.new_version)
    )
    if answer_final.is_file():
        if _sha256_bytes(answer_final.read_bytes()) != record.answer_sha256:
            return OperationStatus(
                operation_id=operation_id,
                state="inconsistent",
                detail="已发布文件与操作记录不符；保留现场，请人工核对",
            )
        if record.metadata_sha256 is not None:
            metadata_final = _resolve_in_target(
                target.root,
                _metadata_target_path(record.document_id, record.new_version),
            )
            if (
                metadata_final.is_file()
                and _sha256_bytes(metadata_final.read_bytes()) != record.metadata_sha256
            ):
                return OperationStatus(
                    operation_id=operation_id,
                    state="inconsistent",
                    detail="Metadata differs from operation record",
                )
        return OperationStatus(
            operation_id=operation_id,
            state="recoverable",
            detail=(
                "文件已发布但版本入口尚未更新；入口发布前不算正式答案，"
                "可用同一操作标识与原批准重试以完成保存"
            ),
        )
    return OperationStatus(
        operation_id=operation_id,
        state="pending",
        detail="操作已记录但尚未发布任何文件；可用同一操作标识与原批准重试",
    )

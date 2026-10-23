"""Workspace identity, whole-directory initialization, and immutable publication.

Accept fresh/empty directories only; complete sibling staging and close databases before
publishing atomically. Never scan/take over crash remnants; clean only this call's
unpublished staging.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import tempfile
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from ..records.common import ArtifactRef
from .errors import (
    UnknownWorkspaceError,
    WorkspaceConflictError,
    WorkspaceIntegrityError,
)
from .locking import SpaceLock

if TYPE_CHECKING:
    from .store import RecordStore

WORKSPACE_FORMAT = "hvs-catalog-workspace"
WORKSPACE_FORMAT_VERSION = 1
WORKSPACE_FILENAME = "workspace.json"
CONFIG_FILENAME = "config.json"
BUSINESS_DB = "business.db"
CHECKPOINT_DB = "checkpoint.db"
LOCK_FILENAME = ".lock"
SOURCE_DIR = "sources"
RUNS_DIR = "runs"
BACKUPS_DIR = "backups"
_FORBIDDEN_CONFIG_KEY_FRAGMENTS = (
    "secret",
    "password",
    "token",
    "credential",
    "api_key",
)


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _write_file_published(path: Path, data: bytes) -> None:
    """Write/fsync a sibling temporary file, rename atomically, then sync the directory."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.parent / f".{path.name}.tmp-{os.getpid()}-{uuid.uuid4().hex[:8]}"
    try:
        with tmp.open("xb") as file:
            file.write(data)
            file.flush()
            os.fsync(file.fileno())
        os.replace(tmp, path)
        _fsync_dir(path.parent)
    finally:
        tmp.unlink(missing_ok=True)


@dataclass(frozen=True)
class SpaceIdentity:
    space_id: str
    format: str
    format_version: int
    created_at: str

    def marker_payload(self) -> dict:
        return {
            "format": self.format,
            "format_version": self.format_version,
            "space_id": self.space_id,
            "created_at": self.created_at,
        }


def _read_marker(root: Path) -> SpaceIdentity:
    marker_path = root / WORKSPACE_FILENAME
    if not marker_path.exists():
        marker_path = resolve_within(root, ".state/" + WORKSPACE_FILENAME)
    if not marker_path.is_file():
        raise UnknownWorkspaceError(
            f"{root} 不是已初始化的工作空间（缺少 {WORKSPACE_FILENAME}）；"
            "演示空间数据库不能当作正式空间打开，也不会被隐式初始化"
        )
    try:
        payload = json.loads(marker_path.read_text(encoding="utf-8"))
        if payload.get("format") != WORKSPACE_FORMAT:
            raise UnknownWorkspaceError(f"未知工作空间格式：{payload.get('format')!r}")
        if payload.get("format_version") not in {WORKSPACE_FORMAT_VERSION, 2}:
            raise UnknownWorkspaceError(
                f"未知工作空间格式版本：v{payload.get('format_version')}"
                f"（当前支持 v{WORKSPACE_FORMAT_VERSION}），不自动迁移"
            )
        return SpaceIdentity(
            space_id=str(payload["space_id"]),
            format=str(payload["format"]),
            format_version=int(payload["format_version"]),
            created_at=str(payload["created_at"]),
        )
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
        raise UnknownWorkspaceError(f"空间标记无法读取：{exc}") from exc


def _require_empty_target(root: Path) -> None:
    if root.exists() and (not root.is_dir() or any(root.iterdir())):
        raise WorkspaceConflictError(
            f"目录 {root} 非空或不是目录；初始化整体拒绝，已有资料保持不变。"
            "不接管中断残留，请另选空目录"
        )


def _publish_initialization(staging: Path, root: Path) -> None:
    """Publish directories atomically; POSIX rename cannot overwrite nonempty targets."""
    _require_empty_target(root)
    try:
        os.rename(staging, root)
    except OSError as exc:
        raise WorkspaceConflictError(
            f"初始化发布冲突，目标 {root} 保持不变；请另选空目录"
        ) from exc
    _fsync_dir(root.parent)


def initialize_workspace(
    root: Path, *, config: dict | None = None, benchmark: bool = False
) -> Workspace:
    """Create complete workspaces; compatible reinitialization only reads original
    configuration.
    """
    from .store import RecordStore

    root = Path(root).resolve()
    if (root / WORKSPACE_FILENAME).exists() or (
        root / ".state" / WORKSPACE_FILENAME
    ).exists():
        space = open_workspace(root)
        if benchmark and space.identity.format_version != 2:
            space.close()
            raise WorkspaceConflictError(
                "Benchmark requires a shared benchmark workspace."
            )
        return space
    _require_empty_target(root)
    _validate_config(config if config is not None else {})
    root.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{root.name}-init-", dir=root.parent))
    try:
        identity = SpaceIdentity(
            uuid.uuid4().hex, WORKSPACE_FORMAT, 2 if benchmark else 1, _now()
        )
        state = staging / ".state" if benchmark else staging
        state.mkdir(exist_ok=True)
        for dirname in ("materials", RUNS_DIR) if benchmark else (SOURCE_DIR, RUNS_DIR):
            (staging / dirname).mkdir()
        _write_file_published(
            state / CONFIG_FILENAME,
            (json.dumps(config or {}, ensure_ascii=False, indent=2) + "\n").encode(),
        )
        store = RecordStore(
            state / BUSINESS_DB, create=True, space_id=identity.space_id
        )
        store.close()
        sqlite3.connect(state / CHECKPOINT_DB).close()
        (state / LOCK_FILENAME).touch()
        _write_file_published(
            state / WORKSPACE_FILENAME,
            (
                json.dumps(identity.marker_payload(), ensure_ascii=False, indent=2)
                + "\n"
            ).encode(),
        )
        _publish_initialization(staging, root)
    finally:
        # Clean this call's unpublished directories only, leaving other calls/historical remnants untouched.
        if staging.exists():
            shutil.rmtree(staging)
    return open_workspace(root)


def open_workspace(root: Path, *, read_only: bool = False) -> Workspace:
    """Open explicitly without implicit initialization, repair, or migration."""
    root = Path(root).resolve()
    identity = _read_marker(root)
    state = root / ".state" if identity.format_version == 2 else root
    for name in (BUSINESS_DB, CHECKPOINT_DB):
        if not (state / name).is_file():
            raise WorkspaceIntegrityError(
                f"空间 {root} 缺少数据库文件 {name}，标记与内容不符"
            )
    db_identity = _read_space_id(state / BUSINESS_DB)
    if db_identity != identity.space_id:
        raise WorkspaceIntegrityError(
            f"空间 {identity.space_id} 的标记与业务数据库身份（{db_identity}）不符"
        )
    return Workspace(root, identity, read_only=read_only)


def _read_space_id(business_db: Path) -> str | None:
    try:
        conn = sqlite3.connect(f"file:{business_db}?mode=ro", uri=True)
    except sqlite3.Error:
        return None
    try:
        row = conn.execute("SELECT value FROM meta WHERE key = 'space_id'").fetchone()
    except sqlite3.Error:
        return None
    finally:
        conn.close()
    return str(row[0]) if row else None


def _validate_config(config: dict) -> None:
    if not isinstance(config, dict):
        raise WorkspaceConflictError("配置必须是 JSON 对象")
    lowered = {key.lower(): key for key in config}
    for fragment in _FORBIDDEN_CONFIG_KEY_FRAGMENTS:
        for low, original in lowered.items():
            if fragment in low:
                raise WorkspaceConflictError(
                    f"配置键 {original!r} 含有凭据类字段名（{fragment}）；配置只保存认证引用，不保存凭据值"
                )
    try:
        json.dumps(config)
    except (TypeError, ValueError) as exc:
        raise WorkspaceConflictError(f"配置不是可保存的 JSON 内容：{exc}") from exc


def resolve_within(root: Path, relative: str | Path) -> Path:
    """Resolve root-relative paths, rejecting escapes/symlinks."""
    rel = Path(relative)
    if rel.is_absolute() or rel.as_posix().startswith(("~", "/")):
        raise WorkspaceIntegrityError(f"文件路径必须是相对路径：{relative}")
    if any(part in {"", ".."} for part in rel.parts):
        raise WorkspaceIntegrityError(f"文件路径越界：{relative}")
    path = root / rel
    current = root
    for part in rel.parts:
        current = current / part
        if current.is_symlink():
            raise WorkspaceIntegrityError(f"文件路径包含符号链接：{current}")
    resolved = path.resolve()
    root_resolved = root.resolve()
    if resolved != root_resolved and root_resolved not in resolved.parents:
        raise WorkspaceIntegrityError(f"文件路径解析后越出空间根目录：{relative}")
    return path


class Workspace:
    """Opened workspace; callers perform writes under its lock."""

    def __init__(
        self, root: Path, identity: SpaceIdentity, *, read_only: bool = False
    ) -> None:
        self.root = root
        self.identity = identity
        self._store: RecordStore | None = None
        self.read_only = read_only
        self.state_root = root / ".state" if identity.format_version == 2 else root

    def lock(self) -> SpaceLock:
        return SpaceLock(self.state_root)

    def store(self) -> RecordStore:
        if self._store is None:
            from .store import RecordStore

            self._store = RecordStore(
                self.state_root / BUSINESS_DB,
                materials_root=self.root,
                read_only=self.read_only,
            )
        return self._store

    def checkpoint_connection(self) -> sqlite3.Connection:
        path = self.state_root / CHECKPOINT_DB
        conn = (
            sqlite3.connect(
                path.as_uri() + "?mode=ro", uri=True, check_same_thread=False
            )
            if self.read_only
            else sqlite3.connect(path, check_same_thread=False)
        )
        conn.row_factory = sqlite3.Row
        return conn

    def read_config(self) -> dict:
        return json.loads(
            (self.state_root / CONFIG_FILENAME).read_text(encoding="utf-8")
        )

    def update_config(self, changes: dict) -> dict:
        if self.read_only:
            raise WorkspaceIntegrityError("只读空间不能修改配置")
        _validate_config(changes)
        with self.lock():
            merged = {**self.read_config(), **changes}
            _validate_config(merged)
            _write_file_published(
                self.state_root / CONFIG_FILENAME,
                (json.dumps(merged, ensure_ascii=False, indent=2) + "\n").encode(),
            )
        return merged

    def publish_file(self, relative_path: str, data: bytes) -> ArtifactRef:
        if self.read_only:
            raise WorkspaceIntegrityError("只读空间不能发布文件")
        if self.identity.format_version == 2 and relative_path.startswith(
            "runs/inputs/"
        ):
            relative_path = ".state/inputs/" + relative_path.removeprefix(
                "runs/inputs/"
            )
        rel = Path(relative_path)
        top = rel.parts[0] if rel.parts else ""
        allowed = (
            {"materials", "runs", ".state", "preparations"}
            if self.identity.format_version == 2
            else {SOURCE_DIR, RUNS_DIR}
        )
        if top not in allowed:
            raise WorkspaceConflictError(
                f"空间文件必须位于 {SOURCE_DIR}/ 或 {RUNS_DIR}/ 下：{relative_path}"
            )
        target = resolve_within(self.root, rel)
        digest = hashlib.sha256(data).hexdigest()
        if target.exists():
            if hashlib.sha256(target.read_bytes()).hexdigest() == digest:
                return ArtifactRef(path=rel.as_posix(), sha256=digest)
            raise WorkspaceConflictError(
                f"目标文件已存在且内容不同，拒绝覆盖：{relative_path}"
            )
        _write_file_published(target, data)
        return ArtifactRef(path=rel.as_posix(), sha256=digest)

    def publish_source_file(self, name: str, data: bytes) -> ArtifactRef:
        return self.publish_file(f"{SOURCE_DIR}/{name}", data)

    def publish_run_file(
        self, execution_id: str, name: str, data: bytes
    ) -> ArtifactRef:
        return self.publish_file(self.run_path(execution_id, name), data)

    def run_path(self, execution_id: str, name: str) -> str:
        if self.identity.format_version == 2:
            if name.startswith(("results/", "traces/")):
                return f".state/executions/{execution_id}/{name}"
            return f"runs/{execution_id}/trace/{name}"
        return f"{RUNS_DIR}/{execution_id}/{name}"

    def publish_run_stream(self, execution_id: str, name: str, stream) -> ArtifactRef:
        """Publish a binary stream immutably without loading it into memory."""
        if self.read_only:
            raise WorkspaceIntegrityError("只读空间不能发布文件")
        relative = self.run_path(execution_id, name)
        target = resolve_within(self.root, relative)
        target.parent.mkdir(parents=True, exist_ok=True)
        staging = target.parent / f".{target.name}.tmp-{uuid.uuid4().hex}"
        digest = hashlib.sha256()
        try:
            with staging.open("xb") as output:
                while chunk := stream.read(1024 * 1024):
                    digest.update(chunk)
                    output.write(chunk)
                output.flush()
                os.fsync(output.fileno())
            if target.exists():
                with target.open("rb") as existing:
                    old = hashlib.file_digest(existing, "sha256").hexdigest()
                if old != digest.hexdigest():
                    raise WorkspaceConflictError(
                        "目标文件已存在且内容不同，拒绝覆盖：" + relative
                    )
            else:
                os.replace(staging, target)
                _fsync_dir(target.parent)
            return ArtifactRef(path=relative, sha256=digest.hexdigest())
        finally:
            staging.unlink(missing_ok=True)

    def read_file(self, ref: ArtifactRef) -> bytes:
        path = resolve_within(self.root, ref.path)
        if not path.is_file():
            raise WorkspaceIntegrityError(f"引用文件缺失：{ref.path}")
        data = path.read_bytes()
        if hashlib.sha256(data).hexdigest() != ref.sha256:
            raise WorkspaceIntegrityError(f"引用文件哈希改变：{ref.path}")
        return data

    def close(self) -> None:
        if self._store is not None:
            self._store.close()
            self._store = None

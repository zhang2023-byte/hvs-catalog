"""工作空间目录、身份标记、本机配置与材料文件的安全发布。

空间结构：``workspace.json``（身份与格式标记，初始化最后发布）、
``config.json``（本机配置）、``business.db``、``checkpoint.db``、
``sources/``、``runs/``、``backups/``、稳定锁文件 ``.lock``。

初始化是带预写清单（WAL）的事务：暂存目录 ``.workspace-init`` 内先
写入清单，之后每发布一个条目（目录、锁文件、数据库、配置、完成标
记）都先把条目追加进清单并落盘，再执行实际发布。清单自建立之初绑
定空间身份，任何回滚或清理前都先整体校验清单（含每个条目的路径、
类型与哈希）并逐项核对暂存内容，损坏、绑定不符或含未知资料的暂存
目录无法证明属于本程序，整体拒绝且逐字节保留。完成后的清理同样在
初始化互斥锁内进行。初始化互斥锁位于暂存目录内，不从父进程继承，
也不在持锁期间被替换。已有非托管文件可以保留，但预留文件名冲突时
整体拒绝；重复初始化同一兼容空间只读取已有配置，不隐式修复或迁
移。演示空间没有正式空间标记，不能当作正式空间打开。
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from ..records.common import ArtifactRef
from .errors import (
    UnknownWorkspaceError,
    WorkspaceBusyError,
    WorkspaceConflictError,
    WorkspaceIntegrityError,
)
from .locking import LockIdentity, SpaceLock, write_lock_identity

if TYPE_CHECKING:
    from .store import RecordStore

WORKSPACE_FORMAT = "hvs-catalog-workspace"
WORKSPACE_FORMAT_VERSION = 1

WORKSPACE_FILENAME = "workspace.json"
CONFIG_FILENAME = "config.json"
BUSINESS_DB = "business.db"
CHECKPOINT_DB = "checkpoint.db"
LOCK_FILENAME = ".lock"
INIT_STAGING_DIRNAME = ".workspace-init"
JOURNAL_FILENAME = "journal.json"
INIT_LOCK_FILENAME = "init.lock"
SOURCE_DIR = "sources"
RUNS_DIR = "runs"
BACKUPS_DIR = "backups"

#: 初始化会占用的全部名称；与这些名字冲突时整体拒绝。
RESERVED_NAMES = frozenset(
    {
        WORKSPACE_FILENAME,
        CONFIG_FILENAME,
        BUSINESS_DB,
        CHECKPOINT_DB,
        LOCK_FILENAME,
        INIT_STAGING_DIRNAME,
        SOURCE_DIR,
        RUNS_DIR,
        BACKUPS_DIR,
    }
)

#: 配置中不允许出现的键名片段：凭据值不能写入配置文件。
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
    """可靠写文件：同目录临时文件、fsync、原子改名，再同步目录。"""
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


def _read_marker_or_none(root: Path) -> SpaceIdentity | None:
    try:
        return _read_marker(root)
    except UnknownWorkspaceError:
        return None


def _read_marker(root: Path) -> SpaceIdentity:
    marker_path = root / WORKSPACE_FILENAME
    if not marker_path.is_file():
        raise UnknownWorkspaceError(
            f"{root} 不是已初始化的工作空间（缺少 {WORKSPACE_FILENAME}）；"
            "演示空间数据库不能当作正式空间打开，也不会被隐式初始化"
        )
    try:
        payload = json.loads(marker_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise UnknownWorkspaceError(f"空间标记无法读取：{exc}") from exc
    if payload.get("format") != WORKSPACE_FORMAT:
        raise UnknownWorkspaceError(
            f"未知工作空间格式：{payload.get('format')!r}"
            f"（当前支持 {WORKSPACE_FORMAT} v{WORKSPACE_FORMAT_VERSION}）"
        )
    if payload.get("format_version") != WORKSPACE_FORMAT_VERSION:
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


def _check_reserved_conflicts(root: Path) -> None:
    conflicts = sorted(name for name in RESERVED_NAMES if (root / name).exists())
    if conflicts:
        raise WorkspaceConflictError(
            f"目录 {root} 内已有与工作空间预留名冲突的内容：{conflicts}；"
            "初始化整体拒绝，已有资料保持不变"
        )


# ---- 初始化清单（WAL） ----


def _journal_path(staging: Path) -> Path:
    return staging / JOURNAL_FILENAME


def _write_init_journal(staging: Path, journal: dict) -> None:
    _write_file_published(_journal_path(staging), json.dumps(journal).encode())


#: 初始化协议实际允许发布的根级名称与类型；清单条目越出此表即损坏。
_INIT_PUBLISHABLE: dict[str, str] = {
    SOURCE_DIR: "directory",
    RUNS_DIR: "directory",
    BACKUPS_DIR: "directory",
    LOCK_FILENAME: "file",
    CONFIG_FILENAME: "file",
    CHECKPOINT_DB: "file",
    BUSINESS_DB: "file",
    WORKSPACE_FILENAME: "file",
}


def _init_journal_problems(journal: object) -> list[str]:
    """结构性核对整个清单：状态、空间绑定与每个已发布条目。

    任何删除（回滚或完成清理）发生前，先完整核对清单本身；条目缺
    path/kind/哈希、路径越界或重复、或路径不是初始化协议发布的根级
    名称（嵌套路径、别名或任意用户文件）都属于无法证明归属的损坏
    清单——不能把一个带合法哈希的任意路径当作初始化产物。
    """
    if not isinstance(journal, dict):
        return ["清单不是 JSON 对象"]
    problems: list[str] = []
    if journal.get("state") not in {"active", "complete"}:
        problems.append(f"state 为 {journal.get('state')!r}")
    space_id = journal.get("space_id")
    if not isinstance(space_id, str) or not space_id:
        problems.append("缺少空间身份绑定（space_id）")
    entries = journal.get("published")
    if not isinstance(entries, list):
        problems.append("published 不是列表")
        return problems
    seen: set[str] = set()
    for index, entry in enumerate(entries):
        label = f"published[{index}]"
        if not isinstance(entry, dict):
            problems.append(f"{label} 不是对象")
            continue
        rel = entry.get("path")
        if not isinstance(rel, str) or not rel:
            problems.append(f"{label} 缺少路径")
            continue
        path = Path(rel)
        if path.is_absolute() or any(part in {"", ".."} for part in path.parts):
            problems.append(f"{label} 路径越界：{rel}")
            continue
        if len(path.parts) != 1:
            problems.append(f"{label} 路径不是初始化发布的根级名称：{rel}")
            continue
        if rel in seen:
            problems.append(f"{label} 路径重复：{rel}")
            continue
        seen.add(rel)
        expected_kind = _INIT_PUBLISHABLE.get(rel)
        if expected_kind is None:
            problems.append(f"{label} 不是初始化协议发布的名称：{rel}")
            continue
        kind = entry.get("kind")
        if kind != expected_kind:
            problems.append(
                f"{label} 类型与协议不符：{rel} 应为 {expected_kind}，清单写 {kind!r}"
            )
            continue
        if kind == "file":
            digest = entry.get("sha256")
            if not isinstance(digest, str) or len(digest) != 64:
                problems.append(f"{label} 文件条目缺少有效的 sha256")
    return problems


def _read_init_journal(staging: Path) -> dict | None:
    """读取并完整校验初始化清单；缺失返回 None，损坏按无法接管拒绝。"""
    path = _journal_path(staging)
    if not path.is_file():
        return None
    try:
        journal = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise WorkspaceIntegrityError(
            f"初始化清单无法读取：{exc}；身份不明的 {INIT_STAGING_DIRNAME}"
            " 拒绝接管，内容保持不变"
        ) from exc
    problems = _init_journal_problems(journal)
    if problems:
        raise WorkspaceIntegrityError(
            "初始化清单内容不完整（" + "；".join(problems) + "）；"
            f"无法证明 {INIT_STAGING_DIRNAME} 归属，拒绝接管，内容保持不变"
        )
    return journal


def _journal_append(staging: Path, journal: dict, entry: dict) -> None:
    """预写清单：条目先落盘，再执行实际发布。"""
    journal["published"].append(entry)
    _write_init_journal(staging, journal)


def _rollback_journal(root: Path, journal: dict) -> None:
    """完整预检后按清单逆序回滚本次初始化发布的条目。

    先对全部条目逐段验证目标路径（任何一段符号链接都拒绝，不跟随）
    并核对已发布目标的类型、内容与哈希；任一条目不安全都在删除任何
    文件之前发现并整体拒绝，不能边删边遇到后面的危险条目。
    """
    actions: list[tuple[Path, str]] = []
    for entry in journal["published"]:
        rel = Path(entry["path"])
        current = root
        for part in rel.parts:
            current = current / part
            if current.is_symlink():
                raise WorkspaceIntegrityError(
                    f"初始化清单路径包含符号链接：{current}；拒绝接管，内容保持不变"
                )
        target = root / rel
        if not target.exists() and not target.is_symlink():
            continue  # 尚未发布或已回滚
        if entry["kind"] == "directory":
            if not target.is_dir():
                raise WorkspaceIntegrityError(
                    f"清单声明的目录已被替换为其他内容，拒绝接管：{rel}"
                )
            if any(target.iterdir()):
                raise WorkspaceIntegrityError(
                    f"中断初始化发布的目录非空，不能确认全部属于本次初始化：{rel}"
                )
            actions.append((target, "rmdir"))
        else:
            if not target.is_file():
                raise WorkspaceIntegrityError(
                    f"清单声明的文件已被替换为其他内容，拒绝接管：{rel}"
                )
            if hashlib.sha256(target.read_bytes()).hexdigest() != entry["sha256"]:
                raise WorkspaceIntegrityError(
                    f"中断初始化发布的文件内容已变化，不能确认属于本次初始化：{rel}"
                )
            actions.append((target, "unlink"))
    # 预检全部通过后才修改文件系统：逆序删除自身产物
    for target, action in reversed(actions):
        if action == "rmdir":
            target.rmdir()
        else:
            target.unlink()


def _staging_lock(staging: Path) -> SpaceLock:
    return SpaceLock(staging, lock_filename=INIT_LOCK_FILENAME, expected_identity=None)


#: 暂存目录内允许出现的初始化自身产物；出现其他内容即拒绝删除。
_STAGING_FILE_NAMES = frozenset(
    {
        JOURNAL_FILENAME,
        INIT_LOCK_FILENAME,
        CONFIG_FILENAME,
        BUSINESS_DB,
        CHECKPOINT_DB,
        WORKSPACE_FILENAME,
    }
)
_STAGING_DIR_NAMES = frozenset({SOURCE_DIR, RUNS_DIR, BACKUPS_DIR})


def _check_removable_staging(staging: Path, journal: dict, *, completed: bool) -> None:
    """删除暂存目录前逐项核对内容；无法证明归属时整体拒绝且保留原字节。

    只允许初始化自身的已知产物：清单、初始化锁、暂存期准备的文件与
    空的准备目录；完成态只允许清单与锁。有清单哈希的文件同时核对内
    容未被替换。未知文件、符号链接或非空目录都不能删除。
    """
    allowed_files: set[str] = (
        {JOURNAL_FILENAME, INIT_LOCK_FILENAME}
        if completed
        else set(_STAGING_FILE_NAMES)
    )
    hashes = {
        entry["path"]: entry["sha256"]
        for entry in journal["published"]
        if entry["kind"] == "file"
    }
    for child in sorted(staging.iterdir(), key=lambda item: item.name):
        if child.is_symlink():
            raise WorkspaceConflictError(
                f"{staging} 内的 {child.name} 是符号链接，无法证明归属；"
                "整体拒绝，内容保持不变"
            )
        if child.is_dir():
            if child.name not in _STAGING_DIR_NAMES or any(child.iterdir()):
                raise WorkspaceConflictError(
                    f"{staging} 内的目录 {child.name} 不是初始化的空准备目录；"
                    "整体拒绝，内容保持不变"
                )
            continue
        if child.name not in allowed_files:
            raise WorkspaceConflictError(
                f"{staging} 内出现无法证明属于初始化的文件：{child.name}；"
                "整体拒绝，内容保持不变"
            )
        digest = hashes.get(child.name)
        if (
            digest is not None
            and hashlib.sha256(child.read_bytes()).hexdigest() != digest
        ):
            raise WorkspaceConflictError(
                f"{staging} 内的 {child.name} 与清单记录不符，可能已被替换；"
                "整体拒绝，内容保持不变"
            )


def _remove_completed_staging(staging: Path, journal: dict) -> None:
    """在互斥锁内清理完成残留的暂存目录；只删除核对通过的已知产物。"""
    _check_removable_staging(staging, journal, completed=True)
    for child in sorted(staging.iterdir(), key=lambda item: item.name):
        if child.is_dir():
            child.rmdir()
        else:
            child.unlink()
    staging.rmdir()


def _require_complete_journal_for(root: Path, journal: dict) -> None:
    """完成清理的归属核对：状态为 complete 且绑定当前空间。"""
    if journal["state"] != "complete":
        raise WorkspaceConflictError(
            f"{root} 已有完成标记，但 {INIT_STAGING_DIRNAME} 清单状态为"
            f" {journal['state']!r}，不一致，拒绝接管"
        )
    identity = _read_marker(root)
    if journal["space_id"] != identity.space_id:
        raise WorkspaceConflictError(
            f"{INIT_STAGING_DIRNAME} 清单绑定空间 {journal['space_id']!r}，"
            f"与当前空间 {identity.space_id!r} 不一致；整体拒绝，内容保持不变"
        )


def _cleanup_after_completed_init(root: Path) -> None:
    """完成标记已发布时清理残留暂存目录；任何归属疑点都拒绝清理。

    清单先整体校验并与当前空间核对绑定，随后在初始化互斥锁内复查
    并只删除核对通过的已知产物，不与并发初始化竞争清理。
    """
    staging = root / INIT_STAGING_DIRNAME
    if not staging.exists():
        return
    if staging.is_symlink() or not staging.is_dir():
        raise WorkspaceConflictError(
            f"{staging} 不是暂存目录，无法证明归属；整体拒绝，内容保持不变"
        )
    journal = _read_init_journal(staging)
    if journal is None:
        raise WorkspaceConflictError(
            f"{root} 已有完成标记，但 {INIT_STAGING_DIRNAME} 无法证明是"
            "初始化残留（缺少完整清单）；整体拒绝，已有资料保持不变"
        )
    _require_complete_journal_for(root, journal)
    _check_removable_staging(staging, journal, completed=True)
    lock = _staging_lock(staging)
    lock.acquire()
    try:
        current = _read_init_journal(staging)
        if current is None or current != journal:
            raise WorkspaceConflictError(
                f"{INIT_STAGING_DIRNAME} 的清单在等待初始化互斥锁期间发生了"
                "变化；整体拒绝，内容保持不变"
            )
        _remove_completed_staging(staging, journal)
    finally:
        lock.release()


def _restore_root_before_init(root: Path) -> None:
    """处理上次中断的初始化：锁内按清单回滚自身产物，再移除暂存目录。

    清单缺失或损坏的暂存目录无法证明归属，整体拒绝且逐字节保留；
    初始化互斥锁被占用时报告忙，不与并发初始化竞争清理。回滚与移除
    前都先核对暂存内容，未知资料不因重试初始化而被删除。
    """
    staging = root / INIT_STAGING_DIRNAME
    if not staging.exists():
        return
    if staging.is_symlink():
        raise WorkspaceConflictError(
            f"{staging} 是符号链接，无法证明归属；整体拒绝，内容保持不变"
        )
    journal = _read_init_journal(staging)
    if journal is None:
        raise WorkspaceConflictError(
            f"{staging} 缺少初始化清单，无法证明属于本程序；整体拒绝且"
            "逐字节保留，请人工确认后移除该目录再初始化"
        )
    lock = _staging_lock(staging)
    lock.acquire()
    try:
        if (root / WORKSPACE_FILENAME).exists():
            # 等待期间并发初始化已完成：按完成规则只清理暂存目录
            _require_complete_journal_for(root, journal)
            _remove_completed_staging(staging, journal)
            return
        _check_removable_staging(staging, journal, completed=False)
        _rollback_journal(root, journal)
    finally:
        lock.release()
    _remove_tree(staging)


def _remove_tree(path: Path) -> None:
    if not path.exists():
        return
    for child in sorted(path.rglob("*"), reverse=True):
        if child.is_dir():
            child.rmdir()
        else:
            child.unlink()
    path.rmdir()


def initialize_workspace(root: Path, *, config: dict | None = None) -> Workspace:
    """初始化或打开兼容的工作空间；返回可用的空间句柄。"""
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    root = root.resolve()
    if (root / WORKSPACE_FILENAME).exists():
        # 重复初始化同一兼容空间：只读取已有配置，不重复创建
        _cleanup_after_completed_init(root)
        return open_workspace(root)

    _restore_root_before_init(root)
    _check_reserved_conflicts(root)
    _validate_config(config or {})
    return _run_initialization(root, config or {})


def _publish_init_directory(
    root: Path, staging: Path, journal: dict, name: str
) -> None:
    """预写清单后发布目录条目。"""
    _journal_append(staging, journal, {"path": name, "kind": "directory"})
    (root / name).mkdir()
    _fsync_dir(root)


def _publish_init_file(root: Path, staging: Path, journal: dict, name: str) -> None:
    """预写清单（含哈希）后发布文件条目。"""
    source = staging / name
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    _journal_append(staging, journal, {"path": name, "kind": "file", "sha256": digest})
    os.replace(source, root / name)
    _fsync_dir(root)


def _create_init_lock_file(root: Path, staging: Path, journal: dict) -> None:
    """预写清单后创建空间锁文件；清单哈希与其空内容一致。"""
    digest = hashlib.sha256(b"").hexdigest()
    _journal_append(
        staging, journal, {"path": LOCK_FILENAME, "kind": "file", "sha256": digest}
    )
    fd = os.open(root / LOCK_FILENAME, os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o644)
    os.close(fd)
    _fsync_dir(root)


def _run_initialization(root: Path, config: dict) -> Workspace:
    from .store import RecordStore

    staging = root / INIT_STAGING_DIRNAME
    try:
        staging.mkdir()
    except FileExistsError as exc:
        raise WorkspaceBusyError(f"{root} 的初始化正由另一执行者进行") from exc
    lock = _staging_lock(staging)
    lock.acquire()
    try:
        identity = SpaceIdentity(
            space_id=uuid.uuid4().hex,
            format=WORKSPACE_FORMAT,
            format_version=WORKSPACE_FORMAT_VERSION,
            created_at=_now(),
        )
        # 清单自建立之初绑定空间身份：完成清理时据此证明暂存目录归属
        journal: dict = {
            "state": "active",
            "space_id": identity.space_id,
            "published": [],
        }
        _write_init_journal(staging, journal)

        # 1) 在暂存目录准备全部文件；数据库连接必须先关闭并回收
        #    WAL 日志，再作为单个文件发布
        for dirname in (SOURCE_DIR, RUNS_DIR, BACKUPS_DIR):
            (staging / dirname).mkdir()
        (staging / CONFIG_FILENAME).write_text(
            json.dumps(config, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        store = RecordStore(
            staging / BUSINESS_DB, create=True, space_id=identity.space_id
        )
        store.close()
        checkpoint_conn = sqlite3.connect(staging / CHECKPOINT_DB)
        checkpoint_conn.close()

        # 2) 预写清单后逐个发布；锁文件先建立并把身份写入暂存业务库，
        #    使数据库发布时的清单哈希与其最终内容一致
        for dirname in (SOURCE_DIR, RUNS_DIR, BACKUPS_DIR):
            _publish_init_directory(root, staging, journal, dirname)
        _create_init_lock_file(root, staging, journal)
        write_lock_identity(
            staging / BUSINESS_DB, LockIdentity.of(root / LOCK_FILENAME)
        )
        _publish_init_file(root, staging, journal, CONFIG_FILENAME)
        _publish_init_file(root, staging, journal, CHECKPOINT_DB)
        _publish_init_file(root, staging, journal, BUSINESS_DB)

        # 3) 全部条目发布完成：记录完成状态后最后发布空间完成标记
        journal["state"] = "complete"
        _write_init_journal(staging, journal)
        (staging / WORKSPACE_FILENAME).write_bytes(
            json.dumps(identity.marker_payload(), ensure_ascii=False, indent=2).encode()
            + b"\n"
        )
        _publish_init_file(root, staging, journal, WORKSPACE_FILENAME)
    finally:
        lock.release()
    _remove_tree(staging)
    return open_workspace(root)


def open_workspace(root: Path) -> Workspace:
    """显式打开已初始化的工作空间；不隐式初始化或修复未知空间。"""
    root = Path(root).resolve()
    identity = _read_marker(root)
    for name in (BUSINESS_DB, CHECKPOINT_DB):
        if not (root / name).is_file():
            raise WorkspaceIntegrityError(
                f"空间 {root} 缺少数据库文件 {name}，标记与内容不符"
            )
    db_identity = _read_space_id(root / BUSINESS_DB)
    if db_identity != identity.space_id:
        raise WorkspaceIntegrityError(
            f"空间 {identity.space_id} 的标记与业务数据库身份（{db_identity}）不符"
        )
    return Workspace(root, identity)


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
                    f"配置键 {original!r} 含有凭据类字段名（{fragment}）；"
                    "配置只保存认证引用，不保存凭据值"
                )
    try:
        json.dumps(config)
    except (TypeError, ValueError) as exc:
        raise WorkspaceConflictError(f"配置不是可保存的 JSON 内容：{exc}") from exc


def resolve_within(root: Path, relative: str | Path) -> Path:
    """把记录内相对路径解析到根内；越界或符号链接逃逸时拒绝。"""
    rel = Path(relative)
    if rel.is_absolute() or rel.as_posix().startswith(("~", "/")):
        raise WorkspaceIntegrityError(f"文件路径必须是相对路径：{relative}")
    parts = rel.parts
    if any(part in {"", ".."} for part in parts):
        raise WorkspaceIntegrityError(f"文件路径越界：{relative}")
    path = root / rel
    # 逐段检查符号链接，防止中途经链接逃出根目录
    current = root
    for part in parts:
        current = current / part
        if current.is_symlink():
            raise WorkspaceIntegrityError(f"文件路径包含符号链接：{current}")
    resolved = path.resolve()
    root_resolved = root.resolve()
    if resolved != root_resolved and root_resolved not in resolved.parents:
        raise WorkspaceIntegrityError(f"文件路径解析后越出空间根目录：{relative}")
    return path


class Workspace:
    """已打开的工作空间句柄；写操作由调用方在锁内进行。"""

    def __init__(self, root: Path, identity: SpaceIdentity) -> None:
        self.root = root
        self.identity = identity
        self._store: RecordStore | None = None
        self._lock: SpaceLock | None = None

    def lock(self) -> SpaceLock:
        """返回带身份核对的排他锁；占用情况以 flock 为准。

        每次读取数据库记录的锁文件身份，repair_lock 之后立即生效。
        """
        from .locking import read_lock_identity

        expected = read_lock_identity(self.root / BUSINESS_DB)
        return SpaceLock(self.root, expected_identity=expected)

    def store(self) -> RecordStore:
        if self._store is None:
            from .store import RecordStore

            self._store = RecordStore(self.root / BUSINESS_DB, materials_root=self.root)
        return self._store

    def checkpoint_connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.root / CHECKPOINT_DB, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        return conn

    # ---- 配置 ----

    def read_config(self) -> dict:
        return json.loads((self.root / CONFIG_FILENAME).read_text(encoding="utf-8"))

    def update_config(self, changes: dict) -> dict:
        """明确更新配置；凭据类键被拒绝，更新整体写入。"""
        _validate_config(changes)
        with self.lock():
            current = self.read_config()
            merged = {**current, **changes}
            _validate_config(merged)
            _write_file_published(
                self.root / CONFIG_FILENAME,
                (json.dumps(merged, ensure_ascii=False, indent=2) + "\n").encode(),
            )
        return merged

    # ---- 材料与运行文件发布 ----

    def publish_file(self, relative_path: str, data: bytes) -> ArtifactRef:
        """安全发布来源或运行文件；同路径同内容复用，不同内容拒绝覆盖。"""
        rel = Path(relative_path)
        top = rel.parts[0] if rel.parts else ""
        if top not in {SOURCE_DIR, RUNS_DIR}:
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
        """发布来源材料文件到 ``sources/<name>``。"""
        return self.publish_file(f"{SOURCE_DIR}/{name}", data)

    def publish_run_file(
        self, execution_id: str, name: str, data: bytes
    ) -> ArtifactRef:
        """发布运行归档文件到 ``runs/<execution_id>/<name>``。"""
        return self.publish_file(f"{RUNS_DIR}/{execution_id}/{name}", data)

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

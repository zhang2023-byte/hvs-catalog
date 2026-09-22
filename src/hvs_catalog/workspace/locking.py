"""空间排他锁：标准库 flock 非阻塞实现，配合进程内线程重入保护。

占用与否以 ``fcntl.flock`` 的持有状态为准：锁文件存在不代表占用，
删除活跃锁文件也不能解除占用——锁文件身份（inode 与状态变更时间）由
业务数据库的元数据固定，被删除或替换时明确报错，等待显式修复。
稳定锁文件在正常释放时不删除；进程退出后由系统释放持有的锁。
Python 打开的文件描述符默认不可 exec 继承；``fork`` 复制的内存注册表
与描述符在子进程内立即清理（``os.register_at_fork``），清理不等待继
承的普通线程锁——fork 瞬间持锁的线程不存在于子进程，等待会永久卡
死。子进程必须重新执行完整的 flock 检查，不能借用父进程的持有记录
进入临界区，也不会释放父进程仍持有的锁。当前平台为 POSIX（macOS
已验证）；不支持 flock 的平台明确报错，不退化为不加锁。
"""

from __future__ import annotations

import fcntl
import json
import os
import sqlite3
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar, Self

from .errors import LockCorruptedError, WorkspaceBusyError, WorkspaceError

# fcntl 在模块顶部无条件导入：不支持 flock 的平台在导入本模块时即失败，
# 不退化为不加锁。
_FLOCK_AVAILABLE = True

LOCK_META_KEY = "lock_file_identity"


@dataclass(frozen=True)
class LockIdentity:
    """锁文件的稳定身份：inode 与状态变更时间，用于发现文件被替换。"""

    inode: int
    ctime_ns: int

    @classmethod
    def of(cls, path: Path) -> LockIdentity:
        stat = path.stat()
        return cls(inode=stat.st_ino, ctime_ns=stat.st_ctime_ns)

    def as_json(self) -> dict[str, int]:
        return {"inode": self.inode, "ctime_ns": self.ctime_ns}

    @classmethod
    def from_json(cls, payload: dict) -> LockIdentity:
        return cls(inode=int(payload["inode"]), ctime_ns=int(payload["ctime_ns"]))


def read_lock_identity(db_path: Path) -> LockIdentity | None:
    """从业务数据库元数据读取锁文件身份；数据库不可读或未记录时返回 None。"""
    if not db_path.is_file():
        return None
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    except sqlite3.Error:
        return None
    try:
        row = conn.execute(
            "SELECT value FROM meta WHERE key = ?", (LOCK_META_KEY,)
        ).fetchone()
    except sqlite3.Error:
        return None
    finally:
        conn.close()
    if row is None:
        return None
    try:
        return LockIdentity.from_json(json.loads(row[0]))
    except (KeyError, TypeError, ValueError):
        return None


def write_lock_identity(db_path: Path, identity: LockIdentity) -> None:
    """把锁文件身份写入业务数据库元数据（初始化与修复时调用）。"""
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "INSERT INTO meta (key, value) VALUES (?, ?)"
            " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (LOCK_META_KEY, json.dumps(identity.as_json())),
        )
        conn.commit()
    finally:
        conn.close()


@dataclass
class _Holder:
    thread_ident: int
    depth: int
    fd: int


class SpaceLock:
    """一个工作空间的排他锁句柄；同线程可重入，跨线程或跨进程互斥。"""

    _registry: ClassVar[dict[str, _Holder]] = {}
    _registry_guard = threading.Lock()

    def __init__(
        self,
        root: Path,
        *,
        lock_filename: str = ".lock",
        expected_identity: LockIdentity | None = None,
    ) -> None:
        self.root = Path(root)
        self.path = self.root / lock_filename
        self._expected_identity = expected_identity

    @property
    def held_by_current_thread(self) -> bool:
        key = str(self.root)
        with self._registry_guard:
            holder = self._registry.get(key)
            return holder is not None and holder.thread_ident == threading.get_ident()

    def acquire(self) -> None:
        """获取锁；被占用或锁损坏时抛错，不提供隐式等待。"""
        if not _FLOCK_AVAILABLE:  # pragma: no cover - 非 POSIX 平台
            raise WorkspaceError(
                "当前平台不支持 flock，空间锁不可用；已验证平台为 macOS"
            )
        key = str(self.root)
        with self._registry_guard:
            holder = self._registry.get(key)
            if holder is not None:
                if holder.thread_ident != threading.get_ident():
                    raise WorkspaceBusyError(
                        f"空间 {self.root} 的锁由本进程另一线程持有"
                    )
                holder.depth += 1
                return
        self._ensure_lock_file()
        fd = os.open(self.path, os.O_RDWR)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(fd)
            raise WorkspaceBusyError(
                f"空间 {self.root} 正被另一执行者使用（锁被持有）"
            ) from None
        with self._registry_guard:
            self._registry[key] = _Holder(
                thread_ident=threading.get_ident(), depth=1, fd=fd
            )

    def _ensure_lock_file(self) -> None:
        """确保锁文件存在且与数据库记录的身份一致。"""
        if not self.path.exists():
            if self._expected_identity is not None:
                raise LockCorruptedError(
                    f"空间 {self.root} 的锁文件缺失（记录身份 "
                    f"inode={self._expected_identity.inode}）；"
                    "活跃锁不能靠删除或重建文件绕过，"
                    "请确认无执行者后调用 repair_lock 修复"
                )
            fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o644)
            os.close(fd)
            return
        identity = LockIdentity.of(self.path)
        if self._expected_identity is not None and identity != self._expected_identity:
            raise LockCorruptedError(
                f"空间 {self.root} 的锁文件被替换"
                f"（记录身份 inode={self._expected_identity.inode}，"
                f"实际 inode={identity.inode}）；活跃锁不能靠删除或重建文件绕过，"
                "请确认无执行者后调用 repair_lock 修复"
            )

    def release(self) -> None:
        key = str(self.root)
        with self._registry_guard:
            holder = self._registry.get(key)
            if holder is None or holder.thread_ident != threading.get_ident():
                raise WorkspaceError(f"空间 {self.root} 的锁未由当前线程持有")
            holder.depth -= 1
            if holder.depth > 0:
                return
            del self._registry[key]
            fd = holder.fd
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)

    def __enter__(self) -> Self:
        self.acquire()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.release()


def repair_lock(root: Path, *, business_db: Path, lock_filename: str = ".lock") -> None:
    """显式修复锁文件：重建文件并把新身份写入业务数据库。

    调用者必须先确认没有其他执行者持有该空间的锁（例如已停止相关
    进程）；本操作不验证这一点，因此只在人工确认后使用。
    """
    path = Path(root) / lock_filename
    if not path.exists():
        fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
        os.close(fd)
    write_lock_identity(Path(business_db), LockIdentity.of(path))


def _after_fork_in_child() -> None:
    """fork 后清理子进程继承的持锁记录与描述符。

    flock 状态依附于打开文件描述，父进程与子进程共享同一把锁；子进程
    清掉注册表并关闭继承的描述符副本后，必须重新走完整的 flock 检查
    （父进程持锁时得到忙），也不会误释放父进程仍持有的锁。

    回调不能获取继承的 ``_registry_guard``：fork 瞬间持锁的其他线程
    不会出现在子进程，锁的副本永远处于锁定状态，等待即死锁。fork 返
    回后子进程只有发起线程存活，直接重置注册表并换一把新锁即可。
    """
    holders = list(SpaceLock._registry.values())
    SpaceLock._registry.clear()
    SpaceLock._registry_guard = threading.Lock()
    for holder in holders:
        try:
            os.close(holder.fd)
        except OSError:  # pragma: no cover - 描述符已被意外关闭
            pass


os.register_at_fork(after_in_child=_after_fork_in_child)

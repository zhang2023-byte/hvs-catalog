"""POSIX 非阻塞 flock；稳定锁文件、同线程重入与 fork/exec 清理。

可信操作者不得删除或替换运行中的锁文件；正常释放不删除文件。
不支持 flock 的平台导入失败，不退化成不加锁。
"""

from __future__ import annotations

import fcntl
import os
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar, Self

from .errors import WorkspaceBusyError, WorkspaceError


@dataclass
class _Holder:
    thread_ident: int
    depth: int
    fd: int


class SpaceLock:
    """同空间跨进程／线程互斥；同线程可重入。"""

    _registry: ClassVar[dict[str, _Holder]] = {}
    _registry_guard = threading.Lock()
    # 覆盖尚未登记 holder 的在途 fd。fork 只与 open/登记、close/移除
    # 的短临界区协调，不等待 registry guard 或在途 flock。
    _descriptors: ClassVar[set[int]] = set()
    _descriptor_guard = threading.Lock()

    def __init__(self, root: Path, *, lock_filename: str = ".lock") -> None:
        self.root = Path(root).resolve()
        self.path = self.root / lock_filename

    @property
    def held_by_current_thread(self) -> bool:
        with self._registry_guard:
            holder = self._registry.get(str(self.path))
            return holder is not None and holder.thread_ident == threading.get_ident()

    def acquire(self) -> None:
        # 在同一注册表临界区完成 flock 与登记，避免线程借用尚未登记的锁。
        with self._registry_guard:
            key = str(self.path)
            holder = self._registry.get(key)
            if holder is not None:
                if holder.thread_ident != threading.get_ident():
                    raise WorkspaceBusyError(
                        f"空间 {self.root} 的锁由本进程另一线程持有"
                    )
                holder.depth += 1
                return
            with self._descriptor_guard:
                fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o644)
                self._descriptors.add(fd)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                self._registry[key] = _Holder(threading.get_ident(), 1, fd)
            except BaseException as exc:
                self._close_descriptor(fd)
                if isinstance(exc, OSError):
                    raise WorkspaceBusyError(
                        f"空间 {self.root} 正被另一执行者使用（锁被持有）"
                    ) from None
                raise

    def release(self) -> None:
        with self._registry_guard:
            key = str(self.path)
            holder = self._registry.get(key)
            if holder is None or holder.thread_ident != threading.get_ident():
                raise WorkspaceError(f"空间 {self.root} 的锁未由当前线程持有")
            holder.depth -= 1
            if holder.depth > 0:
                return
            try:
                fcntl.flock(holder.fd, fcntl.LOCK_UN)
            finally:
                self._close_descriptor(holder.fd)
                del self._registry[key]

    @classmethod
    def _close_descriptor(cls, fd: int) -> None:
        # fork 不能看到已 close、尚未移除的号码，否则可能误关复用 fd。
        with cls._descriptor_guard:
            try:
                os.close(fd)
            finally:
                cls._descriptors.remove(fd)

    def __enter__(self) -> Self:
        self.acquire()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.release()


def _before_fork() -> None:
    SpaceLock._descriptor_guard.acquire()


def _after_fork_in_parent() -> None:
    SpaceLock._descriptor_guard.release()


def _after_fork_in_child() -> None:
    # before 回调已排除 open/登记、close/移除的中间态。子回调不等待
    # 继承的线程锁，只关闭自己继承的 fd 副本，不 LOCK_UN 父进程。
    for fd in SpaceLock._descriptors:
        try:
            os.close(fd)
        except OSError:  # pragma: no cover
            pass
    SpaceLock._descriptors.clear()
    SpaceLock._registry.clear()
    SpaceLock._registry_guard = threading.Lock()
    SpaceLock._descriptor_guard = threading.Lock()


os.register_at_fork(
    before=_before_fork,
    after_in_parent=_after_fork_in_parent,
    after_in_child=_after_fork_in_child,
)

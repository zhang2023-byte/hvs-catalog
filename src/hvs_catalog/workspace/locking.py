"""POSIX nonblocking flock with stable files, same-thread reentry, and fork/exec cleanup.

Trusted operators must not delete/replace active lock files; normal release retains
them. Platforms lacking flock fail import rather than running unlocked.
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
    """Exclude other workspace processes/threads while allowing same-thread reentry."""

    _registry: ClassVar[dict[str, _Holder]] = {}
    _registry_guard = threading.Lock()
    # Include in-flight descriptors without registered holders. fork coordinates only brief open/register
    # and close/remove sections, never waiting for registry guards or in-flight flock.
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
        # flock and registration share one registry section so threads cannot borrow unregistered locks.
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
        # fork cannot observe closed-but-not-removed numbers and accidentally close reused descriptors.
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
    # Before-fork callbacks exclude open/register and close/remove intermediate states. Child callbacks never wait
    # on inherited thread locks; close child descriptor copies without unlocking parent locks.
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

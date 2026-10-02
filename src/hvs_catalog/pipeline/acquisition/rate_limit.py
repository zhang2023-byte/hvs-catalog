"""Cross-process shared arXiv throttle gate for engineering state (local C07).

Processes/workspaces of one OS user share local state, defaulting to user cache outside
workspaces. Acquire an independent stable .lock file before data
existence/read/initialization checks. Under that lock, temporary-file/fsync/atomic
replacement exposes only complete state, preventing first-creation races; replacing the
data inode leaves lock coordination intact.

transfer atomically persists in-flight markers and spacing preregistration before
yielding an exclusive connection lease, retaining the lock through HTTP requests,
response reads, and error closure. Finalization atomically clears markers and records
completion time plus interval, no earlier than actual send. Thus local connections never
overlap and actual send spacing respects MIN_REQUEST_INTERVAL, default/minimum three
seconds. Wait in small unlocked steps and reread newly registered cooldown. Crashes or
failed finalization leave markers that make successors fail closed with
RateLimitUnconfirmedError, regardless of expired potentially early preregistration. Slow
writes can delay but never shorten actual spacing.

Persist Retry-After within leases without nested locks, or independently outside
transfer; cooldown survives restart. Corrupt/empty/invalid-version/type state fails
closed with RateLimitStateError and preserves the scene; reset also rejects because
cooldown is unknown, not zero. Manual recovery requires coordinating all local arXiv
processes to stop and verifying actual server waits before deleting/repairing state. No
invented safe fixed wait or force-clear flag bypasses valid waits. State describes
request timing only, not scientific records/provenance. Operators coordinate aggregate
requests across machines.
"""

from __future__ import annotations

import json
import math
import os
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

#: Minimum adjacent-request interval in seconds: both default and production floor.
MIN_REQUEST_INTERVAL = 3.0

#: State format version; invalid types/values count as corruption and block sends.
#: Version 2 adds optional in_flight_since markers; older code rejects the new version
#: instead of ignoring markers. New code likewise rejects v1 because unconfirmed requests
#: cannot be ruled out in old state.
STATE_VERSION = 2

#: Unlocked segmented-wait recheck interval in seconds; reread state each step.
WAIT_RECHECK_SECONDS = 1.0

#: Lock-contention polling interval in seconds for bounded waits.
LOCK_WAIT_POLL_SECONDS = 0.05

_STATE_FILE_NAME = "arxiv-rate-gate.json"
_CACHE_DIR_NAME = "hvs-catalog"
_WINDOWS_LOCK_POLL_SECONDS = 0.05

try:  # pragma: no cover - platform-dependent import
    import fcntl
except ImportError:  # pragma: no cover - Windows
    fcntl = None  # type: ignore[assignment]

try:  # pragma: no cover - platform-dependent import
    import msvcrt
except ImportError:  # pragma: no cover - POSIX
    msvcrt = None  # type: ignore[assignment]


class Clock(Protocol):
    """Injectable epoch-seconds clock with sleeping support.

    Optional monotonic() is used only for wait-budget deadlines unaffected by
    system-clock changes. Persist timestamps from epoch now() exclusively. Fall back to
    now() when monotonic is absent, as for fake clocks.
    """

    def now(self) -> float: ...

    def sleep(self, seconds: float) -> None: ...


class SystemClock:
    """Production wall clock; interrupted sleep completes the remaining duration."""

    def now(self) -> float:
        return time.time()

    def monotonic(self) -> float:
        """Monotonic readings for wait budgets only, unaffected by wall-clock changes."""
        return time.monotonic()

    def sleep(self, seconds: float) -> None:
        remaining = max(0.0, float(seconds))
        deadline = time.monotonic() + remaining
        while remaining > 0:
            time.sleep(remaining)
            remaining = deadline - time.monotonic()


def default_rate_gate_state_path() -> Path:
    """Compute the default throttle-state path in user cache, independent of workspaces.

    HVS_CATALOG_ARXIV_RATE_STATE overrides it with a complete file path, for example in
    isolated tests or restricted home directories. Compute only without creating files;
    imports have no disk/network effects.
    """
    override = os.environ.get("HVS_CATALOG_ARXIV_RATE_STATE")
    if override:
        return Path(override)
    home = Path.home()
    if sys.platform == "darwin":
        base = home / "Library" / "Caches"
    elif sys.platform == "win32":  # pragma: no cover - untested platform
        local = os.environ.get("LOCALAPPDATA", str(home / "AppData" / "Local"))
        return Path(local) / _CACHE_DIR_NAME / "Cache" / _STATE_FILE_NAME
    else:
        base = Path(os.environ.get("XDG_CACHE_HOME", str(home / ".cache")))
    return base / _CACHE_DIR_NAME / _STATE_FILE_NAME


class RateLimitError(RuntimeError):
    """Throttle gate rejects the operation, such as resetting before spacing/cooldown
    expiry.
    """

    def __init__(self, message: str, *, wait_seconds: float | None = None) -> None:
        super().__init__(message)
        self.wait_seconds = wait_seconds


class RateLimitStateError(RuntimeError):
    """Corrupt/empty/invalid-version/type throttle state blocks sending and preserves the
    scene.

    Never automatically reset or overwrite. reset also rejects because cooldown is
    unknown, not zero. Before any further request, operators must coordinate all local
    arXiv processes to stop, verify actual server waits, then manually delete/repair
    state.
    """


class RateLimitUnconfirmedError(RateLimitError):
    """Unconfirmed in-flight marker blocks sending.

    The prior exchange may be unfinished, crashed, or failed during finalization.
    Expired next_allowed_at cannot establish actual three-second spacing, so waiting
    cannot bypass markers and no automatic TTL/reset applies. Coordinate local processes
    to stop, verify sends, then manually delete/repair state as for corruption.
    """


class RateLimitPersistenceError(RateLimitError):
    """Exchange finished but finalization could not persist reliably; retain the marker
    conservatively.

    Later requests fail closed until manual recovery. Preserve original
    transport/persistence exceptions through __cause__ for diagnosis.
    """


@dataclass(frozen=True)
class RateGateSnapshot:
    """Read-only throttle snapshot for status/operator diagnosis.

    Non-None state_error means corrupt state with meaningless remaining fields and
    denied sending pending explicit recovery. Non-None in_flight_since means an
    unconfirmed request likewise blocks sends without wait bypass. Never rewrite
    snapshot state.
    """

    next_allowed_at: float | None
    cooldown_until: float | None
    updated_at: float | None
    state_error: str | None = None
    in_flight_since: float | None = None


def _is_number(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _finite_or_zero(value: object) -> float:
    if not _is_number(value):
        return 0.0
    number = float(value)
    return number if math.isfinite(number) else 0.0


def _effective_base(state: dict) -> float:
    """Earliest next-request time: the later of ordinary spacing and server cooldown."""
    return max(
        _finite_or_zero(state.get("next_allowed_at")),
        _finite_or_zero(state.get("cooldown_until")),
    )


def _lock_file(fd: int) -> None:
    if fcntl is not None:
        fcntl.flock(fd, fcntl.LOCK_EX)
    elif msvcrt is not None:  # pragma: no cover - untested platform
        while True:
            try:
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                return
            except OSError:
                time.sleep(_WINDOWS_LOCK_POLL_SECONDS)
    else:  # pragma: no cover - theoretically unreachable
        raise RuntimeError("当前平台没有可用的文件锁实现")


def _try_lock_file(fd: int) -> bool:
    """Try locking nonblockingly; return true on success and false on contention."""
    if fcntl is not None:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except OSError:
            return False
    if msvcrt is not None:  # pragma: no cover - untested platform
        try:
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            return True
        except OSError:
            return False
    raise RuntimeError("当前平台没有可用的文件锁实现")  # pragma: no cover


def _unlock_file(fd: int) -> None:
    if fcntl is not None:
        fcntl.flock(fd, fcntl.LOCK_UN)
    elif msvcrt is not None:  # pragma: no cover - untested platform
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)


class TransferLease:
    """Exclusive HTTP connection lease issued by RateLimitGate.transfer.

    No other local same-user arXiv connection can exist during the lease, including
    transport/error closure. record_cooldown writes under the existing lock without
    nesting. Calling it after lease completion is a programming error.
    """

    def __init__(self, gate: RateLimitGate, state: dict, granted_at: float) -> None:
        self._gate = gate
        self._state = state
        self.granted_at = granted_at
        self._active = True

    def record_cooldown(self, until: float) -> None:
        """Persist server cooldown inside the lease, retaining the later deadline."""
        if not self._active:
            raise RuntimeError("连接租约已结束，不能再登记冷却")
        value = float(until)
        if not math.isfinite(value):
            raise ValueError(f"冷却时刻必须是有限数值：{until!r}")
        current = self._state.get("cooldown_until")
        merged = value if not _is_number(current) else max(float(current), value)
        self._state["cooldown_until"] = merged
        self._state["updated_at"] = self._gate.clock.now()
        self._gate._write_state_file(self._state)


class RateLimitGate:
    """Cross-process shared arXiv throttle gate.

    state_path should be identical for all processes of one user. min_interval is in
    seconds and cannot be below MIN_REQUEST_INTERVAL. clock is injectable for tests and
    defaults to SystemClock in production.
    """

    def __init__(
        self,
        state_path: str | os.PathLike[str],
        *,
        min_interval: float = MIN_REQUEST_INTERVAL,
        clock: Clock | None = None,
    ) -> None:
        interval = float(min_interval)
        if not math.isfinite(interval) or interval < MIN_REQUEST_INTERVAL:
            raise ValueError(
                f"请求间隔不能低于生产行为下限 {MIN_REQUEST_INTERVAL} 秒："
                f"{min_interval!r}；测试应注入时钟而不是缩小间隔"
            )
        self.state_path = Path(state_path)
        self.min_interval = interval
        self.clock: Clock = clock if clock is not None else SystemClock()

    # -- Internal stable lock and atomically replaced data ----------------------

    def _lock_path(self) -> Path:
        """Stable coordination lock, independent of the replaceable data file."""
        return self.state_path.with_name(self.state_path.name + ".lock")

    def _open_lock(self) -> int:
        lock_path = self._lock_path()
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        _lock_file(fd)
        return fd

    def _open_lock_fd(self) -> int:
        """Open the lock file without acquiring, for bounded acquisition."""
        lock_path = self._lock_path()
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        return os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)

    def _monotonic_now(self) -> float:
        """Read monotonic budget time, falling back to now() for injected clocks lacking
        monotonic. Persisted timestamps always use epoch now(), never mixed clocks.
        """
        monotonic = getattr(self.clock, "monotonic", None)
        if callable(monotonic):
            return float(monotonic())
        return self.clock.now()

    def _acquire_lock(self, fd: int, *, deadline: float | None) -> None:
        """Acquire the coordination lock: block without a budget, otherwise poll
        nonblockingly against one fixed deadline. Timeout raises RateLimitError without
        ownership/state changes.

        Recheck after successful acquisition too: scheduling oversleep may grant the
        lock after deadline. Release and reject late locks; only strictly late times
        reject, while equality is accepted.
        """
        if deadline is None:
            _lock_file(fd)
            return
        while not _try_lock_file(fd):
            budget_left = deadline - self._monotonic_now()
            if budget_left <= 0:
                raise RateLimitError(
                    "在等待预算内未能取得协调锁（另一传输仍在持锁）；"
                    "未占用许可，未改动状态",
                    wait_seconds=0.0,
                )
            self.clock.sleep(min(budget_left, LOCK_WAIT_POLL_SECONDS))
        if self._monotonic_now() > deadline:
            # On late acquisition due to scheduling/release delay, release the new lock and reject.
            _unlock_file(fd)
            raise RateLimitError(
                "取得协调锁时已超出本次调用总等待预算（迟到的调度或释放）；"
                "未占用许可，未改动状态",
                wait_seconds=0.0,
            )

    def _read_state_file(self) -> dict | None:
        """Read/validate data under the lock; missing files return None as fresh state.

        Atomic locked writes mean observed empty/malformed contents reflect actual prior
        corruption or manual damage and must be treated accordingly.
        """
        if not self.state_path.exists():
            return None
        try:
            raw = self.state_path.read_bytes()
        except OSError as exc:
            raise RateLimitStateError(
                f"节流状态无法读取，已拒绝发送并保留现场：{self.state_path}（{exc}）"
            ) from exc
        if not raw:
            raise RateLimitStateError(
                f"节流状态文件为空（遗留损坏），已拒绝发送并保留现场：{self.state_path}"
            )
        try:
            state = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            raise RateLimitStateError(
                f"节流状态损坏，已拒绝发送并保留现场：{self.state_path}（{exc}）"
            ) from exc
        if not isinstance(state, dict):
            raise RateLimitStateError(
                f"节流状态不是对象，已拒绝发送并保留现场：{self.state_path}"
            )
        # Require the exact int type, excluding bool/float/str/None/containers, and current version.
        # Python True == 1 and 1.0 == 1, so equality alone cannot reject
        # these malformed types.
        version = state.get("version")
        if isinstance(version, bool) or not isinstance(version, int):
            raise RateLimitStateError(
                f"节流状态版本类型非法（{type(version).__name__}），"
                f"已拒绝发送并保留现场：{self.state_path}"
            )
        if version != STATE_VERSION:
            raise RateLimitStateError(
                f"节流状态版本不兼容（{version} != {STATE_VERSION}），"
                f"已拒绝发送并保留现场：{self.state_path}"
            )
        for key in (
            "next_allowed_at",
            "cooldown_until",
            "updated_at",
            "in_flight_since",
        ):
            value = state.get(key)
            if value is None:
                continue
            if not _is_number(value) or not math.isfinite(float(value)):
                raise RateLimitStateError(
                    f"节流状态字段 {key} 非法（{value!r}），已拒绝发送并保留现场："
                    f"{self.state_path}"
                )
        return state

    def _write_state_file(self, state: dict) -> None:
        """Atomically replace data under the lock so observers see only complete contents."""
        payload = json.dumps({"version": STATE_VERSION, **state}).encode("utf-8")
        tmp_path = self.state_path.with_name(self.state_path.name + ".tmp")
        fd = os.open(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            view = memoryview(payload)
            while view:
                written = os.write(fd, view)
                view = view[written:]
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(tmp_path, self.state_path)
        try:
            dir_fd = os.open(self.state_path.parent, os.O_RDONLY)
        except OSError:
            return  # pragma: no cover - replacement is complete before directory-open failure
        try:
            os.fsync(dir_fd)
        except OSError:
            pass  # pragma: no cover - some filesystems lack directory fsync
        finally:
            os.close(dir_fd)

    # -- Connection leases --------------------------------------------------

    def _check_admission_deadline(
        self, max_wait: float | None, deadline: float | None, *, stage: str
    ) -> None:
        """After blocking write/fsync, reject positive-budget lease delivery if late,
        without yielding/sending. Retain persisted preregistration/cooldown
        conservatively. Zero-budget nonblocking attempts tolerate intrinsic I/O
        duration; unlimited mode has no deadline.
        """
        if max_wait is None or max_wait <= 0:
            return
        if self._monotonic_now() > deadline:
            raise RateLimitError(
                f"{stage}已超出本次调用总等待预算（迟到的状态写入或 fsync）；"
                "保守保留已持久化的预登记与冷却，未签发租约、未发送",
                wait_seconds=0.0,
            )

    def _complete_exchange(self, state: dict) -> None:
        """Perform the sole lease-finalization write while retaining the lock.

        One atomic replacement clears markers and sets next admission to completion time
        plus interval, no earlier than actual send and preserving three-second spacing.
        Retain cooldown/other fields. Write failures leave markers for fail-closed
        successors and propagate through exception chains without silent suppression.
        """
        completed = self.clock.now()
        state.pop("in_flight_since", None)
        current = state.get("next_allowed_at")
        target = completed + self.min_interval
        if not _is_number(current) or target > float(current):
            state["next_allowed_at"] = target
        state["updated_at"] = completed
        self._write_state_file(state)

    @contextmanager
    def transfer(self, *, max_wait: float | None = None) -> Iterator[TransferLease]:
        """Acquire an exclusive HTTP exchange lease.

        max_wait is one total admission budget with a monotonic deadline fixed before
        initial locking, never reset. Initial/reacquired locks and all spacing/cooldown
        reads share it. Check both required wait and remaining budget; reject excess
        without a permit, next_allowed_at changes, or discarded cooldown. Repeated
        extensions cannot create unlimited waiting. Bound each sleep by remaining budget
        and accept exact-deadline readiness.

        Recheck after lock acquisition, state reads before writing, and blocking
        marker/preregistration write/fsync. Scheduling, release, read, and write delays
        cannot deliver positive-budget leases past deadline. Release locks and never
        yield/send on rejection, conservatively retaining persisted
        markers/preregistration even before sending to avoid uncertain writes.

        Before delivery, atomically persist in_flight_since and spacing preregistration.
        Successors fail closed on markers regardless of expired spacing or budgets,
        without automatic TTL/reset; recovery requires coordination.
        Normal/error/transport exits finalize atomically before unlocking: clear markers
        and set completion-plus-interval spacing. Failed finalization raises
        RateLimitPersistenceError with original __cause__ and retains markers. Crashes
        before marker writes leave unchanged state/no sends; afterward, even before
        sends, successors fail closed. Failed replacement retains marked old state;
        post-replacement directory fsync failure leaves visible new completion spacing.
        Neither outcome loses both marker and safe spacing for sent requests. No
        hard-real-time scheduling or transactional rollback is claimed.

        max_wait=None explicitly permits blocking/unlimited waits. max_wait=0 attempts
        nonblockingly, rejecting lock/cooldown waits immediately but tolerating
        intrinsic CPU/I/O time, without promising zero duration or adding
        positive-budget grace.

        Acquire stable lock within budget, wait/recheck in unlocked steps, atomically
        preregister on admission, hold through exchange/error closure, then finalize and
        release. The OS releases crash-held locks while persistent markers require
        manual recovery.
        """
        if max_wait is not None:
            max_wait = float(max_wait)
            if not math.isfinite(max_wait) or max_wait < 0:
                raise ValueError(f"max_wait 必须是非负有限数值或 None：{max_wait!r}")
        # Fix the total-budget deadline before initial acquisition; never reset it afterward.
        deadline = None if max_wait is None else self._monotonic_now() + max_wait
        fd = self._open_lock_fd()
        try:
            if max_wait is None:
                self._acquire_lock(fd, deadline=None)
            elif max_wait > 0:
                self._acquire_lock(fd, deadline=deadline)
            elif not _try_lock_file(fd):
                # max_wait == 0 attempts nonblockingly and rejects lock contention without polling.
                raise RateLimitError(
                    "零预算（非阻塞尝试）下协调锁被占用；未占用许可，未改动状态",
                    wait_seconds=0.0,
                )
            try:
                while True:
                    state = self._read_state_file()
                    if state is None:
                        state = {}
                    marker = state.get("in_flight_since")
                    if marker is not None:
                        # Unconfirmed in-flight requests fail closed regardless of expired next_allowed_at;
                        # waiting cannot bypass markers and no automatic TTL applies.
                        raise RateLimitUnconfirmedError(
                            f"检测到未确认的在途请求（in_flight_since="
                            f"{float(marker):.3f}）：前一交换可能未完成或收尾"
                            "失败，无法保证实际发送间距；已拒绝发送，需人工"
                            "检查状态文件后恢复"
                        )
                    now = self.clock.now()
                    base = _effective_base(state)
                    if base <= now:
                        # After ready-state reads and before permit writes, recheck the same deadline:
                        # delayed reads/scheduling cannot admit positive-budget leases. Zero-budget
                        # nonblocking attempts tolerate state I/O duration and omit this check.
                        if (
                            max_wait is not None
                            and max_wait > 0
                            and self._monotonic_now() > deadline
                        ):
                            raise RateLimitError(
                                "状态已就绪，但签发许可前已超出本次调用总"
                                "等待预算（迟到的状态读取或调度）；未占用"
                                "许可，未改动状态",
                                wait_seconds=0.0,
                            )
                        break
                    required = base - now
                    budget_left = (
                        None if deadline is None else deadline - self._monotonic_now()
                    )
                    if budget_left is not None:
                        if required > budget_left:
                            # Exhausted budget or excessive required wait rejects without permits
                            # or changes to persisted constraints.
                            raise RateLimitError(
                                f"节流门要求再等待 {required:.3f} 秒，累计已超过本次"
                                f"调用总等待预算 {max_wait:.3f} 秒（剩余 "
                                f"{max(budget_left, 0.0):.3f}）；冷却已持久化，"
                                "请稍后重试",
                                wait_seconds=required,
                            )
                        step = min(required, WAIT_RECHECK_SECONDS, budget_left)
                    else:
                        step = min(required, WAIT_RECHECK_SECONDS)
                    _unlock_file(fd)
                    try:
                        self.clock.sleep(step)
                    finally:
                        self._acquire_lock(fd, deadline=deadline)
                granted = self.clock.now()
                # Before delivery, atomically persist the identifiable in-flight marker and preregistration.
                # Successors fail closed on markers even when pre-write-based next_allowed_at
                # has already expired.
                state["in_flight_since"] = granted
                state["next_allowed_at"] = granted + self.min_interval
                state["updated_at"] = granted
                self._write_state_file(state)
                # Blocking write/fsync belongs to positive-budget admission; if late, never yield/send.
                # Retain persisted markers/preregistration conservatively even without a send,
                # avoiding a second uncertain write to clear markers.
                self._check_admission_deadline(
                    max_wait, deadline, stage="在途标记与许可预登记写入后"
                )
                lease = TransferLease(self, state, granted)
                body_error: BaseException | None = None
                try:
                    yield lease
                except BaseException as exc:
                    body_error = exc
                    raise
                finally:
                    lease._active = False
                    # Finalization must reliably persist both marker clearing and completion spacing;
                    # never silently suppress failure or clear separately through an unsafe write.
                    # Preserve original exceptions through __cause__ for diagnosis.
                    try:
                        self._complete_exchange(state)
                    except OSError as persist_error:
                        raise RateLimitPersistenceError(
                            "交换结束但收尾状态（清除在途标记并保存完成式"
                            "间隔）未能可靠落盘；在途标记保守保留，后续请求"
                            "将拒绝发送直至人工恢复",
                        ) from (body_error if body_error is not None else persist_error)
            finally:
                _unlock_file(fd)
        finally:
            os.close(fd)

    # -- Independent operations outside transfer ----------------------------------------

    def record_cooldown(self, until: float) -> None:
        """Register server cooldown outside transfer, denying requests before epoch until.

        Corrupt state still fails closed. During transfer use
        TransferLease.record_cooldown; nesting this method deadlocks with the active
        lease.
        """
        value = float(until)
        if not math.isfinite(value):
            raise ValueError(f"冷却时刻必须是有限数值：{until!r}")
        fd = self._open_lock()
        try:
            state = self._read_state_file()
            if state is None:
                state = {}
            current = state.get("cooldown_until")
            merged = value if not _is_number(current) else max(float(current), value)
            state["cooldown_until"] = merged
            state["updated_at"] = self.clock.now()
            self._write_state_file(state)
        finally:
            _unlock_file(fd)
            os.close(fd)

    def snapshot(self) -> RateGateSnapshot:
        """Read-only snapshot; state_error explains corruption without rewriting the scene."""
        fd = self._open_lock()
        try:
            try:
                state = self._read_state_file()
            except RateLimitStateError as exc:
                return RateGateSnapshot(
                    next_allowed_at=None,
                    cooldown_until=None,
                    updated_at=None,
                    state_error=str(exc),
                )
        finally:
            _unlock_file(fd)
            os.close(fd)
        if state is None:
            return RateGateSnapshot(
                next_allowed_at=None,
                cooldown_until=None,
                updated_at=None,
                state_error=None,
            )

        def pick(key: str) -> float | None:
            value = state.get(key)
            return float(value) if _is_number(value) else None

        return RateGateSnapshot(
            next_allowed_at=pick("next_allowed_at"),
            cooldown_until=pick("cooldown_until"),
            updated_at=pick("updated_at"),
            state_error=None,
            in_flight_since=pick("in_flight_since"),
        )

    def reset(self) -> None:
        """Explicit recovery check: clear only valid state whose spacing/cooldown both
        expired.

        Reject unexpired next_allowed_at/cooldown_until with RateLimitError; no
        force-clear flag bypasses production/server waits. Corruption means unknown
        cooldown and raises RateLimitStateError while preserving bytes. Manual
        repair/deletion requires coordinated stopping of local processes and verified
        actual server waits; invent no guaranteed safe fixed wait. After deletion
        spacing starts from fresh state. Valid fully expired or absent state can clear
        to fresh.
        """
        fd = self._open_lock()
        try:
            state = self._read_state_file()
            if state is not None:
                marker = state.get("in_flight_since")
                if marker is not None:
                    # Recovery cannot clear or bypass an unconfirmed in-flight request.
                    raise RateLimitUnconfirmedError(
                        "状态中存在未确认的在途请求（in_flight_since="
                        f"{float(marker):.3f}）；恢复操作不能清除或绕过未确"
                        "认状态，需人工核实后处理状态文件"
                    )
                now = self.clock.now()
                pending: list[tuple[str, float]] = []
                for key in ("next_allowed_at", "cooldown_until"):
                    value = state.get(key)
                    if _is_number(value) and float(value) > now:
                        pending.append((key, float(value)))
                if pending:
                    latest = max(value for _, value in pending)
                    names = "、".join(key for key, _ in pending)
                    raise RateLimitError(
                        f"状态中存在未到期的 {names}（最近至 {latest:.3f}）；"
                        "恢复操作不能绕过生产间隔或服务端等待，请待其自然过期",
                        wait_seconds=latest - now,
                    )
            self._write_state_file({})
        finally:
            _unlock_file(fd)
            os.close(fd)


__all__ = [
    "LOCK_WAIT_POLL_SECONDS",
    "MIN_REQUEST_INTERVAL",
    "STATE_VERSION",
    "WAIT_RECHECK_SECONDS",
    "Clock",
    "RateGateSnapshot",
    "RateLimitError",
    "RateLimitGate",
    "RateLimitPersistenceError",
    "RateLimitStateError",
    "RateLimitUnconfirmedError",
    "SystemClock",
    "TransferLease",
    "default_rate_gate_state_path",
]

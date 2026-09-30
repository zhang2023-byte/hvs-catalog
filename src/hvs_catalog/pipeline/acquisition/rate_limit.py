"""arXiv 请求的跨进程共享节流门（仅工程状态，C07 的本机部分）。

同一操作系统用户在本机启动的多个进程、多个工作空间共用一份节流状
态文件（默认位于用户缓存目录，不在任何工作空间内）。

**协调与初始化**：协调使用独立、稳定的锁文件（``<状态文件>.lock``），
在任何数据文件的存在性检查、读取或初始化**之前**取得。数据文件只在
持锁下经"临时文件 + fsync + 原子替换"写入，因此其他进程要么看不到
数据文件、要么看到完整内容——首次并发创建不会把合法初始化误判为损
坏。锁文件与被替换的数据文件是不同 inode，替换不影响协调。

**连接租约**：:meth:`RateLimitGate.transfer` 在锁内读取状态、先
同一次原子写入持久化**在途标记**（``in_flight_since``）与间距预
登记，然后持有锁直到一次 HTTP 交换（请求、响应读取、错误关闭）
完成，收尾时再以单次原子写入清除标记并保存**完成式间隔**（完成
时刻 + 间隔，不早于实际发送）。因此本机同用户任意时刻至多一个
arXiv 连接，相邻实际发送间隔不小于 :data:`MIN_REQUEST_INTERVAL`
（默认 3 秒，也是生产行为下限，构造时传入更小值会被拒绝）。等待
（间距或服务端冷却）以小步长在锁外进行、每步重读状态：等待期间
其他进程登记的新冷却会在下一次重读时生效。**崩溃安全（在途标
记）**：交换中崩溃或收尾写入失败时，已落盘的标记使任何后来者
fail-closed（:class:`RateLimitUnconfirmedError`）——不因可能早于
实际发送的预登记时刻已过而放行；恢复需人工协调（见下）。写入延
迟只可能使后续请求更晚，不会缩短实际间距。

**服务端冷却**（``Retry-After``）经 :meth:`TransferLease.record_
cooldown` 在租约内直接落盘（无嵌套锁），进程重启后仍生效；也可用
独立的 :meth:`RateLimitGate.record_cooldown` 在传输之外登记。

**损坏状态 fail-closed**：数据文件损坏、为空、版本或字段类型非法
时**拒绝发送**（:class:`RateLimitStateError`），不覆盖现场。损坏状
态中的冷却是**未知**而非零：默认恢复（:meth:`RateLimitGate.reset`）
同样拒绝。人工恢复需要操作者先协调停止本机所有 arXiv 请求进程、
核实服务端实际等待条件后，再手动删除或修复状态文件；本模块不发明
保证安全的固定等待，也不提供绕过有效服务端等待的强制清除标志。
节流状态只描述"下一次允许请求的时刻"，属于工程状态：不是科学记
录或来源溯源的一部分。本门只协调本机同一用户的进程；跨机器运行的
总请求量由操作者负责（见 workflow 设计）。
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

#: 相邻请求的最小间隔（秒）。既是默认值，也是生产行为下限。
MIN_REQUEST_INTERVAL = 3.0

#: 状态文件的格式版本；类型或数值不匹配都按损坏处理（拒绝发送）。
#: 版本 2 增加可选的 ``in_flight_since`` 在途标记：旧代码读到新版本
#: 会因版本不匹配 fail-closed（不会静默忽略标记）；新代码同样拒绝
#: 版本 1 的旧状态（无法确认其中是否曾存在未确认请求）。
STATE_VERSION = 2

#: 等待期间的重新核对步长（秒）：等待在锁外分段进行，每步重读状态。
WAIT_RECHECK_SECONDS = 1.0

#: 有界等待模式下竞争锁的轮询步长（秒）。
LOCK_WAIT_POLL_SECONDS = 0.05

_STATE_FILE_NAME = "arxiv-rate-gate.json"
_CACHE_DIR_NAME = "hvs-catalog"
_WINDOWS_LOCK_POLL_SECONDS = 0.05

try:  # pragma: no cover - 平台相关导入
    import fcntl
except ImportError:  # pragma: no cover - Windows
    fcntl = None  # type: ignore[assignment]

try:  # pragma: no cover - 平台相关导入
    import msvcrt
except ImportError:  # pragma: no cover - POSIX
    msvcrt = None  # type: ignore[assignment]


class Clock(Protocol):
    """可注入时钟：读取当前纪元秒并支持等待。

    可选提供 ``monotonic()``（不受系统改时影响的单调读数）：仅用于
    等待预算的截止时刻计算；持久化时间戳一律使用 ``now()`` 的纪元
    值。未提供时退回 ``now()``（如测试假时钟）。
    """

    def now(self) -> float: ...

    def sleep(self, seconds: float) -> None: ...


class SystemClock:
    """生产时钟：真实时间；``sleep`` 被中断后会补足剩余时间。"""

    def now(self) -> float:
        return time.time()

    def monotonic(self) -> float:
        """单调时钟读数：仅用于等待预算，不受系统改时影响。"""
        return time.monotonic()

    def sleep(self, seconds: float) -> None:
        remaining = max(0.0, float(seconds))
        deadline = time.monotonic() + remaining
        while remaining > 0:
            time.sleep(remaining)
            remaining = deadline - time.monotonic()


def default_rate_gate_state_path() -> Path:
    """返回节流状态的默认路径：用户缓存目录，独立于任何工作空间。

    可用环境变量 ``HVS_CATALOG_ARXIV_RATE_STATE`` 指定完整文件路径覆
    盖默认位置（例如隔离测试或受限主目录）。本函数只计算路径，不创
    建文件，模块导入没有任何磁盘或网络副作用。
    """
    override = os.environ.get("HVS_CATALOG_ARXIV_RATE_STATE")
    if override:
        return Path(override)
    home = Path.home()
    if sys.platform == "darwin":
        base = home / "Library" / "Caches"
    elif sys.platform == "win32":  # pragma: no cover - 未实测平台
        local = os.environ.get("LOCALAPPDATA", str(home / "AppData" / "Local"))
        return Path(local) / _CACHE_DIR_NAME / "Cache" / _STATE_FILE_NAME
    else:
        base = Path(os.environ.get("XDG_CACHE_HOME", str(home / ".cache")))
    return base / _CACHE_DIR_NAME / _STATE_FILE_NAME


class RateLimitError(RuntimeError):
    """节流门拒绝当前操作（如恢复时存在未到期的间隔/冷却）。"""

    def __init__(self, message: str, *, wait_seconds: float | None = None) -> None:
        super().__init__(message)
        self.wait_seconds = wait_seconds


class RateLimitStateError(RuntimeError):
    """节流状态损坏、为空、版本或字段类型非法；已拒绝发送并保留现场。

    这是 fail-closed 失败：不自动重置、不覆盖状态文件，默认恢复
    （``reset``）同样拒绝——损坏状态中的冷却是未知而非零。人工恢复
    需要操作者协调停止本机所有 arXiv 请求进程、核实服务端实际等待
    条件后，再手动删除或修复状态文件；在此之前任何请求都必须停止。
    """


class RateLimitUnconfirmedError(RateLimitError):
    """状态中存在未确认的在途请求（in-flight 标记）：拒绝发送。

    前一次交换可能未完成、或在交换中崩溃、或收尾写入失败——无论
    ``next_allowed_at`` 是否已过，都无法保证 ≥3 秒的实际发送间距，
    因此**不能靠等待绕过**，也不自动按 TTL 过期或重置。恢复需要操
    作者协调停止本机所有 arXiv 请求进程、核实实际发送情况后，手动
    删除或修复状态文件（与损坏状态的人工恢复同流程）。
    """


class RateLimitPersistenceError(RateLimitError):
    """交换结束但收尾状态未能可靠落盘：在途标记保守保留。

    后续请求将 fail-closed（:class:`RateLimitUnconfirmedError`）直至
    人工恢复；原始异常（传输错误或持久化错误）经 ``__cause__`` 链
    保留可诊断。
    """


@dataclass(frozen=True)
class RateGateSnapshot:
    """某一时刻读到的节流状态快照，供状态查询与操作员排查使用。

    ``state_error`` 非 ``None`` 表示状态损坏：此时其余字段无意义，
    发送被拒绝，需要显式恢复。``in_flight_since`` 非 ``None`` 表示
    存在未确认的在途请求：同样拒绝发送且不能靠等待绕过。快照只读
    取、从不改写状态文件。
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
    """下一次请求最早允许时刻：普通间距与服务端冷却取较晚者。"""
    return max(
        _finite_or_zero(state.get("next_allowed_at")),
        _finite_or_zero(state.get("cooldown_until")),
    )


def _lock_file(fd: int) -> None:
    if fcntl is not None:
        fcntl.flock(fd, fcntl.LOCK_EX)
    elif msvcrt is not None:  # pragma: no cover - 未实测平台
        while True:
            try:
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                return
            except OSError:
                time.sleep(_WINDOWS_LOCK_POLL_SECONDS)
    else:  # pragma: no cover - 理论上不会出现
        raise RuntimeError("当前平台没有可用的文件锁实现")


def _try_lock_file(fd: int) -> bool:
    """非阻塞尝试取锁：成功返回 True，竞争失败返回 False。"""
    if fcntl is not None:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except OSError:
            return False
    if msvcrt is not None:  # pragma: no cover - 未实测平台
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
    elif msvcrt is not None:  # pragma: no cover - 未实测平台
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)


class TransferLease:
    """一次 HTTP 交换的独占连接租约；由 :meth:`RateLimitGate.transfer` 签发。

    租约存活期间（含传输与错误关闭）本机同用户没有其他 arXiv 连接。
    ``record_cooldown`` 直接写入已持有的锁定协调（无嵌套锁）；租约结
    束后再调用是程序错误。
    """

    def __init__(self, gate: RateLimitGate, state: dict, granted_at: float) -> None:
        self._gate = gate
        self._state = state
        self.granted_at = granted_at
        self._active = True

    def record_cooldown(self, until: float) -> None:
        """在租约内持久化服务端冷却（合并保留较晚者）。"""
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
    """跨进程共享的 arXiv 请求节流门。

    参数：
        state_path: 状态文件路径；同一用户的所有进程应传入同一路径。
        min_interval: 相邻请求最小间隔（秒），不得低于
            :data:`MIN_REQUEST_INTERVAL`。
        clock: 可注入时钟；测试用假时钟驱动，生产默认
            :class:`SystemClock`。
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

    # -- 内部：稳定锁文件 + 原子替换的数据文件 ----------------------

    def _lock_path(self) -> Path:
        """稳定协调锁文件：独立于会被原子替换的数据文件。"""
        return self.state_path.with_name(self.state_path.name + ".lock")

    def _open_lock(self) -> int:
        lock_path = self._lock_path()
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        _lock_file(fd)
        return fd

    def _open_lock_fd(self) -> int:
        """仅打开锁文件（不取锁），供有界获取使用。"""
        lock_path = self._lock_path()
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        return os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)

    def _monotonic_now(self) -> float:
        """等待预算使用的单调读数；注入时钟未提供 ``monotonic`` 时退回
        ``now()``（持久化时间戳始终使用纪元 ``now()``，不混用）。"""
        monotonic = getattr(self.clock, "monotonic", None)
        if callable(monotonic):
            return float(monotonic())
        return self.clock.now()

    def _acquire_lock(self, fd: int, *, deadline: float | None) -> None:
        """取协调锁：无预算时阻塞等待；有预算时在同一固定截止时刻内
        非阻塞轮询，超时抛 :class:`RateLimitError`（未取得锁、未改动
        任何状态）。

        取得锁后还会再核对一次同一截止时刻：轮询睡眠期间的迟到调度
        （实际睡眠超过请求步长）可能让成功的取得落在截止时刻之后，
        此时同样拒绝——不能凭迟到的锁签发许可（严格超出才拒绝，恰
        好相等接受）。"""
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
            # 迟到取得（调度/释放延迟超出预算）：释放刚取得的锁并拒绝。
            _unlock_file(fd)
            raise RateLimitError(
                "取得协调锁时已超出本次调用总等待预算（迟到的调度或释放）；"
                "未占用许可，未改动状态",
                wait_seconds=0.0,
            )

    def _read_state_file(self) -> dict | None:
        """持锁读取并校验数据文件；不存在返回 ``None``（全新状态）。

        写入只在持锁下经临时文件原子替换，因此观察到的空文件或畸形
        内容只能来自真正的历史损坏（或手工破坏），按损坏处理。
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
        # 版本严格校验：必须是 int（排除 bool/float/str/None/容器）且等
        # 于当前版本。Python 中 True == 1、1.0 == 1，相等比较不足以拒
        # 绝这些畸形类型。
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
        """持锁下原子替换写入数据文件；任何观察者只见完整内容。"""
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
            return  # pragma: no cover - 目录打开失败时替换已完成
        try:
            os.fsync(dir_fd)
        except OSError:
            pass  # pragma: no cover - 部分文件系统不支持目录 fsync
        finally:
            os.close(dir_fd)

    # -- 连接租约 --------------------------------------------------

    def _check_admission_deadline(
        self, max_wait: float | None, deadline: float | None, *, stage: str
    ) -> None:
        """阻塞状态 I/O（写入/fsync）之后的迟到核对：正预算下超出即拒
        绝交付租约（不 yield、不发送），保守保留已持久化的预登记与冷
        却；零预算的非阻塞尝试语义容忍固有 I/O 耗时，无界语义无预算。"""
        if max_wait is None or max_wait <= 0:
            return
        if self._monotonic_now() > deadline:
            raise RateLimitError(
                f"{stage}已超出本次调用总等待预算（迟到的状态写入或 fsync）；"
                "保守保留已持久化的预登记与冷却，未签发租约、未发送",
                wait_seconds=0.0,
            )

    def _complete_exchange(self, state: dict) -> None:
        """租约结束（仍持锁）时的唯一收尾写入。

        在同一次原子替换中：清除在途标记，并把下一次许可间隔保守地
        设为**完成时刻** + 间隔（完成时刻不早于实际发送时刻，满足
        ≥3 秒实际发送间距）；冷却等其他字段原样保留。写入失败时在
        途标记保持在场（后续请求 fail-closed），错误由调用方链式抛
        出、不静默吞掉。
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
        """取得一次 HTTP 交换的独占连接租约。

        有界准入：``max_wait`` 是**本次调用的总等待预算**——进入时（在
        首次取锁之前）用单调时钟固定截止时刻，此后不再重置：初次取
        锁、等待期间的重取锁与每轮重读的间距/冷却共享同一预算。每轮
        同时检查“当前还需等待多久”与“预算剩余多少”，任一超出即释放
        资源并抛 :class:`RateLimitError`：不占用许可、不修改
        ``next_allowed_at``，也不丢弃已持久化的冷却（其他进程反复登记
        新冷却不能把本次调用变成无限等待）。单步睡眠不超过剩余预算；
        到期时刻恰好许可的边界情形正常签发。

        迟到核对：同一个截止时刻在**取得锁成功之后**、**状态读取后、
        写入许可前**与**在途标记与预登记的阻塞写入/fsync 完成后**各
        核对一次——迟到的调度、迟到的锁释放、缓慢的状态读取或写入
        都不能在预算之外交付正预算租约（严格超出才拒绝，恰好相等接
        受）。拒绝时释放已持有的锁、不 yield、不发送；已落盘的在途
        标记与预登记保守保留（未发送也不清除标记，避免不确定写入）。

        在途标记与实际发送间距（≥3 秒）：交付租约**之前**，同一次原
        子写入持久化可识别的 ``in_flight_since`` 在途标记与间距预登
        记；任何后来者读到标记即 :class:`RateLimitUnconfirmedError`
        fail-closed（无论 ``next_allowed_at`` 是否已过、无论预算），
        不自动 TTL 过期或重置，恢复需人工协调。正常完成、错误退出
        与传输异常都在解锁前经**单次原子写入**完成收尾：清除标记并
        把下一次许可间隔设为**完成时刻 + 间隔**（完成时刻不早于实
        际发送，满足实际间距下限）；收尾写入失败抛
        :class:`RateLimitPersistenceError`（原始异常经 ``__cause__``
        链保留），标记保守留在盘上。崩溃边界：崩溃于标记写入前→状
        态未变、无发送；崩溃于标记写入后（含发送前或交换中）→标记
        在场，接管者 fail-closed；原子替换的模糊性也安全：替换前失
        败→旧状态（含标记）保留，替换后目录 fsync 失败→新状态（完
        成式间隔、无标记）已可见，两种结果都不产生“无标记且无安全
        间隔”的已发送请求。本设计不依赖硬实时调度，也不声称事务回
        滚。

        ``max_wait=None`` 为显式无界语义（阻塞取锁、无限等待）；
        ``max_wait=0`` 是**非阻塞尝试**：不主动等待锁或冷却（锁竞争
        或需要等待的情形立即拒绝），但不承诺零 CPU/I/O 耗时——状态
        读取等固有 I/O 延迟不使零预算的立即许可失败，也不给它任何
        正预算宽限。

        流程：预算内取稳定锁 → 间距/冷却未到时在锁外小步等待并重读
        （等待期间新登记的冷却会被看到）→ 许可时在同一临界区内预登
        记下一次间距并原子落盘 → 持锁等待调用方完成传输（含错误关
        闭）后释放。崩溃安全：传输中崩溃由操作系统释放锁，已落盘的
        预登记使下一个许可不早于本次发送的许可时刻 + 间隔。
        """
        if max_wait is not None:
            max_wait = float(max_wait)
            if not math.isfinite(max_wait) or max_wait < 0:
                raise ValueError(f"max_wait 必须是非负有限数值或 None：{max_wait!r}")
        # 固定总预算截止时刻：在首次取锁之前计算，之后绝不重置。
        deadline = None if max_wait is None else self._monotonic_now() + max_wait
        fd = self._open_lock_fd()
        try:
            if max_wait is None:
                self._acquire_lock(fd, deadline=None)
            elif max_wait > 0:
                self._acquire_lock(fd, deadline=deadline)
            elif not _try_lock_file(fd):
                # max_wait == 0：非阻塞尝试——锁被占用即拒绝，不轮询。
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
                        # 未确认在途请求：无论 next_allowed_at 是否已过都
                        # fail-closed，不能靠等待绕过，也不自动 TTL 过期。
                        raise RateLimitUnconfirmedError(
                            f"检测到未确认的在途请求（in_flight_since="
                            f"{float(marker):.3f}）：前一交换可能未完成或收尾"
                            "失败，无法保证实际发送间距；已拒绝发送，需人工"
                            "检查状态文件后恢复"
                        )
                    now = self.clock.now()
                    base = _effective_base(state)
                    if base <= now:
                        # 状态就绪后、写入许可前核对同一截止时刻：迟到的
                        # 读取或调度不能签发正预算租约（零预算的非阻塞
                        # 尝试语义容忍状态 I/O 耗时，不适用此检查）。
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
                            # 总预算耗尽（或当前需要已超出剩余预算）：拒
                            # 绝且不占用许可、不改写已持久化约束。
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
                # 交付前先持久化可识别的在途标记与间距预登记（同一次原
                # 子写入）：接管者看到标记即 fail-closed，不因写前基准
                # 的 next_allowed_at 已过而放行。
                state["in_flight_since"] = granted
                state["next_allowed_at"] = granted + self.min_interval
                state["updated_at"] = granted
                self._write_state_file(state)
                # 阻塞写入/fsync 纳入正预算的实际租约交付边界：写完若
                # 已超出预算，不 yield、不发送；已落盘的标记与预登记
                # 保守保留（未发送也不清除标记，避免不确定写入）。
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
                    # 收尾（清除标记 + 完成式间隔）必须可靠落盘：失败不
                    # 静默吞掉、也不另用不安全的单独写入清除标记；原始
                    # 异常经 __cause__ 链保留可诊断。
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

    # -- 传输之外的独立操作 ----------------------------------------

    def record_cooldown(self, until: float) -> None:
        """在传输之外登记服务端冷却：``until``（纪元秒）前不允许请求。

        状态损坏时同样拒绝（fail-closed）。传输过程中应使用
        :meth:`TransferLease.record_cooldown`；在本方法上嵌套调用会与
        已持有的租约互锁。
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
        """只读快照：损坏时 ``state_error`` 说明原因，不改写现场。"""
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
        """显式恢复检查：只在状态有效且间隔与冷却都已过期时清空。

        - 有效状态中存在未到期的 ``next_allowed_at`` 或 ``cooldown_
          until``：拒绝（:class:`RateLimitError`）。生产请求间隔与服
          务端等待不能被恢复操作绕过，本方法**不提供**任何强制清除
          标志；请等到间隔/冷却自然过期后再清空。
        - 状态损坏：冷却是**未知**而非零，默认恢复同样拒绝并保留字
          节（:class:`RateLimitStateError`）。人工恢复需要操作者先协
          调停止本机所有 arXiv 请求进程、核实服务端实际等待条件，
          再手动删除或修复状态文件；本模块不发明保证安全的固定等
          待，删除文件后间距从全新状态重新起算。
        - 状态有效且全部过期（或尚无状态文件）：清空为全新状态。
        """
        fd = self._open_lock()
        try:
            state = self._read_state_file()
            if state is not None:
                marker = state.get("in_flight_since")
                if marker is not None:
                    # 未确认在途请求不能被恢复操作清除或绕过。
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

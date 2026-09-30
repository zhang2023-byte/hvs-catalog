"""arXiv 节流门合成测试：连接租约、间距下限、冷却与跨进程（lane A）。

单元层面注入假时钟（3 秒间距在虚拟时间内等待，不真实睡眠）；跨进
程场景用真实子进程与真实时钟验证文件锁、连接不重叠与共享冷却。所
有状态文件写在测试临时目录，不触碰用户缓存目录。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap

import pytest

from hvs_catalog.pipeline.acquisition.rate_limit import (
    LOCK_WAIT_POLL_SECONDS,
    MIN_REQUEST_INTERVAL,
    WAIT_RECHECK_SECONDS,
    RateGateSnapshot,
    RateLimitError,
    RateLimitGate,
    RateLimitPersistenceError,
    RateLimitStateError,
    RateLimitUnconfirmedError,
    SystemClock,
    default_rate_gate_state_path,
)


class FakeClock:
    def __init__(self, start: float = 1000.0) -> None:
        self._now = float(start)
        self.sleeps: list[float] = []

    def now(self) -> float:
        return self._now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(float(seconds))
        self._now += max(0.0, float(seconds))


# ---------------------------------------------------------------- 间距下限


def test_interval_floor_is_enforced(tmp_path):
    with pytest.raises(ValueError, match="下限"):
        RateLimitGate(tmp_path / "gate.json", min_interval=1.0)
    with pytest.raises(ValueError):
        RateLimitGate(tmp_path / "gate.json", min_interval=2.99)
    with pytest.raises(ValueError):
        RateLimitGate(tmp_path / "gate.json", min_interval=float("nan"))
    gate = RateLimitGate(tmp_path / "gate.json", min_interval=3.0, clock=FakeClock())
    assert gate.min_interval == MIN_REQUEST_INTERVAL == 3.0


def test_state_file_created_with_owner_only_permissions(tmp_path):
    clock = FakeClock()
    gate = RateLimitGate(tmp_path / "gate.json", clock=clock)
    with gate.transfer():
        pass
    mode = os.stat(tmp_path / "gate.json").st_mode & 0o777
    assert mode == 0o600
    state = json.loads((tmp_path / "gate.json").read_text(encoding="utf-8"))
    assert state["version"] == 2
    assert state["next_allowed_at"] == 1003.0
    assert state.get("in_flight_since") is None  # 完成收尾后标记已清除


def test_atomic_write_leaves_no_temp_artifacts(tmp_path):
    """原子替换写入：不留 .tmp 残留，锁文件独立于数据文件。"""
    clock = FakeClock(start=1000.0)
    gate = RateLimitGate(tmp_path / "gate.json", clock=clock)
    for _ in range(3):
        with gate.transfer():
            pass
        gate.record_cooldown(clock.now() + 5)
    assert not (tmp_path / "gate.json.tmp").exists()
    assert (tmp_path / "gate.json.lock").exists()  # 稳定协调锁
    lock_mode = os.stat(tmp_path / "gate.json.lock").st_mode & 0o777
    assert lock_mode == 0o600


def test_transfer_max_wait_allows_wait_within_bound(tmp_path):
    clock = FakeClock(start=1000.0)
    gate = RateLimitGate(tmp_path / "gate.json", clock=clock)
    with gate.transfer():
        pass  # next_allowed_at = 1003
    with gate.transfer(max_wait=5.0) as lease:
        assert lease.granted_at == 1003.0
    assert sum(clock.sleeps) == pytest.approx(3.0)


def test_transfer_max_wait_rejects_without_reserving(tmp_path):
    """等待超过上限时拒绝且不占用许可；状态不被修改。"""
    clock = FakeClock(start=1000.0)
    gate = RateLimitGate(tmp_path / "gate.json", clock=clock)
    gate.record_cooldown(1040.0)
    with pytest.raises(RateLimitError) as excinfo, gate.transfer(max_wait=10.0):
        pass
    assert excinfo.value.wait_seconds == pytest.approx(40.0)
    snapshot = gate.snapshot()
    assert snapshot.cooldown_until == 1040.0  # 未被占用或改写
    assert snapshot.next_allowed_at is None
    # 未占用许可：不带上限的租约仍在原时刻获得许可。
    with gate.transfer() as lease:
        assert lease.granted_at == 1040.0


@pytest.mark.parametrize("bad", [-1.0, float("nan"), float("inf")])
def test_transfer_max_wait_validates_input(tmp_path, bad):
    gate = RateLimitGate(tmp_path / "gate.json", clock=FakeClock())
    with pytest.raises(ValueError), gate.transfer(max_wait=bad):
        pass


def test_transfer_max_wait_zero_allows_immediate_permit(tmp_path):
    clock = FakeClock(start=1000.0)
    gate = RateLimitGate(tmp_path / "gate.json", clock=clock)
    with gate.transfer(max_wait=0.0) as lease:
        assert lease.granted_at == 1000.0


# --------------------------------------- 固定总预算（round4：有界准入）


def make_extending_clock(clock: FakeClock, state_path, *, extend_seconds: float):
    """每次锁外等待步后把共享冷却延长到当前时刻之后 extend_seconds。

    模拟其他进程反复登记短冷却（审核复现场景）；用另一门实例在主门
    已让出锁的窗口内写入。返回恢复函数：后续不模拟延长的断言前必须
    调用，否则无限额租约会因冷却被无限延长而永远等待（这是
    ``max_wait=None`` 的文档化语义，不是缺陷）。
    """
    original_sleep = clock.sleep

    def interleaved(seconds: float) -> None:
        original_sleep(seconds)
        other = RateLimitGate(state_path, clock=clock)
        other.record_cooldown(clock.now() + extend_seconds)

    clock.sleep = interleaved  # type: ignore[method-assign]

    def restore() -> None:
        clock.sleep = original_sleep  # type: ignore[method-assign]

    return restore


def test_transfer_total_budget_not_reset_by_repeated_cooldown_extensions(tmp_path):
    """审核复现场景：冷却被反复小步延长时，总预算固定不重置。

    max_wait=3，每次等待步后另一门把冷却延至当前时刻 +2：无固定截止
    时（旧缺陷）每轮剩余都 ≤ 上限，永不拒绝；修复后在预算耗尽时抛出
    且不占用许可、不丢弃冷却。
    """
    state = tmp_path / "gate.json"
    clock = FakeClock(start=1000.0)
    gate = RateLimitGate(state, clock=clock)
    gate.record_cooldown(1002.0)
    restore = make_extending_clock(clock, state, extend_seconds=2.0)
    with (
        pytest.raises(RateLimitError, match="总等待预算"),
        gate.transfer(max_wait=3.0),
    ):
        pass
    # 等待只消耗了固定预算内的秒数，每步不超过重核步长。
    assert clock.sleeps and all(step <= WAIT_RECHECK_SECONDS for step in clock.sleeps)
    assert sum(clock.sleeps) <= 3.0
    # 不占用许可、不改写已持久化冷却（最后一次延长后为 1004）。
    snapshot = RateLimitGate(state, clock=FakeClock(1000.0)).snapshot()
    assert snapshot.next_allowed_at is None
    assert snapshot.cooldown_until == 1004.0
    # 停止模拟延长后，无限额租约在冷却结束时获得许可（冷却未被丢弃）。
    restore()
    with gate.transfer() as lease:
        assert lease.granted_at == 1004.0


def test_transfer_max_wait_exact_boundary_grants(tmp_path):
    """冷却恰好在预算截止时刻结束：正常签发，不拒绝。"""
    clock = FakeClock(start=1000.0)
    gate = RateLimitGate(tmp_path / "gate.json", clock=clock)
    gate.record_cooldown(1003.0)  # 需要等 3 秒 == max_wait
    with gate.transfer(max_wait=3.0) as lease:
        assert lease.granted_at == 1003.0
    assert sum(clock.sleeps) == pytest.approx(3.0)


def test_transfer_max_wait_exceeded_rejects_without_sleeping(tmp_path):
    """总需求从一开始就超出预算：立即拒绝（不睡眠），状态不被改写。

    预算是上限：超出时不必先睡满预算；需要先睡再耗尽的场景由反复延
    长冷却的回归覆盖（上一测试）。
    """
    state = tmp_path / "gate.json"
    clock = FakeClock(start=1000.0)
    gate = RateLimitGate(state, clock=clock)
    gate.record_cooldown(1003.5)  # 需要 3.5 秒 > max_wait=3
    with (
        pytest.raises(RateLimitError, match="总等待预算"),
        gate.transfer(max_wait=3.0),
    ):
        pass
    assert clock.sleeps == []  # 明显超出：立即拒绝，不消耗时间
    snapshot = gate.snapshot()
    assert snapshot.next_allowed_at is None  # 未占用许可
    assert snapshot.cooldown_until == 1003.5  # 冷却未被改写/丢弃
    with gate.transfer() as lease:
        assert lease.granted_at == 1003.5


def test_transfer_initial_lock_contention_bounded_by_deadline(tmp_path):
    """初始取锁竞争计入同一固定预算：超时拒绝且未取得锁、未改状态。"""
    import fcntl

    state = tmp_path / "gate.json"
    lock_path = tmp_path / "gate.json.lock"
    holder_fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(holder_fd, fcntl.LOCK_EX)
    try:
        clock = FakeClock(start=1000.0)
        gate = RateLimitGate(state, clock=clock)
        with (
            pytest.raises(RateLimitError, match="协调锁"),
            gate.transfer(max_wait=0.5),
        ):
            pass
        # 轮询步长受限，总等待恰好为预算。
        assert clock.sleeps and all(
            step <= LOCK_WAIT_POLL_SECONDS for step in clock.sleeps
        )
        assert sum(clock.sleeps) == pytest.approx(0.5)
        # 无许可、未创建数据文件（无任何持久化副作用）。
        assert not state.exists()
    finally:
        fcntl.flock(holder_fd, fcntl.LOCK_UN)
        os.close(holder_fd)
    with gate.transfer() as lease:  # 释放后正常取许可
        assert lease.granted_at == pytest.approx(1000.5)


def test_transfer_reacquire_contention_bounded_by_deadline(tmp_path):
    """等待期间重取锁的竞争同样计入固定预算。"""
    import fcntl

    state = tmp_path / "gate.json"
    lock_path = tmp_path / "gate.json.lock"
    clock = FakeClock(start=1000.0)
    gate = RateLimitGate(state, clock=clock)
    gate.record_cooldown(1002.0)
    holder_fds: list[int] = []
    original_sleep = clock.sleep

    def interleaved(seconds: float) -> None:
        original_sleep(seconds)
        # 主门已让出锁：另一持有者尝试抢住锁并不释放。用非阻塞获取：
        # 已有持有者时无需重复持有（阻塞获取会与既有持有者互锁）。
        fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(fd)
            return
        holder_fds.append(fd)

    clock.sleep = interleaved  # type: ignore[method-assign]
    try:
        with (
            pytest.raises(RateLimitError, match="协调锁"),
            gate.transfer(max_wait=3.0),
        ):
            pass
        # 先记录断言材料，再在 finally 释放持有者（snapshot 会取阻塞
        # 锁，不能在持有者仍持锁时调用）。
        first_step = clock.sleeps[0]
        later_steps = tuple(clock.sleeps[1:])
        total_slept = sum(clock.sleeps)
    finally:
        clock.sleep = original_sleep  # 恢复，避免后续无限额租约被钩子卡住
        for fd in holder_fds:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)
    assert first_step == 1.0  # 第一步是冷却重核步
    assert all(step <= LOCK_WAIT_POLL_SECONDS for step in later_steps)
    assert total_slept == pytest.approx(3.0)  # 重取锁竞争耗尽余下预算
    snapshot = gate.snapshot()
    assert snapshot.next_allowed_at is None
    assert snapshot.cooldown_until == 1002.0
    with gate.transfer() as lease:
        assert lease.granted_at >= 1002.0


def test_transfer_max_wait_zero_rejects_lock_contention_immediately(tmp_path):
    import fcntl

    lock_path = tmp_path / "gate.json.lock"
    holder_fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(holder_fd, fcntl.LOCK_EX)
    try:
        clock = FakeClock(start=1000.0)
        gate = RateLimitGate(tmp_path / "gate.json", clock=clock)
        with (
            pytest.raises(RateLimitError, match="协调锁"),
            gate.transfer(max_wait=0.0),
        ):
            pass
        assert clock.sleeps == []  # 零预算：立即拒绝，不睡眠
    finally:
        fcntl.flock(holder_fd, fcntl.LOCK_UN)
        os.close(holder_fd)


# ------------------------------- 迟到准入核对（round5：迟到锁/迟到读取不签发）


def test_transfer_late_lock_poll_oversleep_rejects_beyond_budget(tmp_path):
    """初始取锁轮询期间实际睡眠超步（迟到调度）：即使锁随后取得且
    状态已就绪，也必须在严格超出预算时拒绝，不能凭迟到的锁签发。"""
    import fcntl

    state = tmp_path / "gate.json"
    lock_path = tmp_path / "gate.json.lock"
    clock = FakeClock(start=1000.0)
    seed = RateLimitGate(state, clock=FakeClock(1000.0))
    seed.record_cooldown(1000.5)  # 已就绪：若无迟到核对会直接签发
    holder_fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(holder_fd, fcntl.LOCK_EX)
    original_sleep = clock.sleep

    def oversleeping_poll(seconds: float) -> None:
        original_sleep(seconds)
        original_sleep(1.0)  # 模拟调度延迟：实际睡超请求步长
        fcntl.flock(holder_fd, fcntl.LOCK_UN)  # 预算耗尽后锁才空出

    clock.sleep = oversleeping_poll  # type: ignore[method-assign]
    gate = RateLimitGate(state, clock=clock)
    try:
        with (
            pytest.raises(RateLimitError, match="取得协调锁时已超出"),
            gate.transfer(max_wait=0.5),
        ):
            pass
        assert clock.sleeps[0] <= LOCK_WAIT_POLL_SECONDS
        assert sum(clock.sleeps) > 0.5  # 实际等待已超出预算
    finally:
        clock.sleep = original_sleep
        fcntl.flock(holder_fd, fcntl.LOCK_UN)
        os.close(holder_fd)
    snapshot = gate.snapshot()
    assert snapshot.next_allowed_at is None  # 未占用许可
    assert snapshot.cooldown_until == 1000.5  # 冷却保留
    with gate.transfer() as lease:  # 锁/文件描述符未泄漏
        assert lease.granted_at >= 1000.5


def test_transfer_reacquire_oversleep_rejects_late_lock_success(tmp_path):
    """冷却等待步实际睡眠超步：重取锁虽成功但已落在截止之后，拒绝。"""
    state = tmp_path / "gate.json"
    clock = FakeClock(start=1000.0)
    gate = RateLimitGate(state, clock=clock)
    gate.record_cooldown(1001.0)
    original_sleep = clock.sleep

    def oversleeping(seconds: float) -> None:
        original_sleep(seconds)
        original_sleep(3.0)  # 迟到调度：单步 1 秒实际睡到 1004

    clock.sleep = oversleeping  # type: ignore[method-assign]
    try:
        with (
            pytest.raises(RateLimitError, match="取得协调锁时已超出"),
            gate.transfer(max_wait=1.0),
        ):
            pass
        assert clock.sleeps[0] == 1.0
        assert sum(clock.sleeps) == 4.0  # 请求 1 秒 + 迟到 3 秒
    finally:
        clock.sleep = original_sleep
    snapshot = gate.snapshot()
    assert snapshot.next_allowed_at is None
    assert snapshot.cooldown_until == 1001.0
    with gate.transfer() as lease:
        assert lease.granted_at == 1004.0  # 无泄漏：迟到后仍可正常取许可


def test_transfer_slow_state_read_past_deadline_rejects_before_permit(tmp_path):
    """状态读取缓慢（每轮读耗时 0.2 秒）：状态就绪但读取后已超出正预
    算，签发许可前拒绝；许可未写、冷却保留。"""
    state = tmp_path / "gate.json"
    clock = FakeClock(start=1000.0)
    gate = RateLimitGate(state, clock=clock)
    gate.record_cooldown(1001.0)
    original_read = gate._read_state_file

    def slow_read():
        clock.sleep(0.2)  # 模拟状态 I/O 延迟
        return original_read()

    gate._read_state_file = slow_read  # type: ignore[method-assign]
    try:
        with (
            pytest.raises(RateLimitError, match="签发许可前已超出"),
            gate.transfer(max_wait=1.0),
        ):
            pass
        # 每轮读取都计入预算：等待与读取合计超出 1 秒预算后拒绝。
        assert sum(clock.sleeps) > 1.0
    finally:
        gate._read_state_file = original_read  # type: ignore[method-assign]
    snapshot = gate.snapshot()
    assert snapshot.next_allowed_at is None
    assert snapshot.cooldown_until == 1001.0
    with gate.transfer() as lease:
        assert lease.granted_at >= 1001.0


def test_transfer_max_wait_zero_tolerates_state_read_latency(tmp_path):
    """零预算 = 非阻塞尝试：不等待锁/冷却，但容忍状态 I/O 耗时——
    就绪状态下缓慢读取不使零预算立即许可失败（不是零耗时承诺）。"""
    state = tmp_path / "gate.json"
    clock = FakeClock(start=1000.0)
    gate = RateLimitGate(state, clock=clock)
    original_read = gate._read_state_file

    def slow_read():
        clock.sleep(0.2)
        return original_read()

    gate._read_state_file = slow_read  # type: ignore[method-assign]
    try:
        with gate.transfer(max_wait=0.0) as lease:
            assert lease.granted_at == pytest.approx(1000.2)
    finally:
        gate._read_state_file = original_read  # type: ignore[method-assign]
    assert clock.sleeps == [0.2]


def test_transfer_real_clock_delayed_read_rejects_late_admission(tmp_path):
    """真实时钟复核（审核者复现同构）：0.02 秒预算遇到 0.12 秒的读取
    延迟必须拒绝；不建立连接、不写许可。"""
    import time

    gate = RateLimitGate(tmp_path / "gate.json", clock=SystemClock())
    original_read = gate._read_state_file

    def delayed_read():
        time.sleep(0.12)
        return original_read()

    gate._read_state_file = delayed_read  # type: ignore[method-assign]
    try:
        with (
            pytest.raises(RateLimitError, match="签发许可前已超出"),
            gate.transfer(max_wait=0.02),
        ):
            pass
    finally:
        gate._read_state_file = original_read  # type: ignore[method-assign]
    assert not (tmp_path / "gate.json").exists()  # 未写任何许可/状态
    with gate.transfer(max_wait=5.0) as lease:  # 无锁泄漏，正常签发
        assert lease.granted_at > 0


def test_transfer_real_clock_immediate_attempt_succeeds(tmp_path):
    """真实时钟正常路径：无延迟、就绪状态，小预算立即签发且写入预登记。"""
    clock = SystemClock()
    gate = RateLimitGate(tmp_path / "gate.json", clock=clock)
    with gate.transfer(max_wait=1.0) as lease:
        granted = lease.granted_at
        assert clock.now() - granted < 1.0  # 未耗尽预算即签发
    snapshot = gate.snapshot()
    assert snapshot.next_allowed_at == pytest.approx(granted + MIN_REQUEST_INTERVAL)


# ------------------- 写入/fsync 边界与实际发送间距（extra1）


def make_slow_write_gate(state, clock, *, delay: float, max_wait=None):
    """构造写入延迟注入的门：每次状态写入耗时 delay 秒（虚拟时间）。"""
    gate = RateLimitGate(state, clock=clock)
    original_write = gate._write_state_file

    def slow_write(payload: dict) -> None:
        clock.sleep(delay)  # 模拟阻塞写入/fsync 延迟
        original_write(payload)

    gate._write_state_file = slow_write  # type: ignore[method-assign]
    return gate


def test_transfer_write_delay_beyond_budget_rejects_without_delivery(tmp_path):
    """写入耗时超出正预算：不交付租约、不发送；已落盘的在途标记与预
    登记保守保留（extra2 语义修正：迟到拒绝也留下标记，后续必须
    fail-closed 而非按可能过早的预登记放行——旧的“等待后正常签发”
    断言 assert grant@1004 属旧设计的不安全自动恢复，已按标记设计
    改为更强的新言）。"""
    state = tmp_path / "gate.json"
    clock = FakeClock(start=1000.0)
    gate = make_slow_write_gate(state, clock, delay=4.0)
    with (
        pytest.raises(RateLimitError, match="许可预登记写入后已超出"),
        gate.transfer(max_wait=1.0),
    ):
        pass
    snapshot = gate.snapshot()
    assert snapshot.next_allowed_at == 1003.0  # 写前基准的预登记保留
    assert snapshot.cooldown_until is None
    assert snapshot.in_flight_since == 1000.0  # 在途标记同样保留
    # 未确认在途：后续请求 fail-closed，不能等预登记过期绕过。
    with (
        pytest.raises(RateLimitUnconfirmedError, match="在途"),
        gate.transfer(max_wait=100.0),
    ):
        pass


def test_transfer_real_clock_slow_write_rejects_before_delivery(tmp_path):
    """真实时钟（审核者同构）：0.12 秒写入延迟 × 0.02 秒预算 → 拒绝。"""
    import time

    gate = RateLimitGate(tmp_path / "gate.json", clock=SystemClock())
    original_write = gate._write_state_file

    def delayed_write(payload: dict) -> None:
        time.sleep(0.12)
        original_write(payload)

    gate._write_state_file = delayed_write  # type: ignore[method-assign]
    try:
        with (
            pytest.raises(RateLimitError, match="许可预登记写入后已超出"),
            gate.transfer(max_wait=0.02),
        ):
            pass
    finally:
        gate._write_state_file = original_write  # type: ignore[method-assign]
    snapshot = gate.snapshot()
    assert snapshot.next_allowed_at is not None  # 保守预登记已落盘保留
    assert snapshot.cooldown_until is None
    assert snapshot.in_flight_since is not None  # 在途标记保留
    # 锁已释放（OS 层面无泄漏）：零预算尝试被在途标记拒绝（extra2 语
    # 义修正：不再按预登记等待，fail-closed 更强）。
    with (
        pytest.raises(RateLimitUnconfirmedError, match="在途"),
        gate.transfer(max_wait=0.0),
    ):
        pass


def test_transfer_write_delay_persists_send_basis_reservation(tmp_path):
    """每次写入延迟 4 秒：完成式间隔以完成时刻为基准（不早于实际发送
    时刻 1004），标记在收尾中清除，下一个许可不早于实际发送 +3 秒。"""
    state = tmp_path / "gate.json"
    clock = FakeClock(start=1000.0)
    gate = make_slow_write_gate(state, clock, delay=4.0)
    with gate.transfer() as lease:
        assert lease.granted_at == 1000.0  # 准入决定时刻（写前）
        send_time = clock.now()  # 实际发送时刻：在途标记写入完成后
        assert send_time == 1004.0
    snapshot = gate.snapshot()
    assert snapshot.next_allowed_at == send_time + MIN_REQUEST_INTERVAL
    assert snapshot.cooldown_until is None
    assert snapshot.in_flight_since is None  # 收尾已清除标记
    with gate.transfer() as lease2:  # 完成式间隔后下一个许可正常签发，无泄漏
        assert lease2.granted_at >= send_time + MIN_REQUEST_INTERVAL


def test_transfer_error_exit_still_persists_send_spacing(tmp_path):
    """传输异常退出：收尾写入仍在解锁前清除标记并保存完成式间隔，且
    不触碰交换中登记的冷却（完成时刻不早于实际发送，间隔只强不弱）。"""
    state = tmp_path / "gate.json"
    clock = FakeClock(start=1000.0)
    gate = make_slow_write_gate(state, clock, delay=4.0)
    with pytest.raises(RuntimeError, match="boom"), gate.transfer() as lease:
        send_time = clock.now()
        lease.record_cooldown(send_time + 1.0)  # 交换中登记服务端冷却
        raise RuntimeError("boom")
    snapshot = gate.snapshot()
    assert snapshot.next_allowed_at >= send_time + MIN_REQUEST_INTERVAL
    assert snapshot.cooldown_until == send_time + 1.0  # 冷却未被触碰
    assert snapshot.in_flight_since is None  # 错误退出同样清除标记


def test_transfer_zero_budget_tolerates_write_latency_and_extends_basis(tmp_path):
    """零预算非阻塞尝试容忍写入 I/O 耗时，且间距基准同样跟随实际发送。"""
    state = tmp_path / "gate.json"
    clock = FakeClock(start=1000.0)
    gate = make_slow_write_gate(state, clock, delay=0.2)
    with gate.transfer(max_wait=0.0) as lease:
        assert lease.granted_at == 1000.0
        send_time = clock.now()
    snapshot = gate.snapshot()
    assert snapshot.next_allowed_at == pytest.approx(send_time + MIN_REQUEST_INTERVAL)


CRASH_SCRIPT = textwrap.dedent(
    """
    import os, sys, time
    from hvs_catalog.pipeline.acquisition.rate_limit import RateLimitGate

    gate = RateLimitGate(sys.argv[1])
    with gate.transfer() as lease:
        send = time.time()
        print(f"SEND {send:.6f}", flush=True)
        time.sleep(0.1)  # 交换进行中
        os._exit(9)  # 崩溃：不解锁、不执行退出补齐
    """
)


def test_crashed_exchange_leaves_conservative_spacing_state(tmp_path):
    """交换中硬崩溃（丢失收尾写入）：OS 释放锁，在途标记在场使下一
    个许可 fail-closed（extra2 语义修正：不再按可能过早的预登记等
    待后放行——旧断言属不安全自动恢复，新言更强：直接拒绝）。"""
    state = tmp_path / "gate.json"
    crashed = subprocess.run(
        [sys.executable, "-c", CRASH_SCRIPT, str(state)],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert crashed.returncode == 9, crashed.stderr
    (line,) = [ln for ln in crashed.stdout.splitlines() if ln.startswith("SEND")]
    send_time = float(line.split()[1])
    gate = RateLimitGate(state, clock=SystemClock())
    snapshot = gate.snapshot()
    assert snapshot.state_error is None
    # 崩溃时标记在场：未确认在途，任何接管者 fail-closed。
    assert snapshot.in_flight_since is not None
    assert snapshot.next_allowed_at >= send_time - 1.0  # 预登记也在场
    with (
        pytest.raises(RateLimitUnconfirmedError, match="在途"),
        gate.transfer(max_wait=0.0),
    ):
        pass


SLOW_CRASH_SCRIPT = textwrap.dedent(
    """
    import os, sys
    from hvs_catalog.pipeline.acquisition.rate_limit import RateLimitGate

    class FakeClock:
        def __init__(self):
            self._now = 1000.0

        def now(self):
            return self._now

        def sleep(self, seconds):
            self._now += max(0.0, float(seconds))

    clock = FakeClock()
    gate = RateLimitGate(sys.argv[1], clock=clock)
    original_write = gate._write_state_file

    def slow_write(payload):
        clock.sleep(4.0)  # 每次状态写入延迟 4 秒（虚拟时间）
        original_write(payload)

    gate._write_state_file = slow_write
    with gate.transfer() as lease:
        print(f"SEND {clock.now():.3f}", flush=True)  # 实际发送时刻 1004
        os._exit(9)  # 发送后、收尾写入前硬崩溃：标记留在盘上
    """
)


def test_slow_write_crash_fast_successor_refuses_immediately(tmp_path):
    """审核者同构场景：慢写入首客户端实际发送后崩溃，重启的快速接
    管者必须**立即拒绝**（在途标记），而不是等已过期的预登记放行。"""
    state = tmp_path / "gate.json"
    crashed = subprocess.run(
        [sys.executable, "-c", SLOW_CRASH_SCRIPT, str(state)],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert crashed.returncode == 9, crashed.stderr
    (line,) = [ln for ln in crashed.stdout.splitlines() if ln.startswith("SEND")]
    assert float(line.split()[1]) == 1004.0  # 实际发送时刻（写前基准 1000）
    successor = RateLimitGate(state, clock=FakeClock(start=1004.0))
    snapshot = successor.snapshot()
    assert snapshot.in_flight_since == 1000.0  # 标记在场
    assert snapshot.next_allowed_at == 1003.0  # 预登记已过期（旧缺陷会放行）
    # 快速接管者立即拒绝，零等待（不是把过期预登记等过去）。
    with (
        pytest.raises(RateLimitUnconfirmedError, match="在途"),
        successor.transfer(max_wait=100.0),
    ):
        pass
    assert successor.clock.sleeps == []


def test_marker_initial_write_failure_never_fetches(tmp_path):
    """在途标记首次写入失败：不交付租约、不进入交换体（未发送）。"""
    state = tmp_path / "gate.json"
    clock = FakeClock(start=1000.0)
    gate = RateLimitGate(state, clock=clock)
    original_write = gate._write_state_file

    def failing_write(payload: dict) -> None:
        raise OSError("simulated initial write failure")

    gate._write_state_file = failing_write  # type: ignore[method-assign]
    entered = []
    try:
        with pytest.raises(OSError, match="initial write"), gate.transfer():
            entered.append(True)  # 不应执行
    finally:
        gate._write_state_file = original_write  # type: ignore[method-assign]
    assert entered == []  # 未交付：交换体从未运行
    assert not state.exists()  # 未写入任何状态


def test_exit_persistence_failure_keeps_marker_and_refuses_successor(tmp_path):
    """收尾写入失败：错误可观察（不静默吞掉），标记保留，快速接管者
    fail-closed；无主体异常时链指向持久化错误本身。"""
    state = tmp_path / "gate.json"
    clock = FakeClock(start=1000.0)
    gate = RateLimitGate(state, clock=clock)
    original_write = gate._write_state_file
    calls = {"n": 0}

    def fail_on_second_write(payload: dict) -> None:
        calls["n"] += 1
        if calls["n"] >= 2:
            raise OSError("simulated exit write failure")
        original_write(payload)

    gate._write_state_file = fail_on_second_write  # type: ignore[method-assign]
    try:
        with (
            pytest.raises(RateLimitPersistenceError, match="未能可靠落盘"),
            gate.transfer(),
        ):
            pass  # 正常完成，但收尾写入失败
        snapshot = gate.snapshot()
        assert snapshot.in_flight_since == 1000.0  # 标记保守保留
        assert snapshot.next_allowed_at == 1003.0
        with (
            pytest.raises(RateLimitUnconfirmedError, match="在途"),
            gate.transfer(max_wait=0.0),
        ):
            pass
    finally:
        gate._write_state_file = original_write  # type: ignore[method-assign]


def test_exit_persistence_failure_chains_body_error(tmp_path):
    """交换体异常 + 收尾写入失败：持久化错误经 __cause__ 链保留原始
    传输异常，可诊断。"""
    state = tmp_path / "gate.json"
    clock = FakeClock(start=1000.0)
    gate = RateLimitGate(state, clock=clock)
    original_write = gate._write_state_file
    calls = {"n": 0}

    def fail_on_second_write(payload: dict) -> None:
        calls["n"] += 1
        if calls["n"] >= 2:
            raise OSError("simulated exit write failure")
        original_write(payload)

    gate._write_state_file = fail_on_second_write  # type: ignore[method-assign]
    try:
        with pytest.raises(RateLimitPersistenceError) as exc_info, gate.transfer():
            raise RuntimeError("transport boom")
        assert isinstance(exc_info.value.__cause__, RuntimeError)
        assert str(exc_info.value.__cause__) == "transport boom"
    finally:
        gate._write_state_file = original_write  # type: ignore[method-assign]
    assert gate.snapshot().in_flight_since is not None


def test_safe_completed_replacement_then_dir_fsync_error(tmp_path, monkeypatch):
    """替换成功后目录 fsync 失败（替换模糊性的安全侧）：目录 fsync 错
    误被有意容忍（部分文件系统不支持）；替换已完成，盘上为完成式状
    态（无标记、有安全间隔），接管者按正常间距等待而非 fail-closed。
    崩溃边界解释：此时若断电，目录项可能回滚到旧状态（含标记 →
    fail-closed）或保持新状态（完成式间隔 → 正常等待），两种结果都
    不产生“无标记且无安全间隔”的已发送请求；替换**前**的写入/
    fsync 失败则会传播（见收尾失败测试：标记保留、fail-closed）。"""
    import stat as stat_module

    state = tmp_path / "gate.json"
    clock = FakeClock(start=1000.0)
    gate = RateLimitGate(state, clock=clock)
    real_fsync = os.fsync
    fail = {"on": False}

    def flaky_fsync(fd: int) -> None:
        if fail["on"] and stat_module.S_ISDIR(os.fstat(fd).st_mode):
            raise OSError("simulated dir fsync failure")
        return real_fsync(fd)

    monkeypatch.setattr(os, "fsync", flaky_fsync)
    with gate.transfer():  # 目录 fsync 失败被容忍：正常完成、不抛错
        fail["on"] = True  # 从收尾开始让目录 fsync 失败（替换已完成）
    payload = json.loads(state.read_text(encoding="utf-8"))
    assert payload.get("in_flight_since") is None  # 替换已生效：无标记
    assert payload["next_allowed_at"] == 1003.0  # 完成式间隔在场
    # 接管者不 fail-closed：按正常间距等待（零预算被间距拒绝）。
    successor = RateLimitGate(state, clock=FakeClock(start=1001.0))
    with (
        pytest.raises(RateLimitError, match="节流门要求再等待"),
        successor.transfer(max_wait=0.0),
    ):
        pass


def test_marker_retained_by_lease_and_standalone_cooldown_writes(tmp_path):
    """交换中的租约冷却登记与传输外的独立冷却登记都不得丢失在途标
    记（标记只能由正常收尾清除）。"""
    state = tmp_path / "gate.json"
    clock = FakeClock(start=1000.0)
    gate = RateLimitGate(state, clock=clock)
    with gate.transfer() as lease:
        on_disk = json.loads(state.read_text(encoding="utf-8"))
        assert on_disk["in_flight_since"] == 1000.0  # 交付前已持久化标记
        lease.record_cooldown(1050.0)
        on_disk = json.loads(state.read_text(encoding="utf-8"))
        assert on_disk["in_flight_since"] == 1000.0  # 冷却登记后标记仍在
        assert on_disk["cooldown_until"] == 1050.0
    snapshot = gate.snapshot()
    assert snapshot.in_flight_since is None  # 正常收尾后清除
    assert snapshot.cooldown_until == 1050.0
    # 独立 record_cooldown 同样保留既有标记（用手工标记状态模拟崩溃现场）。
    write_state(
        state, '{"version": 2, "in_flight_since": 1000.0, "next_allowed_at": 1003.0}'
    )
    gate.record_cooldown(1052.0)
    snapshot = gate.snapshot()
    assert snapshot.in_flight_since == 1000.0
    assert snapshot.cooldown_until == 1052.0
    assert snapshot.next_allowed_at == 1003.0


def test_reset_and_snapshot_handle_unconfirmed_marker(tmp_path):
    """reset 不能清除或绕过在途标记；snapshot 明确报告未确认状态。"""
    state = tmp_path / "gate.json"
    write_state(
        state,
        '{"version": 2, "in_flight_since": 1000.0, "next_allowed_at": 1003.0, "cooldown_until": 1002.0}',
    )
    gate = RateLimitGate(state, clock=FakeClock(start=1100.0))  # 间隔/冷却均早已过期
    snapshot = gate.snapshot()
    assert snapshot.in_flight_since == 1000.0
    assert snapshot.state_error is None
    with pytest.raises(RateLimitUnconfirmedError, match="在途"):
        gate.reset()  # 即使间隔/冷却都过期，标记仍不可被恢复操作绕过
    assert json.loads(state.read_text(encoding="utf-8"))["in_flight_since"] == 1000.0


# ---------------------------------------------------------------- 连接租约


def test_transfer_spacing_survives_process_restart(tmp_path):
    clock = FakeClock(start=1000.0)
    grants = []
    for _ in range(3):
        gate = RateLimitGate(tmp_path / "gate.json", clock=clock)  # 每次新实例=重启
        with gate.transfer() as lease:
            grants.append(lease.granted_at)
    assert grants == [1000.0, 1003.0, 1006.0]
    # 等待在锁外小步进行：总等待量正确，且每步不超过重核步长。
    assert sum(clock.sleeps) == pytest.approx(6.0)
    assert clock.sleeps and all(step <= WAIT_RECHECK_SECONDS for step in clock.sleeps)


def test_transfer_waits_for_cooldown_registered_before_start(tmp_path):
    clock = FakeClock(start=1000.0)
    gate = RateLimitGate(tmp_path / "gate.json", clock=clock)
    with gate.transfer():
        pass  # next_allowed_at = 1003
    gate.record_cooldown(1040.0)
    gate2 = RateLimitGate(tmp_path / "gate.json", clock=clock)
    with gate2.transfer() as lease:
        assert lease.granted_at == 1040.0
    assert sum(clock.sleeps) == pytest.approx(40.0)
    assert gate2.snapshot().next_allowed_at == 1043.0


def test_transfer_rechecks_cooldown_registered_while_waiting(tmp_path):
    """等待期间登记的冷却必须在发送前生效（无预留许可绕过新冷却）。

    等待在锁外小步进行：第一步等待内由另一门实例（模拟另一进程）
    登记亚长冷却；主门下一步重读时看到并等到冷却结束才签发租约。
    """
    clock = FakeClock(start=1000.0)
    gate = RateLimitGate(tmp_path / "gate.json", clock=clock)
    gate.record_cooldown(1010.0)  # 先有一个较短冷却
    cooldown_added = False
    original_sleep = clock.sleep

    def interleaved_sleep(seconds: float) -> None:
        nonlocal cooldown_added
        if not cooldown_added:
            # 此刻主门已让出锁（锁外分段等待）：另一进程可以登记冷却。
            other = RateLimitGate(tmp_path / "gate.json", clock=clock)
            other.record_cooldown(1050.0)
            cooldown_added = True
        original_sleep(seconds)

    clock.sleep = interleaved_sleep  # type: ignore[method-assign]
    with gate.transfer() as lease:
        assert cooldown_added
        assert lease.granted_at == 1050.0
    assert gate.snapshot().next_allowed_at == 1053.0


def test_lease_records_cooldown_without_nested_lock(tmp_path):
    clock = FakeClock(start=1000.0)
    gate = RateLimitGate(tmp_path / "gate.json", clock=clock)
    with gate.transfer() as lease:
        lease.record_cooldown(1020.0)
        lease.record_cooldown(1015.0)  # 合并保留较晚者
    fresh = RateLimitGate(tmp_path / "gate.json", clock=FakeClock(1000.0))
    assert fresh.snapshot().cooldown_until == 1020.0


def test_lease_rejects_use_after_release(tmp_path):
    gate = RateLimitGate(tmp_path / "gate.json", clock=FakeClock())
    with gate.transfer() as lease:
        pass
    with pytest.raises(RuntimeError, match="已结束"):
        lease.record_cooldown(1100.0)


def test_lease_rejects_nonfinite_cooldown(tmp_path):
    gate = RateLimitGate(tmp_path / "gate.json", clock=FakeClock())
    with gate.transfer() as lease:
        with pytest.raises(ValueError):
            lease.record_cooldown(float("inf"))
        with pytest.raises(ValueError):
            lease.record_cooldown(float("nan"))


# ---------------------------------------------------------------- 冷却持久


def test_record_cooldown_rejects_invalid_values(tmp_path):
    gate = RateLimitGate(tmp_path / "gate.json", clock=FakeClock())
    with pytest.raises(ValueError):
        gate.record_cooldown(float("inf"))
    with pytest.raises(ValueError):
        gate.record_cooldown(float("nan"))


def test_cooldown_merge_keeps_the_later_deadline(tmp_path):
    clock = FakeClock()
    gate = RateLimitGate(tmp_path / "gate.json", clock=clock)
    gate.record_cooldown(1050.0)
    gate.record_cooldown(1020.0)
    assert gate.snapshot().cooldown_until == 1050.0


# ---------------------------------------------------------------- 状态健壮性（fail-closed）


def write_state(path, payload: str) -> None:
    path.write_text(payload, encoding="utf-8")


@pytest.mark.parametrize(
    "payload",
    [
        "not-json{",
        "",
        '{"version": 99, "next_allowed_at": 999999.0}',
        '{"version": true, "cooldown_until": null}',
        '{"version": 1.0, "next_allowed_at": 100.0}',
        '{"version": "1", "next_allowed_at": 100.0}',
        '{"version": null, "next_allowed_at": 100.0}',
        '{"version": [1], "next_allowed_at": 100.0}',
        '{"version": 2, "in_flight_since": "soon"}',
        '{"version": 2, "in_flight_since": true}',
        '{"version": 2, "in_flight_since": [1]}',
        '{"version": 2, "in_flight_since": 1e999}',
        '{"next_allowed_at": 100.0}',
        '{"version": 1, "next_allowed_at": "soon"}',
        '{"version": 1, "cooldown_until": null, "updated_at": [1]}',
        '["not", "a", "dict"]',
    ],
)
def test_corrupt_state_fails_closed_and_preserves_evidence(tmp_path, payload):
    path = tmp_path / "gate.json"
    write_state(path, payload)
    clock = FakeClock(start=100.0)
    gate = RateLimitGate(path, clock=clock)
    with pytest.raises(RateLimitStateError), gate.transfer():
        pass
    with pytest.raises(RateLimitStateError):
        gate.record_cooldown(200.0)
    with pytest.raises(RateLimitStateError):
        gate.reset()  # 损坏状态的冷却是未知而非零：默认恢复同样拒绝
    # 现场保留：损坏内容未被自动重置或覆盖。
    assert path.read_text(encoding="utf-8") == payload


def test_corrupt_state_does_not_discard_known_unexpired_cooldown(tmp_path):
    """损坏前已登记的冷却不能因状态损坏而被静默丢弃。"""
    path = tmp_path / "gate.json"
    clock = FakeClock(start=100.0)
    gate = RateLimitGate(path, clock=clock)
    gate.record_cooldown(9999.0)
    write_state(path, "{broken")  # 模拟崩溃写入
    gate2 = RateLimitGate(path, clock=clock)
    # 无法确认冷却是否有效：拒绝发送（fail-closed），等待显式恢复。
    with pytest.raises(RateLimitStateError), gate2.transfer():
        pass
    assert path.read_text(encoding="utf-8") == "{broken"


def test_snapshot_reports_corruption_without_overwriting(tmp_path):
    path = tmp_path / "gate.json"
    write_state(path, "{broken")
    gate = RateLimitGate(path, clock=FakeClock())
    snapshot = gate.snapshot()
    assert isinstance(snapshot, RateGateSnapshot)
    assert snapshot.state_error is not None and "损坏" in snapshot.state_error
    assert snapshot.next_allowed_at is None
    assert path.read_text(encoding="utf-8") == "{broken"


def test_recovery_reset_clears_fully_expired_state(tmp_path):
    """间隔与冷却都已自然过期的有效状态可清空（清空不绕过任何约束）。"""
    clock = FakeClock(start=1000.0)
    gate = RateLimitGate(tmp_path / "gate.json", clock=clock)
    with gate.transfer():
        pass  # next_allowed_at = 1003
    gate.record_cooldown(1010.0)
    clock.sleep(20.0)  # 两个约束都自然过期
    gate.reset()
    assert gate.snapshot().next_allowed_at is None
    assert gate.snapshot().cooldown_until is None
    with gate.transfer() as lease:
        assert lease.granted_at == 1020.0


def test_reset_refuses_unexpired_cooldown_with_no_bypass_flag(tmp_path):
    """未过期冷却不可被恢复操作清除，且不存在强制清除标志。"""
    import inspect

    parameters = inspect.signature(RateLimitGate.reset).parameters
    assert list(parameters) == ["self"]  # 没有强制清除参数
    clock = FakeClock(start=1000.0)
    gate = RateLimitGate(tmp_path / "gate.json", clock=clock)
    gate.record_cooldown(5000.0)
    with pytest.raises(RateLimitError, match="未到期"):
        gate.reset()
    assert gate.snapshot().cooldown_until == 5000.0  # 未被静默丢弃
    clock.sleep(4001.0)  # 冷却自然过期后清空才被允许
    gate.reset()
    assert gate.snapshot().cooldown_until is None


def test_reset_refuses_active_spacing(tmp_path):
    """审核复现场景：租约后立即 reset 不得绕过生产 3 秒间隔。"""
    clock = FakeClock(start=100.0)
    gate = RateLimitGate(tmp_path / "gate.json", clock=clock)
    with gate.transfer():
        pass  # next_allowed_at = 103
    with pytest.raises(RateLimitError, match="未到期"):
        gate.reset()
    # 间隔内的新租约仍受 3 秒约束；自然过期后恢复可用。
    with gate.transfer() as lease:
        assert lease.granted_at == 103.0
    with pytest.raises(RateLimitError):
        gate.reset()  # 新租约又登记了 106
    clock.sleep(3.0)
    gate.reset()
    with gate.transfer() as lease:
        assert lease.granted_at == 106.0


def test_snapshot_defaults_when_no_file(tmp_path):
    gate = RateLimitGate(tmp_path / "gate.json", clock=FakeClock())
    snapshot = gate.snapshot()
    assert snapshot.next_allowed_at is None
    assert snapshot.cooldown_until is None
    assert snapshot.updated_at is None
    assert snapshot.state_error is None
    assert not (tmp_path / "gate.json").exists()


# ---------------------------------------------------------------- 跨进程


MULTI_PROCESS_SCRIPT = textwrap.dedent(
    """
    import sys, time
    from hvs_catalog.pipeline.acquisition.rate_limit import RateLimitGate
    from hvs_catalog.pipeline.acquisition.transport import (
        ArxivHttpClient, TransportResponse,
    )

    class SlowFakeTransport:
        def fetch(self, url, *, headers=None, timeout=None, max_bytes=None):
            start = time.time()
            time.sleep(float(sys.argv[2]))
            end = time.time()
            print(f"WINDOW {start:.6f} {end:.6f}", flush=True)
            return TransportResponse(
                status_code=200,
                headers={"content-type": "application/atom+xml"},
                body=b"ok",
                request_url=url,
                final_url=url,
            )

    gate = RateLimitGate(sys.argv[1])
    client = ArxivHttpClient(transport=SlowFakeTransport(), gate=gate)
    client.get("https://export.arxiv.org/api/query?x=1")
    """
)


LOCK_HOLDER_SCRIPT = textwrap.dedent(
    """
    import os, sys, time
    import fcntl

    lock_path = sys.argv[1]
    hold = float(sys.argv[2])
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX)
    print("HELD", flush=True)
    time.sleep(hold)
    fcntl.flock(fd, fcntl.LOCK_UN)
    os.close(fd)
    print("RELEASED", flush=True)
    """
)

TRANSFER_SCRIPT = textwrap.dedent(
    """
    import sys, time
    from hvs_catalog.pipeline.acquisition.rate_limit import RateLimitGate

    gate = RateLimitGate(sys.argv[1])
    started = time.time()
    with gate.transfer() as lease:
        print(f"GRANTED {lease.granted_at:.6f} {started:.6f}", flush=True)
    """
)


def test_reset_on_missing_state_normalizes_fresh(tmp_path):
    """尚无状态文件时 reset 写入干净空状态（不涉及任何约束）。"""
    gate = RateLimitGate(tmp_path / "gate.json", clock=FakeClock(start=100.0))
    gate.reset()
    assert gate.snapshot().state_error is None
    with gate.transfer() as lease:
        assert lease.granted_at == 100.0


def test_first_creation_race_with_held_coordination_lock(tmp_path):
    """首次创建竞态：稳定锁先于数据文件可见性。

    进程 A 持有锁文件（模拟正在初始化/写入的另一使用者）期间，进程
    B 的完整 transfer 并发运行：B 必须阻塞等待而不是把未见数据文件
    误判为损坏；A 释放后 B 正常获得许可并初始化状态。
    """
    state = tmp_path / "gate.json"
    holder = subprocess.Popen(
        [sys.executable, "-c", LOCK_HOLDER_SCRIPT, str(state) + ".lock", "1.5"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert holder.stdout is not None
        assert holder.stdout.readline().strip() == "HELD"  # A 已持有锁
        import time as time_module

        started = time_module.monotonic()
        waiter = subprocess.run(
            [sys.executable, "-c", TRANSFER_SCRIPT, str(state)],
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
        elapsed = time_module.monotonic() - started
        assert waiter.returncode == 0, waiter.stderr
        assert "GRANTED" in waiter.stdout
        # B 确实等待了锁（而非误判损坏立即失败）。
        assert elapsed >= 1.0
    finally:
        holder_rest, holder_err = holder.communicate(timeout=120)
        assert holder.returncode == 0, holder_err
        assert "RELEASED" in holder_rest
        if holder.poll() is None:  # pragma: no cover - 清理未退出进程
            holder.kill()
            holder.wait(timeout=30)
    # 状态文件完整存在（原子替换后无空文件窗口）。
    payload = json.loads(state.read_text(encoding="utf-8"))
    assert payload["version"] == 2


def test_concurrent_first_use_two_processes_both_succeed(tmp_path):
    """两个进程同时首次使用同一状态：都成功且许可间隔 ≥3 秒。"""
    state = tmp_path / "gate.json"
    processes = [
        subprocess.Popen(
            [sys.executable, "-c", TRANSFER_SCRIPT, str(state)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for _ in range(2)
    ]
    grants = []
    for process in processes:
        stdout, stderr = process.communicate(timeout=120)
        assert process.returncode == 0, stderr
        (line,) = [ln for ln in stdout.splitlines() if ln.startswith("GRANTED")]
        grants.append(float(line.split()[1]))
    grants.sort()
    assert grants[1] - grants[0] >= 2.95


def test_client_transfers_do_not_overlap_across_processes(tmp_path):
    """两个真实进程经客户端传输（每次 >3 秒）：连接窗口不得重叠。

    同时核对实际发送时刻的间距 ≥3 秒：租约覆盖整个 HTTP 交换，第二
    个进程只有在第一个释放锁后才可能开始。
    """
    state = tmp_path / "gate.json"
    processes = [
        subprocess.Popen(
            [sys.executable, "-c", MULTI_PROCESS_SCRIPT, str(state), "4.0"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for _ in range(2)
    ]
    windows = []
    for process in processes:
        stdout, stderr = process.communicate(timeout=180)
        assert process.returncode == 0, stderr
        (line,) = [ln for ln in stdout.splitlines() if ln.startswith("WINDOW")]
        start_text, end_text = line.split()[1:3]
        windows.append((float(start_text), float(end_text)))
    (start1, end1), (start2, _end2) = sorted(windows)
    # 连接窗口不重叠（允许调度层面的极小 epsilon）。
    assert start2 >= end1 - 0.25
    # 实际发送时刻间距满足 3 秒下限。
    assert start2 - start1 >= 2.95
    # 状态文件记录了两次预登记。
    state_json = json.loads(state.read_text(encoding="utf-8"))
    assert state_json["version"] == 2


# ---------------------------------------------------------------- 副作用与默认路径


def test_importing_modules_has_no_disk_side_effects(tmp_path):
    script = (
        "import hvs_catalog.pipeline.acquisition.protocol as p;"
        "import hvs_catalog.pipeline.acquisition.transport as t;"
        "import hvs_catalog.pipeline.acquisition.rate_limit as r;"
        "assert p.ARXIV_API_BASE_URL and t.USER_AGENT and r.MIN_REQUEST_INTERVAL"
    )
    env = {
        "HOME": str(tmp_path),
        "XDG_CACHE_HOME": str(tmp_path / "xdg"),
        "HVS_CATALOG_ARXIV_RATE_STATE": str(tmp_path / "override.json"),
    }
    result = subprocess.run(
        [sys.executable, "-c", script],
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert not list(tmp_path.rglob("arxiv-rate-gate.json"))
    assert not (tmp_path / "override.json").exists()


def test_default_state_path_honors_env_override(tmp_path):
    override = tmp_path / "alt.json"
    old = os.environ.get("HVS_CATALOG_ARXIV_RATE_STATE")
    os.environ["HVS_CATALOG_ARXIV_RATE_STATE"] = str(override)
    try:
        assert default_rate_gate_state_path() == override
    finally:
        if old is None:
            os.environ.pop("HVS_CATALOG_ARXIV_RATE_STATE", None)
        else:
            os.environ["HVS_CATALOG_ARXIV_RATE_STATE"] = old


def test_system_clock_exists_for_production():
    clock = SystemClock()
    assert 0 < clock.now() < float("inf")
    first = clock.monotonic()
    second = clock.monotonic()
    assert first <= second  # 单调读数不回退；仅用于等待预算
    assert 0 < first < float("inf")

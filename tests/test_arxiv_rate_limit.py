"""Synthetic arXiv throttle tests: connection leases, minimum spacing, cooldown, and
cross-process coordination (lane A).

Unit tests inject clocks so the three-second spacing uses virtual time. Cross-process
tests use real subprocesses and clocks to verify file locks, nonoverlapping connections,
and shared cooldown. State files stay in temporary test directories, outside user
caches.
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


# ---------------------------------------------------------------- Minimum spacing


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
    assert state.get("in_flight_since") is None  # Finalization clears the marker.


def test_atomic_write_leaves_no_temp_artifacts(tmp_path):
    """Atomic replacement leaves no temporary files; the lock file is separate from the
    data file.
    """
    clock = FakeClock(start=1000.0)
    gate = RateLimitGate(tmp_path / "gate.json", clock=clock)
    for _ in range(3):
        with gate.transfer():
            pass
        gate.record_cooldown(clock.now() + 5)
    assert not (tmp_path / "gate.json.tmp").exists()
    assert (tmp_path / "gate.json.lock").exists()  # Stable coordination lock.
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
    """Reject waits above the limit without granting a permit or modifying state."""
    clock = FakeClock(start=1000.0)
    gate = RateLimitGate(tmp_path / "gate.json", clock=clock)
    gate.record_cooldown(1040.0)
    with pytest.raises(RateLimitError) as excinfo, gate.transfer(max_wait=10.0):
        pass
    assert excinfo.value.wait_seconds == pytest.approx(40.0)
    snapshot = gate.snapshot()
    assert (
        snapshot.cooldown_until == 1040.0
    )  # No permit is held and state is unchanged.
    assert snapshot.next_allowed_at is None
    # An unlimited lease still obtains its permit at the original time.
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


# --------------------------------------- Fixed total budget and bounded admission


def make_extending_clock(clock: FakeClock, state_path, *, extend_seconds: float):
    """After each wait outside the lock, extend shared cooldown to now + extend_seconds.

    Simulate repeated short cooldown extensions by another gate while the main gate
    yields its lock. Return a restoration function; call it before assertions without
    extension simulation. Otherwise an unlimited lease waits forever under repeatedly
    extended cooldown, as documented for max_wait=None.
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
    """Review reproducer: repeated small cooldown extensions must not reset the fixed total
    budget.

    With max_wait=3, another gate extends cooldown to now +2 after every step. Without a
    fixed deadline, each remaining wait stays within the limit and never rejects. Reject
    once the budget expires, without granting a permit or discarding cooldown.
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
    # Consume only the fixed budget; each wait stays within the recheck step.
    assert clock.sleeps and all(step <= WAIT_RECHECK_SECONDS for step in clock.sleeps)
    assert sum(clock.sleeps) <= 3.0
    # Grant no permit and retain the persisted cooldown, last extended to 1004.
    snapshot = RateLimitGate(state, clock=FakeClock(1000.0)).snapshot()
    assert snapshot.next_allowed_at is None
    assert snapshot.cooldown_until == 1004.0
    # After extensions stop, an unlimited lease obtains a permit when retained cooldown expires.
    restore()
    with gate.transfer() as lease:
        assert lease.granted_at == 1004.0


def test_transfer_max_wait_exact_boundary_grants(tmp_path):
    """Issue a permit when cooldown ends exactly at the budget deadline."""
    clock = FakeClock(start=1000.0)
    gate = RateLimitGate(tmp_path / "gate.json", clock=clock)
    gate.record_cooldown(1003.0)  # Required wait is three seconds, equal to max_wait.
    with gate.transfer(max_wait=3.0) as lease:
        assert lease.granted_at == 1003.0
    assert sum(clock.sleeps) == pytest.approx(3.0)


def test_transfer_max_wait_exceeded_rejects_without_sleeping(tmp_path):
    """Reject immediately without sleeping or changing state if the initial wait exceeds
    the budget.

    The budget is a ceiling, not a required sleep duration. The preceding
    repeated-extension regression covers exhaustion after actual waits.
    """
    state = tmp_path / "gate.json"
    clock = FakeClock(start=1000.0)
    gate = RateLimitGate(state, clock=clock)
    gate.record_cooldown(1003.5)  # Required wait 3.5 seconds exceeds max_wait=3.
    with (
        pytest.raises(RateLimitError, match="总等待预算"),
        gate.transfer(max_wait=3.0),
    ):
        pass
    assert (
        clock.sleeps == []
    )  # Clearly excessive: reject immediately without consuming time.
    snapshot = gate.snapshot()
    assert snapshot.next_allowed_at is None  # No permit is granted.
    assert (
        snapshot.cooldown_until == 1003.5
    )  # Cooldown is neither changed nor discarded.
    with gate.transfer() as lease:
        assert lease.granted_at == 1003.5


def test_transfer_initial_lock_contention_bounded_by_deadline(tmp_path):
    """Initial lock contention consumes the same fixed budget; timeout grants no lock and
    changes no state.
    """
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
        # Bounded polling consumes exactly the allowed total wait.
        assert clock.sleeps and all(
            step <= LOCK_WAIT_POLL_SECONDS for step in clock.sleeps
        )
        assert sum(clock.sleeps) == pytest.approx(0.5)
        # No permit or data file is created: no persistent side effects.
        assert not state.exists()
    finally:
        fcntl.flock(holder_fd, fcntl.LOCK_UN)
        os.close(holder_fd)
    with gate.transfer() as lease:  # Obtain a permit normally after release.
        assert lease.granted_at == pytest.approx(1000.5)


def test_transfer_reacquire_contention_bounded_by_deadline(tmp_path):
    """Lock reacquisition contention during waiting consumes the same fixed budget."""
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
        # While the main gate yields, another holder acquires the lock without releasing it; use nonblocking acquisition.
        # Do not acquire twice when already held; blocking acquisition would deadlock with the existing holder.
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
        # Record assertion evidence before releasing the holder in finally. snapshot takes a blocking
        # lock, so do not call it while the holder still owns the lock.
        first_step = clock.sleeps[0]
        later_steps = tuple(clock.sleeps[1:])
        total_slept = sum(clock.sleeps)
    finally:
        clock.sleep = original_sleep  # Restore the hook so subsequent unlimited leases do not block forever.
        for fd in holder_fds:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)
    assert first_step == 1.0  # The first step rechecks cooldown.
    assert all(step <= LOCK_WAIT_POLL_SECONDS for step in later_steps)
    assert total_slept == pytest.approx(
        3.0
    )  # Lock reacquisition exhausts the remaining budget.
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
        assert clock.sleeps == []  # Zero budget rejects immediately without sleeping.
    finally:
        fcntl.flock(holder_fd, fcntl.LOCK_UN)
        os.close(holder_fd)


# ------------------------------- Late admission: reject delayed locks or reads


def test_transfer_late_lock_poll_oversleep_rejects_beyond_budget(tmp_path):
    """If polling sleeps beyond its requested step, reject admission strictly past the
    deadline even when the lock subsequently becomes available and state is ready.
    """
    import fcntl

    state = tmp_path / "gate.json"
    lock_path = tmp_path / "gate.json.lock"
    clock = FakeClock(start=1000.0)
    seed = RateLimitGate(state, clock=FakeClock(1000.0))
    seed.record_cooldown(
        1000.5
    )  # Ready state would grant immediately without the late-admission check.
    holder_fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(holder_fd, fcntl.LOCK_EX)
    original_sleep = clock.sleep

    def oversleeping_poll(seconds: float) -> None:
        original_sleep(seconds)
        original_sleep(
            1.0
        )  # Simulate scheduling delay by sleeping beyond the requested step.
        fcntl.flock(
            holder_fd, fcntl.LOCK_UN
        )  # The lock becomes available only after budget expiry.

    clock.sleep = oversleeping_poll  # type: ignore[method-assign]
    gate = RateLimitGate(state, clock=clock)
    try:
        with (
            pytest.raises(RateLimitError, match="取得协调锁时已超出"),
            gate.transfer(max_wait=0.5),
        ):
            pass
        assert clock.sleeps[0] <= LOCK_WAIT_POLL_SECONDS
        assert sum(clock.sleeps) > 0.5  # Actual wait has exceeded the budget.
    finally:
        clock.sleep = original_sleep
        fcntl.flock(holder_fd, fcntl.LOCK_UN)
        os.close(holder_fd)
    snapshot = gate.snapshot()
    assert snapshot.next_allowed_at is None  # No permit is granted.
    assert snapshot.cooldown_until == 1000.5  # Retain cooldown.
    with gate.transfer() as lease:  # No lock or file descriptor leaks.
        assert lease.granted_at >= 1000.5


def test_transfer_reacquire_oversleep_rejects_late_lock_success(tmp_path):
    """Reject delayed cooldown waits when successful lock reacquisition occurs after the
    deadline.
    """
    state = tmp_path / "gate.json"
    clock = FakeClock(start=1000.0)
    gate = RateLimitGate(state, clock=clock)
    gate.record_cooldown(1001.0)
    original_sleep = clock.sleep

    def oversleeping(seconds: float) -> None:
        original_sleep(seconds)
        original_sleep(3.0)  # Scheduling delay: a nominal one-second step reaches 1004.

    clock.sleep = oversleeping  # type: ignore[method-assign]
    try:
        with (
            pytest.raises(RateLimitError, match="取得协调锁时已超出"),
            gate.transfer(max_wait=1.0),
        ):
            pass
        assert clock.sleeps[0] == 1.0
        assert (
            sum(clock.sleeps) == 4.0
        )  # One requested second plus three delayed seconds.
    finally:
        clock.sleep = original_sleep
    snapshot = gate.snapshot()
    assert snapshot.next_allowed_at is None
    assert snapshot.cooldown_until == 1001.0
    with gate.transfer() as lease:
        assert (
            lease.granted_at == 1004.0
        )  # No leak: normal permit acquisition still works after delayed rejection.


def test_transfer_slow_state_read_past_deadline_rejects_before_permit(tmp_path):
    """Slow state reads (0.2 seconds each) exhaust a positive budget before admission even
    when state is ready; write no permit and retain cooldown.
    """
    state = tmp_path / "gate.json"
    clock = FakeClock(start=1000.0)
    gate = RateLimitGate(state, clock=clock)
    gate.record_cooldown(1001.0)
    original_read = gate._read_state_file

    def slow_read():
        clock.sleep(0.2)  # Simulate state I/O delay.
        return original_read()

    gate._read_state_file = slow_read  # type: ignore[method-assign]
    try:
        with (
            pytest.raises(RateLimitError, match="签发许可前已超出"),
            gate.transfer(max_wait=1.0),
        ):
            pass
        # Count every read toward the budget; reject when waiting plus reading exceeds one second.
        assert sum(clock.sleeps) > 1.0
    finally:
        gate._read_state_file = original_read  # type: ignore[method-assign]
    snapshot = gate.snapshot()
    assert snapshot.next_allowed_at is None
    assert snapshot.cooldown_until == 1001.0
    with gate.transfer() as lease:
        assert lease.granted_at >= 1001.0


def test_transfer_max_wait_zero_tolerates_state_read_latency(tmp_path):
    """Zero budget means a nonblocking attempt: do not wait for lock/cooldown, but tolerate
    state I/O time. A slow ready-state read does not reject immediately; zero budget is
    not a zero-duration promise.
    """
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


@pytest.mark.integration
def test_transfer_real_clock_delayed_read_rejects_late_admission(tmp_path):
    """Real-clock review reproducer: reject a 0.12-second read under a 0.02-second budget;
    open no connection and write no permit.
    """
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
    assert not (tmp_path / "gate.json").exists()  # No permit or state is written.
    with gate.transfer(
        max_wait=5.0
    ) as lease:  # No lock leak; ordinary admission works.
        assert lease.granted_at > 0


@pytest.mark.integration
def test_transfer_real_clock_immediate_attempt_succeeds(tmp_path):
    """Real-clock normal path: ready state without delay grants immediately under a small
    budget and persists preregistration.
    """
    clock = SystemClock()
    gate = RateLimitGate(tmp_path / "gate.json", clock=clock)
    with gate.transfer(max_wait=1.0) as lease:
        granted = lease.granted_at
        assert clock.now() - granted < 1.0  # Grant before the budget expires.
    snapshot = gate.snapshot()
    assert snapshot.next_allowed_at == pytest.approx(granted + MIN_REQUEST_INTERVAL)


# ------------------- Write/fsync boundaries and actual send spacing (extra1)


def make_slow_write_gate(state, clock, *, delay: float, max_wait=None):
    """Build a gate with injected write delay: every state write consumes delay seconds of
    virtual time.
    """
    gate = RateLimitGate(state, clock=clock)
    original_write = gate._write_state_file

    def slow_write(payload: dict) -> None:
        clock.sleep(delay)  # Simulate blocking write/fsync delay.
        original_write(payload)

    gate._write_state_file = slow_write  # type: ignore[method-assign]
    return gate


def test_transfer_write_delay_beyond_budget_rejects_without_delivery(tmp_path):
    """Reject positive-budget admission after an overlong write, delivering no lease and
    sending nothing. Retain persisted in-flight markers and preregistration
    conservatively. Under extra2 semantics, later requests fail closed instead of
    waiting out potentially premature preregistration; the old grant@1004 assertion
    allowed unsafe automatic recovery.
    """
    state = tmp_path / "gate.json"
    clock = FakeClock(start=1000.0)
    gate = make_slow_write_gate(state, clock, delay=4.0)
    with (
        pytest.raises(RateLimitError, match="许可预登记写入后已超出"),
        gate.transfer(max_wait=1.0),
    ):
        pass
    snapshot = gate.snapshot()
    assert (
        snapshot.next_allowed_at == 1003.0
    )  # Retain preregistration based on the pre-write time.
    assert snapshot.cooldown_until is None
    assert snapshot.in_flight_since == 1000.0  # Retain the in-flight marker too.
    # Unconfirmed in-flight state makes later requests fail closed; preregistration expiry cannot bypass it.
    with (
        pytest.raises(RateLimitUnconfirmedError, match="在途"),
        gate.transfer(max_wait=100.0),
    ):
        pass


@pytest.mark.integration
def test_transfer_real_clock_slow_write_rejects_before_delivery(tmp_path):
    """Real-clock review reproducer: a 0.12-second write under a 0.02-second budget
    rejects.
    """
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
    assert (
        snapshot.next_allowed_at is not None
    )  # Retain persisted conservative preregistration.
    assert snapshot.cooldown_until is None
    assert snapshot.in_flight_since is not None  # Retain the in-flight marker.
    # The OS lock is released without leaks; a zero-budget attempt rejects the in-flight marker under extra2
    # semantics rather than waiting for preregistration, providing stronger fail-closed behavior.
    with (
        pytest.raises(RateLimitUnconfirmedError, match="在途"),
        gate.transfer(max_wait=0.0),
    ):
        pass


def test_transfer_write_delay_persists_send_basis_reservation(tmp_path):
    """With four-second writes, completion-based spacing starts no earlier than actual send
    time 1004. Finalization clears the marker; the next permit is no earlier than actual
    send +3 seconds.
    """
    state = tmp_path / "gate.json"
    clock = FakeClock(start=1000.0)
    gate = make_slow_write_gate(state, clock, delay=4.0)
    with gate.transfer() as lease:
        assert lease.granted_at == 1000.0  # Admission decision time before writing.
        send_time = clock.now()  # Actual send time after writing the in-flight marker.
        assert send_time == 1004.0
    snapshot = gate.snapshot()
    assert snapshot.next_allowed_at == send_time + MIN_REQUEST_INTERVAL
    assert snapshot.cooldown_until is None
    assert snapshot.in_flight_since is None  # Finalization cleared the marker.
    with (
        gate.transfer() as lease2
    ):  # Grant normally after completion-based spacing, without leaks.
        assert lease2.granted_at >= send_time + MIN_REQUEST_INTERVAL


def test_transfer_error_exit_still_persists_send_spacing(tmp_path):
    """On transport exceptions, finalize before unlocking: clear the marker and preserve
    completion-based spacing without changing cooldown registered during the exchange.
    Completion is no earlier than actual send, so spacing never weakens.
    """
    state = tmp_path / "gate.json"
    clock = FakeClock(start=1000.0)
    gate = make_slow_write_gate(state, clock, delay=4.0)
    with pytest.raises(RuntimeError, match="boom"), gate.transfer() as lease:
        send_time = clock.now()
        lease.record_cooldown(
            send_time + 1.0
        )  # Register server cooldown during the exchange.
        raise RuntimeError("boom")
    snapshot = gate.snapshot()
    assert snapshot.next_allowed_at >= send_time + MIN_REQUEST_INTERVAL
    assert snapshot.cooldown_until == send_time + 1.0  # Cooldown is unchanged.
    assert snapshot.in_flight_since is None  # Error exit clears the marker too.


def test_transfer_zero_budget_tolerates_write_latency_and_extends_basis(tmp_path):
    """Zero-budget nonblocking attempts tolerate write I/O time and still base spacing on
    actual send time.
    """
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


@pytest.mark.integration
def test_crashed_exchange_leaves_conservative_spacing_state(tmp_path):
    """After a hard crash during exchange skips finalization, the OS releases the lock but
    the in-flight marker makes the next permit fail closed. Extra2 replaces unsafe
    automatic recovery based on possibly premature preregistration with immediate
    rejection.
    """
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
    # A retained crash marker means unconfirmed in-flight state; every successor fails closed.
    assert snapshot.in_flight_since is not None
    assert snapshot.next_allowed_at >= send_time - 1.0  # Preregistration remains too.
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


@pytest.mark.integration
def test_slow_write_crash_fast_successor_refuses_immediately(tmp_path):
    """Review reproducer: a slow writer crashes after sending; a restarted fast successor
    must reject immediately on the in-flight marker rather than admit after expired
    preregistration.
    """
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
    assert (
        float(line.split()[1]) == 1004.0
    )  # Actual send time; the pre-write reference was 1000.
    successor = RateLimitGate(state, clock=FakeClock(start=1004.0))
    snapshot = successor.snapshot()
    assert snapshot.in_flight_since == 1000.0  # The marker remains.
    assert (
        snapshot.next_allowed_at == 1003.0
    )  # Preregistration has expired; the old defect would admit.
    # The fast successor rejects immediately with no wait, rather than relying on expired preregistration.
    with (
        pytest.raises(RateLimitUnconfirmedError, match="在途"),
        successor.transfer(max_wait=100.0),
    ):
        pass
    assert successor.clock.sleeps == []


def test_marker_initial_write_failure_never_fetches(tmp_path):
    """If the initial in-flight write fails, deliver no lease and never enter the exchange
    body.
    """
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
            entered.append(True)  # Must not execute.
    finally:
        gate._write_state_file = original_write  # type: ignore[method-assign]
    assert entered == []  # No lease was delivered; the exchange body never ran.
    assert not state.exists()  # No state was written.


def test_exit_persistence_failure_keeps_marker_and_refuses_successor(tmp_path):
    """Finalization write failures remain observable, retain the marker, and make fast
    successors fail closed. Without a body exception, chain directly to the persistence
    error.
    """
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
            pass  # Normal exchange completion followed by a failed finalization write.
        snapshot = gate.snapshot()
        assert snapshot.in_flight_since == 1000.0  # Retain the marker conservatively.
        assert snapshot.next_allowed_at == 1003.0
        with (
            pytest.raises(RateLimitUnconfirmedError, match="在途"),
            gate.transfer(max_wait=0.0),
        ):
            pass
    finally:
        gate._write_state_file = original_write  # type: ignore[method-assign]


def test_exit_persistence_failure_chains_body_error(tmp_path):
    """If the exchange body and finalization both fail, preserve the original transport
    exception through the persistence error's __cause__ chain.
    """
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
    """Tolerate directory fsync failure after successful replacement on filesystems that do
    not support it. The new state has no marker and includes safe completion spacing, so
    successors wait normally. A power loss may restore old marked state (fail closed) or
    retain new spacing; neither outcome loses both safeguards for a sent request.
    Write/fsync failures before replacement still propagate and retain the marker.
    """
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
    with (
        gate.transfer()
    ):  # Directory fsync failure is tolerated; completion raises no error.
        fail["on"] = (
            True  # Fail directory fsync starting with finalization, after replacement succeeds.
        )
    payload = json.loads(state.read_text(encoding="utf-8"))
    assert (
        payload.get("in_flight_since") is None
    )  # Replacement took effect: no marker remains.
    assert payload["next_allowed_at"] == 1003.0  # Completion-based spacing remains.
    # Successors wait normally instead of failing closed; zero-budget attempts reject on spacing.
    successor = RateLimitGate(state, clock=FakeClock(start=1001.0))
    with (
        pytest.raises(RateLimitError, match="节流门要求再等待"),
        successor.transfer(max_wait=0.0),
    ):
        pass


def test_marker_retained_by_lease_and_standalone_cooldown_writes(tmp_path):
    """Cooldown registration through either an active lease or an independent call must
    retain the in-flight marker; only normal finalization clears it.
    """
    state = tmp_path / "gate.json"
    clock = FakeClock(start=1000.0)
    gate = RateLimitGate(state, clock=clock)
    with gate.transfer() as lease:
        on_disk = json.loads(state.read_text(encoding="utf-8"))
        assert (
            on_disk["in_flight_since"] == 1000.0
        )  # Persist the marker before delivering the lease.
        lease.record_cooldown(1050.0)
        on_disk = json.loads(state.read_text(encoding="utf-8"))
        assert (
            on_disk["in_flight_since"] == 1000.0
        )  # The marker survives cooldown registration.
        assert on_disk["cooldown_until"] == 1050.0
    snapshot = gate.snapshot()
    assert snapshot.in_flight_since is None  # Normal finalization clears it.
    assert snapshot.cooldown_until == 1050.0
    # Independent record_cooldown also retains existing markers; manually mark state to simulate a crash.
    write_state(
        state, '{"version": 2, "in_flight_since": 1000.0, "next_allowed_at": 1003.0}'
    )
    gate.record_cooldown(1052.0)
    snapshot = gate.snapshot()
    assert snapshot.in_flight_since == 1000.0
    assert snapshot.cooldown_until == 1052.0
    assert snapshot.next_allowed_at == 1003.0


def test_reset_and_snapshot_handle_unconfirmed_marker(tmp_path):
    """reset cannot clear or bypass an in-flight marker; snapshot explicitly reports
    unconfirmed state.
    """
    state = tmp_path / "gate.json"
    write_state(
        state,
        '{"version": 2, "in_flight_since": 1000.0, "next_allowed_at": 1003.0, "cooldown_until": 1002.0}',
    )
    gate = RateLimitGate(
        state, clock=FakeClock(start=1100.0)
    )  # Spacing and cooldown expired long ago.
    snapshot = gate.snapshot()
    assert snapshot.in_flight_since == 1000.0
    assert snapshot.state_error is None
    with pytest.raises(RateLimitUnconfirmedError, match="在途"):
        gate.reset()  # Expired spacing/cooldown still cannot let recovery bypass the marker.
    assert json.loads(state.read_text(encoding="utf-8"))["in_flight_since"] == 1000.0


# ---------------------------------------------------------------- Connection leases


def test_transfer_spacing_survives_process_restart(tmp_path):
    clock = FakeClock(start=1000.0)
    grants = []
    for _ in range(3):
        gate = RateLimitGate(
            tmp_path / "gate.json", clock=clock
        )  # Every new instance represents a restart.
        with gate.transfer() as lease:
            grants.append(lease.granted_at)
    assert grants == [1000.0, 1003.0, 1006.0]
    # Wait in small steps outside the lock; total time is correct and each step respects the recheck bound.
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
    """Cooldown registered while waiting must apply before sending; no reserved permit
    bypasses it.

    During the first unlocked wait, another gate simulates a process registering longer
    cooldown. The main gate rereads it on the next step and grants only after expiry.
    """
    clock = FakeClock(start=1000.0)
    gate = RateLimitGate(tmp_path / "gate.json", clock=clock)
    gate.record_cooldown(1010.0)  # Begin with a shorter cooldown.
    cooldown_added = False
    original_sleep = clock.sleep

    def interleaved_sleep(seconds: float) -> None:
        nonlocal cooldown_added
        if not cooldown_added:
            # The main gate has yielded the lock during segmented waiting, so another process can register cooldown.
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
        lease.record_cooldown(1015.0)  # Merge by retaining the later deadline.
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


# ---------------------------------------------------------------- Persistent cooldown


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


# ---------------------------------------------------------------- State robustness: fail closed


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
        gate.reset()  # Corrupted state means unknown cooldown, not zero; default recovery also rejects.
    # Preserve the scene: do not automatically reset or overwrite corrupted contents.
    assert path.read_text(encoding="utf-8") == payload


def test_corrupt_state_does_not_discard_known_unexpired_cooldown(tmp_path):
    """State corruption must not silently discard previously registered cooldown."""
    path = tmp_path / "gate.json"
    clock = FakeClock(start=100.0)
    gate = RateLimitGate(path, clock=clock)
    gate.record_cooldown(9999.0)
    write_state(path, "{broken")  # Simulate a crash during writing.
    gate2 = RateLimitGate(path, clock=clock)
    # Cooldown validity is unknown: refuse sending and await explicit recovery.
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
    """Clear valid state only after both spacing and cooldown naturally expire."""
    clock = FakeClock(start=1000.0)
    gate = RateLimitGate(tmp_path / "gate.json", clock=clock)
    with gate.transfer():
        pass  # next_allowed_at = 1003
    gate.record_cooldown(1010.0)
    clock.sleep(20.0)  # Both constraints have naturally expired.
    gate.reset()
    assert gate.snapshot().next_allowed_at is None
    assert gate.snapshot().cooldown_until is None
    with gate.transfer() as lease:
        assert lease.granted_at == 1020.0


def test_reset_refuses_unexpired_cooldown_with_no_bypass_flag(tmp_path):
    """Recovery cannot clear unexpired cooldown and exposes no force-clear flag."""
    import inspect

    parameters = inspect.signature(RateLimitGate.reset).parameters
    assert list(parameters) == ["self"]  # No force-clear argument exists.
    clock = FakeClock(start=1000.0)
    gate = RateLimitGate(tmp_path / "gate.json", clock=clock)
    gate.record_cooldown(5000.0)
    with pytest.raises(RateLimitError, match="未到期"):
        gate.reset()
    assert (
        gate.snapshot().cooldown_until == 5000.0
    )  # Cooldown was not silently discarded.
    clock.sleep(
        4001.0
    )  # Clearing becomes permissible only after natural cooldown expiry.
    gate.reset()
    assert gate.snapshot().cooldown_until is None


def test_reset_refuses_active_spacing(tmp_path):
    """Review reproducer: reset immediately after a lease must not bypass the production
    three-second spacing.
    """
    clock = FakeClock(start=100.0)
    gate = RateLimitGate(tmp_path / "gate.json", clock=clock)
    with gate.transfer():
        pass  # next_allowed_at = 103
    with pytest.raises(RateLimitError, match="未到期"):
        gate.reset()
    # A new lease still respects three seconds; recovery works after natural expiry.
    with gate.transfer() as lease:
        assert lease.granted_at == 103.0
    with pytest.raises(RateLimitError):
        gate.reset()  # The new lease preregisters 106 again.
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


# ---------------------------------------------------------------- Cross-process coordination


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
    """With no state file, reset writes clean empty state without bypassing constraints."""
    gate = RateLimitGate(tmp_path / "gate.json", clock=FakeClock(start=100.0))
    gate.reset()
    assert gate.snapshot().state_error is None
    with gate.transfer() as lease:
        assert lease.granted_at == 100.0


@pytest.mark.integration
def test_first_creation_race_with_held_coordination_lock(tmp_path):
    """First-creation race: the stable lock precedes data-file visibility.

    While process A holds the lock as an initializing writer, process B runs transfer
    concurrently. B must wait rather than classify a missing data file as corrupt. After
    A releases, B obtains a permit and initializes state normally.
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
        assert holder.stdout.readline().strip() == "HELD"  # A already owns the lock.
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
        # B actually waited for the lock rather than immediately misclassifying corruption.
        assert elapsed >= 1.0
    finally:
        holder_rest, holder_err = holder.communicate(timeout=120)
        assert holder.returncode == 0, holder_err
        assert "RELEASED" in holder_rest
        if (
            holder.poll() is None
        ):  # pragma: no cover - clean up a process that has not exited
            holder.kill()
            holder.wait(timeout=30)
    # A complete state file exists; atomic replacement leaves no empty-file window.
    payload = json.loads(state.read_text(encoding="utf-8"))
    assert payload["version"] == 2


@pytest.mark.integration
def test_concurrent_first_use_two_processes_both_succeed(tmp_path):
    """Two processes first using shared state both succeed with at least three seconds
    between permits.
    """
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


@pytest.mark.integration
def test_client_transfers_do_not_overlap_across_processes(tmp_path):
    """Two real processes use client transfers lasting over three seconds; connection
    windows must not overlap.

    Verify actual send starts are at least three seconds apart. The lease covers the
    entire HTTP exchange, so the second process cannot start before the first releases
    the lock.
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
    # Connection windows do not overlap, allowing a tiny scheduling epsilon.
    assert start2 >= end1 - 0.25
    # Actual send starts satisfy the three-second minimum.
    assert start2 - start1 >= 2.95
    # The state file records both preregistrations.
    state_json = json.loads(state.read_text(encoding="utf-8"))
    assert state_json["version"] == 2


# ---------------------------------------------------------------- Side effects and default paths


@pytest.mark.integration
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

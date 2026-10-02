"""Exclusive workspace locks: process exclusion, thread reentry, and release on process
exit (A03/A04).

Use real subprocesses; test_runtime_subprocess.py covers full task chains for holder
termination and concurrent startup.
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import time

import pytest

from hvs_catalog.workspace import (
    WorkspaceBusyError,
    initialize_workspace,
    open_workspace,
)


def _child(script: str) -> subprocess.Popen:
    return subprocess.Popen(
        [sys.executable, "-c", script],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def _wait_ready(proc: subprocess.Popen, marker: str, timeout: float = 10.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        line = proc.stdout.readline()
        if marker in line:
            return
        if proc.poll() is not None:
            raise AssertionError(
                f"子进程提前退出（{proc.returncode}）：{proc.stderr.read()}"
            )
    raise AssertionError("等待子进程就绪超时")


def test_same_thread_reentrant_other_thread_busy(tmp_path):
    space = initialize_workspace(tmp_path / "space")
    lock = space.lock()
    with lock:
        with lock:  # Same-thread reentry.
            assert lock.held_by_current_thread
        errors: list[Exception] = []

        def other() -> None:
            try:
                space.lock().acquire()
            except WorkspaceBusyError as exc:
                errors.append(exc)

        thread = threading.Thread(target=other)
        thread.start()
        thread.join()
        assert len(errors) == 1
    assert not lock.held_by_current_thread
    space.close()


@pytest.mark.integration
def test_cross_process_lock_returns_busy(tmp_path):
    space = initialize_workspace(tmp_path / "space")
    root = str(space.root)
    holder = _child(
        "import time\n"
        f"from hvs_catalog.workspace import open_workspace\n"
        f"space = open_workspace({root!r})\n"
        "lock = space.lock()\n"
        "lock.acquire()\n"
        "print('LOCKED', flush=True)\n"
        "time.sleep(30)\n"
    )
    try:
        _wait_ready(holder, "LOCKED")
        with pytest.raises(WorkspaceBusyError):
            space.lock().acquire()
    finally:
        holder.kill()
        holder.wait()
    space.close()


@pytest.mark.integration
def test_lock_released_after_holder_process_termination(tmp_path):
    space = initialize_workspace(tmp_path / "space")
    root = str(space.root)
    holder = _child(
        "import os\n"
        f"from hvs_catalog.workspace import open_workspace\n"
        f"space = open_workspace({root!r})\n"
        "lock = space.lock()\n"
        "lock.acquire()\n"
        "print('LOCKED', flush=True)\n"
        "os._exit(70)\n"
    )
    _wait_ready(holder, "LOCKED")
    holder.wait(timeout=10)
    # Acquire again after process exit; retain the lock file itself.
    lock = space.lock()
    lock.acquire()
    lock.release()
    assert (space.root / ".lock").exists()
    space.close()


def test_two_spaces_lock_independently(tmp_path):
    a = initialize_workspace(tmp_path / "a")
    b = initialize_workspace(tmp_path / "b")
    with a.lock(), b.lock():  # Another workspace remains unaffected.
        pass
    a.close()
    b.close()


@pytest.mark.integration
def test_lock_file_not_inherited_by_subprocess(tmp_path):
    space = initialize_workspace(tmp_path / "space")
    lock = space.lock()
    lock.acquire()
    try:
        probe = subprocess.run(
            [
                sys.executable,
                "-c",
                (
                    "import os\n"
                    "fds = [fd for fd in range(64) if"
                    " os.path.exists(f'/dev/fd/{fd}')]\n"
                    "print(os.listdir('/dev/fd'))"
                ),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        assert probe.returncode == 0, probe.stderr
        # Children must not retain the lock through inherited descriptors; the parent can release normally.
    finally:
        lock.release()
    reopened = open_workspace(space.root)
    reopened.lock().acquire()
    reopened.lock().release()
    reopened.close()
    space.close()


@pytest.mark.integration
def test_exec_closes_lock_descriptor_without_normal_release(tmp_path):
    """A live program replacing its lock-holding process through exec cannot retain the old
    lock.
    """
    space = initialize_workspace(tmp_path / "space")
    child = subprocess.Popen(
        [
            sys.executable,
            "-c",
            (
                "import os, sys\n"
                "from hvs_catalog.workspace import open_workspace\n"
                "space = open_workspace(sys.argv[1])\n"
                "space.lock().acquire()\n"
                "print('LOCKED', flush=True)\n"
                "assert sys.stdin.readline().strip() == 'exec'\n"
                "os.execv(sys.executable, [sys.executable, '-c', \"import sys; print('EXEC', flush=True); sys.stdin.readline()\"])"
            ),
            str(space.root),
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert child.stdout.readline().strip() == "LOCKED"
        with pytest.raises(WorkspaceBusyError):
            space.lock().acquire()
        child.stdin.write("exec\n")
        child.stdin.flush()
        assert child.stdout.readline().strip() == "EXEC"
        assert child.poll() is None
        with space.lock():
            pass
        child.communicate("exit\n", timeout=30)
        assert child.returncode == 0
    finally:
        if child.poll() is None:
            child.kill()
            child.communicate()
        space.close()


@pytest.mark.integration
def test_forked_child_must_reacquire_and_sees_parent_busy(tmp_path):
    """A child forked while its parent owns the lock cannot borrow reentry records to enter
    the critical section.
    """

    space = initialize_workspace(tmp_path / "space")
    lock = space.lock()
    lock.acquire()
    try:
        read_fd, write_fd = os.pipe()
        pid = os.fork()
        if pid == 0:
            # After fork, child registries/descriptors are cleared; reopen the workspace and acquire anew.
            os.close(read_fd)
            try:
                child = open_workspace(space.root)
                with child.lock():
                    os.write(write_fd, b"child entered while parent holds lock")
                os._exit(0)
            except BaseException as exc:  # noqa: BLE001 - report results through the pipe
                os.write(write_fd, repr(exc).encode())
                os._exit(1)
        os.close(write_fd)
        message = os.read(read_fd, 4096).decode()
        os.close(read_fd)
        _pid, status = os.waitpid(pid, 0)
        child_exit = os.waitstatus_to_exitcode(status)
        assert child_exit == 1
        assert "WorkspaceBusyError" in message
    finally:
        lock.release()

    # After parent release, the forked child acquires normally.
    read_fd, write_fd = os.pipe()
    pid = os.fork()
    if pid == 0:
        os.close(read_fd)
        try:
            child = open_workspace(space.root)
            with child.lock():
                os.write(write_fd, b"child acquired after release")
            os._exit(0)
        except BaseException as exc:  # noqa: BLE001
            os.write(write_fd, repr(exc).encode())
            os._exit(1)
    os.close(write_fd)
    message = os.read(read_fd, 4096).decode()
    os.close(read_fd)
    _pid, status = os.waitpid(pid, 0)
    assert os.waitstatus_to_exitcode(status) == 0
    assert message == "child acquired after release"
    space.close()


@pytest.mark.integration
def test_fork_child_survives_registry_guard_held_by_other_thread(tmp_path):
    """Child callbacks must not deadlock if another thread holds the registry lock at fork.

    Only the forking thread survives in the child, so it cannot wait for inherited
    ordinary thread locks. Reset the registry directly and replace its lock so the child
    returns and remains usable.
    """
    code = """
import os
import select
import signal
import sys
import threading
import warnings

warnings.simplefilter("ignore")

from hvs_catalog.workspace.locking import SpaceLock

ready = threading.Event()
release = threading.Event()

def owner():
    with SpaceLock._registry_guard:
        ready.set()
        release.wait(10)

t = threading.Thread(target=owner)
t.start()
ready.wait()

os.makedirs(sys.argv[1], exist_ok=True)
read_fd, write_fd = os.pipe()
pid = os.fork()
if pid == 0:
    os.close(read_fd)
    # Child execution reaches here after at-fork callbacks reset the registry and replace its lock;
    # new locking operations must remain usable.
    lock = SpaceLock(sys.argv[1])
    try:
        lock.acquire()
    except Exception as exc:
        os.write(write_fd, repr(exc).encode())
        os._exit(1)
    lock.release()
    os.write(write_fd, b"child-returned-and-lock-usable")
    os._exit(0)
os.close(write_fd)
release.set()
t.join()
# Deadlocked at-fork children never write the pipe; bound observation and kill to avoid hanging.
if select.select([read_fd], [], [], 5)[0]:
    message = os.read(read_fd, 4096).decode()
else:
    os.kill(pid, signal.SIGKILL)
    message = "child-deadlocked-in-atfork"
os.close(read_fd)
_pid, status = os.waitpid(pid, 0)
print(os.waitstatus_to_exitcode(status), message)
"""
    probe = subprocess.run(
        [sys.executable, "-c", code, str(tmp_path / "child-space")],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert probe.returncode == 0, probe.stderr
    exit_code, message = probe.stdout.strip().splitlines()[-1].split(" ", 1)
    assert exit_code == "0", message
    assert message == "child-returned-and-lock-usable"


_FORK_WINDOW_DRIVER = r"""
import json
import os
import select
import signal
import sys
import threading
import warnings

warnings.simplefilter("ignore")
from hvs_catalog.workspace import initialize_workspace, WorkspaceBusyError
from hvs_catalog.workspace import locking

window, root = sys.argv[1:]
space = initialize_workspace(root)
space.close()
alive_read, alive_write = os.pipe()
release_read, release_write = os.pipe()
worker_pid = os.fork()
if worker_pid == 0:
    os.close(alive_read)
    os.close(release_write)
    entered, proceed = threading.Event(), threading.Event()
    fork_attempted = threading.Event()
    original_open, original_close = os.open, os.close
    original_flock = locking.fcntl.flock
    forking_thread = threading.get_ident()
    target_fd = None
    worker_thread = None
    guarded = window in {"before-open", "after-open", "before-close", "after-close"}

    # During open/register and close/remove, before-fork must wait for complete descriptor transitions.
    # Release workers after observing actual before-fork guard acquisition, not guessed sleep windows.
    if guarded:
        guard = locking.SpaceLock._descriptor_guard
        class GuardProbe:
            def acquire(self):
                if threading.get_ident() == forking_thread and entered.is_set():
                    fork_attempted.set()
                    proceed.set()
                return guard.acquire()
            def release(self):
                guard.release()
            def __enter__(self):
                self.acquire()
                return self
            def __exit__(self, *args):
                self.release()
        locking.SpaceLock._descriptor_guard = GuardProbe()

    def pause():
        entered.set()
        assert proceed.wait(15), "window was not released"

    def open_hook(path, flags, *args):
        global target_fd
        target = threading.get_ident() == worker_thread and str(path) == str(space.root / ".lock")
        if target and window == "before-open":
            pause()
        fd = original_open(path, flags, *args)
        if target:
            target_fd = fd
            if window == "after-open":
                pause()
        return fd

    def flock_hook(fd, flags):
        acquiring = bool(flags & locking.fcntl.LOCK_EX)
        unlocking = flags == locking.fcntl.LOCK_UN
        if threading.get_ident() == worker_thread:
            if (acquiring and window == "before-flock") or (unlocking and window == "before-unlock"):
                pause()
            original_flock(fd, flags)
            if (acquiring and window == "after-flock") or (unlocking and window == "after-unlock"):
                pause()
        else:
            original_flock(fd, flags)

    def close_hook(fd):
        target = threading.get_ident() == worker_thread and fd == target_fd
        if target and window == "before-close":
            pause()
        original_close(fd)
        if target and window in {"before-close", "after-close"}:
            # Reuse the just-freed lowest descriptor as a user fd before close returns and registration removal.
            reused = original_open(space.root / "sentinel", os.O_RDONLY)
            assert reused == target_fd
            if window == "after-close":
                pause()

    (space.root / "sentinel").write_bytes(b"sentinel")
    locking.os.open = open_hook
    locking.os.close = close_hook
    locking.fcntl.flock = flock_hook

    def owner():
        global worker_thread
        worker_thread = threading.get_ident()
        lock = locking.SpaceLock(space.root)
        lock.acquire()
        if window in {"before-unlock", "after-unlock", "before-close", "after-close"}:
            lock.release()

    thread = threading.Thread(target=owner)
    thread.start()
    assert entered.wait(10), "worker did not reach injected window"
    ack_read, ack_write = os.pipe()
    child_pid = os.fork()
    if child_pid == 0:
        # Child callbacks already cleaned up; remove test wrappers and inspect inherited actual descriptors.
        locking.os.open, locking.os.close = original_open, original_close
        locking.fcntl.flock = original_flock
        os.close(ack_read)
        if window in {"before-close", "after-close"}:
            descriptor_ok = os.read(target_fd, 8) == b"sentinel"
        else:
            try:
                os.fstat(target_fd)
            except OSError:
                descriptor_ok = True
            else:
                descriptor_ok = False
        parent_busy = None
        if window in {"after-flock", "before-unlock"}:
            try:
                with locking.SpaceLock(space.root):
                    parent_busy = False
            except WorkspaceBusyError:
                parent_busy = True
        payload = {"pid": os.getpid(), "descriptor_ok": descriptor_ok,
                   "parent_busy": parent_busy,
                   "fork_coordinated": fork_attempted.is_set() if guarded else True}
        os.write(alive_write, json.dumps(payload).encode() + b"\n")
        os.write(ack_write, b"ready")
        os.close(ack_write)
        # Keep children alive until external lock reacquisition even after the lock-holding parent exits abruptly.
        os.read(release_read, 1)
        os._exit(0)
    os.close(ack_write)
    assert select.select([ack_read], [], [], 10)[0], "fork child did not acknowledge"
    assert os.read(ack_read, 5) == b"ready"
    os._exit(70)

os.close(alive_write)
os.close(release_read)
child_pid = None
try:
    assert select.select([alive_read], [], [], 15)[0], "fork child did not become ready"
    data = os.read(alive_read, 4096)
    payload = json.loads(data)
    child_pid = payload["pid"]
    _, status = os.waitpid(worker_pid, 0)
    assert os.waitstatus_to_exitcode(status) == 70
    os.kill(child_pid, 0)
    # A surviving child's in-flight descriptor copies must not keep locks busy after parent death.
    try:
        with locking.SpaceLock(space.root):
            pass
    except WorkspaceBusyError:
        raise AssertionError("parent exited but child keeps space busy: " + str(payload)) from None
    assert payload["descriptor_ok"], payload
    assert payload["fork_coordinated"], payload
    if window in {"after-flock", "before-unlock"}:
        assert payload["parent_busy"] is True, payload
    print("fork-window-ok:" + window, flush=True)
finally:
    os.write(release_write, b"exit")
    os.close(release_write)
    # EOF proves the last writer closed when the child exited, leaving no background lock holder.
    if not select.select([alive_read], [], [], 10)[0]:
        if child_pid is not None:
            os.kill(child_pid, signal.SIGKILL)
        raise AssertionError("fork child did not exit")
    assert os.read(alive_read, 4096) == b""
    os.close(alive_read)
"""


@pytest.mark.integration
@pytest.mark.parametrize(
    "window",
    [
        "before-open",
        "after-open",
        "before-flock",
        "after-flock",
        "before-unlock",
        "after-unlock",
        "before-close",
        "after-close",
    ],
)
def test_fork_inflight_descriptor_parent_hard_exit(tmp_path, window):
    """Real fork precisely covers creation, flock, unlock, close, and descriptor-number
    reuse windows.
    """
    import signal

    probe = subprocess.Popen(
        [sys.executable, "-c", _FORK_WINDOW_DRIVER, window, str(tmp_path / "space")],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    try:
        out, err = probe.communicate(timeout=40)
        assert probe.returncode == 0, out + err
        assert out.strip() == f"fork-window-ok:{window}"
    finally:
        # On assertion failure or timeout, reap the entire test process group, including forked grandchildren.
        try:
            os.killpg(probe.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        probe.communicate()


@pytest.mark.parametrize("error", [OSError, RuntimeError])
def test_acquire_failure_cleans_inflight_descriptor(tmp_path, monkeypatch, error):
    from hvs_catalog.workspace import locking

    space = initialize_workspace(tmp_path / "space")
    before = set(locking.SpaceLock._descriptors)

    def fail(fd, flags):
        raise error("injected flock failure")

    with monkeypatch.context() as patch:
        patch.setattr(locking.fcntl, "flock", fail)
        expected = WorkspaceBusyError if error is OSError else RuntimeError
        with pytest.raises(expected):
            space.lock().acquire()
    assert locking.SpaceLock._descriptors == before
    assert not space.lock().held_by_current_thread
    with space.lock():
        pass
    space.close()

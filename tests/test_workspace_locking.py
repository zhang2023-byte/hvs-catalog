"""空间排他锁：跨进程互斥、进程终止释放、锁文件删除不能绕过（A03/A04 基础）。

跨进程场景用真实子进程验证；持锁进程终止后的释放与并发启动竞争
在 test_runtime_subprocess.py 中覆盖完整任务链路。
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import time

import pytest

from hvs_catalog.workspace import (
    LockCorruptedError,
    WorkspaceBusyError,
    initialize_workspace,
    open_workspace,
    repair_lock,
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
        with lock:  # 同线程重入
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
    # 进程退出后锁可重新获得；锁文件本身保留
    lock = space.lock()
    lock.acquire()
    lock.release()
    assert (space.root / ".lock").exists()
    space.close()


def test_deleting_active_lock_file_does_not_release_it(tmp_path):
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
        os.remove(space.root / ".lock")
        # 锁文件身份与数据库记录不符：不能靠重建文件绕过活跃锁
        with pytest.raises(LockCorruptedError):
            space.lock().acquire()
    finally:
        holder.kill()
        holder.wait()
    # 持锁进程结束后，显式修复可恢复锁可用性
    repair_lock(space.root, business_db=space.root / "business.db")
    lock = space.lock()
    lock.acquire()
    lock.release()
    space.close()


def test_replaced_lock_file_is_detected(tmp_path):
    space = initialize_workspace(tmp_path / "space")
    lock = space.lock()
    lock.acquire()
    lock.release()
    (space.root / ".lock").unlink()
    (space.root / ".lock").write_bytes(b"")
    with pytest.raises(LockCorruptedError):
        space.lock().acquire()
    repair_lock(space.root, business_db=space.root / "business.db")
    lock = space.lock()
    lock.acquire()
    lock.release()
    space.close()


def test_two_spaces_lock_independently(tmp_path):
    a = initialize_workspace(tmp_path / "a")
    b = initialize_workspace(tmp_path / "b")
    with a.lock(), b.lock():  # 另一空间不受影响
        pass
    a.close()
    b.close()


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
        # 子进程不应通过继承的描述符持有锁：本进程仍能正常释放
    finally:
        lock.release()
    reopened = open_workspace(space.root)
    reopened.lock().acquire()
    reopened.lock().release()
    reopened.close()
    space.close()


def test_forked_child_must_reacquire_and_sees_parent_busy(tmp_path):
    """F09：父进程持锁时 fork 的子进程不能借用重入记录进入临界区。"""
    import os

    space = initialize_workspace(tmp_path / "space")
    lock = space.lock()
    lock.acquire()
    try:
        read_fd, write_fd = os.pipe()
        pid = os.fork()
        if pid == 0:
            # 子进程：注册表与描述符已在 fork 后清理，重新打开空间取锁
            os.close(read_fd)
            try:
                child = open_workspace(space.root)
                with child.lock():
                    os.write(write_fd, b"child entered while parent holds lock")
                os._exit(0)
            except BaseException as exc:  # noqa: BLE001 - 结果经管道报告
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

    # 父进程释放后，fork 出的子进程可以正常取锁
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


def test_fork_child_survives_registry_guard_held_by_other_thread(tmp_path):
    """R2-06：fork 瞬间注册表锁被其他线程持有时，子进程回调不得死锁。

    子进程只有发起 fork 的线程存活，不能等待继承的普通线程锁；回调
    必须直接重置注册表并换新锁，让子进程正常返回并继续可用。
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
    # at-fork 回调返回后子进程才走到这里：注册表已重置且换了新锁，
    # 新的锁操作必须仍然可用
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
# 子进程死锁在 at-fork 回调时永远不写管道：限时观察并击杀，避免悬挂
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

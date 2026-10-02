"""Restricted external-converter processes using minimal isolation verified on local macOS.

Cwd, --path, and timeouts alone are not a sandbox. On macOS with /usr/bin/sandbox-exec,
run latexmlc/pandoc with denied networks and minimal credential-free environments; deny
global writes except isolated conversion directories, capture/bound stdout/stderr there,
and deny .ltxml/.pm reads while supplying controlled inputs. Bound input/output bytes
and time, terminating timed-out processes.

Without this isolation, report converters unavailable and never run unisolated. No
container platform/general security framework is provided; untested platform support is
not claimed.
"""

from __future__ import annotations

import os
import platform
import subprocess
from dataclasses import dataclass
from pathlib import Path

__all__ = [
    "CONVERTER_TIMEOUT_SECONDS",
    "MAX_CONVERTER_INPUT_BYTES",
    "MAX_CONVERTER_OUTPUT_BYTES",
    "ConverterAvailability",
    "ConverterRunner",
    "ProcessOutcome",
    "SandboxedConverterRunner",
    "sandbox_profile",
]

#: Maximum conversion runtime in seconds.
CONVERTER_TIMEOUT_SECONDS = 120

#: Byte limit for each stdout/stderr/HTML output.
MAX_CONVERTER_OUTPUT_BYTES = 8 * 1024 * 1024

#: Byte limit for documents sent to external converters.
MAX_CONVERTER_INPUT_BYTES = 4 * 1024 * 1024

_SANDBOX_EXEC = "/usr/bin/sandbox-exec"

_VERSION_FLAGS = {"latexmlc": "--VERSION", "pandoc": "--version"}


@dataclass(frozen=True)
class ConverterAvailability:
    """Converter availability/version with an explicit reason when unavailable."""

    tool: str
    available: bool
    executable: str | None = None
    version: str | None = None
    reason: str | None = None


@dataclass(frozen=True)
class ProcessOutcome:
    """Result of one restricted process run."""

    returncode: int | None
    stdout: bytes
    stderr: bytes
    timed_out: bool = False
    exceeded_output: bool = False
    error: str = ""


class ConverterRunner:
    """Converter execution interface; tests can inject deterministic fakes."""

    def availability(self, tool: str) -> ConverterAvailability:  # pragma: no cover
        raise NotImplementedError

    def run(
        self,
        tool: str,
        command: list[str],
        *,
        workdir: Path,
        timeout: int = CONVERTER_TIMEOUT_SECONDS,
    ) -> ProcessOutcome:  # pragma: no cover
        raise NotImplementedError


def sandbox_profile(convdir: Path) -> str:
    """Generate minimal macOS isolation: deny networks, restrict writes to conversion
    directories, and allowlist reads.

    Allow conversion directories/materials and trusted system/Homebrew dependency trees;
    deny other file-read-data to block external private files/credentials. Directory
    metadata/root reads are needed for bootstrap and do not allow file contents.
    Unsupported environments report unavailable without unisolated execution.
    """
    directory = str(convdir.resolve())
    escaped = directory.replace("/", "\\/")
    allow_read = (
        directory,
        "/usr",
        "/bin",
        "/sbin",
        "/etc",
        "/private/etc",
        "/opt/homebrew",
        "/usr/local",
        "/Library",
        "/System",
        "/private/var/db/timezone",
        "/dev",
    )
    lines = [
        "(version 1)",
        "(allow default)",
        "(deny network*)",
        "(deny file-write*)",
        f'(allow file-write* (subpath "{directory}"))',
        "(deny file-read-data)",
        "(allow file-read-metadata)",
        '(allow file-read-data (literal "/"))',
    ]
    for path in allow_read:
        lines.append(f'(allow file-read-data (subpath "{path}"))')
    lines.append('(deny file-read-data (regex "^' + escaped + '\\/.*\\.(ltxml|pm)$"))')
    # Only our installed observer is trusted, never a binding from the package.
    import json

    trace = Path(__file__).with_name("source_trace.ltxml").resolve()
    lines.append(f"(allow file-read-data (literal {json.dumps(str(trace))}))")
    inputs = convdir / "inputs"
    if inputs.is_dir():
        lines.append(
            f"(deny file-write* (subpath {json.dumps(str(inputs.resolve()))}))"
        )
    lines.append("")
    return "\n".join(lines)


def _sandbox_ready() -> str | None:
    if platform.system() != "Darwin":
        return "当前平台没有经过实测的隔离方案（仅验证 macOS sandbox-exec）"
    if not Path(_SANDBOX_EXEC).is_file():
        return "缺少 /usr/bin/sandbox-exec，无法提供最小隔离"
    return None


class SandboxedConverterRunner(ConverterRunner):
    """Converter runner using minimal macOS sandbox-exec isolation."""

    def __init__(self, *, sandbox_path: str = _SANDBOX_EXEC) -> None:
        self._sandbox_path = sandbox_path

    def availability(self, tool: str) -> ConverterAvailability:
        if tool not in _VERSION_FLAGS:
            return ConverterAvailability(
                tool, available=False, reason=f"未知转换器：{tool}"
            )
        missing = _sandbox_ready()
        if missing is not None:
            return ConverterAvailability(tool, available=False, reason=missing)
        import shutil

        executable = shutil.which(tool)
        if executable is None:
            return ConverterAvailability(
                tool,
                available=False,
                reason=f"系统未安装 {tool}（不要求 uv 自动安装 Perl/TeX）",
            )
        version = _probe_version(tool, executable)
        if version is None:
            return ConverterAvailability(
                tool,
                available=False,
                executable=executable,
                reason=f"{tool} 版本探测失败，不能固定转换器版本",
            )
        return ConverterAvailability(
            tool, available=True, executable=executable, version=version
        )

    def run(
        self,
        tool: str,
        command: list[str],
        *,
        workdir: Path,
        timeout: int = CONVERTER_TIMEOUT_SECONDS,
    ) -> ProcessOutcome:
        missing = _sandbox_ready()
        if missing is not None:
            return ProcessOutcome(
                returncode=None,
                stdout=b"",
                stderr=b"",
                error=f"拒绝运行 {tool}：{missing}；转换保持不可用，不裸跑",
            )
        import time

        profile = sandbox_profile(workdir)
        stdout_path = workdir / "runner.stdout.txt"
        stderr_path = workdir / "runner.stderr.txt"
        env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
            "HOME": str(workdir),
            "TMPDIR": str(workdir),
        }
        argv = [self._sandbox_path, "-p", profile, *command]
        timed_out = False
        exceeded_output = False
        error = ""
        returncode: int | None = None
        with stdout_path.open("wb") as out, stderr_path.open("wb") as err:
            # Independent process groups allow timeout/limit cleanup of all controlled descendants, not just direct children.
            process = subprocess.Popen(
                argv,
                cwd=str(workdir),
                stdout=out,
                stderr=err,
                env=env,
                start_new_session=True,
            )
            # Record the process-group ID immediately: getpgid fails after direct-child exit while descendants
            # may remain in the original group (second-review finding 18).
            try:
                group_id = os.getpgid(process.pid)
            except (ProcessLookupError, OSError):  # pragma: no cover - race condition
                group_id = process.pid
            deadline = time.monotonic() + timeout
            try:
                while True:
                    try:
                        returncode = process.wait(timeout=0.2)
                        break
                    except subprocess.TimeoutExpired:
                        pass
                    if _generated_bytes(workdir) > MAX_CONVERTER_OUTPUT_BYTES:
                        exceeded_output = True
                        error = f"{tool} 生成输出超过 {MAX_CONVERTER_OUTPUT_BYTES} 字节上限；终止进程组"
                        break
                    if time.monotonic() >= deadline:
                        timed_out = True
                        error = f"{tool} 超过 {timeout} 秒时间上限；终止进程组"
                        break
            finally:
                # Includes cancellation/BaseException, not only normal returns.
                _terminate_group(process, group_id)
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:  # pragma: no cover
                    process.kill()
                    process.wait(timeout=10)
        stdout = _capped_read(stdout_path)
        stderr = _capped_read(stderr_path)
        if not exceeded_output and _generated_bytes(workdir) > (
            MAX_CONVERTER_OUTPUT_BYTES
        ):
            # Verify actual generated outputs on every exit path, including quick first-wait exits.
            # Recurse through all conversion-directory depths (second-review nested2/flat2 cases:
            # dd wrote 9 MB each to ordinary/subdirectory files but previously reported success).
            exceeded_output = True
            error = (
                f"{tool} 生成输出超过 {MAX_CONVERTER_OUTPUT_BYTES} 字节上限，"
                "按失败处理（覆盖目录内全部产物）"
            )
        return ProcessOutcome(
            returncode=returncode,
            stdout=stdout,
            stderr=stderr,
            timed_out=timed_out,
            exceeded_output=exceeded_output,
            error=error,
        )


def _safe_size(path: Path) -> int:
    """Read sizes defensively because converters may remove intermediate files such as
    captured stdout.
    """
    try:
        return path.stat().st_size
    except OSError:
        return 0


def _generated_bytes(workdir: Path) -> int:
    """Recursively total regular-file bytes under the conversion directory, including
    nested outputs/stdout/stderr.

    Do not follow symlinks or count their targets, preventing link-based traversal
    during accounting.
    """
    total = 0
    for root, dirs, files in os.walk(workdir, followlinks=False):
        if Path(root) == workdir:
            dirs[:] = [name for name in dirs if name != "inputs"]
        for name in files:
            try:
                path = Path(root) / name
                if path.is_symlink():
                    continue
                total += path.stat().st_size
            except OSError:  # pragma: no cover - directory-content race
                continue
    return total


def _terminate_group(process: subprocess.Popen, group_id: int | None = None) -> None:
    """Terminate the entire process group, including controlled sandbox descendants.

    Prefer the group_id recorded at launch because the direct process may have exited
    and its PID no longer resolves. Ignore safely when the group has no members.
    """
    import signal

    target = group_id
    if target is None:
        try:
            target = os.getpgid(process.pid)
        except (ProcessLookupError, OSError):
            process.kill()
            return
    try:
        os.killpg(target, signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            process.kill()
        except (ProcessLookupError, OSError):  # pragma: no cover - already exited
            pass


def _capped_read(path: Path) -> bytes:
    try:
        with path.open("rb") as stream:
            data = stream.read(MAX_CONVERTER_OUTPUT_BYTES + 1)
    except OSError:  # pragma: no cover - the runner creates the directory
        return b""
    return data[:MAX_CONVERTER_OUTPUT_BYTES]


def _probe_version(tool: str, executable: str) -> str | None:
    """Probe converter versions without document input or sandbox execution."""
    try:
        completed = subprocess.run(
            [executable, _VERSION_FLAGS[tool]],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    first = (completed.stdout or completed.stderr).strip().splitlines()
    return first[0].strip() if first else None

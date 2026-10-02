"""外部转换器的受限进程执行（按本机 macOS 实测的最小系统隔离）。

外部转换不能只依靠 cwd、``--path`` 和超时充当沙箱。本模块在有
``/usr/bin/sandbox-exec`` 的 macOS 上以最小系统隔离运行 ``latexmlc``／
``pandoc``：

- **禁网络**（``deny network*``），不带凭据（最小化环境变量）；
- **只写独立转换目录**：全盘拒绝 ``file-write*``，仅允许转换目录内写
  入；stdout／stderr 重定向到目录内文件并限制尺寸；
- **禁包内可执行绑定**：转换目录由程序写入唯一受控输入，并且配置拒
  绝读取目录内 ``.ltxml``／``.pm`` 文件；
- **有界**：输入尺寸、进程输出尺寸与运行时间均有上限，超时终止进程。

不能提供该隔离的环境（非 macOS 或缺少 ``sandbox-exec``）将转换器报告
为不可用，**不裸跑**；不建设容器平台或通用安全框架，也不声称未实测平
台的支持。
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

#: 单次转换的运行时间上限（秒）。
CONVERTER_TIMEOUT_SECONDS = 120

#: 单路进程输出（stdout/stderr/HTML）的字节上限。
MAX_CONVERTER_OUTPUT_BYTES = 8 * 1024 * 1024

#: 传给外部转换器的输入文档字节上限。
MAX_CONVERTER_INPUT_BYTES = 4 * 1024 * 1024

_SANDBOX_EXEC = "/usr/bin/sandbox-exec"

_VERSION_FLAGS = {"latexmlc": "--VERSION", "pandoc": "--version"}


@dataclass(frozen=True)
class ConverterAvailability:
    """一个外部转换器的可用性与版本；不可用时给出明确原因。"""

    tool: str
    available: bool
    executable: str | None = None
    version: str | None = None
    reason: str | None = None


@dataclass(frozen=True)
class ProcessOutcome:
    """一次受限进程运行的结果。"""

    returncode: int | None
    stdout: bytes
    stderr: bytes
    timed_out: bool = False
    exceeded_output: bool = False
    error: str = ""


class ConverterRunner:
    """转换器执行入口；测试可注入确定性的假实现。"""

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
    """生成 macOS 最小隔离 profile：禁网络、只写转换目录、限读范围。

    读取采用明确允许范围：转换目录、实际转换材料与可信系统依赖
    （系统与 Homebrew 安装树）；全盘拒绝其他 ``file-read-data``，阻断
    包外私有文件与凭据读取（目录元数据与根目录本身的读取是进程引
    导的必要条件，允许后不影响内容阻断）。不能提供该限制的环境报告
    转换不可用，不裸跑。
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
    """macOS ``sandbox-exec`` 最小隔离的转换器运行器。"""

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
            # 独立进程组：超时／超限时终止全部受控后代，不只杀直接进程。
            process = subprocess.Popen(
                argv,
                cwd=str(workdir),
                stdout=out,
                stderr=err,
                env=env,
                start_new_session=True,
            )
            # 先行记录进程组 id：直接进程退出后 getpgid 会失效，而后代
            # 可能仍在原组内运行（二次审核发现 18）。
            try:
                group_id = os.getpgid(process.pid)
            except (ProcessLookupError, OSError):  # pragma: no cover - 竞态
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
            # 全部退出路径（含首次等待内快退出）都核验实际生成输出：
            # 递归覆盖转换目录内任意深度的产物（二次审核反例 nested2/
            # flat2：dd 在普通文件与子目录各写 9MB 仍返回成功）。
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
    """防御性取文件尺寸；转换器可能自行清理中间文件（如 stdout 捕获）。"""
    try:
        return path.stat().st_size
    except OSError:
        return 0


def _generated_bytes(workdir: Path) -> int:
    """递归统计转换目录内全部文件字节（含子目录产物与 stdout／stderr）。

    只统计常规文件，不跟随符号链接（目录内符号链接指向的文件不计，
    防止经链接绕过统计）。
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
            except OSError:  # pragma: no cover - 目录内容竞态
                continue
    return total


def _terminate_group(process: subprocess.Popen, group_id: int | None = None) -> None:
    """终止整个进程组（含沙箱内启动的全部受控后代）。

    ``group_id`` 优先使用启动时记录的组 id（直接进程可能已退出，其
    pid 不能再查询组）；组已无成员时安全忽略。
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
        except (ProcessLookupError, OSError):  # pragma: no cover - 已退出
            pass


def _capped_read(path: Path) -> bytes:
    try:
        with path.open("rb") as stream:
            data = stream.read(MAX_CONVERTER_OUTPUT_BYTES + 1)
    except OSError:  # pragma: no cover - 目录由运行器创建
        return b""
    return data[:MAX_CONVERTER_OUTPUT_BYTES]


def _probe_version(tool: str, executable: str) -> str | None:
    """探测转换器版本（无文档输入，不进入沙箱）。"""
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

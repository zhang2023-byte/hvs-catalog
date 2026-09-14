"""独立子进程验证：控制台命令与 python -m 都能查询帮助和版本。

子进程在仓库外的临时目录运行，并清除 PYTHONPATH，确保不依赖源码路径。
"""

import os
import shutil
import subprocess
import sys
from importlib.metadata import version as installed_version
from pathlib import Path

import pytest


def _clean_env() -> dict[str, str]:
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    return env


def _console_script() -> str:
    bin_dir = "Scripts" if os.name == "nt" else "bin"
    candidates = [
        shutil.which("hvs-catalog"),
        str(Path(sys.prefix) / bin_dir / "hvs-catalog"),
    ]
    for candidate in candidates:
        if candidate and Path(candidate).exists():
            return candidate
    pytest.fail("未找到 hvs-catalog 命令；请用 `uv run pytest` 在已安装环境运行测试")


def _run(command: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        cwd=cwd,
        env=_clean_env(),
        capture_output=True,
        text=True,
        check=False,
    )


def test_console_script_version(tmp_path):
    result = _run([_console_script(), "--version"], tmp_path)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == f"hvs-catalog {installed_version('hvs-catalog')}"


def test_console_script_help(tmp_path):
    result = _run([_console_script(), "--help"], tmp_path)
    assert result.returncode == 0, result.stderr
    assert "usage: hvs-catalog" in result.stdout


def test_module_run_version(tmp_path):
    result = _run([sys.executable, "-m", "hvs_catalog", "--version"], tmp_path)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == f"hvs-catalog {installed_version('hvs-catalog')}"


def test_import_from_clean_cwd(tmp_path):
    script = "from importlib.metadata import version; print(version('hvs-catalog'))"
    result = _run([sys.executable, "-c", script], tmp_path)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == installed_version("hvs-catalog")

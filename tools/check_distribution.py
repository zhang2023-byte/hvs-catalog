"""构建发行包，并在仓库外的全新环境中验证安装。

执行内容：

1. 调用 uv build 把 wheel 与源码包生成到仓库外的临时目录；
2. 检查两个包的内容只含构建安装所需文件，排除正式数据、开发材料与运行产物；
3. 用 uv 在仓库外创建全新虚拟环境并安装 wheel；
4. 以独立子进程验证 ``hvs-catalog --help``、``--version``、``python -m`` 与包导入，
   子进程在仓库外目录运行并清除 PYTHONPATH。

用法：uv run python tools/check_distribution.py [Python 版本]

可选参数为创建虚拟环境使用的 Python 版本（如 3.12），默认使用 uv 的默认解释器。
"""

from __future__ import annotations

import argparse
import os
import subprocess
import tarfile
import tempfile
import tomllib
import zipfile
from collections.abc import Iterable, Sequence
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

# 不允许出现在任何发行包中的内容：正式星表数据、开发材料与运行产物。
FORBIDDEN_SEGMENTS = frozenset(
    {
        ".dev",
        ".git",
        ".github",
        ".venv",
        ".work",
        "__pycache__",
        "build",
        "catalog",
        "contracts",
        "dist",
        "docs",
        "tests",
        "tools",
    }
)

# 源码包根目录下只允许构建安装所需文件；.gitignore 由 hatchling 固定加入。
SDIST_ALLOWED_ROOTS = frozenset(
    {".gitignore", "PKG-INFO", "pyproject.toml", "README.md", "src"}
)


def project_version() -> str:
    pyproject = tomllib.loads(
        (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    )
    return str(pyproject["project"]["version"])


def top_levels(names: Iterable[str]) -> set[str]:
    return {name.split("/", maxsplit=1)[0] for name in names}


def find_forbidden_paths(names: Iterable[str]) -> list[str]:
    forbidden = []
    for name in names:
        if any(segment in FORBIDDEN_SEGMENTS for segment in name.split("/")):
            forbidden.append(name)
    return forbidden


def check_wheel(names: Sequence[str]) -> list[str]:
    """wheel 顶层只允许包目录 hvs_catalog/ 及其 dist-info 元数据。"""
    violations = find_forbidden_paths(names)
    for top in sorted(top_levels(names)):
        if top in FORBIDDEN_SEGMENTS:
            continue  # 这些路径已按完整成员路径报告
        is_package = top == "hvs_catalog"
        is_dist_info = top.startswith("hvs_catalog-") and top.endswith(".dist-info")
        if not is_package and not is_dist_info:
            violations.append(f"wheel 顶层出现意外内容：{top}")
    return violations


def check_sdist(names: Sequence[str], version: str) -> list[str]:
    """源码包顶层只允许 hvs_catalog-<version>/，其下只允许构建安装所需文件。"""
    violations = find_forbidden_paths(names)
    root = f"hvs_catalog-{version}"
    for name in names:
        if not name.startswith(f"{root}/"):
            violations.append(f"源码包顶层出现意外内容：{name}")
            continue
        member = name.removeprefix(f"{root}/").split("/", maxsplit=1)[0]
        if member and member not in SDIST_ALLOWED_ROOTS:
            violations.append(f"源码包包含构建安装不需要的内容：{name}")
    return violations


def run(command: Sequence[object], cwd: Path) -> subprocess.CompletedProcess[str]:
    print(f"$ {' '.join(str(part) for part in command)}")
    return subprocess.run(
        [str(part) for part in command],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
    )


def fail(result: subprocess.CompletedProcess[str], step: str) -> int:
    print(f"[失败] {step}（退出码 {result.returncode}）")
    if result.stdout:
        print(result.stdout)
    if result.stderr:
        print(result.stderr)
    return 1


def verify_installation(venv_dir: Path, work_dir: Path, version: str) -> list[str]:
    """在仓库外目录用独立子进程验证已安装的命令与包。"""
    problems = []
    bin_dir = venv_dir / ("Scripts" if os.name == "nt" else "bin")
    python = bin_dir / "python"
    script = bin_dir / "hvs-catalog"
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    checks = [
        ([script, "--version"], f"hvs-catalog {version}"),
        ([script, "--help"], "usage: hvs-catalog"),
        ([python, "-m", "hvs_catalog", "--version"], f"hvs-catalog {version}"),
        (
            [
                python,
                "-c",
                "from importlib.metadata import version; print(version('hvs-catalog'))",
            ],
            version,
        ),
    ]
    for command, expected in checks:
        result = subprocess.run(
            [str(part) for part in command],
            cwd=work_dir,
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            problems.append(
                f"命令失败（退出码 {result.returncode}）：{' '.join(str(p) for p in command)}\n{result.stderr}"
            )
            continue
        if expected not in result.stdout:
            problems.append(
                f"输出不符合预期：{' '.join(str(p) for p in command)}\n"
                f"期望包含 {expected!r}，实际输出 {result.stdout!r}"
            )
    return problems


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="check_distribution",
        description="构建发行包，并在仓库外的全新环境安装 wheel、用独立子进程验证帮助与版本。",
    )
    parser.add_argument(
        "python",
        nargs="?",
        default=None,
        help="创建虚拟环境使用的 Python 版本，如 3.12；默认使用 uv 的默认解释器",
    )
    args = parser.parse_args(argv)

    version = project_version()
    with tempfile.TemporaryDirectory(prefix="hvs-catalog-check-") as tmp_name:
        tmp = Path(tmp_name)
        dist_dir = tmp / "dist"
        built = run(["uv", "build", "--out-dir", dist_dir], REPO_ROOT)
        if built.returncode != 0:
            return fail(built, "uv build")
        wheels = sorted(dist_dir.glob("*.whl"))
        sdists = sorted(dist_dir.glob("*.tar.gz"))
        if len(wheels) != 1 or len(sdists) != 1:
            print(
                f"[失败] 期望各生成一个 wheel 和源码包，实际 wheel={wheels}，sdist={sdists}"
            )
            return 1
        with zipfile.ZipFile(wheels[0]) as archive:
            wheel_names = archive.namelist()
        with tarfile.open(sdists[0]) as archive:
            sdist_names = archive.getnames()
        violations = [
            *check_wheel(wheel_names),
            *check_sdist(sdist_names, version),
        ]
        if violations:
            print("[失败] 发行内容检查未通过：")
            for violation in violations:
                print(f"  - {violation}")
            return 1
        print(f"[通过] 发行内容检查：{wheels[0].name} 与 {sdists[0].name}")

        venv_dir = tmp / "venv"
        venv_command = ["uv", "venv", venv_dir]
        if args.python:
            venv_command += ["--python", args.python]
        created = run(venv_command, tmp)
        if created.returncode != 0:
            return fail(created, "uv venv")
        bin_python = venv_dir / ("Scripts" if os.name == "nt" else "bin") / "python"
        installed = run(
            ["uv", "pip", "install", "--python", bin_python, wheels[0]], tmp
        )
        if installed.returncode != 0:
            return fail(installed, "uv pip install")
        work_dir = tmp / "work"
        work_dir.mkdir()
        problems = verify_installation(venv_dir, work_dir, version)
        if problems:
            print("[失败] 仓库外子进程验证未通过：")
            for problem in problems:
                print(f"  - {problem}")
            return 1
    print(
        f"[通过] 仓库外全新环境安装 wheel，独立子进程查询帮助与版本均正常（{version}）"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

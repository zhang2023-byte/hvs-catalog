"""构建发行包，并在仓库外的全新环境中验证安装。

执行内容：

1. 调用 uv build 把 wheel 与源码包生成到仓库外的临时目录；
2. 检查两个包的内容只含构建安装所需文件，排除正式数据、开发材料与运行产物；
3. 用 uv 在仓库外创建全新虚拟环境并安装 wheel；
4. 以独立子进程验证 ``hvs-catalog --help``、``--version``、``python -m`` 与包导入，
   子进程在仓库外目录运行并清除 PYTHONPATH；
5. 用安装后的 ``hvs-catalog demo`` 在仓库外走查持久化恢复演示：重启找回待办
   与复用结果、同一决定只生效一次、冲突决定被拒绝、真实进程终止后的重复
   调用与计费；
6. 用安装后的 R05 公共接口在仓库外走查工作空间全链路：初始化空间、导入
   合成材料、执行暂停、新进程提交专家决定并完成、备份、恢复到新目录后
   在新进程核对已完成状态且重复提交保护仍然有效。

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


def verify_demo(script: Path, work_dir: Path) -> list[str]:
    """在仓库外用独立子进程走查演示：重启恢复、结果复用、决定规则与重复计费。

    退出码 70 与 hvs_catalog.demo.runner.CRASH_EXIT_CODE 一致，代表
    “收到响应但尚未保存”的真实进程终止。
    """
    problems: list[str] = []
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)

    def run_demo(args: Sequence[object]) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [str(script), "demo", *[str(arg) for arg in args]],
            cwd=work_dir,
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )

    def expect(
        step: str,
        args: Sequence[object],
        *,
        code: int = 0,
        includes: Sequence[str] = (),
        stderr_includes: str | None = None,
    ) -> None:
        result = run_demo(args)
        if result.returncode != code:
            problems.append(
                f"演示步骤 {step} 退出码 {result.returncode}（期望 {code}）："
                f"demo {' '.join(str(a) for a in args)}\n{result.stdout}{result.stderr}"
            )
            return
        for text in includes:
            if text not in result.stdout:
                problems.append(
                    f"演示步骤 {step} 输出缺少 {text!r}，实际输出 {result.stdout!r}"
                )
        if stderr_includes is not None and stderr_includes not in result.stderr:
            problems.append(
                f"演示步骤 {step} 错误输出缺少 {stderr_includes!r}，"
                f"实际错误输出 {result.stderr!r}"
            )

    space = work_dir / "demo-space"
    crash_space = work_dir / "demo-crash-space"

    # 场景一：暂停 → 新进程找回待办与已保存结果 → 决定并复用结果
    expect(
        "run",
        ["run", "--space", space],
        includes=(
            "[暂停]",
            "是否识别为同一天体",
            "580±20 km/s（来源：当前论文）",
            "累计调用 2 次",
        ),
    )
    expect(
        "status",
        ["status", "--space", space],
        includes=("[固定输入]", "等待裁决", "已保存结果 2 项"),
    )
    expect(
        "decide",
        ["decide", "--space", space, "--choice", "merge"],
        includes=("[复用]", "[完成]", "对象分组：2 组", "累计调用 2 次"),
    )
    # 同一决定重复提交只生效一次；冲突决定被拒绝
    expect(
        "decide-again",
        ["decide", "--space", space, "--choice", "merge"],
        includes=("未重复生效", "累计调用 2 次"),
    )
    expect(
        "decide-conflict",
        ["decide", "--space", space, "--choice", "keep-separate"],
        code=2,
        stderr_includes="[拒绝]",
    )

    # 场景二：真实终止进程后恢复，展示重复调用与重复计费
    expect(
        "crash",
        ["run", "--space", crash_space, "--crash-after-response"],
        code=70,
        includes=("尚未保存",),
    )
    expect(
        "status-after-crash",
        ["status", "--space", crash_space],
        includes=("累计调用 1 次",),
    )
    expect(
        "resume",
        ["run", "--space", crash_space],
        includes=("重复计费", "累计调用 3 次"),
    )
    expect(
        "crash-decide",
        ["decide", "--space", crash_space, "--choice", "keep-separate"],
        includes=("[完成]", "累计调用 3 次"),
    )
    return problems


R05_DRIVER = '''
"""仓库外 R05 公共接口走查：初始化、执行、裁决、备份、恢复与重放保护。

只用安装后的包内公共入口，不依赖仓库源码路径。步骤：
run（初始化+执行暂停）、decide（提交决定+完成）、verify（备份+恢复+核对）。
"""

import json
import sys
from pathlib import Path

from hvs_catalog.records.common import RecordRef
from hvs_catalog.runtime import resume_execution
from hvs_catalog.synthetic import (
    open_service,
    start_execution,
    submit_expert_decision,
)
from hvs_catalog.synthetic.flow import SyntheticFlow
from hvs_catalog.workspace import backup_workspace, restore_workspace

step, root = sys.argv[1], Path(sys.argv[2])
space_path = root / "space"
ledger = root / "ledger.db"
if step == "run":
    from hvs_catalog.workspace import initialize_workspace

    space = initialize_workspace(space_path)
    execution, flow, _ = start_execution(space, paper="A", request_key="r05-dist")
    service = open_service(ledger)
    report = resume_execution(
        space, execution.execution_id, flow, service=service
    )
    service.close()
    assert report.outcome == "paused", report.outcome
    question = report.pending_question
    print("R05-PAUSED")
    print(
        json.dumps(
            question.question_ref.model_dump(mode="json"), ensure_ascii=False
        )
    )
    space.close()
elif step == "decide":
    from hvs_catalog.workspace import open_workspace

    question_ref = RecordRef.model_validate(json.loads(sys.argv[3]))
    space = open_workspace(space_path)
    submit_expert_decision(
        space,
        execution_id="exec-000001",
        question_ref=question_ref,
        request_key="r05-dist-dec",
    )
    service = open_service(ledger)
    report = resume_execution(
        space, "exec-000001", SyntheticFlow(paper="A"), service=service
    )
    service.close()
    assert report.outcome == "finished", report.outcome
    from hvs_catalog.synthetic import material as m

    revision = space.store().current_revision(m.paper_record_id("A", "exec-000001"))
    assert revision == 2, revision
    print("R05-FINISHED")
    space.close()
elif step == "verify":
    from hvs_catalog.workspace import open_workspace

    question_ref = RecordRef.model_validate(json.loads(sys.argv[3]))
    space = open_workspace(space_path)
    backup_workspace(space, root / "backup")
    restored = restore_workspace(root / "backup", root / "restored")
    assert restored.identity.space_id == space.identity.space_id
    report = resume_execution(
        restored, "exec-000001", SyntheticFlow(paper="A"), service=None
    )
    assert report.outcome == "finished", report.outcome
    assert report.mode == "already-finished", report.mode
    from hvs_catalog.synthetic import material as m

    paper_id = m.paper_record_id("A", "exec-000001")
    assert restored.store().current_revision(paper_id) == 2
    # 同一决定在恢复出的空间重复提交：回放原回执，不再生效
    replay = submit_expert_decision(
        restored,
        execution_id="exec-000001",
        question_ref=question_ref,
        request_key="r05-dist-dec",
    )
    assert not replay.first_save
    assert restored.store().current_revision(paper_id) == 2
    print("R05-VERIFIED")
    restored.close()
    space.close()
else:
    raise SystemExit(f"未知步骤：{step}")
'''


def verify_r05(python: Path, work_dir: Path) -> list[str]:
    """仓库外用安装后的 R05 公共接口走查全链路（A22 发行场景）。

    三个独立子进程分别完成执行暂停、决定与完成、备份恢复与重放保护，
    全部清除 PYTHONPATH，只依赖安装后的包。
    """
    problems: list[str] = []
    driver = work_dir / "r05_check.py"
    driver.write_text(R05_DRIVER, encoding="utf-8")
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)

    def run_step(name: str, *args: object) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [str(python), str(driver), name, str(work_dir), *map(str, args)],
            cwd=work_dir,
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )

    run_result = run_step("run")
    if run_result.returncode != 0:
        problems.append(
            f"R05 执行步骤失败（退出码 {run_result.returncode}）：\n"
            f"{run_result.stdout}{run_result.stderr}"
        )
        return problems
    lines = [line for line in run_result.stdout.splitlines() if line.strip()]
    if not lines or lines[0] != "R05-PAUSED":
        problems.append(f"R05 执行步骤输出异常：{run_result.stdout!r}")
        return problems
    question_ref = lines[1]

    decide_result = run_step("decide", question_ref)
    if decide_result.returncode != 0 or "R05-FINISHED" not in decide_result.stdout:
        problems.append(
            f"R05 决定步骤失败（退出码 {decide_result.returncode}）：\n"
            f"{decide_result.stdout}{decide_result.stderr}"
        )
        return problems

    verify_result = run_step("verify", question_ref)
    if verify_result.returncode != 0 or "R05-VERIFIED" not in verify_result.stdout:
        problems.append(
            f"R05 备份恢复核对步骤失败（退出码 {verify_result.returncode}）：\n"
            f"{verify_result.stdout}{verify_result.stderr}"
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
        script = venv_dir / ("Scripts" if os.name == "nt" else "bin") / "hvs-catalog"
        problems = verify_demo(script, work_dir)
        if problems:
            print("[失败] 仓库外演示走查未通过：")
            for problem in problems:
                print(f"  - {problem}")
            return 1
        problems = verify_r05(bin_python, work_dir)
        if problems:
            print("[失败] 仓库外 R05 公共接口走查未通过：")
            for problem in problems:
                print(f"  - {problem}")
            return 1
    print(
        f"[通过] 仓库外全新环境安装 wheel，独立子进程查询帮助与版本均正常（{version}）；"
        "持久化恢复演示走查通过（重启恢复、结果复用、决定规则、重复计费）；"
        "R05 公共接口走查通过（初始化、执行、裁决、备份、恢复、重放保护）"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

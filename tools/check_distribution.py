"""Build and verify current wheel entry points with synthetic materials and loopback models.

Checks include fixed-source preparation, roster corrections, process recovery, backups,
and explicit guards for unavailable scientific workflows. No real model or Gold calls.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import tarfile
import tempfile
import textwrap
import tomllib
import zipfile
from collections.abc import Iterable, Sequence
from pathlib import Path

from installed_extraction_checks import verify_installed_extraction
from installed_llm_checks import verify_installed_llm
from installed_pdf_fixture import PDF_FIXTURE
from installed_preparation_checks import verify_preparation_batches

REPO_ROOT = Path(__file__).resolve().parents[1]

# Exclude official catalog data, development materials, and run outputs from every distribution.
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

# Retired modules must never reappear; retain valid modules/package entry points.
RETIRED_MODULE_PATHS = frozenset(
    {
        "hvs_catalog/pipeline/mock.py",
        "hvs_catalog/pipeline/registry.py",
        "hvs_catalog/pipeline/methods.py",
        "hvs_catalog/demo/runner.py",
        "hvs_catalog/demo/graph.py",
        "hvs_catalog/demo/service.py",
        "hvs_catalog/demo/store.py",
        "hvs_catalog/synthetic/flow.py",
        "hvs_catalog/synthetic/runner.py",
        "hvs_catalog/synthetic/service.py",
        "hvs_catalog/evaluation/git_revision.py",
        "hvs_catalog/evaluation/scoring/agent.py",
        "hvs_catalog/evaluation/scoring/limitations.py",
        "hvs_catalog/evaluation/scoring/builders.py",
        "hvs_catalog/materials/_mineru_worker.py",
        "hvs_catalog/materials/_mineru_api_worker.py",
        "hvs_catalog/materials/_mineru_table_worker.py",
        "hvs_catalog/materials/cloud_tables.py",
    }
)

# sdist roots allow build/install files only; hatchling adds .gitignore automatically.
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
    """Wheel top level allows only hvs_catalog/ and its dist-info metadata."""
    violations = find_forbidden_paths(names)
    violations.extend(name for name in names if name in RETIRED_MODULE_PATHS)
    for top in sorted(top_levels(names)):
        if top in FORBIDDEN_SEGMENTS:
            continue  # These paths were already reported by full member path.
        is_package = top == "hvs_catalog"
        is_dist_info = top.startswith("hvs_catalog-") and top.endswith(".dist-info")
        if not is_package and not is_dist_info:
            violations.append(f"wheel 顶层出现意外内容：{top}")
    return violations


def check_sdist(names: Sequence[str], version: str) -> list[str]:
    """Source archives allow only hvs_catalog-<version>/ with required build/install
    contents.
    """
    violations = find_forbidden_paths(names)
    root = f"hvs_catalog-{version}"
    for name in names:
        if name.removeprefix(f"{root}/src/") in RETIRED_MODULE_PATHS:
            violations.append(name)
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
    """Verify installed commands/imports in independent external-directory subprocesses."""
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
        (
            [
                python,
                "-c",
                textwrap.dedent("""\
                    import json
                    from jsonschema import Draft202012Validator
                    from pydantic import ValidationError
                    from hvs_catalog.llm.prompts import assemble_blueprint, PromptInput
                    from hvs_catalog.pipeline import ExtractionConfig
                    from hvs_catalog.records import (
                        LiteratureContribution,
                        literature_contribution_to_json,
                        load_literature_contribution,
                        parse_literature_contribution,
                        save_literature_contribution,
                    )
                    evidence = [{"location": "T:p1-b1", "quote": "Fictional evidence."}]
                    payload = {
                        "metadata": {
                            "arxiv_id": "2601.99999", "version": "v1",
                            "title": "Fictional paper", "authors": ["A. Example"],
                        },
                        "stars": [{
                            "identifiers": [{"value": "FIC-A", "evidence": evidence}],
                            "contribution_type": {"value": "follow_up", "evidence": evidence},
                            "quantities": [{"quantity": "distance", "values": [{
                                "lower_bound": "7.00", "upper_bound": "9.00",
                                "lower_inclusive": False, "unit": "kpc",
                                "evidence": evidence,
                            }]}],
                        }],
                        "notes": ["FIC-A: The interval has no reported central value. See T:p1-b1."],
                    }
                    document = parse_literature_contribution(payload)
                    save_literature_contribution("literature_contribution.json", document)
                    loaded = load_literature_contribution("literature_contribution.json")
                    assert json.loads(literature_contribution_to_json(loaded)) == payload
                    schema = LiteratureContribution.model_json_schema()
                    Draft202012Validator.check_schema(schema)
                    validator = Draft202012Validator(schema)
                    validator.validate(payload)
                    for stage, submission in (
                        ("roster", {"stars": [{key: payload["stars"][0][key] for key in ("identifiers", "contribution_type")}], "notes": ""}),
                        ("quantities", {"quantities": payload["stars"][0]["quantities"], "notes": ""}),
                    ):
                        blueprint = assemble_blueprint(stage)
                        Draft202012Validator(blueprint.tool().parameters).validate(submission)
                        assert blueprint.system.count("\\n# ") == 3
                        assert "{{quantity_table}}" not in blueprint.system
                    try:
                        blueprint.request(ExtractionConfig().default_model, PromptInput(manuscript="Fictional paper."))
                    except ValueError:
                        pass
                    else:
                        raise AssertionError("Quantity definitions enabled request construction")
                    payload["stars"][0]["quantities"][0]["values"][0]["value"] = "8.00"
                    assert not validator.is_valid(payload)
                    try:
                        parse_literature_contribution(payload)
                    except ValidationError:
                        pass
                    else:
                        raise AssertionError("Point value and bounds were accepted together")
                    print("literature contribution installed round trip passed")
                    """),
            ],
            "literature contribution installed round trip passed",
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


ACQUISITION_DRIVER = (
    PDF_FIXTURE
    + "\nSYNTHETIC_PDF_HELPERS = "
    + repr(PDF_FIXTURE)
    + "\n"
    + '''
"""External installed acquisition CLI checks using offline synthetic materials.

Initialize via documented public Python commands, build synthetic tar.gz source packages, then use installed console scripts in independent subprocesses for import/status (human and JSON)/material/error codes. netflow verifies discovery/fetch/revision success and fresh-process recovery using installed Python, cleared PYTHONPATH, and test-side injected fake protocol services. The product exposes no rate-limit/offline bypass. Two subprocesses cover page-boundary interruption, complete recovery, fetch, pending revisions, and material output without real arXiv access.
"""

import io
import json
import os
import subprocess
import sys
import tarfile
from pathlib import Path

root = Path(sys.argv[1])
step = sys.argv[2]
script = Path(sys.argv[3])
space = root / "acquisition-space"
# Subprocess step cwd; initialize the workspace itself through the documented public command.
root.mkdir(parents=True, exist_ok=True)

# Same public initialization command as README, the sole initialization entry point.
DOCUMENTED_INIT = (
    "from hvs_catalog.workspace import initialize_workspace; "
    "initialize_workspace(r'%s')" % space
)

NETCHILD_SOURCE = SYNTHETIC_PDF_HELPERS + """import sys
from pathlib import Path

# Check acquisition network paths in subprocesses after installation (offline synthetic).
# Use the installed interpreter to import hvs_catalog and replace the CLI service factory
# with a synthetic protocol service (test injection; no offline/rate-limit bypass in the
# product CLI). Call CLI main in separate interruption and resume/fetch/revision/material processes.

import io
import tarfile

from hvs_catalog.cli import main
import hvs_catalog.cli_acquisition as cli_acq
from hvs_catalog.pipeline.acquisition.lifecycle import AcquisitionService
from hvs_catalog.pipeline.acquisition.protocol import (
    ArxivIdBatchResult,
    ArxivIdLookup,
    ArxivPaperMeta,
    ArxivSearchPage,
    ArxivSearchRequest,
    ArxivSourceDownload,
    parse_arxiv_identifier,
)
from hvs_catalog.workspace import open_workspace


class FakeClock:
    def __init__(self):
        self._now = 1700000000.0

    def now(self):
        return self._now

    def sleep(self, seconds):
        self._now += max(0.0, seconds)


def make_meta(cid, version):
    return ArxivPaperMeta(
        canonical_id=cid,
        version=version,
        api_id_url="http://arxiv.org/abs/%sv%d" % (cid, version),
        title="Paper " + cid,
        authors=("Author One",),
        categories=("astro-ph.CO",),
        primary_category="astro-ph.CO",
        published="2024-01-01T00:00:00Z",
        updated="2024-01-02T00:00:00Z",
        abstract="installed smoke abstract",
        doi=None,
        journal_ref=None,
    )


def make_page(request, entries, total, fetched_at):
    return ArxivSearchPage(
        request=request,
        response_url="https://export.arxiv.org/api/query synthetic",
        fetched_at=fetched_at,
        total_results=total,
        returned_start_index=request.start,
        entries=entries,
        anomalies=(),
    )


PACKAGE_BYTES = None


def package_bytes():
    global PACKAGE_BYTES
    if PACKAGE_BYTES is None:
        PACKAGE_BYTES = make_pdf("Synthetic netflow smoke")
    return PACKAGE_BYTES


class FakeApi:
    def __init__(self):
        self.metadata = {"2401.05555": make_meta("2401.05555", 2)}

    def search(self, request):
        if request.start == 0:
            return make_page(
                request, (make_meta("2401.06666", 1),), 2, 1700000100.0
            )
        if request.start == 1:
            return make_page(
                request, (make_meta("2401.06667", 1),), 2, 1700000200.0
            )
        raise AssertionError("unscripted page %d" % request.start)

    def metadata_by_ids(self, identifiers):
        lookups = []
        for item in identifiers:
            requested = parse_arxiv_identifier(
                item if isinstance(item, str) else item.canonical_id
            )
            meta = self.metadata.get(requested.canonical_id)
            lookups.append(
                ArxivIdLookup(
                    requested=requested,
                    status="found" if meta is not None else "not_found",
                    meta=meta,
                )
            )
        return ArxivIdBatchResult(
            response_url="synthetic",
            fetched_at=1700000000.0,
            lookups=tuple(lookups),
            anomalies=(),
        )

    def download_pdf(self, identifier):
        versioned = (
            identifier if isinstance(identifier, str) else identifier.versioned_id
        )
        payload = package_bytes()
        return ArxivSourceDownload(
            identifier=parse_arxiv_identifier(versioned),
            final_url="https://arxiv.org/e-print/" + versioned,
            content_type="application/gzip",
            payload=payload,
            byte_count=len(payload),
            fetched_at=1700000000.0,
        )


phase = sys.argv[1]
space_path = sys.argv[2]
workspace = open_workspace(Path(space_path))
service = AcquisitionService(workspace, FakeApi(), clock=FakeClock())


def factory(space_arg):
    return service


cli_acq._build_acquisition_service = factory

if phase == "partial":
    code = main([
        "acquisition", "discover", "--space", space_path,
        "--from", "2024-01-01", "--to", "2024-01-31",
        "--terms", "dark matter", "--category", "astro-ph.CO",
        "--max-pages", "1",
    ])
    print("NETFLOW-PARTIAL-CODE=%d" % code)
    sys.exit(0 if code == 3 else 1)

if phase == "resume":
    checks = []
    code_discover = main([
        "acquisition", "discover", "--space", space_path,
        "--from", "2024-01-01", "--to", "2024-01-31",
        "--terms", "dark matter", "--category", "astro-ph.CO",
    ])
    checks.append(("discover-resume", code_discover, 0))
    code_fetch = main([
        "acquisition", "fetch", "--space", space_path, "--id", "2401.05555v1",
    ])
    checks.append(("fetch", code_fetch, 0))
    code_rev = main([
        "acquisition", "revisions", "--space", space_path,
        "--id", "2401.05555",
    ])
    checks.append(("revisions", code_rev, 0))
    code_mat = main([
        "acquisition", "material", "--space", space_path, "--id", "2401.05555",
    ])
    checks.append(("material", code_mat, 0))
    bad = []
    for name, code, want_code in checks:
        if code != want_code:
            bad.append("%s 退出码 %d（期望 %d）" % (name, code, want_code))
    if bad:
        for item in bad:
            print("[问题] " + item)
        sys.exit(1)
    print("NETFLOW-RESUME-OK")
    sys.exit(0)

sys.exit(2)
"""

def run_cli(*args: object) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    return subprocess.run(
        [str(script), "acquisition", *map(str, args)],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


def run_child(*command: object) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    return subprocess.run(
        list(map(str, command)),
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


def make_package(path: Path) -> None:
    path.write_bytes(make_pdf("Synthetic installed PDF"))


if step == "setup":
    # Use README's exact public initialization command in a subprocess with installed Python.
    result = run_child(sys.executable, "-c", DOCUMENTED_INIT)
    if result.returncode != 0:
        print("[问题] 文档化初始化失败：%s%s" % (result.stdout, result.stderr))
        raise SystemExit(1)
    make_package(root / "smoke.tar.gz")
    print("ACQUISITION-READY")
    raise SystemExit(0)

if step == "flow":
    problems = []
    imported = run_cli(
        "import", "--space", space, "--file", root / "smoke.tar.gz",
        "--id", "2401.01234v1", "--provenance", "distribution smoke",
    )
    if imported.returncode != 0 or "state=imported" not in imported.stdout:
        problems.append("import 失败：%d %s %s" % (
            imported.returncode, imported.stdout, imported.stderr))
    again = run_cli(
        "import", "--space", space, "--file", root / "smoke.tar.gz",
        "--id", "2401.01234v1", "--provenance", "second declaration",
    )
    if again.returncode != 0 or "重放" not in again.stdout:
        problems.append("重复导入未按重放处理：%d %s %s" % (
            again.returncode, again.stdout, again.stderr))
    missing_provenance = run_cli(
        "import", "--space", space, "--file", root / "smoke.tar.gz",
        "--id", "2401.01234v1",
    )
    if missing_provenance.returncode != 2:
        problems.append("缺来源声明应退出 2，实际 %d" % missing_provenance.returncode)
    status_text = run_cli("status", "--space", space)
    if status_text.returncode != 0 or "state=imported" not in status_text.stdout:
        problems.append("status 失败：%d %s %s" % (
            status_text.returncode, status_text.stdout, status_text.stderr))
    status_json = run_cli("status", "--space", space, "--json")
    try:
        payload = json.loads(status_json.stdout)
        logged = [
            item["provenance"]
            for item in payload["selections"][0]["provenance_log"]
        ]
    except (ValueError, KeyError, IndexError):
        logged = []
        problems.append("status --json 输出不可解析：%r" % status_json.stdout)
    if logged != ["distribution smoke", "second declaration"]:
        problems.append("溯源日志异常：%s" % logged)
    material = run_cli("material", "--space", space, "--id", "2401.01234")
    if material.returncode != 0 or "arxiv-source-2401.01234" not in material.stdout:
        problems.append("material 失败：%d %s %s" % (
            material.returncode, material.stdout, material.stderr))
    # Acquisition v2 separates selection from declaration reuse; old-version lookup leaves current unchanged.
    newer = run_cli(
        "import", "--space", space, "--file", root / "smoke.tar.gz",
        "--id", "2401.01234v2", "--provenance", "distribution v2",
    )
    old = run_cli("status", "--space", space, "--id", "2401.01234v1", "--json")
    try:
        history = json.loads(old.stdout)
        current = history["selections"][0]
        previous = history["versions"][0]
        if (newer.returncode != 0 or old.returncode != 0
            or current["version"] != 2 or previous["version"] != 1
            or previous["raw_path"] != current["raw_path"]
            or previous["provenance_log"][0]["provenance"] != "distribution smoke"):
            problems.append("版本回查/内容复用异常")
    except (ValueError, KeyError, IndexError):
        problems.append("版本回查输出不可解析：%r" % old.stdout)
    not_ready = run_cli("material", "--space", space, "--id", "2401.09999")
    if not_ready.returncode != 6:
        problems.append("未就绪材料退出码应为 6，实际 %d" % not_ready.returncode)
    uninitialized = run_cli("status", "--space", root / "not-a-space")
    if uninitialized.returncode != 1:
        problems.append("未初始化空间退出码应为 1，实际 %d" % uninitialized.returncode)
    bad_discover = run_cli(
        "discover", "--space", space, "--from", "2024-01-01", "--to", "2024-01-02"
    )
    if bad_discover.returncode != 1 or "至少提供其一" not in bad_discover.stderr:
        problems.append("缺检索条件应拒绝（退出码 1）：%d %s" % (
            bad_discover.returncode, bad_discover.stderr))
    if problems:
        for problem in problems:
            print("[问题] %s" % problem)
        raise SystemExit(1)
    print("ACQUISITION-CLI-OK")
    raise SystemExit(0)

if step == "netflow":
    # Installed offline synthetic discover/fetch/revisions success and cross-process recovery:
    # two independent installed-Python subprocesses with cleared PYTHONPATH and test-side fake-service injection.
    netchild = root / "acquisition_network.py"
    netchild.write_text(NETCHILD_SOURCE, encoding="utf-8")
    partial = run_child(sys.executable, netchild, "partial", space)
    if partial.returncode != 0 or "NETFLOW-PARTIAL-CODE=3" not in partial.stdout:
        print("[问题] 页界中断步骤异常：%d %s %s" % (
            partial.returncode, partial.stdout, partial.stderr))
        raise SystemExit(1)
    resume = run_child(sys.executable, netchild, "resume", space)
    if resume.returncode != 0 or "NETFLOW-RESUME-OK" not in resume.stdout:
        print("[问题] 恢复步骤异常：%d %s %s" % (
            resume.returncode, resume.stdout, resume.stderr))
        raise SystemExit(1)
    # Independently verify restored stored facts with pure CLI status, without injection.
    verify = run_cli("status", "--space", space)
    if verify.returncode != 0 or "state=complete" not in verify.stdout:
        print("[问题] 恢复后状态查询异常：%d %s %s" % (
            verify.returncode, verify.stdout, verify.stderr))
        raise SystemExit(1)
    print("ACQUISITION-NETFLOW-OK")
    raise SystemExit(0)

raise SystemExit(2)
'''
)


def verify_acquisition(python: Path, work_dir: Path, script: Path) -> list[str]:
    """Exercise installed acquisition CLI offline outside the repository.

    Initialize workspaces/build synthetic packages in subprocesses, then independently
    test imports/replay, human/JSON status, materials, and error codes using installed
    console scripts, cleared PYTHONPATH, and no networks.
    """
    problems: list[str] = []
    driver = work_dir / "acquisition_check.py"
    driver.write_text(ACQUISITION_DRIVER, encoding="utf-8")
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)

    def run_step(name: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                str(python),
                str(driver),
                str(work_dir / "acquisition"),
                name,
                str(script),
            ],
            cwd=work_dir,
            env=env,
            capture_output=True,
            text=True,
            timeout=300,
            check=False,
        )

    for step, marker, code in (
        ("setup", "ACQUISITION-READY", 0),
        ("flow", "ACQUISITION-CLI-OK", 0),
        ("netflow", "ACQUISITION-NETFLOW-OK", 0),
    ):
        result = run_step(step)
        if result.returncode != code:
            problems.append(
                f"获取步骤 {step} 退出码 {result.returncode}（期望 {code}）：\n"
                f"{result.stdout}{result.stderr}"
            )
            continue
        if marker not in result.stdout:
            problems.append(f"获取步骤 {step} 输出缺少 {marker!r}：{result.stdout!r}")
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
        print("[通过] 退役模块均不在 wheel/sdist 中")

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
        problems = verify_acquisition(bin_python, work_dir, script)
        if problems:
            print("[失败] 仓库外获取子系统 CLI 走查未通过：")
            for problem in problems:
                print(f"  - {problem}")
            return 1
        problems = verify_preparation_batches(bin_python, work_dir, script)
        if problems:
            print("[失败] 仓库外批量准备与阶段隔离走查未通过：")
            for problem in problems:
                print(f"  - {problem}")
            return 1
        problems = verify_installed_llm(venv_dir, work_dir, script)
        if problems:
            print("[失败] 仓库外模型接口与 CLI 走查未通过：")
            for problem in problems:
                print(f"  - {problem}")
            return 1
        problems = verify_installed_extraction(venv_dir, work_dir, script)
        if problems:
            print("[失败] 仓库外提取配置、提示与恢复走查未通过：")
            for problem in problems:
                print(f"  - {problem}")
            return 1
    print(
        f"[通过] Python {args.python}: wheel 安装、材料准备、两阶段提取、补正恢复、最终组装、评分与离线重评、备份及入口边界。全部检查使用合成材料和本地 HTTP。"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

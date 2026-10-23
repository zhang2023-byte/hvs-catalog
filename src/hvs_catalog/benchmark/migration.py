"""Migrate completed benchmarks into a new directory before replacing any original."""

import hashlib
import json
import re
import shutil
from pathlib import Path

from ..workspace import initialize_workspace, open_workspace
from ..workspace.layout import resolve_within
from .archive import archive_execution
from .experiments import read_experiment, verify_selection, write_json
from .materials import migrate_package, selection
from .reporting import compare_experiments, write_report


def _copy(source, target):
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        if source.read_bytes() != target.read_bytes():
            raise ValueError(f"Conflicting migration destination: {target}")
    else:
        shutil.copyfile(source, target)
    if (
        hashlib.sha256(source.read_bytes()).digest()
        != hashlib.sha256(target.read_bytes()).digest()
    ):
        raise ValueError("A migrated file differs from its original bytes.")


def _costs(source, target, root, name):
    """Keep billing facts; relocate only request/response references."""
    if not source.exists():
        return
    for path in sorted(source.iterdir()):
        if path.name != "requests.json":
            _copy(path, target / path.name)
            continue
        rows = json.loads(path.read_bytes())
        for row in rows:
            paper = row["arxiv_id"]
            row["execution_id"] = f"{name}/{paper}"
            for key, directory in (
                ("request_ref", "requests"),
                ("response_ref", "responses"),
            ):
                if row.get(key):
                    relative = (
                        f"runs/{name}/{paper}/trace/{directory}/"
                        + Path(row[key]["path"]).name
                    )
                    actual = resolve_within(root, relative)
                    row[key] = {
                        "path": relative,
                        "sha256": hashlib.sha256(actual.read_bytes()).hexdigest(),
                    }
        write_json(target / path.name, rows)


def _copy_preparation(source, output, path):
    """Relocate known preparation navigation, preserving original factual evidence."""
    relative = path.relative_to(source / "preparations")
    target = output / "preparations" / relative
    original = path.read_bytes()
    content = original
    if relative.as_posix() == "mineru-cloud/report.html":
        text = original.decode()
        prefix = "file://" + str(source) + "/"

        def link(match):
            old = match.group(1).removeprefix(prefix)
            if old.startswith("materials/"):
                old = old.replace(
                    "/mineru_cloud/full.md", "/mineru_cloud/manuscript.md"
                )
                old = old.replace(
                    "/mineru_cloud/layout.json", "/mineru_cloud/raw/layout.json"
                )
                return 'href="../../' + old + '"'
            return 'href="' + old.removeprefix("preparations/mineru-cloud/") + '"'

        text = re.sub(r'href="(' + re.escape(prefix) + r'[^"]+)"', link, text)
        layout = """<section><h2>目录与代码适配</h2><pre>materials/&lt;arxiv_id&gt;/
├── paper.pdf、metadata.json                两版共享的固定来源
├── mineru_local/                          历史本地解析
└── mineru_cloud/                          本次云端解析
    ├── material.json、result.zip、manuscript.md
    ├── raw/                              官方 layout.json 等原件
    ├── images/
    └── pdf-blocks.json</pre><p>云端解析完成后，Benchmark 已统一重构目录。上方统计与核验记录保留当时事实，本文材料链接已更新。每轮通过 <code>benchmark prepare --variant mineru_cloud</code> 明确选择云端材料；旧版选择 <code>mineru_local</code>。清单内的文件引用相对于整个 Benchmark 根目录。原三轮仍绑定旧版材料；本次没有启动新科学提取。</p>"""
        text = re.sub(
            r"<section><h2>目录与代码适配</h2>.*?(?=<p>CDN的TLS下载失败后)",
            lambda _: layout,
            text,
            flags=re.DOTALL,
        )
        content = text.encode()
    elif relative.as_posix() == "deepseek-text-only/README.md":
        text = (
            original.decode()
            .replace(
                "准备脚本[prepare_offline.py](prepare_offline.py)只读取已存在材料并生成配置及预览，不执行提取、评分或真实模型调用。",
                "原临时准备脚本已随目录重构移除。后续使用 `hvs-catalog benchmark --root <Benchmark目录> preview --variant <mineru_local或mineru_cloud> --papers <ID...> --config <配置文件> --name <预览名>` 生成离线预览，不创建实验或请求模型。",
            )
            .replace(
                "既有`exec01`～`exec03`保持原样。",
                "此处记录纯文本准备当时的事实。其后`exec01`～`exec03`已迁入统一布局，预测与调用内容不变。原材料清单、文本视图和预览哈希保留当时证据，旧路径不作为当前导入入口。",
            )
        )
        content = text.encode()
    if target.exists() and target.read_bytes() not in (original, content):
        raise ValueError(f"Conflicting migration destination: {target}")
    from ..workspace.layout import _write_file_published

    _write_file_published(target, content)


def migrate_benchmark(source, output, *, progress=None):
    source, output = Path(source).resolve(), Path(output).resolve()
    if source == output or source in output.parents or output in source.parents:
        raise ValueError(
            "Migration requires a separate destination outside the source tree."
        )
    if (source / ".state/workspace.json").exists():
        raise ValueError("The source already uses the shared benchmark layout.")
    if output.exists():
        target = open_workspace(output)
        if target.read_config().get("migration_origin") != str(source):
            target.close()
            raise ValueError("Destination belongs to another operation.")
    else:
        target = initialize_workspace(
            output, benchmark=True, config={"migration_origin": str(source)}
        )
    counts = {"material_packages": 0, "archived_papers": 0, "rounds": []}
    try:
        with target.lock():
            for folder in sorted((source / "materials").iterdir()):
                if not folder.is_dir():
                    continue
                for variant, old in (
                    ("mineru_local", folder),
                    ("mineru_cloud", folder / "mineru_cloud"),
                ):
                    if (old / "material.json").exists():
                        migrate_package(old, target, folder.name, variant)
                        counts["material_packages"] += 1
            for old_round in sorted((source / "runs").iterdir()):
                if not old_round.is_dir():
                    continue
                original = json.loads((old_round / "experiment.json").read_bytes())
                if original.get("status") != "finished":
                    raise ValueError(
                        "Only explicitly finished legacy experiments can be archived."
                    )
                name = original["id"]
                history = output / ".state/history" / name
                _copy(old_round / "experiment.json", history / "experiment.json")
                for filename, digest in original["fixed_files"].items():
                    path = resolve_within(old_round, filename)
                    if hashlib.sha256(path.read_bytes()).hexdigest() != digest:
                        raise ValueError(
                            f"Legacy frozen file changed: {name}/{filename}"
                        )
                    _copy(path, history / filename)
                for filename in ("report.md", "verification.json", "execution.log"):
                    if (old_round / filename).exists():
                        _copy(old_round / filename, history / filename)
                rows = original["execution_map"]
                papers = [row["arxiv_id"] for row in rows]
                spec = {
                    "format": "hvs.benchmark-experiment",
                    "version": 2,
                    "id": name,
                    "purpose": original["purpose"],
                    "created_at": original["created_at"],
                    "scope": original["scope"],
                    "configuration": original["configuration"],
                    "materials": {
                        "variant": "mineru_local",
                        "papers": selection(output, papers, "mineru_local"),
                    },
                    "source": original["source"],
                    "archived_origin": {
                        "path": f".state/history/{name}/experiment.json",
                        "sha256": hashlib.sha256(
                            (old_round / "experiment.json").read_bytes()
                        ).hexdigest(),
                    },
                }
                for row in rows:
                    paper = row["arxiv_id"]
                    old_space = open_workspace(
                        resolve_within(old_round, row["workspace"]), read_only=True
                    )
                    try:
                        archive_execution(
                            old_space,
                            target,
                            row["execution_id"],
                            name,
                            paper,
                            "mineru_local",
                        )
                    finally:
                        old_space.close()
                    counts["archived_papers"] += 1
                    if progress:
                        progress(name, paper, counts["archived_papers"])
                new_round = output / "runs" / name
                write_json(new_round / "experiment.json", spec)
                if (old_round / "scoring").exists():
                    for path in sorted((old_round / "scoring").iterdir()):
                        _copy(path, new_round / "scoring" / path.name)
                _costs(old_round / "costs", new_round / "costs", output, name)
                # Original invoice observations remain immutable supporting evidence.
                for filename in (
                    "billing-before.json",
                    "billing-after.json",
                    "pricing.json",
                    "pricing-route.json",
                    "budget-revision.json",
                    "repetition-plan.json",
                    "background-monitoring.json",
                    "final-monitor-verification.json",
                    "repetition-input-verification.json",
                    "three-round-summary.md",
                    "three-round-comparison.json",
                    "three-round-papers.csv",
                    "runtime-timing-observation.json",
                    "billing-observation.json",
                ):
                    if (old_round / filename).exists() and not (
                        history / filename
                    ).exists():
                        _copy(old_round / filename, history / filename)
                verify_selection(output, read_experiment(output, name))
                write_report(output, name)
                counts["rounds"].append(name)
            if (source / "preparations").exists():
                for path in (source / "preparations").rglob("*"):
                    relative = path.relative_to(source / "preparations")
                    if not path.is_file() or any(
                        part in {"space", "__pycache__"} for part in relative.parts
                    ):
                        continue
                    if path.suffix == ".py" or path.name == ".DS_Store":
                        continue
                    _copy_preparation(source, output, path)
            comparison = output / "comparisons/legacy"
            if not comparison.exists():
                compare_experiments(output, counts["rounds"], "legacy")
            _write_readme(output, counts["rounds"])
            write_json(
                output / ".state/migration.json",
                {
                    "format": "hvs.benchmark-migration",
                    "version": 1,
                    "source": str(source),
                    "verified": True,
                    **counts,
                },
            )
        return counts
    finally:
        target.close()


def _write_readme(root, rounds):
    next_name = f"exec{max((int(name[4:]) for name in rounds), default=0) + 1:02d}"
    lines = [
        "# Benchmark",
        "",
        "固定材料、独立实验与历史归档。整个目录是完整可搬迁单元，实验共用材料。",
        "",
        "每篇 `materials/<arxiv_id>/` 保存一份 PDF 和元数据；`mineru_local/` 与 `mineru_cloud/` 分别保存旧版和云端解析包。每轮必须明确选择一种材料，不自动回退。纯文本或含图模式由模型配置单独指定。",
        "",
        "`runs/execNN/experiment.json` 是实验条件的权威来源；单篇目录保存最终 run、科学结果与 trace。`.state/` 保存共享归档和运行状态，结束实验不可续跑。评分与费用独立保存；报告和跨轮比较从保存事实生成。",
        "",
        "历史迁移仅调整组织与引用；原始冻结条件及当时报告保留于 `.state/history/`，其中旧路径为当时事实。已有评分输入与评分结果保持原字节。`preparations/` 保留此前准备证据，其预览和日志不表示已运行新实验，也不自动更新为云端材料。",
        "",
        "## 使用",
        "",
        "命令入口：`hvs-catalog benchmark --root <本目录> --help`。`prepare` 只固定条件，`run` 才请求模型，`score` 才读取指定 Gold。新实验的范围、模型、轮次及预算另行确定。",
        "",
        "`preview --variant <材料版本> --papers <ID...> --config <配置> --name <名称>` 离线保存所选材料的请求预览，不分配实验编号。`close --round <execNN> --end` 明确结束未完成实验并保留已有结果。",
        "",
        "```bash",
        "hvs-catalog benchmark --root .work/benchmark prepare --variant mineru_cloud --papers 2207.04406 --config /path/to/config.json --purpose '材料版本比较'",
        f"hvs-catalog benchmark --root .work/benchmark status --round {next_name}",
        "```",
        "",
        f"以上为用法示例，不表示已授权或创建 {next_name}。不要直接运行历史准备脚本。",
        "",
        "## 实验索引",
        "",
        "| 执行 | 目的 | 报告 |",
        "|---|---|---|",
    ]
    for name in rounds:
        spec = read_experiment(root, name)
        lines.append(
            f"| [{name}](runs/{name}/experiment.json) | {spec['purpose']} | [结果与证据](runs/{name}/report.md) |"
        )
    lines += [
        "",
        "[跨轮比较](comparisons/legacy/report.md)",
        "",
    ]
    for path, label in (
        ("preparations/mineru-cloud/report.html", "MinerU Cloud 准备报告"),
        ("preparations/deepseek-text-only/README.md", "纯文本配置与准备证据"),
    ):
        if (root / path).exists():
            lines.extend([f"[{label}]({path})", ""])
    (root / "README.md").write_text("\n".join(lines))

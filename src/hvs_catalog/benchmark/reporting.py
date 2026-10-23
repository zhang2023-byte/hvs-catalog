"""Derive human-readable reports from authoritative saved experiment facts."""

import json
from pathlib import Path

from .experiments import experiment_status, read_experiment, write_json


def write_report(root, name):
    root = Path(root)
    spec = read_experiment(root, name)
    status = experiment_status(root, name)
    path = root / "runs" / name
    lines = [
        f"# {name}",
        "",
        spec["purpose"],
        "",
        f"材料：`{status['material_variant']}`；范围：`{spec['scope']}`；状态：`{status['state']}`。",
        "",
        "| 论文 | 结果 | 调用数 | 证据 |",
        "|---|---|---:|---|",
    ]
    for paper in status["papers"]:
        identity = paper["arxiv_id"]
        outcome = (paper["status"] or {}).get("result") or "unfinished"
        attempts = (paper["usage"] or {}).get("attempts", 0)
        link = (
            f"[运行]({identity}/run.json) · [调用]({identity}/trace/trace.json)"
            if paper["state"] == "archived"
            else "尚未归档"
        )
        lines.append(f"| {identity} | {outcome} | {attempts} | {link} |")
    for directory, label, filename in (
        ("scoring", "评分与冻结输入", "scoring-report.json"),
        ("costs", "费用", "summary.json"),
    ):
        if (path / directory / filename).exists():
            lines.extend(["", f"[{label}]({directory}/{filename})"])
    (path / "report.md").write_text("\n".join(lines) + "\n")
    return path / "report.md"


def compare_experiments(root, names, output):
    root = Path(root)
    if not names or len(names) != len(set(names)):
        raise ValueError("Select distinct experiments.")
    rows = []
    for name in names:
        spec = read_experiment(root, name)
        folder = root / "runs" / name
        score_path, cost_path = (
            folder / "scoring/scoring-report.json",
            folder / "costs/summary.json",
        )
        rows.append(
            {
                "id": name,
                "purpose": spec["purpose"],
                "materials": spec["materials"],
                "status": experiment_status(root, name),
                "score": json.loads(score_path.read_bytes())["micro"]
                if score_path.exists()
                else None,
                "costs": json.loads(cost_path.read_bytes())
                if cost_path.exists()
                else None,
            }
        )
    target = root / "comparisons" / output
    if target.exists():
        raise FileExistsError("Comparison output already exists.")
    if Path(output).name != output or output in {".", ".."}:
        raise ValueError("Comparison name must be a directory name.")
    write_json(
        target / "comparison.json",
        {"format": "hvs.benchmark-comparison", "version": 1, "experiments": rows},
    )
    lines = [
        "# 实验比较",
        "",
        "| 实验 | 材料 | 篇数 | L1 F1 | L2 F1 |",
        "|---|---|---:|---:|---:|",
    ]
    for row in rows:
        score = row["score"]

        def percentage(key, score=score):
            return f"{score[key]['f1']:.2%}" if score else "未评分"

        lines.append(
            f"| [{row['id']}](../../runs/{row['id']}/report.md) | {row['materials']['variant']} | {len(row['materials']['papers'])} | {percentage('l1')} | {percentage('l2')} |"
        )
    (target / "report.md").write_text("\n".join(lines) + "\n")
    return target

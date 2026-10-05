"""Generate readable English rules from their source; --check reads only."""

from __future__ import annotations

import argparse
import sys
import tomllib
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "src/hvs_catalog/resources/science/rules.toml"
OUTPUT = ROOT / "docs/science-rules.md"
AUTOMATIC_OUTPUT = ROOT / "docs/science-rules-automatic.md"


def check_text(row: dict, fields: tuple[str, ...]) -> None:
    for field in fields:
        if not isinstance(row[field], str) or not row[field].strip():
            raise ValueError(f"{field} 必须是非空文本")


def read_source(path: Path) -> dict:
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    if data["format_version"] != 2:
        raise ValueError("不支持的规则材料格式版本")
    if data["instruction_language"] != "en":
        raise ValueError("提取指令必须使用英文")
    check_text(data, ("title", "usage"))
    check_text(data["ambiguity_policies"], ("expert_assisted", "automatic"))
    if data["review_status"] not in {"draft", "approved"}:
        raise ValueError("review_status 必须是 draft 或 approved")

    required = {
        "rules": ("id", "title", "summary", "explanation"),
        "states": ("id", "name", "definition"),
        "quantities": ("id", "name", "definition", "legacy_path"),
        "examples": ("id", "input", "expected"),
    }
    for section, fields in required.items():
        rows = data[section]
        if not rows:
            raise ValueError(f"{section} 不能为空")
        for row in rows:
            check_text(row, fields)
        ids = [row["id"] for row in rows]
        if len(ids) != len(set(ids)):
            raise ValueError(f"{section} 有重复编号")

    if len(data["states"]) != 5 or len(data["quantities"]) != 18:
        raise ValueError("当前审定范围要求五种作者状态和 18 项物理量")
    paths = [row["legacy_path"] for row in data["quantities"]]
    if len(paths) != len(set(paths)):
        raise ValueError("物理量的旧路径重复")

    rule_ids = {row["id"] for row in data["rules"]}
    for example in data["examples"]:
        if not example["rules"] or not set(example["rules"]) <= rule_ids:
            raise ValueError(f"{example['id']} 引用了不存在的主规则")

    legacy_ids = [old_id for rule in data["rules"] for old_id in rule["legacy_rules"]]
    repeated = [key for key, count in Counter(legacy_ids).items() if count != 1]
    if repeated or len(legacy_ids) != data["legacy"]["rule_count"]:
        raise ValueError("旧规则对应必须完整且每条只列一次")
    return data


def cell(value: str) -> str:
    return value.replace("|", r"\|").replace("\n", "<br>")


def render(data: dict, policy: str = "expert_assisted") -> str:
    lines = [
        "<!-- Generated -->",
        f"# {data['title']}",
        "",
        "## Main rules in order",
        "",
        data["usage"],
        "",
    ]
    for index, rule in enumerate(data["rules"], start=1):
        lines.extend(
            [f"{index}. **{rule['title']} ({rule['id']})**: {rule['summary']}", ""]
        )

    lines.extend(["## Explanations to consult as needed", ""])
    for rule in data["rules"]:
        lines.extend(
            [
                f"### {rule['id']} {rule['title']}",
                "",
                rule["explanation"].strip(),
                "",
            ]
        )

    for heading, key in [
        ("Authors' overall assessment", "states"),
        ("Quantity scope and definitions", "quantities"),
    ]:
        lines.extend([f"## {heading}", "", "| Name | Meaning |", "|---|---|"])
        for row in data[key]:
            lines.append(f"| {cell(row['name'])} | {cell(row['definition'])} |")
        lines.append("")

    lines.extend(
        [
            "## Synthetic examples",
            "",
            (
                "These fictional examples illustrate the rules. They are neither "
                "evidence for the current paper nor approved precedents."
            ),
            "",
        ]
    )
    for example in data["examples"]:
        lines.extend(
            [
                f"### {example['id']}",
                "",
                f"**Input:** {example['input']}",
                "",
                f"**Expected:** {example['expected']}",
                "",
                f"See rules: {', '.join(example['rules'])}.",
                "",
            ]
        )

    lines.extend(["## Ambiguity policy", "", data["ambiguity_policies"][policy], ""])
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=SOURCE)
    parser.add_argument("--output", type=Path, default=OUTPUT)
    parser.add_argument("--policy", choices=("expert_assisted", "automatic"))
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args(argv)
    try:
        source, output = args.source.resolve(), args.output.resolve()
        if source == output:
            raise ValueError("阅读版输出不能覆盖规则来源")
        data = read_source(source)
        outputs = [(output, args.policy or "expert_assisted")]
        if args.policy is None and output == OUTPUT:
            outputs.append((AUTOMATIC_OUTPUT, "automatic"))
        for destination, policy in outputs:
            rendered = render(data, policy)
            if args.check:
                if (
                    not destination.exists()
                    or destination.read_text(encoding="utf-8") != rendered
                ):
                    print(f"阅读版缺失或已过期：{destination}", file=sys.stderr)
                    return 1
            else:
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_text(rendered, encoding="utf-8")
                print(f"已生成 {destination}")
        if args.check:
            print("规则材料检查通过，英文阅读版与来源一致。")
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(f"无法生成文献提取规则阅读版：{exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

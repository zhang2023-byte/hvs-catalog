"""Generate readable English rules from their source; --check reads only."""

from __future__ import annotations

import argparse
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "src/hvs_catalog/resources/science/rules.toml"
OUTPUT = ROOT / "docs/science-rules.md"
QUANTITY_TABLE = "{{quantity_table}}"


def check_text(row: dict, fields: tuple[str, ...]) -> None:
    for field in fields:
        if not isinstance(row[field], str) or not row[field].strip():
            raise ValueError(f"{field} 必须是非空文本")


def read_source(path: Path) -> dict:
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    if data["format_version"] != 4:
        raise ValueError("不支持的规则材料格式版本")
    if data["instruction_language"] != "en":
        raise ValueError("提取指令必须使用英文")
    check_text(data, ("title", "version"))
    if data["review_status"] not in {"draft", "approved"}:
        raise ValueError("review_status 必须是 draft 或 approved")

    for row in data["rules"]:
        check_text(row, ("id", "title", "text"))
    if [row["id"] for row in data["rules"]] != ["S01", "S02", "S03", "S04"]:
        raise ValueError("科学规则必须按顺序包含 S01 至 S04")
    for row in data["rules"]:
        expected = 1 if row["id"] == "S03" else 0
        if row["text"].count(QUANTITY_TABLE) != expected:
            raise ValueError("物理量表必须且只能在第三条规则中出现一次")

    quantity_ids = []
    for row in data["quantities"]:
        check_text(row, ("definition", "common_units"))
        ids = row["ids"]
        if (
            not isinstance(ids, list)
            or not ids
            or any(not isinstance(qid, str) or not qid.strip() for qid in ids)
        ):
            raise ValueError("每行物理量定义必须包含非空编号列表")
        quantity_ids.extend(ids)
    if len(quantity_ids) != 11 or len(set(quantity_ids)) != 11:
        raise ValueError("当前审定范围要求 11 个不同的物理量编号")
    return data


def cell(value: str) -> str:
    return value.replace("|", r"\|").replace("\n", "<br>")


def render(data: dict) -> str:
    table = [
        "| Quantity | Definition | Common units |",
        "|---|---|---|",
    ]
    for row in data["quantities"]:
        quantities = ", ".join(f"`{cell(qid)}`" for qid in row["ids"])
        table.append(
            f"| {quantities} | {cell(row['definition'])} | {cell(row['common_units'])} |"
        )
    quantity_table = "\n".join(table)
    lines = ["<!-- Generated -->", f"# {data['title']}", ""]
    for index, rule in enumerate(data["rules"], start=1):
        lines.extend(
            [
                f"## {index}. {rule['title']}",
                "",
                rule["text"].strip().replace(QUANTITY_TABLE, quantity_table),
                "",
            ]
        )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=SOURCE)
    parser.add_argument("--output", type=Path, default=OUTPUT)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args(argv)
    try:
        source, output = args.source.resolve(), args.output.resolve()
        if source == output:
            raise ValueError("阅读版输出不能覆盖规则来源")
        data = read_source(source)
        for destination in [output]:
            rendered = render(data)
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

"""从共同数据定义生成格式说明、JSON Schema 与填写模板；--check 仅检查，不修改文件。"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "contracts/generated"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=OUTPUT)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args(argv)
    try:
        from hvs_catalog.records.generate import check_outputs, write_outputs

        output = args.output.resolve()
        if args.check:
            problems = check_outputs(output)
            if problems:
                for problem in problems:
                    print(problem, file=sys.stderr)
                return 1
            print("生成说明检查通过，产物与共同定义一致。")
        else:
            outputs = write_outputs(output)
            print(f"已生成 {len(outputs)} 个文件到 {output}")
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(f"无法生成数据格式说明：{exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

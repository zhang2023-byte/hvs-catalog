"""从共同数据定义生成格式说明、JSON Schema 与填写模板；--check 仅检查，不修改文件。

同时渲染记录契约（``hvs_catalog.records``）与 Gold 格式契约
（``hvs_catalog.evaluation``，输出在 ``gold/`` 子树）。
"""

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
        from hvs_catalog.evaluation.generate import (
            GOLD_OUTPUT_DIRNAME,
        )
        from hvs_catalog.evaluation.generate import (
            check_outputs as check_gold_outputs,
        )
        from hvs_catalog.evaluation.generate import (
            write_outputs as write_gold_outputs,
        )
        from hvs_catalog.records.generate import (
            check_outputs,
            write_outputs,
        )

        output = args.output.resolve()
        if args.check:
            problems = [
                *check_outputs(output),
                *check_gold_outputs(output / GOLD_OUTPUT_DIRNAME),
            ]
            if problems:
                for problem in problems:
                    print(problem, file=sys.stderr)
                return 1
            print("生成说明检查通过，产物与共同定义一致。")
        else:
            outputs = write_outputs(output)
            gold_outputs = write_gold_outputs(output / GOLD_OUTPUT_DIRNAME)
            print(f"已生成 {len(outputs)} 个文件到 {output}")
            print(
                f"已生成 {len(gold_outputs)} 个 Gold 格式文件到 "
                f"{output / GOLD_OUTPUT_DIRNAME}"
            )
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(f"无法生成数据格式说明：{exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

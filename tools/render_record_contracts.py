"""Generate documentation, schemas, and templates from shared definitions; --check reads
only.

Render current record, Gold and scoring contracts.
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
            check_gold_outputs,
            write_gold_outputs,
        )
        from hvs_catalog.evaluation.generate_scoring import (
            check_scoring_outputs,
            write_scoring_outputs,
        )
        from hvs_catalog.records.generate import (
            check_outputs,
            write_outputs,
        )

        output = args.output.resolve()
        if args.check:
            problems = [
                *check_outputs(output),
                *check_scoring_outputs(output / "scoring"),
                *check_gold_outputs(output / "gold"),
            ]
            if problems:
                for problem in problems:
                    print(problem, file=sys.stderr)
                return 1
            print("生成说明检查通过，产物与共同定义一致。")
        else:
            outputs = write_outputs(output)
            outputs.update(
                {
                    "scoring/" + name: text
                    for name, text in write_scoring_outputs(output / "scoring").items()
                }
            )
            outputs.update(
                {
                    "gold/" + name: text
                    for name, text in write_gold_outputs(output / "gold").items()
                }
            )
            print(f"已生成 {len(outputs)} 个文件到 {output}")

    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(f"无法生成数据格式说明：{exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

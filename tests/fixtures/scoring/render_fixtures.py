"""Deterministically regenerate frozen JSON inputs from samples.py.

Usage: ``uv run --offline python tests/fixtures/scoring/render_fixtures.py``. Filenames
follow ``samples.SAMPLE_FILES``. Expected results are maintained manually in
``expected/expected.json`` from the initial specification, not generated here.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from samples import SAMPLE_FILES, all_bundles, bundle_json


def main() -> int:
    bundles = all_bundles()
    if set(SAMPLE_FILES) != set(bundles):
        raise SystemExit(
            "SAMPLE_FILES 与 all_bundles 不一致："
            f"多余 {sorted(set(SAMPLE_FILES) - set(bundles))}，"
            f"缺失 {sorted(set(bundles) - set(SAMPLE_FILES))}"
        )
    inputs = HERE / "inputs"
    inputs.mkdir(exist_ok=True)
    for sample_id, bundle in sorted(bundles.items()):
        path = inputs / SAMPLE_FILES[sample_id]
        path.write_text(
            json.dumps(
                bundle_json(bundle), ensure_ascii=False, indent=2, sort_keys=True
            )
            + "\n",
            encoding="utf-8",
        )
        print(f"wrote {path.relative_to(HERE)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

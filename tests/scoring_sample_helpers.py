"""评分固定样例与回归测试共用的加载与构造帮助。

- 冻结样例：``tests/fixtures/scoring/inputs/`` 下按职责命名的 JSON 输入
  与 ``expected/expected.json`` 的人工手算预期；本模块按文件内
  ``sample_id`` 建立索引，测试不依赖文件名解析样例编号。
- 回归构造：单对象单物理量的最小整批输入、证据片段与评分入口，
  供评分回归测试复用，避免测试模块之间互相导入。

全部材料虚构；预期结果不得由评分程序反算生成。
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from hvs_catalog.evaluation.scoring import (
    AuxiliaryScoringConfig,
    EvidenceFragment,
    GoldObjectEntry,
    GoldPaperEntry,
    GoldQuantityEntry,
    GoldValueEntry,
    PredictionObjectEntry,
    PredictionPaperEntry,
    PredictionQuantityEntry,
    PredictionValueEntry,
    ScoringPlan,
    build_measured_value,
    gold_input_digest,
    protocol_only_config,
    score_batch,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "scoring"
INPUTS = FIXTURES / "inputs"
EXPECTED = json.loads((FIXTURES / "expected" / "expected.json").read_text("utf-8"))

# 输入文件按职责命名；样例编号以文件内 sample_id 为准。
_BUNDLE_FILES = {
    json.loads(path.read_text("utf-8"))["sample_id"]: path
    for path in sorted(INPUTS.glob("*.json"))
}
SAMPLE_IDS = sorted(_BUNDLE_FILES)


def sample_path(sample_id: str) -> Path:
    return _BUNDLE_FILES[sample_id]


def load_bundle(sample_id: str):
    data = json.loads(sample_path(sample_id).read_text("utf-8"))
    return (
        ScoringPlan.model_validate(data["plan"]),
        [GoldPaperEntry.model_validate(entry) for entry in data["gold"]],
        [PredictionPaperEntry.model_validate(entry) for entry in data["predictions"]],
        AuxiliaryScoringConfig.model_validate(data["auxiliary"]),
    )


def score_sample(sample_id: str):
    plan, gold, predictions, auxiliary = load_bundle(sample_id)
    return score_batch(plan, gold, predictions, auxiliary=auxiliary)


# ---- 预期结果（分数串）与报告行的比较工具 ----


def parse_rate(value):
    """把预期分数串（"1/3"、"0"、"1"、"NA"、null）解析为可比形式。"""
    if value is None or value == "NA":
        return None
    if "/" in value:
        numerator, denominator = value.split("/", 1)
        return pytest.approx(int(numerator) / int(denominator))
    return pytest.approx(float(value))


def rows_of(paper):
    return sorted(
        (
            row.layer,
            row.status,
            row.gold_ref or "",
            row.prediction_ref or "",
            ",".join(row.fields),
        )
        for row in paper.pairings
    )


def expected_rows(expected_paper):
    return sorted(
        (layer, status, gold_ref or "", prediction_ref or "", fields)
        for layer, status, gold_ref, prediction_ref, fields in expected_paper[
            "pairings"
        ]
    )


def check_l0(actual, expected):
    assert actual.planned == expected["planned"]
    assert actual.delivered == expected["delivered"]
    assert actual.format_valid == expected["format_valid"]
    assert actual.completion_counts == expected["completion"]
    assert actual.delivered_over_planned == parse_rate(
        expected["delivered_over_planned"]
    )
    assert actual.valid_over_delivered == parse_rate(expected["valid_over_delivered"])
    assert actual.valid_over_planned == parse_rate(expected["valid_over_planned"])


def check_metric(actual, expected, count_keys):
    if actual is None or expected is None:
        assert actual is None and expected is None
        return
    for key in count_keys:
        assert getattr(actual, key) == expected[key], key
    for rate in ("precision", "recall", "f1"):
        assert getattr(actual, rate) == parse_rate(expected[rate]), rate


# ---- 回归测试共用的合成构造（单对象单物理量） ----

DIST = "observed_phase_space.distance"
RV = "observed_phase_space.radial_velocity"
STD = "as reported in the paper"
HELI = "heliocentric"
M1 = "under Galactic potential M1"
M2 = "under Galactic potential M2"
M1_REWRITE = "under Galactic potential M1 (same adopted model)"
MATERIALS_NAME = "synthetic-materials"
MATERIALS_FP = hashlib.sha256(b"synthetic-materials").hexdigest()

#: 回归构造共用的虚构论文键（与固定样例的论文键互不冲突）。
REGRESSION_PAPER_KEY = "synthetic-regression"


def make_value(value_id, **kwargs):
    kwargs.setdefault("condition", STD)
    kwargs.setdefault("representation", HELI)
    return build_measured_value(value_id=value_id, **kwargs)


def make_fragment(fragment_id, text, supports):
    return EvidenceFragment(
        fragment_id=fragment_id,
        text=text,
        location=f"synthetic-paper.tex#{fragment_id}",
        material_name=MATERIALS_NAME,
        material_fingerprint=MATERIALS_FP,
        supports_fields=list(supports),
    )


def build_single_quantity_bundle(gold_values, pred_values):
    """单对象单物理量的最小整批输入；gold_values/pred_values 是
    (record_id, 条件, MeasuredValue 参数, 片段列表) 元组。"""

    def gold_entries():
        return [
            GoldValueEntry(
                record_id=record_id,
                value=make_value(record_id, condition=condition, **params),
                evidence_fragments=list(fragments),
            )
            for record_id, condition, params, fragments in gold_values
        ]

    def pred_entries():
        return [
            PredictionValueEntry(
                record_id=record_id,
                value=make_value(record_id, condition=condition, **params),
                evidence_fragments=list(fragments),
            )
            for record_id, condition, params, fragments in pred_values
        ]

    gold = [
        GoldPaperEntry(
            paper_key=REGRESSION_PAPER_KEY,
            coverage="complete",
            objects=[
                GoldObjectEntry(
                    label="A",
                    identifiers=["SYN-A"],
                    quantities=[
                        GoldQuantityEntry(quantity_path=DIST, values=gold_entries())
                    ],
                )
            ],
        )
    ]
    predictions = [
        PredictionPaperEntry(
            paper_key=REGRESSION_PAPER_KEY,
            delivered=True,
            format_valid=True,
            completion="complete",
            objects=[
                PredictionObjectEntry(
                    paper_key=REGRESSION_PAPER_KEY,
                    label="A",
                    identifiers=["SYN-A"],
                    quantities=[
                        PredictionQuantityEntry(
                            quantity_path=DIST, values=pred_entries()
                        )
                    ],
                )
            ],
        )
    ]
    plan = load_bundle("synthetic-s06b8")[0]
    plan = plan.model_copy(
        update={
            "paper_keys": [REGRESSION_PAPER_KEY],
            "scopes": [
                plan.scopes[0].model_copy(
                    update={
                        "paper_key": REGRESSION_PAPER_KEY,
                        "quantity_paths": [DIST],
                    }
                )
            ],
            "gold_ref": plan.gold_ref.model_copy(
                update={"fingerprint": gold_input_digest(gold)}
            ),
            "materials_ref": plan.materials_ref.model_copy(
                update={"name": MATERIALS_NAME, "fingerprint": MATERIALS_FP}
            ),
        }
    )
    return plan, gold, predictions


def score_with_protocol_only(plan, gold, preds, **kwargs):
    return score_batch(plan, gold, preds, auxiliary=protocol_only_config(), **kwargs)

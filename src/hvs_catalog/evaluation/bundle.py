"""评分演示的输入集保存、指纹核对与离线重评（评分集成）。

``save_scoring_bundle`` 把一次评分的全部纯输入（计划、Gold、预测与
辅助配置）连同重算摘要写成单个规范 JSON 文件；摘要绑定**完整计划**
（``plan_digest``：覆盖范围、程序/规则/材料/Gold/辅助引用、版本及
全部计分与溯源字段）以及 Gold、预测与辅助配置。``load_scoring_
bundle`` 在读取时按同一规范化规则重算全部摘要并与文件内记录核对，
任一内容被改动即拒绝（变更检测），正常往返幂等。
``score_scoring_bundle`` 用核对后的输入离线重评——同一文件在任何新
进程中给出相同摘要的评分结果，不需要重新提取。

文件只包含合成演示输入；真实 Gold 与含答案材料的保存、传输仍按
获准私有评分空间执行，本模块不提供真实仓扫描。
"""

from __future__ import annotations

import json
from pathlib import Path

from ..records.common import sha256_of
from .scoring import (
    AuxiliaryScoringConfig,
    GoldPaperEntry,
    PredictionPaperEntry,
    ScoringPlan,
    ScoringReport,
    auxiliary_config_digest,
    gold_input_digest,
    predictions_input_digest,
    score_batch,
)

__all__ = [
    "BUNDLE_SCHEMA_NAME",
    "BUNDLE_SCHEMA_VERSION",
    "ScoringBundleError",
    "load_scoring_bundle",
    "plan_digest",
    "save_scoring_bundle",
    "score_scoring_bundle",
]

BUNDLE_SCHEMA_NAME = "hvs.evaluation.scoring-bundle"
BUNDLE_SCHEMA_VERSION = 1


class ScoringBundleError(ValueError):
    """输入集缺失、结构不符或指纹核对失败。"""


def plan_digest(plan: ScoringPlan) -> str:
    """完整评分计划的规范摘要：覆盖范围、程序/规则/材料/Gold/辅助引用、
    版本及全部计分与溯源字段。

    摘要是完整性检测（保存与加载用同一规范化规则重算比对），不是
    密码学身份认证，不要求防御可重签名的恶意作者。
    """
    return sha256_of(plan.model_dump(mode="json"))


def _digests(plan: ScoringPlan, gold, predictions, auxiliary) -> dict[str, str]:
    return {
        "plan": plan_digest(plan),
        "gold": gold_input_digest(gold),
        "predictions": predictions_input_digest(predictions),
        "auxiliary": auxiliary_config_digest(auxiliary),
    }


def save_scoring_bundle(
    path: str | Path,
    *,
    plan: ScoringPlan,
    gold: list[GoldPaperEntry],
    predictions: list[PredictionPaperEntry],
    auxiliary: AuxiliaryScoringConfig,
) -> dict:
    """把评分输入集与摘要写成规范 JSON；返回写入的摘要。"""
    digests = _digests(plan, gold, predictions, auxiliary)
    payload = {
        "schema": {"name": BUNDLE_SCHEMA_NAME, "version": BUNDLE_SCHEMA_VERSION},
        "digests": digests,
        "plan": plan.model_dump(mode="json"),
        "gold": [entry.model_dump(mode="json") for entry in gold],
        "predictions": [entry.model_dump(mode="json") for entry in predictions],
        "auxiliary": auxiliary.model_dump(mode="json"),
    }
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return digests


def _validate_schema(payload: dict) -> None:
    schema = payload.get("schema")
    if not isinstance(schema, dict) or schema.get("name") != BUNDLE_SCHEMA_NAME:
        raise ScoringBundleError(
            f"不是评分输入集文件（schema.name 应为 {BUNDLE_SCHEMA_NAME}）"
        )
    if schema.get("version") != BUNDLE_SCHEMA_VERSION:
        raise ScoringBundleError(f"不支持的评分输入集版本：{schema.get('version')!r}")


def load_scoring_bundle(
    path: str | Path,
) -> tuple[ScoringPlan, list, list, AuxiliaryScoringConfig]:
    """读取并核对评分输入集；内容被改动时拒绝（变更检测）。"""
    target = Path(path)
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ScoringBundleError(f"无法读取评分输入集 {target}：{exc}") from exc
    if not isinstance(payload, dict):
        raise ScoringBundleError("评分输入集必须是 JSON 对象")
    _validate_schema(payload)
    recorded = payload.get("digests")
    if not isinstance(recorded, dict):
        raise ScoringBundleError("评分输入集缺少摘要记录")
    try:
        plan = ScoringPlan.model_validate(payload["plan"])
        gold = [GoldPaperEntry.model_validate(item) for item in payload.get("gold", [])]
        predictions = [
            PredictionPaperEntry.model_validate(item)
            for item in payload.get("predictions", [])
        ]
        auxiliary = AuxiliaryScoringConfig.model_validate(payload["auxiliary"])
    except KeyError as exc:
        raise ScoringBundleError(f"评分输入集缺少字段：{exc.args[0]}") from exc
    except ValueError as exc:
        raise ScoringBundleError(f"评分输入集内容不合法：{exc}") from exc
    recomputed = _digests(plan, gold, predictions, auxiliary)
    mismatched = sorted(
        name for name in recomputed if recomputed[name] != recorded.get(name)
    )
    if mismatched:
        raise ScoringBundleError(
            "评分输入集与保存时的摘要不符（"
            + "、".join(mismatched)
            + "）；输入在保存后被改动，拒绝按原指纹重评"
        )
    return plan, gold, predictions, auxiliary


def score_scoring_bundle(path: str | Path) -> ScoringReport:
    """核对输入集指纹并离线重评；同一文件在任何进程得到相同摘要。"""
    plan, gold, predictions, auxiliary = load_scoring_bundle(path)
    return score_batch(plan, gold, predictions, auxiliary=auxiliary)

"""Freeze explicit scoring inputs, verify all fingerprints, and replay offline.

Bundles retain the complete scope, Gold/prediction records, comparison mode, program,
rules, materials, and optional saved-run snapshot. Real Gold and answer-bearing bundles
belong in authorized private scoring spaces. No extraction or repository scanning
occurs during replay.
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
    """Missing, malformed, or fingerprint-invalid scoring input bundle."""


def plan_digest(plan: ScoringPlan) -> str:
    """Canonical complete-plan digest covering scope, program/rules/material/Gold/auxiliary
    references, versions, scoring, and provenance.

    Recomputing at save/load detects changes; this is integrity detection, not
    cryptographic identity authentication, and need not defend against malicious authors
    who can resign contents.
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
    """Write scoring inputs and digests as canonical JSON; return the written digests."""
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
    """Load and verify scoring inputs, rejecting changed contents."""
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
    for field in ("gold", "predictions"):
        if field not in payload:
            raise ScoringBundleError(f"评分输入集缺少字段：{field}")
        if not isinstance(payload[field], list):
            raise ScoringBundleError(f"评分输入集字段 {field} 必须是 JSON 列表")
    try:
        plan = ScoringPlan.model_validate(payload["plan"])
        gold = [GoldPaperEntry.model_validate(item) for item in payload["gold"]]
        predictions = [
            PredictionPaperEntry.model_validate(item) for item in payload["predictions"]
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
    """Verify bundle fingerprints and rescore offline with identical digests across
    processes.
    """
    plan, gold, predictions, auxiliary = load_scoring_bundle(path)
    if plan.comparison_mode == "numeric" and plan.program_ref.name == "hvs-catalog":
        from .saved import program_reference

        if plan.program_ref != program_reference():
            raise ScoringBundleError(
                "冻结输入绑定的程序与当前安装不符；请使用原程序重评或明确生成新快照"
            )
    return score_batch(plan, gold, predictions, auxiliary=auxiliary)

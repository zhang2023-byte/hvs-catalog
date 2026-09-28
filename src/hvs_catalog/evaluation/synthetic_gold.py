"""评分与实验用的合成 Gold 及评分计划引用（全部虚构材料）。

合成条目按 ``pipeline`` 模拟材料的固定内容在评分侧独立书写：提取侧
不读取本模块，Gold 内容不回灌提取或专家决定。对象名、数值、条件与
单位和模拟材料逐字一致（同一条虚构原文的两份转录），仅用于验证适
配、评分与两组对照报告的离线链路；不是真实论文、真实 Gold 或正式
评分结果。
"""

from __future__ import annotations

from ..pipeline import PAPER_A, PAPER_B, PAPER_C
from ..pipeline.mock import C_LINES
from ..records.common import sha256_of
from ..synthetic import material as m
from .scoring import (
    GoldObjectEntry,
    GoldPaperEntry,
    GoldQuantityEntry,
    GoldValueEntry,
    InputReference,
    PaperEvaluationScope,
    ScoringPlan,
    auxiliary_config_digest,
    build_measured_value,
    gold_input_digest,
    protocol_only_config,
)

__all__ = [
    "RV_QUANTITY",
    "build_experiment_plan",
    "synthetic_gold_entries",
    "synthetic_gold_for_paper",
]

RV_QUANTITY = "observed_phase_space.radial_velocity"

_SYNTHETIC_RULES_SEED = "synthetic-scoring-science-rules"
_SYNTHETIC_SCHEME_SEED = "synthetic-scoring-scheme"
_SYNTHETIC_PROGRAM = {"program": "hvs_catalog.evaluation.scoring.score_batch"}


def _fp(seed: str) -> str:
    return sha256_of(seed)


def _materials_fingerprint(paper_key: str) -> str:
    """合成材料指纹：来源包正文逐字节的哈希。"""
    if paper_key == PAPER_C:
        text = "\n".join(C_LINES) + "\n"
    else:
        letter = {PAPER_A: "A", PAPER_B: "B"}[paper_key]
        text = m.tex_bytes(letter).decode("utf-8")
    return sha256_of(text)


def _value(record_id: str, value: str, error: str, condition: str) -> GoldValueEntry:
    return GoldValueEntry(
        record_id=record_id,
        value=build_measured_value(
            value_id=record_id,
            value=value,
            error=error,
            unit="km/s",
            condition=condition,
            frame="heliocentric",
            purpose="measurement",
            source="this_paper",
        ),
    )


_SYNTHETIC_GOLD: dict[str, GoldPaperEntry] = {
    PAPER_A: GoldPaperEntry(
        paper_key=PAPER_A,
        coverage="complete",
        objects=[
            GoldObjectEntry(
                label="contribution-a-1",
                identifiers=[m.OBJECT_NAMES["A"]],
                contribution_type="follow_up",
                author_judgment=None,
                quantities=[
                    GoldQuantityEntry(
                        quantity_path=RV_QUANTITY,
                        values=[
                            _value(
                                "gold-rv-a-1",
                                "520",
                                "10",
                                "New spectrum; heliocentric frame.",
                            )
                        ],
                    )
                ],
            )
        ],
    ),
    PAPER_B: GoldPaperEntry(
        paper_key=PAPER_B,
        coverage="complete",
        objects=[
            GoldObjectEntry(
                label="contribution-b-1",
                identifiers=[m.OBJECT_NAMES["B"]],
                contribution_type="follow_up",
                author_judgment="unbound",
                quantities=[
                    GoldQuantityEntry(
                        quantity_path=RV_QUANTITY,
                        values=[
                            _value(
                                "gold-rv-b-1",
                                "480",
                                "15",
                                "New spectrum; heliocentric frame.",
                            )
                        ],
                    )
                ],
            )
        ],
    ),
    PAPER_C: GoldPaperEntry(
        paper_key=PAPER_C,
        coverage="complete",
        objects=[
            GoldObjectEntry(
                label="contribution-c-1",
                identifiers=["FIC-C-1"],
                contribution_type="candidates_found",
                author_judgment="unbound",
                quantities=[
                    GoldQuantityEntry(
                        quantity_path=RV_QUANTITY,
                        values=[
                            _value(
                                "gold-rv-c-1",
                                "430",
                                "12",
                                "Survey cross-match; heliocentric frame.",
                            )
                        ],
                    )
                ],
            ),
            GoldObjectEntry(
                label="contribution-c-3",
                identifiers=["FIC-C-3"],
                contribution_type="candidates_found",
                author_judgment="no_overall_conclusion",
                quantities=[
                    GoldQuantityEntry(
                        quantity_path=RV_QUANTITY,
                        values=[
                            _value(
                                "gold-rv-c-3",
                                "405",
                                "9",
                                "Provisional solution; heliocentric frame.",
                            )
                        ],
                    )
                ],
            ),
        ],
    ),
}


def synthetic_gold_entries() -> list[GoldPaperEntry]:
    """全部合成 Gold 条目（A/B/C 三篇，全部虚构）。"""
    return [
        _SYNTHETIC_GOLD[PAPER_A],
        _SYNTHETIC_GOLD[PAPER_B],
        _SYNTHETIC_GOLD[PAPER_C],
    ]


def synthetic_gold_for_paper(paper_key: str) -> GoldPaperEntry:
    try:
        return _SYNTHETIC_GOLD[paper_key]
    except KeyError:
        raise ValueError(
            f"论文 {paper_key!r} 没有对应的合成 Gold（当前支持："
            + ", ".join(_SYNTHETIC_GOLD)
            + "）；真实 Gold 读取需要获准的私有评分空间"
        ) from None


def build_experiment_plan(
    paper_keys: list[str], gold: list[GoldPaperEntry]
) -> ScoringPlan:
    """按合成 Gold 构建评分计划；输入指纹来自实际内容重算。"""
    auxiliary = protocol_only_config()
    return ScoringPlan(
        paper_keys=paper_keys,
        scopes=[
            PaperEvaluationScope(paper_key=key, quantity_paths=[RV_QUANTITY])
            for key in paper_keys
        ],
        gold_ref=InputReference(
            name="synthetic-gold",
            version="synthetic-v1",
            fingerprint=gold_input_digest(gold),
        ),
        materials_ref=InputReference(
            name="synthetic-materials",
            version="fictional-v1",
            fingerprint=sha256_of(
                {key: _materials_fingerprint(key) for key in paper_keys}
            ),
        ),
        science_rules_ref=InputReference(
            name="science-rules",
            version="synthetic-v1",
            fingerprint=_fp(_SYNTHETIC_RULES_SEED),
        ),
        scoring_scheme_ref=InputReference(
            name="scoring-scheme",
            version="synthetic-v1",
            fingerprint=_fp(_SYNTHETIC_SCHEME_SEED),
        ),
        program_ref=InputReference(
            name="scoring-program",
            version="synthetic-v1",
            fingerprint=sha256_of(_SYNTHETIC_PROGRAM),
        ),
        auxiliary_ref=InputReference(
            name="auxiliary",
            version="synthetic-v1",
            fingerprint=auxiliary_config_digest(auxiliary),
        ),
    )

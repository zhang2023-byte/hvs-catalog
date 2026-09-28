"""评分固定手算样例：全部材料虚构（SYN-* 对象、synthetic-* 论文）。

每个样例冻结 plan/Gold/预测输入；预期结果在 ``expected/expected.json``
中手工维护（来自开工规格的手算数字），不得由评分程序反算生成。
``render_fixtures.py`` 用本模块确定性重生成 ``inputs/`` 下的 JSON。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from hvs_catalog.evaluation.scoring import (
    ConfirmedRange,
    EvidenceFragment,
    GoldObjectEntry,
    GoldPaperEntry,
    GoldQuantityEntry,
    GoldValueEntry,
    InputReference,
    PaperEvaluationScope,
    PredictionObjectEntry,
    PredictionPaperEntry,
    PredictionQuantityEntry,
    PredictionValueEntry,
    ScopedCoverage,
    ScoringPlan,
    auxiliary_config_digest,
    build_measured_value,
    gold_input_digest,
    protocol_only_config,
)

DIST = "observed_phase_space.distance"
RV = "observed_phase_space.radial_velocity"
TANG = "derived_kinematics.tangential_velocity"
VZ = "derived_kinematics.galactocentric_vz"
RA = "observed_phase_space.ra"
PLX = "observed_phase_space.parallax"

M1 = "under Galactic potential M1"
M2 = "under Galactic potential M2"
VAGUE = "adopting the Galactic potential model"
STD = "as reported in the paper"
HELI = "heliocentric"

DEFAULT_AUX = protocol_only_config()

#: 冻结输入文件名按样例职责命名（键为 ``sample_id``，与内容一一对应）。
#: 生成器据此写出 ``inputs/`` 下的文件；阅读时以文件内 ``sample_id``
#: 为准，不依赖文件名解析样例编号。
SAMPLE_FILES = {
    "synthetic-s01": "s01-partially-correct-extra-missed.json",
    "synthetic-s02": "s02-empty-predictions.json",
    "synthetic-s03": "s03-correct-empty.json",
    "synthetic-s03-failed": "s03-failed-variant.json",
    "synthetic-s04": "s04-whole-paper-failure.json",
    "synthetic-s05": "s05-partial-numeric-failure.json",
    "synthetic-s06a1": "s06a1-unit-typesetting-equivalent.json",
    "synthetic-s06a2": "s06a2-unicode-minus-equivalent.json",
    "synthetic-s06a3": "s06a3-trailing-zeros-exponent-equivalent.json",
    "synthetic-s06a4": "s06a4-hms-notation-equivalent.json",
    "synthetic-s06a5": "s06a5-condition-phrase-equivalent.json",
    "synthetic-s06b1": "s06b1-unit-converted-not-original.json",
    "synthetic-s06b2": "s06b2-value-within-error-not-correct.json",
    "synthetic-s06b3a": "s06b3a-error-omitted.json",
    "synthetic-s06b3b": "s06b3b-error-mistyped.json",
    "synthetic-s06b4": "s06b4-symmetric-error-rewritten.json",
    "synthetic-s06b5": "s06b5-confidence-vs-credible.json",
    "synthetic-s06b6": "s06b6-range-with-invented-center.json",
    "synthetic-s06b7": "s06b7-limit-inclusion-flip.json",
    "synthetic-s06b8": "s06b8-condition-swapped-values.json",
    "synthetic-s06c1": "s06c1-trailing-zeros-not-preserved.json",
    "synthetic-s07u1": "s07u1-unknown-macro-undecidable.json",
    "synthetic-s07u1e": "s07u1e-evidence-sufficient.json",
    "synthetic-s07u1es": "s07u1es-evidence-supports-error.json",
    "synthetic-s07u1ei": "s07u1ei-evidence-insufficient-for-error.json",
    "synthetic-s07u2": "s07u2-vague-condition-unpaired.json",
    "synthetic-s08": "s08-duplicate-identity-consumes-tp.json",
    "synthetic-s09": "s09-declared-scope-out-of-scope.json",
    "synthetic-s09-noscope": "s09-undeclared-partial-gold.json",
    "synthetic-s10": "s10-two-papers-one-failed.json",
}


def _fp(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _ref(name: str, seed: str, version: str = "v1") -> InputReference:
    return InputReference(name=name, version=version, fingerprint=_fp(seed))


def _plan(
    paper_keys: list[str],
    scope_paths: dict[str, list[str]],
    gold: list[GoldPaperEntry],
    *,
    scoped_coverages: list[ScopedCoverage] | None = None,
) -> ScoringPlan:
    return ScoringPlan(
        paper_keys=paper_keys,
        scopes=[
            PaperEvaluationScope(paper_key=key, quantity_paths=scope_paths[key])
            for key in paper_keys
        ],
        gold_ref=InputReference(
            name="synthetic-gold",
            version="v1",
            fingerprint=gold_input_digest(gold),
        ),
        materials_ref=_ref("synthetic-materials", "synthetic-materials"),
        science_rules_ref=_ref("science-rules", "science-rules"),
        scoring_scheme_ref=_ref("scoring-scheme", "scoring-scheme"),
        program_ref=_ref("scoring-program", "scoring-program"),
        auxiliary_ref=InputReference(
            name="auxiliary",
            version="v1",
            fingerprint=auxiliary_config_digest(DEFAULT_AUX),
        ),
        scoped_coverages=scoped_coverages or [],
    )


def _gold_value(record_id: str, **kwargs) -> GoldValueEntry:
    return GoldValueEntry(record_id=record_id, value=build_measured_value(**kwargs))


def _pred_value(record_id: str, **kwargs) -> PredictionValueEntry:
    return PredictionValueEntry(
        record_id=record_id, value=build_measured_value(**kwargs)
    )


def _gold_object(
    label: str, identifiers: list[str], groups: dict[str, list[GoldValueEntry]], **extra
) -> GoldObjectEntry:
    return GoldObjectEntry(
        label=label,
        identifiers=identifiers,
        quantities=[
            GoldQuantityEntry(quantity_path=path, values=values)
            for path, values in groups.items()
        ],
        **extra,
    )


def _pred_object(
    paper_key: str,
    label: str,
    identifiers: list[str],
    groups: dict[str, list[PredictionValueEntry]],
    **extra,
) -> PredictionObjectEntry:
    return PredictionObjectEntry(
        paper_key=paper_key,
        label=label,
        identifiers=identifiers,
        quantities=[
            PredictionQuantityEntry(quantity_path=path, values=values)
            for path, values in groups.items()
        ],
        **extra,
    )


def _delivered(
    paper_key: str,
    objects: list[PredictionObjectEntry],
    *,
    completion: str = "complete",
    errors: list[str] | None = None,
) -> PredictionPaperEntry:
    return PredictionPaperEntry(
        paper_key=paper_key,
        delivered=True,
        format_valid=True,
        completion=completion,
        errors=errors or [],
        objects=objects,
    )


def _failed(paper_key: str, errors: list[str]) -> PredictionPaperEntry:
    return PredictionPaperEntry(
        paper_key=paper_key,
        delivered=False,
        format_valid=False,
        completion="failed",
        errors=errors,
        objects=[],
    )


@dataclass
class SampleBundle:
    sample_id: str
    description: str
    plan: ScoringPlan
    gold: list[GoldPaperEntry]
    predictions: list[PredictionPaperEntry]


# ---------------------------------------------------------------------------
# S01–S05
# ---------------------------------------------------------------------------


def _s01_gold(paper_key: str) -> list[GoldPaperEntry]:
    return [
        GoldPaperEntry(
            paper_key=paper_key,
            coverage="complete",
            objects=[
                _gold_object(
                    "A",
                    ["SYN-A"],
                    {
                        DIST: [
                            _gold_value(
                                "g1",
                                value_id="g1",
                                value="8.2",
                                error="0.3",
                                unit="kpc",
                                condition=M1,
                                representation=HELI,
                            )
                        ],
                        RV: [
                            _gold_value(
                                "g2",
                                value_id="g2",
                                value="600",
                                error="20",
                                unit="km s⁻¹",
                                condition=M1,
                                representation=HELI,
                            )
                        ],
                    },
                    contribution_type="follow_up",
                ),
                _gold_object(
                    "B",
                    ["SYN-B"],
                    {
                        DIST: [
                            _gold_value(
                                "g3",
                                value_id="g3",
                                value="10",
                                unit="kpc",
                                condition=M1,
                                representation=HELI,
                            )
                        ]
                    },
                ),
            ],
        )
    ]


def _s01_bundle() -> SampleBundle:
    key = "synthetic-s01"
    gold = _s01_gold(key)
    predictions = [
        _delivered(
            key,
            [
                _pred_object(
                    key,
                    "A",
                    ["SYN-A"],
                    {
                        DIST: [
                            _pred_value(
                                "p1",
                                value_id="p1",
                                value="8.2",
                                error="0.3",
                                unit="kpc",
                                condition=M1,
                                representation=HELI,
                            )
                        ],
                        RV: [
                            _pred_value(
                                "p2",
                                value_id="p2",
                                value="610",
                                error="20",
                                unit="km s⁻¹",
                                condition=M1,
                                representation=HELI,
                            )
                        ],
                    },
                    contribution_type="follow_up",
                ),
                _pred_object(
                    key,
                    "C",
                    ["SYN-C"],
                    {
                        DIST: [
                            _pred_value(
                                "p3",
                                value_id="p3",
                                value="5",
                                unit="kpc",
                                condition=M1,
                                representation=HELI,
                            )
                        ]
                    },
                ),
            ],
        )
    ]
    return SampleBundle(
        sample_id=key,
        description="S01：A 部分正确、C 多报、B 遗漏，speed 抄错不算 within_gold_error",
        plan=_plan([key], {key: [DIST, RV]}, gold),
        gold=gold,
        predictions=predictions,
    )


def _s02_bundle() -> SampleBundle:
    key = "synthetic-s02"
    gold = _s01_gold(key)
    predictions = [_delivered(key, [])]
    return SampleBundle(
        sample_id=key,
        description="S02：成功但空预测；Gold 分母保留，不是正确空名单",
        plan=_plan([key], {key: [DIST, RV]}, gold),
        gold=gold,
        predictions=predictions,
    )


def _s03_bundle() -> SampleBundle:
    key = "synthetic-s03"
    gold = [GoldPaperEntry(paper_key=key, coverage="complete", objects=[])]
    predictions = [_delivered(key, [])]
    return SampleBundle(
        sample_id=key,
        description="S03：完整 Gold 与成功完整预测均空 → correct_empty",
        plan=_plan([key], {key: [DIST]}, gold),
        gold=gold,
        predictions=predictions,
    )


def _s03_failed_bundle() -> SampleBundle:
    key = "synthetic-s03-failed"
    gold = [GoldPaperEntry(paper_key=key, coverage="complete", objects=[])]
    predictions = [_failed(key, ["simulated whole-paper failure"])]
    return SampleBundle(
        sample_id=key,
        description="S03 失败变体：未交付不能置 correct_empty，失败信息保留",
        plan=_plan([key], {key: [DIST]}, gold),
        gold=gold,
        predictions=predictions,
    )


def _s04_bundle() -> SampleBundle:
    key = "synthetic-s04"
    gold = _s01_gold(key)
    predictions = [_failed(key, ["simulated whole-paper failure"])]
    return SampleBundle(
        sample_id=key,
        description="S04：整篇失败无交付，分母同 S02 空预测",
        plan=_plan([key], {key: [DIST, RV]}, gold),
        gold=gold,
        predictions=predictions,
    )


def _s05_bundle() -> SampleBundle:
    key = "synthetic-s05"
    gold = _s01_gold(key)
    predictions = [
        _delivered(
            key,
            [
                _pred_object(
                    key,
                    "A",
                    ["SYN-A"],
                    {
                        DIST: [
                            _pred_value(
                                "p1",
                                value_id="p1",
                                value="8.2",
                                error="0.3",
                                unit="kpc",
                                condition=M1,
                                representation=HELI,
                            )
                        ]
                    },
                ),
                _pred_object(key, "B", ["SYN-B"], {}),
            ],
            completion="partial",
            errors=["numeric extraction step failed after roster delivery"],
        )
    ]
    return SampleBundle(
        sample_id=key,
        description="S05：名单完整、数值步骤部分失败，保留有效名单与子结果",
        plan=_plan([key], {key: [DIST, RV]}, gold),
        gold=gold,
        predictions=predictions,
    )


# ---------------------------------------------------------------------------
# S06：A1–A5、B1–B7、C1、B8
# ---------------------------------------------------------------------------


def _single_case_bundle(
    sample_id: str,
    description: str,
    quantity: str,
    gold_kwargs: dict,
    pred_kwargs: dict,
) -> SampleBundle:
    gold = [
        GoldPaperEntry(
            paper_key=sample_id,
            coverage="complete",
            objects=[
                _gold_object(
                    "A",
                    ["SYN-A"],
                    {quantity: [_gold_value("g1", value_id="g1", **gold_kwargs)]},
                )
            ],
        )
    ]
    predictions = [
        _delivered(
            sample_id,
            [
                _pred_object(
                    sample_id,
                    "A",
                    ["SYN-A"],
                    {quantity: [_pred_value("p1", value_id="p1", **pred_kwargs)]},
                )
            ],
        )
    ]
    return SampleBundle(
        sample_id=sample_id,
        description=description,
        plan=_plan([sample_id], {sample_id: [quantity]}, gold),
        gold=gold,
        predictions=predictions,
    )


def _s06_bundles() -> list[SampleBundle]:
    common = {"condition": STD, "representation": HELI}
    return [
        _single_case_bundle(
            "synthetic-s06a1",
            "A1：单位排版差异等价",
            RV,
            {"value": "600", "error": "20", "unit": "km s⁻¹", **common},
            {
                "value": "600",
                "error": "20",
                "unit": "\\mathrm{km\\,s^{-1}}",
                **common,
            },
        ),
        _single_case_bundle(
            "synthetic-s06a2",
            "A2：Unicode 负号等价",
            VZ,
            {"value": "−8.2", "error": "0.3", "unit": "km s⁻¹", **common},
            {"value": "-8.2", "error": "0.3", "unit": "km s⁻¹", **common},
        ),
        _single_case_bundle(
            "synthetic-s06a3",
            "A3：系数尾零与指数排版等价",
            TANG,
            {"value": "1.20 × 10³", "error": "20", "unit": "km s⁻¹", **common},
            {
                "value": "1.20\\times10^{3}",
                "error": "20",
                "unit": "km s⁻¹",
                **common,
            },
        ),
        _single_case_bundle(
            "synthetic-s06a4",
            "A4：时分秒表达排版等价，不转换角度",
            RA,
            {"value": "12h34m56.7s", **common},
            {"value": "12^{h}34^{m}56.7^{s}", **common},
        ),
        _single_case_bundle(
            "synthetic-s06a5",
            "A5：M1 上下文条件白名单短语等价",
            DIST,
            {
                "value": "8.2",
                "error": "0.3",
                "unit": "kpc",
                "condition": M1,
                "representation": HELI,
            },
            {
                "value": "8.2",
                "error": "0.3",
                "unit": "kpc",
                "condition": "assuming potential M1",
                "representation": HELI,
            },
        ),
        _single_case_bundle(
            "synthetic-s06b1",
            "B1：单位换算后数值不同，未保留原报表达",
            DIST,
            {"value": "8.2", "error": "0.3", "unit": "kpc", **common},
            {"value": "8200", "error": "300", "unit": "pc", **common},
        ),
        _single_case_bundle(
            "synthetic-s06b2",
            "B2：中心值不同，落在误差内也不算正确",
            RV,
            {"value": "600", "error": "20", "unit": "km s⁻¹", **common},
            {"value": "610", "error": "20", "unit": "km s⁻¹", **common},
        ),
        _single_case_bundle(
            "synthetic-s06b3a",
            "B3 变体一：误差遗漏",
            DIST,
            {"value": "8.2", "error": "0.3", "unit": "kpc", **common},
            {"value": "8.2", "unit": "kpc", **common},
        ),
        _single_case_bundle(
            "synthetic-s06b3b",
            "B3 变体二：误差抄错",
            DIST,
            {"value": "8.2", "error": "0.3", "unit": "kpc", **common},
            {"value": "8.2", "error": "0.2", "unit": "kpc", **common},
        ),
        _single_case_bundle(
            "synthetic-s06b4",
            "B4：对称误差改写为非对称字段",
            DIST,
            {"value": "8.2", "error": "0.3", "unit": "kpc", **common},
            {
                "value": "8.2",
                "lower_error": "0.3",
                "upper_error": "0.3",
                "unit": "kpc",
                **common,
            },
        ),
        _single_case_bundle(
            "synthetic-s06b5",
            "B5：置信区间与可信区间解释不同",
            PLX,
            {
                "limit_kind": "range",
                "range_lower": "7",
                "range_upper": "9",
                "interval_kind": "confidence",
                "coverage": ("95", "%"),
                **common,
            },
            {
                "limit_kind": "range",
                "range_lower": "7",
                "range_upper": "9",
                "interval_kind": "credible",
                "coverage": ("95", "%"),
                **common,
            },
        ),
        _single_case_bundle(
            "synthetic-s06b6",
            "B6：纯区间被补中心值并改写表达",
            PLX,
            {
                "limit_kind": "range",
                "range_lower": "7",
                "range_upper": "9",
                "interval_kind": "unspecified",
                **common,
            },
            {"value": "8", "error": "1", "unit": "mas", **common},
        ),
        _single_case_bundle(
            "synthetic-s06b7",
            "B7：端点包含方式变化",
            RV,
            {
                "limit_kind": "upper_limit",
                "value": "600",
                "unit": "km s⁻¹",
                "upper_inclusive": False,
                **common,
            },
            {
                "limit_kind": "upper_limit",
                "value": "600",
                "unit": "km s⁻¹",
                "upper_inclusive": True,
                **common,
            },
        ),
        _single_case_bundle(
            "synthetic-s06c1",
            "C1：数值相等但原报尾零未保留",
            DIST,
            {"value": "8.20", "unit": "kpc", **common},
            {"value": "8.2", "unit": "kpc", **common},
        ),
    ]


def _s06_b8_bundle() -> SampleBundle:
    key = "synthetic-s06b8"
    gold = [
        GoldPaperEntry(
            paper_key=key,
            coverage="complete",
            objects=[
                _gold_object(
                    "A",
                    ["SYN-A"],
                    {
                        DIST: [
                            _gold_value(
                                "g1",
                                value_id="g1",
                                value="8",
                                unit="kpc",
                                condition=M1,
                                representation=HELI,
                            ),
                            _gold_value(
                                "g2",
                                value_id="g2",
                                value="10",
                                unit="kpc",
                                condition=M2,
                                representation=HELI,
                            ),
                        ]
                    },
                )
            ],
        )
    ]
    predictions = [
        _delivered(
            key,
            [
                _pred_object(
                    key,
                    "A",
                    ["SYN-A"],
                    {
                        DIST: [
                            _pred_value(
                                "p1",
                                value_id="p1",
                                value="10",
                                unit="kpc",
                                condition=M1,
                                representation=HELI,
                            ),
                            _pred_value(
                                "p2",
                                value_id="p2",
                                value="8",
                                unit="kpc",
                                condition=M2,
                                representation=HELI,
                            ),
                        ]
                    },
                )
            ],
        )
    ]
    return SampleBundle(
        sample_id=key,
        description="B8：M1/M2 两组值对调，按条件对应，不跨条件找相同数值",
        plan=_plan([key], {key: [DIST]}, gold),
        gold=gold,
        predictions=predictions,
    )


# ---------------------------------------------------------------------------
# S07–S10
# ---------------------------------------------------------------------------


def _s07_u1_bundle() -> SampleBundle:
    key = "synthetic-s07u1"
    common = {"error": "0.3", "unit": "kpc", "condition": STD, "representation": HELI}
    gold = [
        GoldPaperEntry(
            paper_key=key,
            coverage="complete",
            objects=[
                _gold_object(
                    "A",
                    ["SYN-A"],
                    {DIST: [_gold_value("g1", value_id="g1", value="8.2", **common)]},
                )
            ],
        )
    ]
    predictions = [
        _delivered(
            key,
            [
                _pred_object(
                    key,
                    "A",
                    ["SYN-A"],
                    {
                        DIST: [
                            _pred_value(
                                "p1",
                                value_id="p1",
                                value="\\mystery{8.2}",
                                **common,
                            )
                        ]
                    },
                )
            ],
        )
    ]
    return SampleBundle(
        sample_id=key,
        description="S07/U1：未知宏无法判定，确定 C=0，整批 provisional",
        plan=_plan([key], {key: [DIST]}, gold),
        gold=gold,
        predictions=predictions,
    )


def _s07_u1e_bundle() -> SampleBundle:
    key = "synthetic-s07u1e"
    common = {"error": "0.3", "unit": "kpc", "condition": STD, "representation": HELI}
    materials_fp = _fp("synthetic-materials")
    gold_fragment = EvidenceFragment(
        fragment_id="table-1",
        text="The distance of SYN-A is 8.2 kpc (Table 1).",
        location="synthetic-paper.tex#L12",
        material_name="synthetic-materials",
        material_fingerprint=materials_fp,
        supports_fields=["value"],
    )
    pred_fragment = EvidenceFragment(
        fragment_id="macro-def",
        text="\\newcommand{\\mystery}[1]{#1} % typesetting-only macro",
        location="synthetic-paper.tex#L7",
        material_name="synthetic-materials",
        material_fingerprint=materials_fp,
        supports_fields=["value"],
    )
    gold = [
        GoldPaperEntry(
            paper_key=key,
            coverage="complete",
            objects=[
                _gold_object(
                    "A",
                    ["SYN-A"],
                    {
                        DIST: [
                            GoldValueEntry(
                                record_id="g1",
                                value=build_measured_value(
                                    value_id="g1", value="8.2", **common
                                ),
                                evidence_fragments=[gold_fragment],
                            )
                        ]
                    },
                )
            ],
        )
    ]
    predictions = [
        _delivered(
            key,
            [
                _pred_object(
                    key,
                    "A",
                    ["SYN-A"],
                    {
                        DIST: [
                            PredictionValueEntry(
                                record_id="p1",
                                value=build_measured_value(
                                    value_id="p1", value="\\mystery{8.2}", **common
                                ),
                                evidence_fragments=[pred_fragment],
                            )
                        ]
                    },
                )
            ],
        )
    ]
    return SampleBundle(
        sample_id=key,
        description=(
            "S07/U1 证据充足变体：双方提供绑定材料、声明支持 value 字段的"
            "虚构原文片段，可供 mock 在协议内判定；无 Agent 时仍为未知、暂定"
        ),
        plan=_plan([key], {key: [DIST]}, gold),
        gold=gold,
        predictions=predictions,
    )


def _s07_u1_error_variant(
    sample_id: str, description: str, pred_supports: list[str], gold_supports: list[str]
) -> SampleBundle:
    """U1 的 error 字段变体：pred 侧 error 为未知宏，片段声明决定证据
    是否与当前待判字段（error）相关；value 片段不能支持 error 判定。"""
    key = sample_id
    common = {"unit": "kpc", "condition": STD, "representation": HELI}
    materials_fp = _fp("synthetic-materials")
    gold_fragment = EvidenceFragment(
        fragment_id="table-1",
        text="The distance error of SYN-A is 0.3 kpc (Table 1).",
        location="synthetic-paper.tex#L12",
        material_name="synthetic-materials",
        material_fingerprint=materials_fp,
        supports_fields=gold_supports,
    )
    pred_fragment = EvidenceFragment(
        fragment_id="macro-def",
        text="\\newcommand{\\mystery}[1]{#1} % typesetting-only macro",
        location="synthetic-paper.tex#L7",
        material_name="synthetic-materials",
        material_fingerprint=materials_fp,
        supports_fields=pred_supports,
    )
    gold = [
        GoldPaperEntry(
            paper_key=key,
            coverage="complete",
            objects=[
                _gold_object(
                    "A",
                    ["SYN-A"],
                    {
                        DIST: [
                            GoldValueEntry(
                                record_id="g1",
                                value=build_measured_value(
                                    value_id="g1",
                                    value="8.2",
                                    error="0.3",
                                    **common,
                                ),
                                evidence_fragments=[gold_fragment],
                            )
                        ]
                    },
                )
            ],
        )
    ]
    predictions = [
        _delivered(
            key,
            [
                _pred_object(
                    key,
                    "A",
                    ["SYN-A"],
                    {
                        DIST: [
                            PredictionValueEntry(
                                record_id="p1",
                                value=build_measured_value(
                                    value_id="p1",
                                    value="8.2",
                                    error="\\mystery{0.3}",
                                    **common,
                                ),
                                evidence_fragments=[pred_fragment],
                            )
                        ]
                    },
                )
            ],
        )
    ]
    return SampleBundle(
        sample_id=key,
        description=description,
        plan=_plan([key], {key: [DIST]}, gold),
        gold=gold,
        predictions=predictions,
    )


def _s07_u1es_bundle() -> SampleBundle:
    return _s07_u1_error_variant(
        "synthetic-s07u1es",
        "S07/U1 error 变体（证据充足）：双侧片段声明支持 error 字段，"
        "未知宏可交 mock 判定；无 Agent 时仍为未知、暂定",
        ["error"],
        ["error"],
    )


def _s07_u1ei_bundle() -> SampleBundle:
    return _s07_u1_error_variant(
        "synthetic-s07u1ei",
        "S07/U1 error 变体（证据不足）：双侧片段只声明支持 value 字段，"
        "不能支持 error 判定，保持未知、不调用 Agent",
        ["value"],
        ["value"],
    )


def _s07_u2_bundle() -> SampleBundle:
    key = "synthetic-s07u2"
    gold = [
        GoldPaperEntry(
            paper_key=key,
            coverage="complete",
            objects=[
                _gold_object(
                    "A",
                    ["SYN-A"],
                    {
                        DIST: [
                            _gold_value(
                                "g1",
                                value_id="g1",
                                value="8",
                                unit="kpc",
                                condition=M1,
                                representation=HELI,
                            ),
                            _gold_value(
                                "g2",
                                value_id="g2",
                                value="10",
                                unit="kpc",
                                condition=M2,
                                representation=HELI,
                            ),
                        ]
                    },
                )
            ],
        )
    ]
    predictions = [
        _delivered(
            key,
            [
                _pred_object(
                    key,
                    "A",
                    ["SYN-A"],
                    {
                        DIST: [
                            _pred_value(
                                "p1",
                                value_id="p1",
                                value="8",
                                unit="kpc",
                                condition=VAGUE,
                                representation=HELI,
                            )
                        ]
                    },
                )
            ],
        )
    ]
    return SampleBundle(
        sample_id=key,
        description="S07/U2：模糊势模型不按数值选 M1，未确认配对、provisional",
        plan=_plan([key], {key: [DIST]}, gold),
        gold=gold,
        predictions=predictions,
    )


def _s08_bundle() -> SampleBundle:
    key = "synthetic-s08"
    common = {
        "value": "5.0",
        "error": "0.2",
        "unit": "kpc",
        "condition": STD,
        "representation": HELI,
    }
    gold = [
        GoldPaperEntry(
            paper_key=key,
            coverage="complete",
            objects=[
                _gold_object(
                    "A",
                    ["SYN-A"],
                    {DIST: [_gold_value("g1", value_id="g1", **common)]},
                )
            ],
        )
    ]
    predictions = [
        _delivered(
            key,
            [
                _pred_object(
                    key,
                    "A",
                    ["SYN-A"],
                    {DIST: [_pred_value("p1", value_id="p1", **common)]},
                ),
                _pred_object(
                    key,
                    "A-dup",
                    ["SYN-A"],
                    {
                        DIST: [
                            _pred_value(
                                "p2",
                                value_id="p2",
                                value="9.9",
                                error="0.2",
                                unit="kpc",
                                condition=STD,
                                representation=HELI,
                            )
                        ]
                    },
                ),
            ],
        )
    ]
    return SampleBundle(
        sample_id=key,
        description="S08：同标识重复预测消耗一次 TP，其余 FP，不合并抬分",
        plan=_plan([key], {key: [DIST]}, gold),
        gold=gold,
        predictions=predictions,
    )


def _s09_gold(paper_key: str) -> list[GoldPaperEntry]:
    return [
        GoldPaperEntry(
            paper_key=paper_key,
            coverage="partial",
            confirmed_ranges=[
                ConfirmedRange(object_identifiers=["SYN-A"], quantity_paths=[DIST])
            ],
            objects=[
                _gold_object(
                    "A",
                    ["SYN-A"],
                    {
                        DIST: [
                            _gold_value(
                                "g1",
                                value_id="g1",
                                value="8.2",
                                error="0.3",
                                unit="kpc",
                                condition=M1,
                                representation=HELI,
                            )
                        ],
                        RV: [
                            _gold_value(
                                "g2",
                                value_id="g2",
                                value="600",
                                error="20",
                                unit="km s⁻¹",
                                condition=M1,
                                representation=HELI,
                            )
                        ],
                    },
                ),
                _gold_object(
                    "B",
                    ["SYN-B"],
                    {
                        DIST: [
                            _gold_value(
                                "g3",
                                value_id="g3",
                                value="10",
                                unit="kpc",
                                condition=M1,
                                representation=HELI,
                            )
                        ]
                    },
                ),
            ],
        )
    ]


def _s09_predictions(paper_key: str) -> list[PredictionPaperEntry]:
    return [
        _delivered(
            paper_key,
            [
                _pred_object(
                    paper_key,
                    "A",
                    ["SYN-A"],
                    {
                        DIST: [
                            _pred_value(
                                "p1",
                                value_id="p1",
                                value="8.2",
                                error="0.3",
                                unit="kpc",
                                condition=M1,
                                representation=HELI,
                            )
                        ],
                        RV: [
                            _pred_value(
                                "p2",
                                value_id="p2",
                                value="600",
                                error="20",
                                unit="km s⁻¹",
                                condition=M1,
                                representation=HELI,
                            )
                        ],
                    },
                ),
                _pred_object(
                    paper_key,
                    "B",
                    ["SYN-B"],
                    {
                        DIST: [
                            _pred_value(
                                "p3",
                                value_id="p3",
                                value="5",
                                unit="kpc",
                                condition=M1,
                                representation=HELI,
                            )
                        ]
                    },
                ),
            ],
        )
    ]


def _s09_bundle() -> SampleBundle:
    key = "synthetic-s09"
    gold = _s09_gold(key)
    return SampleBundle(
        sample_id=key,
        description="S09：预声明封闭子范围（A 的 distance），范围外值只记 out_of_scope",
        plan=_plan(
            [key],
            {key: [DIST, RV]},
            gold,
            scoped_coverages=[
                ScopedCoverage(
                    paper_key=key, object_identifiers=["SYN-A"], quantity_paths=[DIST]
                )
            ],
        ),
        gold=gold,
        predictions=_s09_predictions(key),
    )


def _s09_noscope_bundle() -> SampleBundle:
    key = "synthetic-s09-noscope"
    gold = _s09_gold(key)
    return SampleBundle(
        sample_id=key,
        description="S09 负例：部分覆盖 Gold 未预声明封闭范围 → readiness 缺口、无最终分",
        plan=_plan([key], {key: [DIST, RV]}, gold),
        gold=gold,
        predictions=_s09_predictions(key),
    )


def _s10_bundle() -> SampleBundle:
    key_a = "synthetic-s10a"
    key_b = "synthetic-s10b"
    common = {
        "value": "8.2",
        "error": "0.3",
        "unit": "kpc",
        "condition": STD,
        "representation": HELI,
    }
    gold = [
        GoldPaperEntry(
            paper_key=key_a,
            coverage="complete",
            objects=[
                _gold_object(
                    "A",
                    ["SYN-A"],
                    {DIST: [_gold_value("g1", value_id="g1", **common)]},
                )
            ],
        ),
        GoldPaperEntry(
            paper_key=key_b,
            coverage="complete",
            objects=[
                _gold_object(
                    "A",
                    ["SYN-A"],
                    {DIST: [_gold_value("g1", value_id="g1", **common)]},
                )
            ],
        ),
    ]
    predictions = [
        _delivered(
            key_a,
            [
                _pred_object(
                    key_a,
                    "A",
                    ["SYN-A"],
                    {DIST: [_pred_value("p1", value_id="p1", **common)]},
                )
            ],
        ),
        _failed(key_b, ["simulated whole-paper failure"]),
    ]
    return SampleBundle(
        sample_id="synthetic-s10",
        description="S10：两篇计划，第二篇未交付仍计入微汇总分母",
        plan=_plan([key_a, key_b], {key_a: [DIST], key_b: [DIST]}, gold),
        gold=gold,
        predictions=predictions,
    )


def all_bundles() -> dict[str, SampleBundle]:
    bundles = [
        _s01_bundle(),
        _s02_bundle(),
        _s03_bundle(),
        _s03_failed_bundle(),
        _s04_bundle(),
        _s05_bundle(),
        *_s06_bundles(),
        _s06_b8_bundle(),
        _s07_u1_bundle(),
        _s07_u1e_bundle(),
        _s07_u1es_bundle(),
        _s07_u1ei_bundle(),
        _s07_u2_bundle(),
        _s08_bundle(),
        _s09_bundle(),
        _s09_noscope_bundle(),
        _s10_bundle(),
    ]
    return {bundle.sample_id: bundle for bundle in bundles}


def bundle_json(bundle: SampleBundle) -> dict:
    return {
        "sample_id": bundle.sample_id,
        "description": bundle.description,
        "plan": bundle.plan.model_dump(mode="json"),
        "gold": [entry.model_dump(mode="json") for entry in bundle.gold],
        "predictions": [entry.model_dump(mode="json") for entry in bundle.predictions],
        "auxiliary": DEFAULT_AUX.model_dump(mode="json"),
    }

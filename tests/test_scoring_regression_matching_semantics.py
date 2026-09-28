"""评分回归：独立构造的反例复现，聚焦匹配语义与安全门禁（不复用冻结 expected）。

覆盖：ready+blocked 混合批次、别名歧义未决关系、白名单未命中不能
证不同、归属集合内最大一对一匹配（重排/多候选/饱和+额外未知）、
Agent 明确 different、原文片段证据与引用校验、冻结完整性与并发冲
突、production fail closed、证据独立诊断。全部合成材料。
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from hvs_catalog.evaluation.scoring import (
    AgentResponse,
    ConfirmedRange,
    GoldObjectEntry,
    GoldPaperEntry,
    GoldQuantityEntry,
    GoldValueEntry,
    InputReference,
    MockAgentAdapter,
    PaperEvaluationScope,
    PredictionObjectEntry,
    PredictionPaperEntry,
    PredictionQuantityEntry,
    PredictionValueEntry,
    ScoringPlan,
    auxiliary_config_digest,
    build_measured_value,
    gold_input_digest,
    protocol_only_config,
    score_batch,
)
from hvs_catalog.evaluation.scoring.models import EvidenceFragment

DIST = "observed_phase_space.distance"
RV = "observed_phase_space.radial_velocity"
STD = "as reported in the paper"
HELI = "heliocentric"
M1 = "under Galactic potential M1"
M2 = "under Galactic potential M2"
MATERIALS_NAME = "synthetic-materials"
MATERIALS_FP = hashlib.sha256(b"synthetic-materials").hexdigest()


def _value(value_id, **kwargs):
    kwargs.setdefault("condition", STD)
    kwargs.setdefault("representation", HELI)
    return build_measured_value(value_id=value_id, **kwargs)


def _fragment(fragment_id, text, supports=("value",)):
    return EvidenceFragment(
        fragment_id=fragment_id,
        text=text,
        location=f"synthetic-paper.tex#{fragment_id}",
        material_name=MATERIALS_NAME,
        material_fingerprint=MATERIALS_FP,
        supports_fields=list(supports),
    )


def _gold_obj(label, identifiers, groups, **extra):
    return GoldObjectEntry(
        label=label,
        identifiers=identifiers,
        quantities=[
            GoldQuantityEntry(quantity_path=path, values=values)
            for path, values in groups.items()
        ],
        **extra,
    )


def _pred_obj(paper_key, label, identifiers, groups, **extra):
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


def _delivered(paper_key, objects):
    return PredictionPaperEntry(
        paper_key=paper_key,
        delivered=True,
        format_valid=True,
        completion="complete",
        objects=objects,
    )


def _plan(paper_keys, scope_paths, gold, *, scoped=None, registry=None):
    plan = ScoringPlan(
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
        materials_ref=InputReference(
            name=MATERIALS_NAME, version="v1", fingerprint=MATERIALS_FP
        ),
        science_rules_ref=InputReference(
            name="science-rules",
            version="v1",
            fingerprint=hashlib.sha256(b"rules").hexdigest(),
        ),
        scoring_scheme_ref=InputReference(
            name="scoring-scheme",
            version="v1",
            fingerprint=hashlib.sha256(b"scheme").hexdigest(),
        ),
        program_ref=InputReference(
            name="scoring-program",
            version="v1",
            fingerprint=hashlib.sha256(b"program").hexdigest(),
        ),
        auxiliary_ref=InputReference(
            name="auxiliary",
            version="v1",
            fingerprint=auxiliary_config_digest(protocol_only_config()),
        ),
    )
    if scoped:
        plan = plan.model_copy(update={"scoped_coverages": scoped})
    if registry is not None:
        plan = plan.model_copy(update={"known_evidence_record_ids": registry})
    return plan


def _score(plan, gold, preds, **kwargs):
    return score_batch(plan, gold, preds, auxiliary=protocol_only_config(), **kwargs)


# ---------------------------------------------------------------------------
# 裁定1：ready + blocked 混合批次不得输出整批最终率
# ---------------------------------------------------------------------------


def _ready_blocked_bundle():
    key_ready = "synthetic-scoring-ready"
    key_blocked = "synthetic-scoring-blocked"
    common = {"value": "8.2", "error": "0.3", "unit": "kpc"}
    gold = [
        GoldPaperEntry(
            paper_key=key_ready,
            coverage="complete",
            objects=[
                _gold_obj(
                    "A",
                    ["SYN-A"],
                    {
                        DIST: [
                            GoldValueEntry(record_id="g1", value=_value("g1", **common))
                        ]
                    },
                )
            ],
        ),
        GoldPaperEntry(
            paper_key=key_blocked,
            coverage="partial",
            confirmed_ranges=[
                ConfirmedRange(object_identifiers=["SYN-Z"], quantity_paths=[DIST])
            ],
            objects=[_gold_obj("Z", ["SYN-Z"], {})],
        ),
    ]
    predictions = [
        _delivered(
            key_ready,
            [
                _pred_obj(
                    key_ready,
                    "A",
                    ["SYN-A"],
                    {
                        DIST: [
                            PredictionValueEntry(
                                record_id="p1", value=_value("p1", **common)
                            )
                        ]
                    },
                )
            ],
        ),
        _delivered(
            key_blocked,
            [
                _pred_obj(
                    key_blocked,
                    "Z",
                    ["SYN-Z"],
                    {
                        DIST: [
                            PredictionValueEntry(
                                record_id="p2", value=_value("p2", **common)
                            )
                        ]
                    },
                )
            ],
        ),
    ]
    plan = _plan(
        [key_ready, key_blocked], {key_ready: [DIST], key_blocked: [DIST]}, gold
    )
    return plan, gold, predictions


def test_ready_blocked_mix_is_not_final():
    plan, gold, predictions = _ready_blocked_bundle()
    report = _score(plan, gold, predictions)
    assert report.readiness_blocked_papers == ["synthetic-scoring-blocked"]
    assert report.batch_provisional is True
    assert "synthetic-scoring-blocked:readiness_gap" in report.provisional_reasons
    # 全计划 L0 保留
    assert report.micro.l0.planned == 2 and report.micro.l0.delivered == 2
    # 微汇总计数保留但覆盖不全，比率不得作为整批最终率
    assert report.micro.coverage == "partial"
    assert report.micro.l1 is not None and report.micro.l1.precision is None
    assert report.micro.l2 is not None and report.micro.l2.precision is None
    assert report.micro.l2.recall is None and report.micro.l2.f1 is None
    ready = next(p for p in report.papers if p.paper_key.endswith("-ready"))
    blocked = next(p for p in report.papers if p.paper_key.endswith("-blocked"))
    assert ready.l2.precision == 1.0  # 就绪论文确定明细保留
    assert blocked.l1 is None and blocked.l2 is None  # 不从未知 Gold 捏造 G


# ---------------------------------------------------------------------------
# 裁定2：别名歧义保留未决关系，不默认为错误
# ---------------------------------------------------------------------------


def _ambiguity_bundle():
    key = "synthetic-scoring-ambiguous-alias"
    gold = [
        GoldPaperEntry(
            paper_key=key,
            coverage="complete",
            objects=[
                _gold_obj(
                    "A",
                    ["SYN-A", "SYN-SHARED"],
                    {
                        DIST: [
                            GoldValueEntry(
                                record_id="g1",
                                value=_value("g1", value="8.2", unit="kpc"),
                            )
                        ],
                        RV: [
                            GoldValueEntry(
                                record_id="g2",
                                value=_value(
                                    "g2", value="600", error="20", unit="km s⁻¹"
                                ),
                            )
                        ],
                    },
                ),
                _gold_obj(
                    "B",
                    ["SYN-B", "SYN-SHARED"],
                    {
                        DIST: [
                            GoldValueEntry(
                                record_id="g3",
                                value=_value("g3", value="10", unit="kpc"),
                            )
                        ]
                    },
                ),
            ],
        )
    ]
    predictions = [
        _delivered(
            key,
            [
                _pred_obj(
                    key,
                    "A",
                    ["SYN-A"],
                    {
                        DIST: [
                            PredictionValueEntry(
                                record_id="p1",
                                value=_value("p1", value="8.2", unit="kpc"),
                            )
                        ],
                        RV: [
                            PredictionValueEntry(
                                record_id="p2",
                                value=_value(
                                    "p2", value="610", error="20", unit="km s⁻¹"
                                ),
                            )
                        ],
                    },
                ),
                _pred_obj(
                    key,
                    "C",
                    ["SYN-C"],
                    {
                        DIST: [
                            PredictionValueEntry(
                                record_id="p3",
                                value=_value("p3", value="5", unit="kpc"),
                            )
                        ]
                    },
                ),
                _pred_obj(
                    key,
                    "P",
                    ["SYN-SHARED"],
                    {
                        DIST: [
                            PredictionValueEntry(
                                record_id="p4",
                                value=_value("p4", value="7", unit="kpc"),
                            )
                        ]
                    },
                ),
            ],
        )
    ]
    plan = _plan([key], {key: [DIST, RV]}, gold)
    return plan, gold, predictions


def test_alias_ambiguity_pending_not_counted_as_error():
    plan, gold, predictions = _ambiguity_bundle()
    report = _score(plan, gold, predictions)
    paper = report.papers[0]
    # 确定部分保留：A 匹配 TP、C 多报 FP；歧义不默认为 FP/FN
    assert paper.l1.tp == 1 and paper.l1.fp == 1 and paper.l1.fn == 0
    assert paper.l1.pending == 2  # 歧义预测 P + 待定 Gold B
    assert paper.l1.precision is None and paper.l1.recall is None
    assert paper.l1.f1 is None
    assert report.micro.l1.precision is None and report.micro.l1.pending == 2
    # L2 保留实际 P/G 总数与确定 C；涉歧义值不标漏报/多报
    assert paper.l2.correct == 1 and paper.l2.predicted == 4 and paper.l2.gold == 3
    assert paper.l2.precision is None and paper.l2.recall is None
    statuses = [row.status for row in paper.pairings]
    assert statuses.count("unresolved_object_identity") == 2  # p4 与 g3
    assert "value_mismatch" in statuses  # A speed 抄错仍是确定差异
    assert "extra_object_value" in statuses  # C 的多报仍是确定 FP
    assert report.batch_provisional is True
    assert any(d.code == "ambiguous_object_alias" for d in paper.diagnostics)


# ---------------------------------------------------------------------------
# 裁定3：白名单未命中不能证不同（条件域进入未知出口）
# ---------------------------------------------------------------------------


def test_unsupported_condition_difference_is_provisional_not_mismatch():
    key = "synthetic-scoring-condition-unknown"
    gold = [
        GoldPaperEntry(
            paper_key=key,
            coverage="complete",
            objects=[
                _gold_obj(
                    "A",
                    ["SYN-A"],
                    {
                        DIST: [
                            GoldValueEntry(
                                record_id="g1",
                                value=_value(
                                    "g1", value="8.2", unit="kpc", condition=M1
                                ),
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
                _pred_obj(
                    key,
                    "A",
                    ["SYN-A"],
                    {
                        DIST: [
                            PredictionValueEntry(
                                record_id="p1",
                                value=_value(
                                    "p1",
                                    value="8.2",
                                    unit="kpc",
                                    condition="under Galactic potential M1 (same adopted model)",
                                ),
                            )
                        ]
                    },
                )
            ],
        )
    ]
    report = _score(_plan([key], {key: [DIST]}, gold), gold, predictions)
    paper = report.papers[0]
    assert any(row.status == "unresolved_attribution" for row in paper.pairings)
    assert not any(row.status == "attribution_mismatch" for row in paper.pairings)
    assert paper.l2.precision is None
    assert report.batch_provisional is True


# ---------------------------------------------------------------------------
# 裁定4：归属集合内确定性最大一对一匹配
# ---------------------------------------------------------------------------


def _matching_bundle(pred_specs):
    """gold: g1=8.2, g2=8.3（同归属）；pred_specs: [(record_id, value, fragments)]。"""
    key = "synthetic-scoring-matching"
    gold = [
        GoldPaperEntry(
            paper_key=key,
            coverage="complete",
            objects=[
                _gold_obj(
                    "A",
                    ["SYN-A"],
                    {
                        DIST: [
                            GoldValueEntry(
                                record_id="g1",
                                value=_value("g1", value="8.2", unit="kpc"),
                                evidence_fragments=[_fragment("gold-1", "8.2 kpc")],
                            ),
                            GoldValueEntry(
                                record_id="g2",
                                value=_value("g2", value="8.3", unit="kpc"),
                                evidence_fragments=[_fragment("gold-2", "8.3 kpc")],
                            ),
                        ]
                    },
                )
            ],
        )
    ]
    objects = [
        _pred_obj(
            key,
            "A",
            ["SYN-A"],
            {
                DIST: [
                    PredictionValueEntry(
                        record_id=record_id,
                        value=_value(record_id, value=value, unit="kpc"),
                        evidence_fragments=[
                            _fragment(f"pred-{record_id}", f"printed {value}")
                        ],
                    )
                    for record_id, value in pred_specs
                ]
            },
        )
    ]
    return _plan([key], {key: [DIST]}, gold), gold, [_delivered(key, objects)]


def _equivalent_mock(tmp_path):
    """对每个未知候选都答 equivalent 的合成 mock；引用指向实际提供片段。"""

    def handler(request):
        return AgentResponse(
            verdict="equivalent",
            rationale="synthetic fictional context confirms typesetting only",
            source_citations=[f"left:{request.left_fragments[0].fragment_id}"],
        )

    return MockAgentAdapter(handler)


def test_reordering_predictions_does_not_change_matching(tmp_path: Path):
    plan, gold, preds = _matching_bundle([("p1", "\\mystery{8.2}"), ("p2", "8.2")])
    auxiliary = protocol_only_config(store_directory=str(tmp_path / "frozen"))
    first = score_batch(
        plan, gold, preds, auxiliary=auxiliary, agent_adapter=_equivalent_mock(tmp_path)
    )
    reversed_preds = [
        preds[0].model_copy(
            update={
                "objects": [
                    preds[0]
                    .objects[0]
                    .model_copy(
                        update={
                            "quantities": [
                                preds[0]
                                .objects[0]
                                .quantities[0]
                                .model_copy(
                                    update={
                                        "values": list(
                                            reversed(
                                                preds[0].objects[0].quantities[0].values
                                            )
                                        )
                                    }
                                )
                            ]
                        }
                    )
                ]
            }
        )
    ]
    second = score_batch(
        plan,
        gold,
        reversed_preds,
        auxiliary=auxiliary,
        agent_adapter=_equivalent_mock(tmp_path),
    )
    one, two = first.papers[0], second.papers[0]
    assert (one.l2.correct, one.l2.predicted, one.l2.gold) == (2, 2, 2)
    assert (two.l2.correct, two.l2.predicted, two.l2.gold) == (2, 2, 2)
    assert one.l2.precision == 1.0 and two.l2.precision == 1.0


def test_multiple_legitimate_candidates_match_maximally():
    """Gold 两条同内容值（8.2/8.2）与两条同内容预测：最大匹配 C=2。"""
    key = "synthetic-scoring-multi-candidate"
    gold = [
        GoldPaperEntry(
            paper_key=key,
            coverage="complete",
            objects=[
                _gold_obj(
                    "A",
                    ["SYN-A"],
                    {
                        DIST: [
                            GoldValueEntry(
                                record_id="g1",
                                value=_value("g1", value="8.2", unit="kpc"),
                            ),
                            GoldValueEntry(
                                record_id="g2",
                                value=_value("g2", value="8.2", unit="kpc"),
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
                _pred_obj(
                    key,
                    "A",
                    ["SYN-A"],
                    {
                        DIST: [
                            PredictionValueEntry(
                                record_id="p1",
                                value=_value("p1", value="8.2", unit="kpc"),
                            ),
                            PredictionValueEntry(
                                record_id="p2",
                                value=_value("p2", value="8.2", unit="kpc"),
                            ),
                        ]
                    },
                )
            ],
        )
    ]
    report = _score(_plan([key], {key: [DIST]}, gold), gold, predictions)
    paper = report.papers[0]
    assert (paper.l2.correct, paper.l2.predicted, paper.l2.gold) == (2, 2, 2)
    assert paper.l2.precision == 1.0 and paper.l2.recall == 1.0


def test_saturated_matching_with_extra_unknown_keeps_final_counts():
    """已确认匹配达到上界时，额外未知候选如实诊断但不推翻确定 C。"""
    key = "synthetic-scoring-saturated"
    gold = [
        GoldPaperEntry(
            paper_key=key,
            coverage="complete",
            objects=[
                _gold_obj(
                    "A",
                    ["SYN-A"],
                    {
                        DIST: [
                            GoldValueEntry(
                                record_id="g1",
                                value=_value("g1", value="8.2", unit="kpc"),
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
                _pred_obj(
                    key,
                    "A",
                    ["SYN-A"],
                    {
                        DIST: [
                            PredictionValueEntry(
                                record_id="p1",
                                value=_value("p1", value="8.2", unit="kpc"),
                            ),
                            PredictionValueEntry(
                                record_id="p2",
                                value=_value("p2", value="\\mystery{8.2}", unit="kpc"),
                            ),
                        ]
                    },
                )
            ],
        )
    ]
    report = _score(_plan([key], {key: [DIST]}, gold), gold, predictions)
    paper = report.papers[0]
    assert paper.l2.correct == 1 and paper.l2.predicted == 2 and paper.l2.gold == 1
    assert paper.l2.precision == 0.5 and paper.l2.recall == 1.0
    assert paper.l2.f1 == 2 / 3
    assert report.batch_provisional is False  # 未知候选不能影响已确定的 C
    statuses = [row.status for row in paper.pairings]
    assert "matched" in statuses
    assert "unknown_candidate_extra" in statuses  # 如实诊断，不当普通 extra_value
    assert "unresolved_value" not in statuses


def test_unknown_candidate_affecting_counts_stays_provisional():
    key = "synthetic-scoring-extra-unknown"
    gold = [
        GoldPaperEntry(
            paper_key=key,
            coverage="complete",
            objects=[
                _gold_obj(
                    "A",
                    ["SYN-A"],
                    {
                        DIST: [
                            GoldValueEntry(
                                record_id="g1",
                                value=_value("g1", value="\\mystery{8.2}", unit="kpc"),
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
                _pred_obj(
                    key,
                    "A",
                    ["SYN-A"],
                    {
                        DIST: [
                            PredictionValueEntry(
                                record_id="p1",
                                value=_value("p1", value="8.2", unit="kpc"),
                            )
                        ]
                    },
                )
            ],
        )
    ]
    report = _score(_plan([key], {key: [DIST]}, gold), gold, predictions)
    paper = report.papers[0]
    assert paper.l2.correct == 0
    assert paper.l2.precision is None
    assert report.batch_provisional is True
    assert any(row.status == "unresolved_value" for row in paper.pairings)


def test_agent_explicit_different_on_unknown_is_determined(tmp_path: Path):
    key = "synthetic-scoring-agent-different"
    gold = [
        GoldPaperEntry(
            paper_key=key,
            coverage="complete",
            objects=[
                _gold_obj(
                    "A",
                    ["SYN-A"],
                    {
                        DIST: [
                            GoldValueEntry(
                                record_id="g1",
                                value=_value("g1", value="8.2", unit="kpc"),
                                evidence_fragments=[_fragment("gold-1", "8.2 kpc")],
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
                _pred_obj(
                    key,
                    "A",
                    ["SYN-A"],
                    {
                        DIST: [
                            PredictionValueEntry(
                                record_id="p1",
                                value=_value("p1", value="\\mystery{9.9}", unit="kpc"),
                                evidence_fragments=[_fragment("pred-1", "9.9")],
                            )
                        ]
                    },
                )
            ],
        )
    ]
    adapter = MockAgentAdapter(
        lambda request: AgentResponse(
            verdict="different",
            rationale="fictional macro changes the value",
            source_citations=["left:gold-1"],
        )
    )
    report = score_batch(
        _plan([key], {key: [DIST]}, gold),
        gold,
        predictions,
        auxiliary=protocol_only_config(store_directory=str(tmp_path / "frozen")),
        agent_adapter=adapter,
    )
    paper = report.papers[0]
    assert adapter.calls, "证据充足时应调用 Agent"
    assert paper.l2.correct == 0
    assert paper.l2.precision == 0.0 and paper.l2.recall == 0.0
    assert report.batch_provisional is False
    assert any(row.status == "value_mismatch" for row in paper.pairings)
    assert not any(row.status == "unresolved_value" for row in paper.pairings)


# ---------------------------------------------------------------------------
# 裁定6：材料绑定不匹配时不调用 Agent
# ---------------------------------------------------------------------------


def test_wrong_material_binding_prevents_agent_call(tmp_path: Path):
    key = "synthetic-scoring-insufficient-materials"
    wrong_fp = hashlib.sha256(b"other-materials").hexdigest()
    gold = [
        GoldPaperEntry(
            paper_key=key,
            coverage="complete",
            objects=[
                _gold_obj(
                    "A",
                    ["SYN-A"],
                    {
                        DIST: [
                            GoldValueEntry(
                                record_id="g1",
                                value=_value("g1", value="8.2", unit="kpc"),
                                evidence_fragments=[
                                    _fragment("gold-1", "8.2 kpc").model_copy(
                                        update={
                                            "material_name": "other-materials",
                                            "material_fingerprint": wrong_fp,
                                        }
                                    )
                                ],
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
                _pred_obj(
                    key,
                    "A",
                    ["SYN-A"],
                    {
                        DIST: [
                            PredictionValueEntry(
                                record_id="p1",
                                value=_value("p1", value="\\mystery{8.2}", unit="kpc"),
                                evidence_fragments=[_fragment("pred-1", "macro")],
                            )
                        ]
                    },
                )
            ],
        )
    ]
    adapter = MockAgentAdapter(
        lambda request: AgentResponse(
            verdict="equivalent",
            rationale="x",
            source_citations=["left:gold-1"],
        )
    )
    report = score_batch(
        _plan([key], {key: [DIST]}, gold),
        gold,
        predictions,
        auxiliary=protocol_only_config(store_directory=str(tmp_path / "frozen")),
        agent_adapter=adapter,
    )
    assert adapter.calls == []
    assert report.papers[0].l2.precision is None
    assert report.batch_provisional is True


# ---------------------------------------------------------------------------
# 裁定9：证据独立诊断
# ---------------------------------------------------------------------------


def _evidence_bundle(registry, evidence_record_id="synthetic-source"):
    key = "synthetic-scoring-evidence"
    value = build_measured_value(
        value_id="p1",
        value="8.2",
        error="0.3",
        unit="kpc",
        condition=STD,
        representation=HELI,
        evidence_record_id=evidence_record_id,
    )
    gold = [
        GoldPaperEntry(
            paper_key=key,
            coverage="complete",
            objects=[
                _gold_obj(
                    "A",
                    ["SYN-A"],
                    {
                        DIST: [
                            GoldValueEntry(
                                record_id="g1",
                                value=_value(
                                    "g1", value="8.2", error="0.3", unit="kpc"
                                ),
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
                _pred_obj(
                    key,
                    "A",
                    ["SYN-A"],
                    {DIST: [PredictionValueEntry(record_id="p1", value=value)]},
                )
            ],
        )
    ]
    return _plan([key], {key: [DIST]}, gold, registry=registry), gold, predictions


def test_dangling_evidence_ref_diagnosed_but_not_scored():
    plan, gold, preds = _evidence_bundle(
        registry=["synthetic-source"], evidence_record_id="unrelated-source"
    )
    report = _score(plan, gold, preds)
    paper = report.papers[0]
    assert paper.l2.correct == 1 and paper.l2.precision == 1.0  # 诊断不混入质量 C
    assert any(d.code == "dangling_evidence_ref" for d in paper.diagnostics)
    assert paper.evidence_verification["dangling"] == 1


def test_missing_registry_reports_not_verified():
    plan, gold, preds = _evidence_bundle(registry=None)
    report = _score(plan, gold, preds)
    paper = report.papers[0]
    assert paper.evidence_verification["not_verified"] == 1
    assert paper.evidence_verification["values"] == 1
    assert not any(
        d.code.startswith(("dangling", "wrong_material")) for d in paper.diagnostics
    )


def test_wrong_material_fragment_diagnosed():
    key = "synthetic-scoring-wrong-materials"
    gold = [
        GoldPaperEntry(
            paper_key=key,
            coverage="complete",
            objects=[
                _gold_obj(
                    "A",
                    ["SYN-A"],
                    {
                        DIST: [
                            GoldValueEntry(
                                record_id="g1",
                                value=_value("g1", value="8.2", unit="kpc"),
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
                _pred_obj(
                    key,
                    "A",
                    ["SYN-A"],
                    {
                        DIST: [
                            PredictionValueEntry(
                                record_id="p1",
                                value=_value("p1", value="8.2", unit="kpc"),
                                evidence_fragments=[
                                    _fragment("pred-1", "8.2 kpc").model_copy(
                                        update={"material_fingerprint": "0" * 64}
                                    )
                                ],
                            )
                        ]
                    },
                )
            ],
        )
    ]
    report = _score(_plan([key], {key: [DIST]}, gold), gold, predictions)
    paper = report.papers[0]
    assert any(d.code == "wrong_material_evidence" for d in paper.diagnostics)
    assert paper.evidence_verification["wrong_material"] == 1
    assert paper.l2.correct == 1


# ---------------------------------------------------------------------------
# 裁定1 补充：混合批次的报告可序列化，字段完整
# ---------------------------------------------------------------------------


def test_mixed_report_serializable():
    plan, gold, predictions = _ready_blocked_bundle()
    report = _score(plan, gold, predictions)
    payload = json.loads(report.model_dump_json())
    assert payload["micro"]["coverage"] == "partial"
    assert payload["readiness_blocked_papers"] == ["synthetic-scoring-blocked"]

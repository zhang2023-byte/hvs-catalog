"""评分回归：数值比较与合并优先级的独立构造反例（合成材料）。

覆盖：同一确定身份重复预测的 L2 记录池一对一、完整受限数值语法而
非字符白名单、结构化 unit/value/分量路径的真实字段传参、多组件合并
中确定 different 优先于不再影响分数的 unknown/技术失败（失败事实仍
保留）。全部走完整 score_batch 入口或真实比较器，先复现反例再验证
组合不变量。
"""

from __future__ import annotations

import copy

import pytest
from scoring_sample_helpers import (
    DIST,
    M1,
    M2,
    RV,
    STD,
    load_bundle,
)
from scoring_sample_helpers import (
    build_single_quantity_bundle as _bundle,
)
from scoring_sample_helpers import (
    make_fragment as _fragment,
)
from scoring_sample_helpers import (
    score_with_protocol_only as _score,
)

from hvs_catalog.evaluation.scoring import (
    AgentResponse,
    MockAgentAdapter,
    build_measured_value,
    gold_input_digest,
    protocol_only_config,
    score_batch,
)
from hvs_catalog.evaluation.scoring.layers import content_key
from hvs_catalog.evaluation.scoring.models import (
    GoldObjectEntry,
    GoldPaperEntry,
    GoldQuantityEntry,
    GoldValueEntry,
    PredictionObjectEntry,
    PredictionPaperEntry,
    PredictionQuantityEntry,
    PredictionValueEntry,
)
from hvs_catalog.evaluation.scoring.printed import compare_number

HELI = "heliocentric"


def _l2_rows(paper):
    return [row for row in paper.pairings if row.layer == "l2"]


# ---------------------------------------------------------------------------
# 同一确定身份重复预测的 L2 记录池一对一
# ---------------------------------------------------------------------------


def test_s08_duplicate_pool_order_and_label_invariant():
    """固定 S08（一条 5.0、一条 9.9）：反序/改内部编号结果不变。"""
    plan, gold, preds, auxiliary = load_bundle("synthetic-s08")
    forward = score_batch(plan, gold, preds, auxiliary=auxiliary)
    reversed_preds = copy.deepcopy(preds)
    reversed_preds[0].objects = list(reversed(reversed_preds[0].objects))
    backward = score_batch(plan, gold, reversed_preds, auxiliary=auxiliary)
    renamed_preds = copy.deepcopy(preds)
    renamed_preds[0].objects[0].quantities[0].values[0].record_id = "z9"
    renamed_preds[0].objects[1].quantities[0].values[0].record_id = "z8"
    renamed = score_batch(plan, gold, renamed_preds, auxiliary=auxiliary)
    for report in (forward, backward, renamed):
        paper = report.papers[0]
        assert (paper.l1.tp, paper.l1.fp, paper.l1.fn) == (1, 1, 0)
        assert (paper.l2.correct, paper.l2.predicted, paper.l2.gold) == (1, 2, 1)
        assert paper.l2.precision == 0.5 and paper.l2.recall == 1.0
        assert paper.l2.f1 == pytest.approx(2 / 3)
        assert report.batch_provisional is False
        statuses = [row.status for row in _l2_rows(paper)]
        assert "matched" in statuses and "extra_value" in statuses


def test_duplicate_pool_complementary_quantities():
    """重复条目互补物理量：合并记录池后两量都可一对一，不漏计 P/G。"""
    plan, gold, preds = _bundle(
        [
            ("g1", STD, {"value": "8.2", "error": "0.3", "unit": "kpc"}, []),
            ("g2", STD, {"value": "600", "error": "20", "unit": "km s^{-1}"}, []),
        ],
        [("p1", STD, {"value": "8.2", "error": "0.3", "unit": "kpc"}, [])],
        # 需要多 quantity 构造，见下方专用 builder
    )
    # _bundle 是单物理量构造，这里改用显式双物理量输入
    gold[0].objects[0].quantities.append(
        GoldQuantityEntry(
            quantity_path=RV,
            values=[
                GoldValueEntry(
                    record_id="g2",
                    value=build_measured_value(
                        value_id="g2",
                        value="600",
                        error="20",
                        unit="km s⁻¹",
                        condition=STD,
                        representation=HELI,
                    ),
                )
            ],
        )
    )
    del gold[0].objects[0].quantities[0].values[1]
    preds[0].objects[0].quantities.append(
        PredictionQuantityEntry(
            quantity_path=RV,
            values=[
                PredictionValueEntry(
                    record_id="p2",
                    value=build_measured_value(
                        value_id="p2",
                        value="600",
                        error="20",
                        unit="km s⁻¹",
                        condition=STD,
                        representation=HELI,
                    ),
                )
            ],
        )
    )
    dup = PredictionObjectEntry(
        paper_key=preds[0].paper_key,
        label="A-dup",
        identifiers=["SYN-A"],
        quantities=[],
    )
    preds[0].objects.append(dup)
    # 互补：把 RV 值放进 dup 对象（模拟重复条目报告另一物理量）
    preds[0].objects[1].quantities = preds[0].objects[0].quantities[1:]
    preds[0].objects[0].quantities = preds[0].objects[0].quantities[:1]
    plan = plan.model_copy(
        update={
            "gold_ref": plan.gold_ref.model_copy(
                update={"fingerprint": gold_input_digest(gold)}
            ),
            "scopes": [
                scope.model_copy(update={"quantity_paths": [DIST, RV]})
                for scope in plan.scopes
            ],
        }
    )
    report = _score(plan, gold, preds)
    paper = report.papers[0]
    assert (paper.l1.tp, paper.l1.fp, paper.l1.fn) == (1, 1, 0)
    assert (paper.l2.correct, paper.l2.predicted, paper.l2.gold) == (2, 2, 2)
    assert paper.l2.precision == 1.0 and paper.l2.recall == 1.0
    assert report.batch_provisional is False


def test_duplicate_pool_extra_values_consume_gold_once():
    """多余重复值：Gold 只消耗一次，全部预测值入 P。"""
    plan, gold, preds = _bundle(
        [("g1", STD, {"value": "5.0", "error": "0.2", "unit": "kpc"}, [])],
        [
            ("p1", STD, {"value": "5.0", "error": "0.2", "unit": "kpc"}, []),
            ("p2", STD, {"value": "6.0", "error": "0.2", "unit": "kpc"}, []),
        ],
    )
    # 第二条记录放入重复对象条目
    second = preds[0].objects[0].quantities[0].values.pop()
    dup = PredictionObjectEntry(
        paper_key=preds[0].paper_key,
        label="A-dup",
        identifiers=["SYN-A"],
        quantities=[PredictionQuantityEntry(quantity_path=DIST, values=[second])],
    )
    preds[0].objects.append(dup)
    third = PredictionValueEntry(
        record_id="p3",
        value=build_measured_value(
            value_id="p3",
            value="5.0",
            error="0.2",
            unit="kpc",
            condition=STD,
            representation=HELI,
        ),
    )
    preds[0].objects[1].quantities[0].values.append(third)
    report = _score(plan, gold, preds)
    paper = report.papers[0]
    assert (paper.l1.tp, paper.l1.fp, paper.l1.fn) == (1, 1, 0)
    assert (paper.l2.correct, paper.l2.predicted, paper.l2.gold) == (1, 3, 1)
    assert paper.l2.precision == pytest.approx(1 / 3)
    assert paper.l2.recall == 1.0 and paper.l2.f1 == pytest.approx(1 / 2)
    assert report.batch_provisional is False
    assert sum(1 for row in _l2_rows(paper) if row.status == "matched") == 1


def test_duplicate_pool_condition_different_candidate():
    """条件不同的重复身份候选：不重复消耗 Gold，差异如实诊断。"""
    plan, gold, preds = _bundle(
        [("g1", M1, {"value": "5.0", "error": "0.2", "unit": "kpc"}, [])],
        [("p1", M1, {"value": "5.0", "error": "0.2", "unit": "kpc"}, [])],
    )
    dup_value = PredictionValueEntry(
        record_id="p2",
        value=build_measured_value(
            value_id="p2",
            value="5.0",
            error="0.2",
            unit="kpc",
            condition=M2,
            representation=HELI,
        ),
    )
    preds[0].objects.append(
        PredictionObjectEntry(
            paper_key=preds[0].paper_key,
            label="A-dup",
            identifiers=["SYN-A"],
            quantities=[
                PredictionQuantityEntry(quantity_path=DIST, values=[dup_value])
            ],
        )
    )
    report = _score(plan, gold, preds)
    paper = report.papers[0]
    assert (paper.l1.tp, paper.l1.fp, paper.l1.fn) == (1, 1, 0)
    assert (paper.l2.correct, paper.l2.predicted, paper.l2.gold) == (1, 2, 1)
    assert paper.l2.precision == 0.5 and paper.l2.recall == 1.0
    assert report.batch_provisional is False
    assert any(row.status == "attribution_mismatch" for row in _l2_rows(paper))


def test_ambiguity_not_merged_into_duplicate_pool():
    """共享别名的真实身份歧义不入池：仍 pending，不借数值消歧。"""
    key = "synthetic-ambiguous-duplicates"
    common_kwargs = {"condition": STD, "representation": HELI}

    def measured(record_id, value):
        return build_measured_value(
            value_id=record_id,
            value=value,
            error="0.2",
            unit="kpc",
            **common_kwargs,
        )

    gold = [
        GoldPaperEntry(
            paper_key=key,
            coverage="complete",
            objects=[
                GoldObjectEntry(
                    label="A",
                    identifiers=["SYN-A", "SYN-SHARED"],
                    quantities=[
                        GoldQuantityEntry(
                            quantity_path=DIST,
                            values=[
                                GoldValueEntry(
                                    record_id="g1", value=measured("g1", "5.0")
                                )
                            ],
                        )
                    ],
                ),
                GoldObjectEntry(
                    label="B",
                    identifiers=["SYN-B", "SYN-SHARED"],
                    quantities=[
                        GoldQuantityEntry(
                            quantity_path=DIST,
                            values=[
                                GoldValueEntry(
                                    record_id="g3", value=measured("g3", "10")
                                )
                            ],
                        )
                    ],
                ),
            ],
        )
    ]
    predictions = [
        PredictionPaperEntry(
            paper_key=key,
            delivered=True,
            format_valid=True,
            completion="complete",
            objects=[
                PredictionObjectEntry(
                    paper_key=key,
                    label="A",
                    identifiers=["SYN-A"],
                    quantities=[
                        PredictionQuantityEntry(
                            quantity_path=DIST,
                            values=[
                                PredictionValueEntry(
                                    record_id="p1", value=measured("p1", "5.0")
                                )
                            ],
                        )
                    ],
                ),
                PredictionObjectEntry(
                    paper_key=key,
                    label="A-dup",
                    identifiers=["SYN-A"],
                    quantities=[
                        PredictionQuantityEntry(
                            quantity_path=DIST,
                            values=[
                                PredictionValueEntry(
                                    record_id="p2", value=measured("p2", "9.9")
                                )
                            ],
                        )
                    ],
                ),
                PredictionObjectEntry(
                    paper_key=key,
                    label="P",
                    identifiers=["SYN-SHARED"],
                    quantities=[
                        PredictionQuantityEntry(
                            quantity_path=DIST,
                            values=[
                                PredictionValueEntry(
                                    record_id="p4", value=measured("p4", "5.0")
                                )
                            ],
                        )
                    ],
                ),
            ],
        )
    ]
    plan = load_bundle("synthetic-s08")[0].model_copy(
        update={
            "paper_keys": [key],
            "scopes": [
                load_bundle("synthetic-s08")[0]
                .scopes[0]
                .model_copy(update={"paper_key": key, "quantity_paths": [DIST]})
            ],
            "gold_ref": load_bundle("synthetic-s08")[0].gold_ref.model_copy(
                update={"fingerprint": gold_input_digest(gold)}
            ),
        }
    )
    report = score_batch(plan, gold, predictions, auxiliary=protocol_only_config())
    paper = report.papers[0]
    # 确定身份的记录池照常一对一：A 与 A-dup 中 5.0 正确
    assert (paper.l1.tp, paper.l1.fp, paper.l1.fn) == (1, 1, 0)
    assert paper.l1.pending == 2  # P 歧义 + Gold B 待定，不入池
    assert paper.l1.precision is None
    # 歧义值计入 P/G、保留未决行，不参与池匹配也不标漏报/多报
    statuses = [row.status for row in _l2_rows(paper)]
    assert statuses.count("unresolved_object_identity") == 2
    assert "matched" in statuses
    assert (paper.l2.correct, paper.l2.predicted, paper.l2.gold) == (1, 3, 2)
    assert paper.l2.precision is None
    assert report.batch_provisional is True


# ---------------------------------------------------------------------------
# 完整受限数值语法（字符白名单不代表语法有效）
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("gold", "prediction"),
    [
        ("8.2e+", "8.2e3"),  # 未完成指数
        ("8.2e3", "8.2e+"),
        ("10^{", "10"),  # 非配对括号/断裂乘幂
        ("8.2*10^{3", "8.2*10^{3}"),
        ("8.2e+", "8.2e+"),  # 双侧同畸形也不能确认等价
        ("1.2e", "1.2"),
        ("12h34", "12h34m"),  # 时分秒链断裂（缺标记）
    ],
)
def test_malformed_number_syntax_is_undecidable(gold, prediction):
    assert compare_number(gold, prediction) == "undecidable"


@pytest.mark.parametrize(
    ("gold", "prediction", "expected"),
    [
        ("8.2e3", "8.2e3", "equivalent"),
        ("8.2E-3", "8.2E-3", "equivalent"),
        ("1.5*10^{3}", "1.5*10^{3}", "equivalent"),
        ("8.2e3", "8.2e4", "different"),
        ("1.5*10^{3}", "1.5*10^{-3}", "different"),
    ],
)
def test_complete_number_syntax_stays_determined(gold, prediction, expected):
    assert compare_number(gold, prediction) == expected


def test_incomplete_exponent_not_mismatched_in_score_batch():
    plan, gold, preds = _bundle(
        [("g1", STD, {"value": "8.2e+", "error": "0.3", "unit": "kpc"}, [])],
        [("p1", STD, {"value": "8.2e3", "error": "0.3", "unit": "kpc"}, [])],
    )
    report = _score(plan, gold, preds)
    paper = report.papers[0]
    assert paper.l2.precision is None  # 不能最终判 0
    assert report.batch_provisional is True
    assert any(
        row.status == "unresolved_value" and row.fields == ["value"]
        for row in _l2_rows(paper)
    )


def test_identical_malformed_syntax_not_confirmed_equivalent():
    """键捷径不得把畸形语法确认为安全等价（双侧相同畸形仍未知）。"""
    plan, gold, preds = _bundle(
        [("g1", STD, {"value": "8.2e+", "error": "0.3", "unit": "kpc"}, [])],
        [("p1", STD, {"value": "8.2e+", "error": "0.3", "unit": "kpc"}, [])],
    )
    report = _score(plan, gold, preds)
    paper = report.papers[0]
    assert paper.l2.correct == 0
    assert paper.l2.precision is None
    assert report.batch_provisional is True
    assert any(row.status == "unresolved_value" for row in _l2_rows(paper))


def test_content_key_rejects_malformed_syntax():
    measured = build_measured_value(
        value_id="k",
        value="8.2e+",
        error="0.3",
        unit="kpc",
        condition=STD,
        representation=HELI,
    )
    assert content_key(measured) is None
    valid = build_measured_value(
        value_id="k",
        value="8.2",
        error="0.3",
        unit="kpc",
        condition=STD,
        representation=HELI,
    )
    assert content_key(valid) is not None


def test_complete_syntax_differences_stay_final():
    plan, gold, preds = _bundle(
        [("g1", STD, {"value": "8.2e3", "error": "0.3", "unit": "kpc"}, [])],
        [("p1", STD, {"value": "8.2e4", "error": "0.3", "unit": "kpc"}, [])],
    )
    report = _score(plan, gold, preds)
    paper = report.papers[0]
    assert paper.l2.precision == 0.0
    assert report.batch_provisional is False
    assert any(row.status == "value_mismatch" for row in _l2_rows(paper))


# ---------------------------------------------------------------------------
# 结构化字段的真正待判内容传参
# ---------------------------------------------------------------------------


def _report_bundle(report_field, gold_pair, pred_pair, fragments=("coverage.unit",)):
    """结构化报告字段变体：gold_pair/pred_pair 是 (value, unit)。"""
    base = {"value": "8.2", "error": "0.3", "unit": "kpc"}
    gold_fragments = [_fragment("gf", f"gold {report_field} text", list(fragments))]
    pred_fragments = [_fragment("pf", f"pred {report_field} text", list(fragments))]
    return _bundle(
        [
            (
                "g1",
                STD,
                {**base, report_field: gold_pair},
                gold_fragments,
            )
        ],
        [("p1", STD, {**base, report_field: pred_pair}, pred_fragments)],
    )


@pytest.mark.parametrize(
    ("report_field", "gold_pair", "pred_pair", "undecided_sub"),
    [
        ("coverage", ("95", "km s^{-1}"), ("95", "km/s"), "unit"),
        ("lower_quantile", ("2.5", "km s^{-1}"), ("2.5", "km/s"), "unit"),
        ("upper_quantile", ("97.5", "m s^{-1}"), ("97.5", "m/s"), "unit"),
        ("coverage", ("95", "kpc"), ("\\mystery{95}", "kpc"), "value"),
        ("lower_quantile", ("2.5", "kpc"), ("\\mystery{2.5}", "kpc"), "value"),
        ("upper_quantile", ("97.5", "kpc"), ("\\mystery{97.5}", "kpc"), "value"),
    ],
)
def test_report_requests_carry_real_field_content(
    report_field, gold_pair, pred_pair, undecided_sub
):
    plan, gold, preds = _report_bundle(
        report_field,
        gold_pair,
        pred_pair,
        fragments=(f"{report_field}.{undecided_sub}",),
    )
    seen = []

    def handler(request):
        seen.append((request.field_name, request.left, request.right))
        return AgentResponse(
            verdict="different",
            rationale="synthetic",
            source_citations=["left:gf"],
        )

    mock = MockAgentAdapter(handler)
    report = score_batch(
        plan, gold, preds, auxiliary=protocol_only_config(), agent_adapter=mock
    )
    assert len(seen) == 1
    field_name, left, right = seen[0]
    assert field_name == f"{report_field}.{undecided_sub}"
    # 请求携带真正待判内容：.unit 传单位、.value 传报告值
    assert left == (gold_pair[1] if undecided_sub == "unit" else gold_pair[0])
    assert right == (pred_pair[1] if undecided_sub == "unit" else pred_pair[0])
    paper = report.papers[0]
    assert paper.l2.precision == 0.0  # 明确 different → 确定不匹配
    assert report.batch_provisional is False


def test_component_path_requests_carry_named_subfield():
    """具名误差分量：按名称与侧别取真正待判子字段。"""
    base = {"value": "8.2", "unit": "kpc"}
    gold_fragments = [_fragment("gf", "sys err text", ["error_components.sys.error"])]
    pred_fragments = [_fragment("pf", "macro def", ["error_components.sys.error"])]
    plan, gold, preds = _bundle(
        [
            (
                "g1",
                STD,
                {**base, "error_components": [{"name": "sys", "error": "0.3"}]},
                gold_fragments,
            )
        ],
        [
            (
                "p1",
                STD,
                {
                    **base,
                    "error_components": [{"name": "sys", "error": "\\mystery{0.3}"}],
                },
                pred_fragments,
            )
        ],
    )
    seen = []

    def handler(request):
        seen.append((request.field_name, request.left, request.right))
        return AgentResponse(
            verdict="equivalent", rationale="synthetic", source_citations=["left:gf"]
        )

    mock = MockAgentAdapter(handler)
    report = score_batch(
        plan, gold, preds, auxiliary=protocol_only_config(), agent_adapter=mock
    )
    assert seen == [("error_components.sys.error", "0.3", "\\mystery{0.3}")]
    paper = report.papers[0]
    assert paper.l2.correct == 1 and paper.l2.precision == 1.0
    assert report.batch_provisional is False


def test_missing_component_subfield_needs_no_agent():
    """分量子字段一侧缺失：程序已判确定不同，不需要 Agent。"""
    base = {"value": "8.2", "unit": "kpc"}
    plan, gold, preds = _bundle(
        [
            (
                "g1",
                STD,
                {**base, "error_components": [{"name": "sys", "error": "0.3"}]},
                [_fragment("gf", "text", ["error_components"])],
            )
        ],
        [
            (
                "p1",
                STD,
                {
                    **base,
                    "error_components": [{"name": "sys", "lower_error": "0.3"}],
                },
                [_fragment("pf", "text", ["error_components"])],
            )
        ],
    )
    mock = MockAgentAdapter(
        lambda request: AgentResponse(
            verdict="equivalent", rationale="x", source_citations=["left:gf"]
        )
    )
    report = score_batch(
        plan, gold, preds, auxiliary=protocol_only_config(), agent_adapter=mock
    )
    assert mock.calls == []
    paper = report.papers[0]
    assert paper.l2.precision == 0.0
    assert report.batch_provisional is False


# ---------------------------------------------------------------------------
# 多组件合并优先级——确定 different 优先
# ---------------------------------------------------------------------------


def test_insufficient_then_determined_different_wins():
    """value 缺证据 + error 明确 different：最终不匹配，不暂定。"""
    plan, gold, preds = _bundle(
        [
            (
                "g1",
                STD,
                {"value": "8.2", "error": "0.3", "unit": "kpc"},
                [_fragment("gf", "error only", ["error"])],
            )
        ],
        [
            (
                "p1",
                STD,
                {"value": "\\mystery{9.9}", "error": "\\mystery{0.3}", "unit": "kpc"},
                [_fragment("pf", "error only", ["error"])],
            )
        ],
    )
    mock = MockAgentAdapter(
        lambda request: AgentResponse(
            verdict="different", rationale="x", source_citations=["left:gf"]
        )
    )
    report = score_batch(
        plan, gold, preds, auxiliary=protocol_only_config(), agent_adapter=mock
    )
    paper = report.papers[0]
    assert [c["field_name"] for c in mock.calls] == ["error"]  # 只调用有证据字段
    assert any(
        row.status == "value_mismatch" and row.fields == ["error"]
        for row in _l2_rows(paper)
    )
    assert paper.l2.precision == 0.0
    assert report.batch_provisional is False


def test_technical_failure_then_determined_different_wins():
    """value 技术失败 + error 明确 different：最终不匹配，失败事实保留。"""
    plan, gold, preds = _bundle(
        [
            (
                "g1",
                STD,
                {"value": "8.2", "error": "0.3", "unit": "kpc"},
                [_fragment("gf", "both", ["value", "error"])],
            )
        ],
        [
            (
                "p1",
                STD,
                {"value": "\\mystery{9.9}", "error": "\\mystery{0.3}", "unit": "kpc"},
                [_fragment("pf", "both", ["value", "error"])],
            )
        ],
    )

    def fail_value_then_different(request):
        if request.field_name == "value":
            raise RuntimeError("simulated timeout")
        return AgentResponse(
            verdict="different", rationale="x", source_citations=["left:gf"]
        )

    mock = MockAgentAdapter(fail_value_then_different)
    report = score_batch(
        plan, gold, preds, auxiliary=protocol_only_config(), agent_adapter=mock
    )
    paper = report.papers[0]
    assert any(
        row.status == "value_mismatch" and row.fields == ["error"]
        for row in _l2_rows(paper)
    )
    assert paper.l2.precision == 0.0
    assert report.batch_provisional is False  # 失败不再影响得分
    assert report.auxiliary.technical_failures == 1  # 失败事实保留


def test_reversed_field_combination_determined_different_wins():
    """error 缺证据 + value 明确 different（反序组合）：最终不匹配。"""
    plan, gold, preds = _bundle(
        [
            (
                "g1",
                STD,
                {"value": "8.2", "error": "0.3", "unit": "kpc"},
                [_fragment("gf", "value only", ["value"])],
            )
        ],
        [
            (
                "p1",
                STD,
                {"value": "\\mystery{9.9}", "error": "\\mystery{0.3}", "unit": "kpc"},
                [_fragment("pf", "value only", ["value"])],
            )
        ],
    )
    mock = MockAgentAdapter(
        lambda request: AgentResponse(
            verdict="different", rationale="x", source_citations=["left:gf"]
        )
    )
    report = score_batch(
        plan, gold, preds, auxiliary=protocol_only_config(), agent_adapter=mock
    )
    paper = report.papers[0]
    assert [c["field_name"] for c in mock.calls] == ["value"]
    assert any(
        row.status == "value_mismatch" and row.fields == ["value"]
        for row in _l2_rows(paper)
    )
    assert paper.l2.precision == 0.0
    assert report.batch_provisional is False


def test_all_unknown_stays_provisional():
    """全部字段未知且无确定差异：仍暂定。"""
    plan, gold, preds = _bundle(
        [
            (
                "g1",
                STD,
                {"value": "8.2", "error": "0.3", "unit": "kpc"},
                [],
            )
        ],
        [
            (
                "p1",
                STD,
                {"value": "\\mystery{9.9}", "error": "\\mystery{0.3}", "unit": "kpc"},
                [],
            )
        ],
    )
    report = _score(plan, gold, preds)
    paper = report.papers[0]
    assert paper.l2.precision is None
    assert report.batch_provisional is True
    assert any(row.status == "unresolved_value" for row in _l2_rows(paper))


def test_program_difference_needs_no_agent():
    """程序已有确定不同：不需要 Agent 去推翻。"""
    plan, gold, preds = _bundle(
        [("g1", STD, {"value": "8.2", "error": "0.3", "unit": "kpc"}, [])],
        [("p1", STD, {"value": "9.9", "error": "\\mystery{0.3}", "unit": "kpc"}, [])],
    )
    mock = MockAgentAdapter(
        lambda request: AgentResponse(
            verdict="equivalent", rationale="x", source_citations=["left:g1"]
        )
    )
    report = _score(plan, gold, preds, agent_adapter=mock)
    assert mock.calls == []
    paper = report.papers[0]
    assert paper.l2.precision == 0.0
    assert report.batch_provisional is False


def test_unaffecting_failure_does_not_pollute_batch():
    """不影响最终匹配的候选失败：失败事实保留，整批不暂定。"""
    plan, gold, preds = _bundle(
        [
            (
                "g1",
                STD,
                {"value": "8.2", "error": "0.3", "unit": "kpc"},
                [_fragment("gf", "value text", ["value"])],
            )
        ],
        [
            ("p1", STD, {"value": "8.2", "error": "0.3", "unit": "kpc"}, []),
            (
                "p2",
                STD,
                {"value": "\\mystery{8.2}", "error": "0.3", "unit": "kpc"},
                [_fragment("pf", "macro def", ["value"])],
            ),
        ],
    )

    def always_fail(request):
        raise RuntimeError("simulated timeout")

    mock = MockAgentAdapter(always_fail)
    report = score_batch(
        plan, gold, preds, auxiliary=protocol_only_config(), agent_adapter=mock
    )
    paper = report.papers[0]
    assert (paper.l2.correct, paper.l2.predicted, paper.l2.gold) == (1, 2, 1)
    assert paper.l2.precision == 0.5
    assert report.batch_provisional is False
    assert report.auxiliary.technical_failures == 1  # 失败事实仍保留
    statuses = [row.status for row in _l2_rows(paper)]
    assert "matched" in statuses and "unknown_candidate_extra" in statuses


def test_affecting_failure_still_provisional():
    """影响得分的失败仍如实暂定（不得借优先级吞掉真实未决）。"""
    plan, gold, preds = _bundle(
        [
            (
                "g1",
                STD,
                {"value": "8.2", "error": "0.3", "unit": "kpc"},
                [_fragment("gf", "value text", ["value"])],
            )
        ],
        [
            (
                "p1",
                STD,
                {"value": "\\mystery{8.2}", "error": "0.3", "unit": "kpc"},
                [_fragment("pf", "macro def", ["value"])],
            )
        ],
    )

    def always_fail(request):
        raise RuntimeError("simulated timeout")

    mock = MockAgentAdapter(always_fail)
    report = score_batch(
        plan, gold, preds, auxiliary=protocol_only_config(), agent_adapter=mock
    )
    paper = report.papers[0]
    assert paper.l2.correct == 0
    assert paper.l2.precision is None
    assert report.batch_provisional is True
    assert report.auxiliary.technical_failures == 1

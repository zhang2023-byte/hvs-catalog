"""评分回归：入口输入重校验的独立构造反例（合成材料）。

覆盖：同一确定身份池的贡献类型/作者判断诊断完整且不依赖列表顺序；
公开 ``score_batch`` 在任何索引/Agent/缓存操作前复核当前嵌套输入的
结构有效性，拒绝构造后变异产生的重复 ID 等约束违反，不静默覆盖、
不改写调用者输入。全部走完整 score_batch 入口。
"""

from __future__ import annotations

import copy
from pathlib import Path

import pytest
from scoring_sample_helpers import load_bundle

from hvs_catalog.evaluation.scoring import (
    AgentResponse,
    MockAgentAdapter,
    gold_input_digest,
    protocol_only_config,
    score_batch,
)
from hvs_catalog.evaluation.scoring.errors import ScoringInputError

S08 = "synthetic-s08"


def _l2_rows(paper):
    return [row for row in paper.pairings if row.layer == "l2"]


def _s08_diagnostic_bundle(
    *,
    dup_contribution="previous_work",
    dup_author="excluded",
    matched_contribution="this_paper",
    matched_author="included",
    gold_contribution="this_paper",
    gold_author="included",
):
    """S08 变体：Gold 与首条预测的诊断字段一致，重复条目按参数设定。"""
    plan, gold, preds, auxiliary = load_bundle(S08)
    gold[0].objects[0].contribution_type = gold_contribution
    gold[0].objects[0].author_judgment = gold_author
    preds[0].objects[0].contribution_type = matched_contribution
    preds[0].objects[0].author_judgment = matched_author
    preds[0].objects[1].contribution_type = dup_contribution
    preds[0].objects[1].author_judgment = dup_author
    plan = plan.model_copy(
        update={
            "gold_ref": plan.gold_ref.model_copy(
                update={"fingerprint": gold_input_digest(gold)}
            )
        }
    )
    return plan, gold, preds, auxiliary


def _score(plan, gold, preds, auxiliary, **kwargs):
    return score_batch(plan, gold, preds, auxiliary=auxiliary, **kwargs)


# ---------------------------------------------------------------------------
# 池内全部预测的贡献类型/作者判断诊断
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("reverse", [False, True])
def test_duplicate_pool_diagnostics_complete_and_order_independent(reverse):
    """一条与 Gold 一致、一条不同：正反序都定位同一实际错误条目。"""
    plan, gold, preds, auxiliary = _s08_diagnostic_bundle()
    if reverse:
        preds = copy.deepcopy(preds)
        preds[0].objects = list(reversed(preds[0].objects))
    report = _score(plan, gold, preds, auxiliary)
    paper = report.papers[0]
    mismatch = [d for d in paper.diagnostics if d.code.endswith("_mismatch")]
    codes = sorted(d.code for d in mismatch)
    assert codes == ["author_judgment_mismatch", "contribution_type_mismatch"]
    for diagnostic in mismatch:
        # 逐条定位实际错误条目（重复预测），不是首条一致预测
        assert diagnostic.refs == ["A", "A-dup"]
    # 独立诊断绝不混入质量计数
    assert (paper.l1.tp, paper.l1.fp, paper.l1.fn) == (1, 1, 0)
    assert (paper.l2.correct, paper.l2.predicted, paper.l2.gold) == (1, 2, 1)
    assert report.batch_provisional is False


def test_duplicate_pool_diagnostics_survive_internal_renaming():
    """内部编号/label 重命名后仍定位同一实际错误条目。"""
    plan, gold, preds, auxiliary = _s08_diagnostic_bundle()
    preds = copy.deepcopy(preds)
    preds[0].objects[1].label = "Z-dup"
    preds[0].objects[1].quantities[0].values[0].record_id = "z9"
    report = _score(plan, gold, preds, auxiliary)
    paper = report.papers[0]
    mismatch = [d for d in paper.diagnostics if d.code.endswith("_mismatch")]
    assert sorted(d.code for d in mismatch) == [
        "author_judgment_mismatch",
        "contribution_type_mismatch",
    ]
    assert all(d.refs == ["A", "Z-dup"] for d in mismatch)
    assert (paper.l2.correct, paper.l2.predicted, paper.l2.gold) == (1, 2, 1)


def test_duplicate_pool_identical_fields_no_diagnostics():
    """重复条目与 Gold 完全一致：无诊断。"""
    plan, gold, preds, auxiliary = _s08_diagnostic_bundle(
        dup_contribution="this_paper", dup_author="included"
    )
    report = _score(plan, gold, preds, auxiliary)
    paper = report.papers[0]
    assert not [d for d in paper.diagnostics if d.code.endswith("_mismatch")]
    assert (paper.l1.tp, paper.l1.fp, paper.l1.fn) == (1, 1, 0)
    assert (paper.l2.correct, paper.l2.predicted, paper.l2.gold) == (1, 2, 1)


def test_single_field_or_missing_field_semantics_preserved():
    """只有一个字段不同→仅该诊断；预测字段缺失（None）→不诊断（既有语义）。"""
    plan, gold, preds, auxiliary = _s08_diagnostic_bundle(dup_contribution="this_paper")
    report = _score(plan, gold, preds, auxiliary)
    mismatch = [d for d in report.papers[0].diagnostics if d.code.endswith("_mismatch")]
    assert [d.code for d in mismatch] == ["author_judgment_mismatch"]

    plan, gold, preds, auxiliary = _s08_diagnostic_bundle(
        dup_contribution=None, dup_author=None
    )
    report = _score(plan, gold, preds, auxiliary)
    mismatch = [d for d in report.papers[0].diagnostics if d.code.endswith("_mismatch")]
    assert mismatch == []


def test_no_duplicate_diagnostics_for_same_field():
    """同一错误字段不重复记诊断：每条预测×每字段至多一次。"""
    plan, gold, preds, auxiliary = _s08_diagnostic_bundle()
    report = _score(plan, gold, preds, auxiliary)
    codes = [d.code for d in report.papers[0].diagnostics]
    assert len(codes) == len(set(codes))


def test_ambiguity_not_diagnosed_as_field_mismatch():
    """共享别名身份歧义不入池：不产生字段诊断，保持 pending。"""
    plan, gold, preds, auxiliary = load_bundle(S08)
    gold[0].objects[0].identifiers = ["SYN-A", "SYN-SHARED"]
    second_object = copy.deepcopy(gold[0].objects[0])
    second_object.label = "B"
    second_object.identifiers = ["SYN-B", "SYN-SHARED"]
    second_object.quantities[0].values[0].record_id = "g3"
    gold[0].objects.append(second_object)
    plan = plan.model_copy(
        update={
            "gold_ref": plan.gold_ref.model_copy(
                update={"fingerprint": gold_input_digest(gold)}
            )
        }
    )
    preds[0].objects[0].contribution_type = "this_paper"
    preds[0].objects[1].label = "P"
    preds[0].objects[1].identifiers = ["SYN-SHARED"]
    preds[0].objects[1].contribution_type = "previous_work"
    preds[0].objects[1].author_judgment = "excluded"
    report = _score(plan, gold, preds, auxiliary)
    paper = report.papers[0]
    assert any(d.code == "ambiguous_object_alias" for d in paper.diagnostics)
    # 歧义预测不做字段诊断（身份未决，不借诊断内容消歧）
    assert not [d for d in paper.diagnostics if d.code.endswith("_mismatch")]


# ---------------------------------------------------------------------------
# 入口复核当前实际输入，拒绝构造后变异
# ---------------------------------------------------------------------------


def test_post_construction_duplicate_record_id_rejected():
    """有效 S08 构造后把第二 record_id 改为 p1 → 拒绝。"""
    plan, gold, preds, auxiliary = load_bundle(S08)
    preds[0].objects[1].quantities[0].values[0].record_id = "p1"
    with pytest.raises(ScoringInputError):
        _score(plan, gold, preds, auxiliary)


def test_prediction_list_append_duplicate_rejected():
    """列表追加同 record_id 值条目（不经字段赋值）同样拒绝。"""
    plan, gold, preds, auxiliary = load_bundle(S08)
    duplicate = copy.deepcopy(preds[0].objects[1].quantities[0].values[0])
    duplicate.record_id = "p1"
    preds[0].objects[1].quantities[0].values.append(duplicate)
    with pytest.raises(ScoringInputError):
        _score(plan, gold, preds, auxiliary)


def test_prediction_duplicate_object_label_appended_rejected():
    plan, gold, preds, auxiliary = load_bundle(S08)
    clone = copy.deepcopy(preds[0].objects[0])
    preds[0].objects.append(clone)  # 论文内 label 重复（父级约束）
    with pytest.raises(ScoringInputError):
        _score(plan, gold, preds, auxiliary)


def test_gold_duplicate_value_appended_rejected_even_with_recomputed_digest():
    """Gold 侧重复 record_id 追加：即使指纹同步重算也不放行结构非法数据。"""
    plan, gold, preds, auxiliary = load_bundle(S08)
    duplicate = copy.deepcopy(gold[0].objects[0].quantities[0].values[0])
    gold[0].objects[0].quantities[0].values.append(duplicate)
    plan = plan.model_copy(
        update={
            "gold_ref": plan.gold_ref.model_copy(
                update={"fingerprint": gold_input_digest(gold)}
            )
        }
    )
    with pytest.raises(ScoringInputError):
        _score(plan, gold, preds, auxiliary)


def test_gold_duplicate_quantity_path_appended_rejected():
    plan, gold, preds, auxiliary = load_bundle(S08)
    gold[0].objects[0].quantities.append(
        copy.deepcopy(gold[0].objects[0].quantities[0])
    )
    plan = plan.model_copy(
        update={
            "gold_ref": plan.gold_ref.model_copy(
                update={"fingerprint": gold_input_digest(gold)}
            )
        }
    )
    with pytest.raises(ScoringInputError):
        _score(plan, gold, preds, auxiliary)


def test_gold_duplicate_identifiers_rejected():
    plan, gold, preds, auxiliary = load_bundle(S08)
    # 列表追加不经字段赋值校验：对象内标识重复绕过构造约束
    gold[0].objects[0].identifiers.append("SYN-A")
    plan = plan.model_copy(
        update={
            "gold_ref": plan.gold_ref.model_copy(
                update={"fingerprint": gold_input_digest(gold)}
            )
        }
    )
    with pytest.raises(ScoringInputError):
        _score(plan, gold, preds, auxiliary)


def test_valid_duplicate_values_still_score_normally():
    """正常全局唯一的重复值（S08 5.0/9.9）照常计分。"""
    plan, gold, preds, auxiliary = load_bundle(S08)
    report = _score(plan, gold, preds, auxiliary)
    paper = report.papers[0]
    assert (paper.l1.tp, paper.l1.fp, paper.l1.fn) == (1, 1, 0)
    assert (paper.l2.correct, paper.l2.predicted, paper.l2.gold) == (1, 2, 1)
    assert report.batch_provisional is False


def test_rejection_happens_before_agent_or_cache_side_effects(tmp_path: Path):
    """失败前不得调用 Agent、不得发布冻结条目。"""
    plan, gold, preds, _auxiliary = load_bundle(S08)
    store_directory = tmp_path / "frozen"
    mock = MockAgentAdapter(
        lambda request: AgentResponse(
            verdict="equivalent", rationale="x", source_citations=["left:g1"]
        )
    )
    preds[0].objects[1].quantities[0].values[0].record_id = "p1"
    with pytest.raises(ScoringInputError):
        score_batch(
            plan,
            gold,
            preds,
            auxiliary=protocol_only_config(store_directory=str(store_directory)),
            agent_adapter=mock,
        )
    assert mock.calls == []
    assert not list(store_directory.glob("*")) if store_directory.exists() else True


def test_caller_input_not_rewritten(tmp_path: Path):
    """成功评分不改写调用者输入（校验不写回）。"""
    plan, gold, preds, _auxiliary = load_bundle(S08)
    before = (
        plan.model_dump(mode="json"),
        [entry.model_dump(mode="json") for entry in gold],
        [entry.model_dump(mode="json") for entry in preds],
    )
    score_batch(
        plan,
        gold,
        preds,
        auxiliary=protocol_only_config(store_directory=str(tmp_path / "frozen")),
    )
    after = (
        plan.model_dump(mode="json"),
        [entry.model_dump(mode="json") for entry in gold],
        [entry.model_dump(mode="json") for entry in preds],
    )
    assert before == after


def test_frozen_reuse_and_idempotence_unaffected(tmp_path: Path):
    """正常输入的冻结复用与幂等无回归。"""
    plan, gold, preds, _auxiliary = load_bundle("synthetic-s07u1es")
    store_directory = str(tmp_path / "frozen")

    def make_adapter():
        return MockAgentAdapter(
            lambda request: AgentResponse(
                verdict="equivalent",
                rationale="x",
                source_citations=["left:table-1"],
            )
        )

    first_adapter = make_adapter()
    score_batch(
        plan,
        gold,
        preds,
        auxiliary=protocol_only_config(store_directory=store_directory),
        agent_adapter=first_adapter,
    )
    assert len(first_adapter.calls) == 1
    second_adapter = make_adapter()
    second = score_batch(
        plan,
        gold,
        preds,
        auxiliary=protocol_only_config(store_directory=store_directory),
        agent_adapter=second_adapter,
    )
    assert second_adapter.calls == []
    assert second.auxiliary.judgments[0].reused is True
    entries = list((tmp_path / "frozen").glob("*.json"))
    assert len(entries) == 1  # 幂等：不重复发布

"""评分回归：重校验副本贯穿后续流程（合成材料）。

``_revalidate_inputs`` 重建校验后丢弃副本、``score_batch`` 继续使用
可能已变异的原对象——bytes scope 反例把正确 C/P/G=1/2/1 变成 ready
的 0/0/0。修复后全部索引/摘要/Agent 请求/评分/报告统一使用重校验
副本，不写回调用者。全部走完整 score_batch 入口。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from scoring_sample_helpers import load_bundle

from hvs_catalog.evaluation.scoring import (
    AgentResponse,
    MockAgentAdapter,
    protocol_only_config,
    score_batch,
)
from hvs_catalog.evaluation.scoring.errors import ScoringInputError

S08 = "synthetic-s08"
DIST = "observed_phase_space.distance"


def _l2_rows(paper):
    return [row for row in paper.pairings if row.layer == "l2"]


def test_bytes_scope_scores_correctly_via_validated_copy():
    """构造后把 scope 路径改为 UTF-8 bytes，入口应按验证后的文本评分。

    JSON 序列化隐式归一为原字符串，重校验通过；修复前评分使用 bytes
    构成 effective_paths，C/P/G 从 1/2/1 变 0/0/0 且标 ready；修复后
    统一使用校验副本，正确计分 1/2/1。
    """
    plan, gold, preds, auxiliary = load_bundle(S08)
    plan.scopes[0].quantity_paths[0] = DIST.encode()
    report = score_batch(plan, gold, preds, auxiliary=auxiliary)
    paper = report.papers[0]
    assert paper.readiness == "ready"
    assert (paper.l2.correct, paper.l2.predicted, paper.l2.gold) == (1, 2, 1)
    assert paper.l2.precision == 0.5 and paper.l2.recall == 1.0
    assert paper.l2.f1 == pytest.approx(2 / 3)
    assert report.batch_provisional is False
    statuses = [row.status for row in _l2_rows(paper)]
    assert statuses == ["matched", "extra_value"]  # 不再错列 out_of_scope_value
    # 调用者原对象不被写回
    assert isinstance(plan.scopes[0].quantity_paths[0], bytes)


def test_bytes_scope_appended_duplicate_path_rejected_before_side_effects(
    tmp_path: Path,
):
    """追加 bytes 路径元素归一后与现有路径重复：入口拒绝，零副作用。"""
    plan, gold, preds, _auxiliary = load_bundle(S08)
    store_directory = tmp_path / "frozen"
    mock = MockAgentAdapter(
        lambda request: AgentResponse(
            verdict="equivalent", rationale="x", source_citations=["left:g1"]
        )
    )
    plan.scopes[0].quantity_paths.append(DIST.encode())  # 归一后重复
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
    # 调用者原对象不被写回
    assert isinstance(plan.scopes[0].quantity_paths[1], bytes)


def test_prediction_identifier_bytes_flows_through_copy():
    """预测对象标识列表元素变异为 bytes：副本归一后正确计分。"""
    plan, gold, preds, auxiliary = load_bundle(S08)
    preds[0].objects[1].identifiers[0] = b"SYN-A"  # 列表元素赋值不经校验
    report = score_batch(plan, gold, preds, auxiliary=auxiliary)
    paper = report.papers[0]
    assert (paper.l1.tp, paper.l1.fp, paper.l1.fn) == (1, 1, 0)
    assert (paper.l2.correct, paper.l2.predicted, paper.l2.gold) == (1, 2, 1)
    assert report.batch_provisional is False
    # 调用者原对象不被写回
    assert isinstance(preds[0].objects[1].identifiers[0], bytes)


def test_gold_identifier_bytes_flows_through_copy():
    """Gold 对象标识列表元素变异为 bytes：副本归一后正确计分。"""
    plan, gold, preds, auxiliary = load_bundle(S08)
    gold[0].objects[0].identifiers[0] = b"SYN-A"
    report = score_batch(plan, gold, preds, auxiliary=auxiliary)
    paper = report.papers[0]
    assert (paper.l1.tp, paper.l1.fp, paper.l1.fn) == (1, 1, 0)
    assert (paper.l2.correct, paper.l2.predicted, paper.l2.gold) == (1, 2, 1)
    assert report.batch_provisional is False
    assert isinstance(gold[0].objects[0].identifiers[0], bytes)


def test_gold_identifier_bytes_appended_duplicate_rejected():
    """Gold 标识追加 bytes 重复元素：归一后违反对象内唯一约束，拒绝。"""
    plan, gold, preds, auxiliary = load_bundle(S08)
    gold[0].objects[0].identifiers.append(b"SYN-A")
    with pytest.raises(ScoringInputError):
        score_batch(plan, gold, preds, auxiliary=auxiliary)
    assert isinstance(gold[0].objects[0].identifiers[1], bytes)


def test_validated_copies_used_for_digests_and_agent_context(tmp_path: Path):
    """指纹核对与 Agent 上下文/请求使用同一副本内容：冻结复用不受影响。"""
    plan, gold, preds, _auxiliary = load_bundle("synthetic-s07u1es")
    store_directory = str(tmp_path / "frozen")
    # 构造后变异（可归一）：副本归一后与冻结键一致，复用正常
    plan.scopes[0].quantity_paths[0] = plan.scopes[0].quantity_paths[0].encode()

    def make_adapter():
        return MockAgentAdapter(
            lambda request: AgentResponse(
                verdict="equivalent",
                rationale="x",
                source_citations=["left:table-1"],
            )
        )

    first_adapter = make_adapter()
    first = score_batch(
        plan,
        gold,
        preds,
        auxiliary=protocol_only_config(store_directory=store_directory),
        agent_adapter=first_adapter,
    )
    assert len(first_adapter.calls) == 1
    assert first.papers[0].l2.correct == 1  # 副本正确计分
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
    assert len(list((tmp_path / "frozen").glob("*.json"))) == 1


def test_caller_inputs_untouched_after_successful_scoring():
    """成功评分（含变异输入）前后调用者对象内容完全不变。"""
    plan, gold, preds, auxiliary = load_bundle(S08)
    plan.scopes[0].quantity_paths[0] = DIST.encode()
    preds[0].objects[1].identifiers[0] = b"SYN-A"
    snapshot = (
        [type(item).__name__ for item in plan.scopes[0].quantity_paths],
        [type(name).__name__ for name in preds[0].objects[1].identifiers],
    )
    report = score_batch(plan, gold, preds, auxiliary=auxiliary)
    assert report.papers[0].l2.correct == 1
    assert snapshot == (
        [type(item).__name__ for item in plan.scopes[0].quantity_paths],
        [type(name).__name__ for name in preds[0].objects[1].identifiers],
    )


def test_duplicate_record_id_rejection_and_pool_semantics_unaffected():
    """既有契约不回归：重复 record_id 仍拒绝；合法重复值同序计分。"""
    plan, gold, preds, auxiliary = load_bundle(S08)
    preds[0].objects[1].quantities[0].values[0].record_id = "p1"
    with pytest.raises(ScoringInputError):
        score_batch(plan, gold, preds, auxiliary=auxiliary)

    plan, gold, preds, auxiliary = load_bundle(S08)
    reversed_objs = list(reversed(preds[0].objects))
    preds[0].objects = reversed_objs
    report = score_batch(plan, gold, preds, auxiliary=auxiliary)
    paper = report.papers[0]
    assert (paper.l1.tp, paper.l1.fp, paper.l1.fn) == (1, 1, 0)
    assert (paper.l2.correct, paper.l2.predicted, paper.l2.gold) == (1, 2, 1)

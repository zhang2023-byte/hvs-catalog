"""评分回归：输入校验、冻结状态与范围统计的独立构造反例（合成材料）。

覆盖：内容键不同不得绕过按域三态、跨归属未知关系保留并参与匹配
界、材料不足不因注入适配器而变确定、证据片段绑定待判字段、冻结
请求绑定完整计划且缓存上下文不可单侧篡改、写冲突后内存不得复用
被拒判定、证据统计与 scoped L1/L2 同范围。
全部走完整 score_batch 入口或真实内部入口，不用辅助捷径掩盖分支。
"""

import hashlib
import json
import threading
from pathlib import Path

import pytest
from scoring_sample_helpers import (
    DIST,
    HELI,
    M1,
    M1_REWRITE,
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
    AgentRequest,
    AgentResponse,
    FrozenDecisionStore,
    FrozenStoreError,
    MockAgentAdapter,
    build_judgment,
    build_measured_value,
    protocol_only_config,
    score_batch,
)
from hvs_catalog.evaluation.scoring.agent import AuxiliaryAdjudicator
from hvs_catalog.evaluation.scoring.models import (
    EvidenceFragment,
    PredictionQuantityEntry,
    PredictionValueEntry,
)
from hvs_catalog.evaluation.scoring.printed import compare_number, compare_unit

MATERIALS_NAME = "synthetic-materials"
MATERIALS_FP = hashlib.sha256(b"synthetic-materials").hexdigest()


def _l2_rows(paper):
    return [row for row in paper.pairings if row.layer == "l2"]


# ---------------------------------------------------------------------------
# 内容键不同不能证明科学不同，必须走按域三态
# ---------------------------------------------------------------------------


def test_unsupported_number_without_macro_is_unknown_not_mismatch():
    plan, gold, preds = _bundle(
        [("g1", STD, {"value": "about eight", "unit": "kpc"}, [])],
        [("p1", STD, {"value": "eight", "unit": "kpc"}, [])],
    )
    report = _score(plan, gold, preds)
    paper = report.papers[0]
    assert compare_number("about eight", "eight") == "undecidable"
    assert paper.l2.correct == 0
    assert paper.l2.precision is None  # 不能最终判 0
    assert report.batch_provisional is True
    assert any(
        row.status == "unresolved_value" and row.fields == ["value"]
        for row in _l2_rows(paper)
    )
    assert not any(row.status == "value_mismatch" for row in _l2_rows(paper))


def test_unsupported_unit_syntax_is_unknown_not_mismatch():
    plan, gold, preds = _bundle(
        [("g1", STD, {"value": "600", "error": "20", "unit": "km s^{-1}"}, [])],
        [("p1", STD, {"value": "600", "error": "20", "unit": "km/s"}, [])],
    )
    report = _score(plan, gold, preds)
    paper = report.papers[0]
    assert compare_unit("km s^{-1}", "km/s") == "undecidable"
    assert paper.l2.precision is None
    assert report.batch_provisional is True
    assert any(
        row.status == "unresolved_value" and row.fields == ["unit"]
        for row in _l2_rows(paper)
    )


def test_supported_differences_remain_determined():
    """已支持的确定差异仍最终判不同：修复不能把确定差异也变未知。"""
    plan, gold, preds = _bundle(
        [("g1", STD, {"value": "8.2", "error": "0.3", "unit": "kpc"}, [])],
        [("p1", STD, {"value": "8200", "error": "0.3", "unit": "pc"}, [])],
    )
    report = _score(plan, gold, preds)
    paper = report.papers[0]
    assert paper.l2.correct == 0
    assert paper.l2.precision == 0.0 and paper.l2.recall == 0.0
    assert report.batch_provisional is False
    assert any(
        row.status == "value_mismatch" and set(row.fields) == {"unit", "value"}
        for row in _l2_rows(paper)
    )


# ---------------------------------------------------------------------------
# 跨归属未知关系保留，参与整体最大匹配界
# ---------------------------------------------------------------------------


def test_cross_condition_unknown_candidate_blocks_final_zero():
    """跨归属未知关系：G1(M1,8)、G2(未支持改写条件,10)、P(M1,10)。

    P→G2 归属未知是可能边；对应 G2 时 C 可能为 1，不能最终判 0。
    数值相同（10=10）不是条件等价证据。
    """
    plan, gold, preds = _bundle(
        [
            ("g1", M1, {"value": "8", "unit": "kpc"}, []),
            ("g2", M1_REWRITE, {"value": "10", "unit": "kpc"}, []),
        ],
        [("p1", M1, {"value": "10", "unit": "kpc"}, [])],
    )
    report = _score(plan, gold, preds)
    paper = report.papers[0]
    assert (paper.l2.correct, paper.l2.predicted, paper.l2.gold) == (0, 1, 2)
    assert paper.l2.precision is None
    assert report.batch_provisional is True
    assert any(row.status == "unresolved_attribution" for row in _l2_rows(paper))
    assert not any(row.status == "value_mismatch" for row in _l2_rows(paper))


def test_cross_condition_reordering_keeps_counts_and_provisional():
    """重排 Gold/预测列表不改变 C/P/G 与未决状态。"""
    gold_specs = [
        ("g1", M1, {"value": "8", "unit": "kpc"}, []),
        ("g2", M1_REWRITE, {"value": "10", "unit": "kpc"}, []),
    ]
    pred_specs = [
        ("p1", M1, {"value": "10", "unit": "kpc"}, []),
        ("p2", M1_REWRITE, {"value": "10", "unit": "kpc"}, []),
    ]
    forward = _score(*_bundle(gold_specs, pred_specs))
    backward = _score(*_bundle(list(reversed(gold_specs)), list(reversed(pred_specs))))
    one, two = forward.papers[0], backward.papers[0]
    assert (one.l2.correct, one.l2.predicted, one.l2.gold) == (1, 2, 2)
    assert (two.l2.correct, two.l2.predicted, two.l2.gold) == (1, 2, 2)
    assert one.l2.precision is None and two.l2.precision is None
    assert forward.batch_provisional and backward.batch_provisional


def test_multiple_condition_groups_attribution_correspondence_uncertain():
    """C 已达上界但归属未知边可能改变对应关系时仍保持未决。"""
    plan, gold, preds = _bundle(
        [
            ("g1", M1, {"value": "8", "unit": "kpc"}, []),
            ("g2", M2, {"value": "10", "unit": "kpc"}, []),
            ("g3", M1_REWRITE, {"value": "12", "unit": "kpc"}, []),
        ],
        [
            ("p1", M1, {"value": "8", "unit": "kpc"}, []),
            ("p2", M2, {"value": "10", "unit": "kpc"}, []),
            ("p3", M1_REWRITE, {"value": "12", "unit": "kpc"}, []),
        ],
    )
    report = _score(plan, gold, preds)
    paper = report.papers[0]
    assert (paper.l2.correct, paper.l2.predicted, paper.l2.gold) == (3, 3, 3)
    assert paper.l2.precision is None  # 对应关系不确定，不能给最终率
    assert report.batch_provisional is True
    assert (
        sum(1 for row in _l2_rows(paper) if row.status == "unresolved_attribution") >= 1
    )


def test_agent_can_resolve_unknown_attribution_both_directions():
    """归属未知可经 Agent 确认：different 禁边、equivalent 允许比较内容。"""
    gold_fragments = [
        _fragment("gold-cond", "under Galactic potential M1", ["condition"])
    ]
    pred_fragments = [
        _fragment(
            "pred-cond",
            "under Galactic potential M1 (same adopted model)",
            ["condition"],
        )
    ]
    plan, gold, preds = _bundle(
        [
            ("g1", M1, {"value": "8", "unit": "kpc"}, gold_fragments),
            ("g2", M1_REWRITE, {"value": "10", "unit": "kpc"}, gold_fragments),
        ],
        [("p1", M1, {"value": "10", "unit": "kpc"}, pred_fragments)],
    )
    rejected = MockAgentAdapter(
        lambda request: AgentResponse(
            verdict="different", rationale="x", source_citations=["left:gold-cond"]
        )
    )
    first = score_batch(
        plan, gold, preds, auxiliary=protocol_only_config(), agent_adapter=rejected
    )
    assert len(rejected.calls) == 1
    paper = first.papers[0]
    assert (paper.l2.correct, paper.l2.predicted, paper.l2.gold) == (0, 1, 2)
    assert paper.l2.precision == 0.0  # 条件被拒后仅剩 g1（值不同），确定 0
    assert first.batch_provisional is False
    accepted = MockAgentAdapter(
        lambda request: AgentResponse(
            verdict="equivalent", rationale="x", source_citations=["left:gold-cond"]
        )
    )
    second = score_batch(
        plan, gold, preds, auxiliary=protocol_only_config(), agent_adapter=accepted
    )
    assert len(accepted.calls) == 1
    other = second.papers[0]
    assert (other.l2.correct, other.l2.predicted, other.l2.gold) == (1, 1, 2)
    assert other.l2.precision == 1.0  # 对应 g2 时内容相等，确定 1
    assert second.batch_provisional is False


def test_b8_style_swap_stays_determined_zero():
    """B8 交换：归属全部确定、内容全部确定不同，最终 C=0 不变。"""
    plan, gold, preds = _bundle(
        [
            ("g1", M1, {"value": "8", "unit": "kpc"}, []),
            ("g2", M2, {"value": "10", "unit": "kpc"}, []),
        ],
        [
            ("p1", M1, {"value": "10", "unit": "kpc"}, []),
            ("p2", M2, {"value": "8", "unit": "kpc"}, []),
        ],
    )
    report = _score(plan, gold, preds)
    paper = report.papers[0]
    assert (paper.l2.correct, paper.l2.predicted, paper.l2.gold) == (0, 2, 2)
    assert paper.l2.precision == 0.0 and paper.l2.recall == 0.0
    assert report.batch_provisional is False
    assert sum(1 for row in _l2_rows(paper) if row.status == "value_mismatch") == 2


# ---------------------------------------------------------------------------
# 材料不足不因注入适配器而变确定（有/无 mock 不变量）
# ---------------------------------------------------------------------------


def test_u2_with_unused_mock_matches_no_mock_result():
    plan, gold, preds, auxiliary = load_bundle("synthetic-s07u2")
    without = score_batch(plan, gold, preds, auxiliary=auxiliary)
    mock = MockAgentAdapter(
        lambda request: AgentResponse(
            verdict="different",
            rationale="must never be used",
            source_citations=["left:g1"],
        )
    )
    with_mock = score_batch(
        plan,
        gold,
        preds,
        auxiliary=protocol_only_config(),
        agent_adapter=mock,
    )
    assert mock.calls == []
    one, two = without.papers[0], with_mock.papers[0]
    assert one.pairings == two.pairings
    assert (one.l2.correct, one.l2.predicted, one.l2.gold) == (
        two.l2.correct,
        two.l2.predicted,
        two.l2.gold,
    )
    assert one.l2.precision is None and two.l2.precision is None
    assert without.batch_provisional and with_mock.batch_provisional
    assert not any(
        row.status in {"attribution_mismatch", "value_mismatch"}
        for row in _l2_rows(two)
    )
    assert not any(
        row.status in {"attribution_mismatch", "value_mismatch"}
        for row in _l2_rows(one)
    )


def test_insufficient_condition_evidence_reports_no_adapter_call():
    """无片段的条件未知：insufficient_evidence 保持未决且不调用。"""
    plan, gold, preds = _bundle(
        [("g1", M1, {"value": "8", "unit": "kpc"}, [])],
        [("p1", M1_REWRITE, {"value": "8", "unit": "kpc"}, [])],
    )
    mock = MockAgentAdapter(
        lambda request: AgentResponse(
            verdict="equivalent", rationale="x", source_citations=["left:g1"]
        )
    )
    report = _score(plan, gold, preds, agent_adapter=mock)
    assert mock.calls == []
    paper = report.papers[0]
    assert paper.l2.precision is None
    assert report.batch_provisional is True
    assert any(row.status == "unresolved_attribution" for row in _l2_rows(paper))


# ---------------------------------------------------------------------------
# 证据片段绑定待判字段
# ---------------------------------------------------------------------------


def test_value_fragments_cannot_support_error_judgment():
    """字段证据不匹配：value 片段 + error 未知宏 → 不得调用，保持未知。"""
    gold_fragments = [_fragment("table-1", "The distance is 8.2 kpc.", ["value"])]
    pred_fragments = [
        _fragment("macro-def", "\\newcommand{\\mystery}[1]{#1}", ["value"])
    ]
    plan, gold, preds = _bundle(
        [
            (
                "g1",
                STD,
                {"value": "8.2", "error": "0.3", "unit": "kpc"},
                gold_fragments,
            )
        ],
        [
            (
                "p1",
                STD,
                {"value": "8.2", "error": "\\mystery{0.3}", "unit": "kpc"},
                pred_fragments,
            )
        ],
    )
    mock = MockAgentAdapter(
        lambda request: AgentResponse(
            verdict="equivalent", rationale="x", source_citations=["left:table-1"]
        )
    )
    report = _score(plan, gold, preds, agent_adapter=mock)
    assert mock.calls == []
    paper = report.papers[0]
    assert paper.l2.correct == 0
    assert paper.l2.precision is None
    assert report.batch_provisional is True
    assert any(
        row.status == "unresolved_value" and row.fields == ["error"]
        for row in _l2_rows(paper)
    )


def test_error_field_fragments_allow_mock_resolution():
    """error 字段证据充足（u1es 固定样例）：mock 可判定并给出最终率。"""
    plan, gold, preds, _auxiliary = load_bundle("synthetic-s07u1es")
    mock = MockAgentAdapter(
        lambda request: AgentResponse(
            verdict="equivalent", rationale="x", source_citations=["left:table-1"]
        )
    )
    report = score_batch(
        plan,
        gold,
        preds,
        auxiliary=protocol_only_config(store_directory=None),
        agent_adapter=mock,
    )
    paper = report.papers[0]
    assert len(mock.calls) == 1
    assert paper.l2.correct == 1
    assert paper.l2.precision == 1.0
    assert report.batch_provisional is False


def test_error_field_insufficient_frozen_sample_stays_unknown():
    """error 证据不足（u1ei 固定样例）：有 mock 也不调用、保持未知。"""
    plan, gold, preds, _auxiliary = load_bundle("synthetic-s07u1ei")
    mock = MockAgentAdapter(
        lambda request: AgentResponse(
            verdict="equivalent", rationale="x", source_citations=["left:table-1"]
        )
    )
    report = score_batch(
        plan,
        gold,
        preds,
        auxiliary=protocol_only_config(store_directory=None),
        agent_adapter=mock,
    )
    paper = report.papers[0]
    assert mock.calls == []
    assert paper.l2.precision is None
    assert report.batch_provisional is True


def test_fragment_support_declaration_semantics():
    """片段支持声明：前缀覆盖子字段，声明域受限，value 不覆盖 error。"""
    component = _fragment("comp", "sys err text", ["error_components"])
    assert component.supports("error_components")
    assert component.supports("error_components.sys.err")
    assert not component.supports("error")
    value_only = _fragment("val", "8.2 kpc", ["value"])
    assert value_only.supports("value")
    assert not value_only.supports("error")
    try:
        EvidenceFragment.model_validate(
            {
                "fragment_id": "bad",
                "text": "t",
                "location": "l",
                "material_name": "m",
                "material_fingerprint": "0" * 64,
                "supports_fields": ["nonexistent_field"],
            }
        )
        raise AssertionError("未知支持域应被拒绝")
    except ValueError:
        pass


# ---------------------------------------------------------------------------
# 冻结请求绑定完整计划；缓存上下文不可单侧篡改
# ---------------------------------------------------------------------------


def _u1es_adapter():
    return MockAgentAdapter(
        lambda request: AgentResponse(
            verdict="equivalent", rationale="x", source_citations=["left:table-1"]
        )
    )


def test_scope_change_invalidates_frozen_entry(tmp_path: Path):
    plan, gold, preds, _auxiliary = load_bundle("synthetic-s07u1es")
    store_directory = str(tmp_path / "frozen")
    first = score_batch(
        plan,
        gold,
        preds,
        auxiliary=protocol_only_config(store_directory=store_directory),
        agent_adapter=_u1es_adapter(),
    )
    assert (
        len(first.auxiliary.judgments) == 1 and not first.auxiliary.judgments[0].reused
    )
    # 同输入重复评分：复用冻结，不再次调用
    second_adapter = _u1es_adapter()
    second = score_batch(
        plan,
        gold,
        preds,
        auxiliary=protocol_only_config(store_directory=store_directory),
        agent_adapter=second_adapter,
    )
    assert second_adapter.calls == []
    assert second.auxiliary.judgments[0].reused is True
    # 仅扩展评价范围：计划摘要变化，旧 key 不可复用
    widened = plan.model_copy(
        update={
            "scopes": [
                scope.model_copy(update={"quantity_paths": scope.quantity_paths + [RV]})
                for scope in plan.scopes
            ]
        }
    )
    third_adapter = _u1es_adapter()
    third = score_batch(
        widened,
        gold,
        preds,
        auxiliary=protocol_only_config(store_directory=store_directory),
        agent_adapter=third_adapter,
    )
    assert len(third_adapter.calls) == 1
    assert third.auxiliary.judgments[0].reused is False


def test_evidence_registry_change_invalidates_frozen_entry(tmp_path: Path):
    plan, gold, preds, _auxiliary = load_bundle("synthetic-s07u1es")
    store_directory = str(tmp_path / "frozen")
    score_batch(
        plan,
        gold,
        preds,
        auxiliary=protocol_only_config(store_directory=store_directory),
        agent_adapter=_u1es_adapter(),
    )
    changed = plan.model_copy(
        update={"known_evidence_record_ids": ["another-evidence-record"]}
    )
    adapter = _u1es_adapter()
    report = score_batch(
        changed,
        gold,
        preds,
        auxiliary=protocol_only_config(store_directory=store_directory),
        agent_adapter=adapter,
    )
    assert len(adapter.calls) == 1
    assert report.auxiliary.judgments[0].reused is False


def test_tampered_cache_context_digests_rejected(tmp_path: Path):
    plan, gold, preds, _auxiliary = load_bundle("synthetic-s07u1es")
    store_directory = tmp_path / "frozen"
    score_batch(
        plan,
        gold,
        preds,
        auxiliary=protocol_only_config(store_directory=str(store_directory)),
        agent_adapter=_u1es_adapter(),
    )
    entry_path = next(store_directory.glob("*.json"))
    data = json.loads(entry_path.read_text("utf-8"))
    data["context_digests"]["gold"] = "0" * 64  # 与 request 内摘要不一致
    entry_path.write_text(json.dumps(data), encoding="utf-8")
    adapter = _u1es_adapter()
    try:
        score_batch(
            plan,
            gold,
            preds,
            auxiliary=protocol_only_config(store_directory=str(store_directory)),
            agent_adapter=adapter,
        )
        raise AssertionError("被篡改的缓存上下文应导致拒绝")
    except FrozenStoreError:
        pass


def test_frozen_request_carries_plan_digest(tmp_path: Path):
    plan, gold, preds, _auxiliary = load_bundle("synthetic-s07u1es")
    store_directory = tmp_path / "frozen"
    score_batch(
        plan,
        gold,
        preds,
        auxiliary=protocol_only_config(store_directory=str(store_directory)),
        agent_adapter=_u1es_adapter(),
    )
    entry = json.loads(next(store_directory.glob("*.json")).read_text("utf-8"))
    digests = entry["request"]["context_digests"]
    assert digests == entry["context_digests"]
    assert "scoring_plan" in digests
    from hvs_catalog.records.common import sha256_of

    assert digests["scoring_plan"] == sha256_of(plan.model_dump(mode="json"))


# ---------------------------------------------------------------------------
# 写冲突后不得在同实例缓存被拒判定
# ---------------------------------------------------------------------------


def _evidence_bound_request() -> AgentRequest:
    _plan, gold, preds, _ = load_bundle("synthetic-s07u1es")
    gold_fragments = gold[0].objects[0].quantities[0].values[0].evidence_fragments
    pred_fragments = preds[0].objects[0].quantities[0].values[0].evidence_fragments
    digests = {
        name: hashlib.sha256(name.encode()).hexdigest()
        for name in (
            "gold",
            "predictions",
            "materials",
            "science_rules",
            "scoring_scheme",
            "program",
            "auxiliary",
            "scoring_plan",
        )
    }
    return AgentRequest(
        request_kind="printed_expression",
        field_name="error",
        left="0.3",
        right="\\mystery{0.3}",
        left_fragments=gold_fragments,
        right_fragments=pred_fragments,
        candidate_attribution_left=None,
        candidate_attribution_right=None,
        context_digests=digests,
    )


def test_rejected_conflict_not_reused_by_same_instance(tmp_path: Path):
    store = FrozenDecisionStore(tmp_path / "frozen")
    request = _evidence_bound_request()
    winner = build_judgment(
        request,
        response_raw={
            "verdict": "equivalent",
            "rationale": "other writer",
            "source_citations": ["left:table-1"],
        },
        verdict="equivalent",
        rationale="other writer",
        source_citations=["left:table-1"],
        status="ok",
        failure_reason=None,
        recorded_at="2026-01-01T00:00:00",
    )
    store.record(winner)
    # 模拟竞态：本实例首次 lookup 未命中（另一写入者随后持久保存）
    racy = FrozenDecisionStore(tmp_path / "frozen")
    real_lookup = racy.lookup
    state = {"missed": False}

    def miss_first(key):
        if not state["missed"]:
            state["missed"] = True
            return None
        return real_lookup(key)

    racy.lookup = miss_first
    adjudicator = AuxiliaryAdjudicator(
        config=protocol_only_config(),
        adapter=MockAgentAdapter(
            lambda r: AgentResponse(
                verdict="different",
                rationale="mine",
                source_citations=["left:table-1"],
            )
        ),
        store=racy,
    )
    with pytest.raises(FrozenStoreError):
        adjudicator.adjudicate(request)
    # 同实例再次调用：必须读取冻结赢家，不得返回被拒的 different
    after = adjudicator.adjudicate(request)
    assert after.verdict == "equivalent" and after.rationale == "other writer"
    assert adjudicator.uses[-1].reused is True


def test_concurrent_same_context_conflict_single_winner(tmp_path: Path):
    store = FrozenDecisionStore(tmp_path / "frozen")
    request = _evidence_bound_request()
    barrier = threading.Barrier(2)
    outcomes: list[str] = []

    def worker(verdict: str, rationale: str) -> None:
        adapter = MockAgentAdapter(
            lambda r: AgentResponse(
                verdict=verdict, rationale=rationale, source_citations=["left:table-1"]
            )
        )
        adjudicator = AuxiliaryAdjudicator(
            config=protocol_only_config(), adapter=adapter, store=store
        )
        barrier.wait()
        try:
            adjudicator.adjudicate(request)
            outcomes.append("ok")
        except FrozenStoreError:
            # 同键异内容：败者被明确拒绝，绝不覆盖磁盘赢家
            outcomes.append("rejected")

    threads = [
        threading.Thread(target=worker, args=("equivalent", "writer A")),
        threading.Thread(target=worker, args=("different", "writer B")),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    entries = list((tmp_path / "frozen").glob("*.json"))
    assert len(entries) == 1  # 磁盘恰一个完整条目，无双重发布
    stored = json.loads(entries[0].read_text("utf-8"))
    assert stored["verdict"] in {"equivalent", "different"}
    assert stored["context_digests"] == stored["request"]["context_digests"]
    assert not list((tmp_path / "frozen").glob("*.tmp-*"))  # 无临时残留


# ---------------------------------------------------------------------------
# 证据统计与 scoped L1/L2 同范围
# ---------------------------------------------------------------------------


def test_s09_evidence_stats_match_scoped_counts():
    plan, gold, preds, auxiliary = load_bundle("synthetic-s09")
    report = score_batch(plan, gold, preds, auxiliary=auxiliary)
    paper = report.papers[0]
    assert paper.l2.predicted == 1
    verification = paper.evidence_verification
    assert verification["values"] == 1  # 不含范围外对象 B / 范围外物理量
    assert verification["out_of_scope"] == 2
    assert (
        sum(
            verification[key]
            for key in ("not_verified", "registered", "dangling", "wrong_material")
        )
        == verification["values"]
    )


def test_out_of_scope_dangling_evidence_not_mixed_into_scope():
    """范围外对象的悬空证据引用不混入 scope 统计。"""
    plan, gold, preds, auxiliary = load_bundle("synthetic-s09")
    dangling_value = build_measured_value(
        value_id="p3",
        value="10",
        error="1",
        unit="kpc",
        condition=STD,
        representation=HELI,
        evidence_record_id="unregistered-source",
    )
    out_object = next(obj for obj in preds[0].objects if "SYN-B" in obj.identifiers)
    out_object.quantities = [
        PredictionQuantityEntry(
            quantity_path=DIST,
            values=[PredictionValueEntry(record_id="p3", value=dangling_value)],
        )
    ]
    plan = plan.model_copy(update={"known_evidence_record_ids": ["synthetic-source"]})
    report = score_batch(plan, gold, preds, auxiliary=auxiliary)
    paper = report.papers[0]
    verification = paper.evidence_verification
    assert verification["values"] == 1
    assert verification["dangling"] == 0  # 悬空对象在范围外，不混入
    assert verification["out_of_scope"] == 2
    assert paper.l2.predicted == 1  # scoped 计数不受影响

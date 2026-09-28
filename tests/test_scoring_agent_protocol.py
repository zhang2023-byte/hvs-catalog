"""辅助评分 Agent 协议、冻结判定存储与启用门禁的离线测试（全部合成）。

覆盖：原文片段证据与引用校验（含空白/重复/越界/占位引用）、材料
不足不调用、Agent 明确 different 转确定不匹配、冻结条目完整性（顶层
字段篡改）与并发写冲突、production fail closed、protocol_only 仅允许
项目 mock。
"""

from __future__ import annotations

import hashlib
import json
import threading
from pathlib import Path

import pytest
from scoring_sample_helpers import load_bundle

from hvs_catalog.evaluation.scoring import (
    AgentRequest,
    AgentResponse,
    AgentValidationRecord,
    AuxiliaryAdjudicator,
    AuxiliaryEnablementError,
    AuxiliaryScoringConfig,
    FrozenDecisionStore,
    FrozenStoreError,
    MockAgentAdapter,
    SampleOutcome,
    ValidationRound,
    ValidationSample,
    auxiliary_config_digest,
    build_judgment,
    judgment_entry_key,
    protocol_only_config,
    score_batch,
)
from hvs_catalog.evaluation.scoring.models import EvidenceFragment

MATERIALS_FP = hashlib.sha256(b"synthetic-materials").hexdigest()


def fragment(
    fragment_id: str, text: str, *, supports: tuple[str, ...] = ("value",)
) -> EvidenceFragment:
    return EvidenceFragment(
        fragment_id=fragment_id,
        text=text,
        location=f"synthetic-paper.tex#{fragment_id}",
        material_name="synthetic-materials",
        material_fingerprint=MATERIALS_FP,
        supports_fields=list(supports),
    )


def make_request(**overrides) -> AgentRequest:
    payload = {
        "request_kind": "printed_expression",
        "field_name": "value",
        "left": "8.2",
        "right": "\\mystery{8.2}",
        "left_fragments": [fragment("table-1", "The distance is 8.2 kpc.")],
        "right_fragments": [fragment("macro-def", "\\newcommand{\\mystery}[1]{#1}")],
        "candidate_attribution_left": "condition=as reported",
        "candidate_attribution_right": "condition=as reported",
        "context_digests": {
            name: hashlib.sha256(name.encode()).hexdigest()
            for name in ("gold", "predictions", "science_rules", "auxiliary")
        },
    }
    payload.update(overrides)
    return AgentRequest.model_validate(payload)


def ok_response(verdict="equivalent", citations=("left:table-1",)):
    return AgentResponse(
        verdict=verdict,
        rationale="synthetic rationale",
        source_citations=list(citations),
    )


def test_mock_adapter_records_calls_and_returns_validated_response():
    adapter = MockAgentAdapter(lambda request: ok_response("different"))
    response = adapter.judge(make_request())
    assert response.verdict == "different"
    assert len(adapter.calls) == 1
    assert "method" not in adapter.calls[0] and "group" not in adapter.calls[0]
    assert "left_fragments" in adapter.calls[0]


def test_request_requires_material_bound_fragments():
    with pytest.raises(ValueError):
        AgentRequest.model_validate(
            {**make_request().model_dump(), "left_fragments": []}
        )
    with pytest.raises(ValueError):
        EvidenceFragment.model_validate(
            {
                "fragment_id": "bad",
                "text": "text",
                "location": "synthetic-paper.tex#bad",
                "material_name": "synthetic-materials",
                "material_fingerprint": "not-hex",
            }
        )


@pytest.mark.parametrize(
    "handler",
    [
        lambda request: {
            "verdict": "maybe",
            "rationale": "x",
            "source_citations": ["left:table-1"],
        },
        lambda request: {
            "verdict": "equivalent",
            "rationale": "  ",
            "source_citations": ["left:table-1"],
        },
        lambda request: {
            "verdict": "equivalent",
            "rationale": "ok",
            "source_citations": [],
        },
        lambda request: {
            "verdict": "equivalent",
            "rationale": "ok",
            "source_citations": ["left"],
        },
        lambda request: {
            "verdict": "equivalent",
            "rationale": "ok",
            "source_citations": ["nowhere"],
        },
        lambda request: {
            "verdict": "equivalent",
            "rationale": "ok",
            "source_citations": ["TODO"],
        },
        lambda request: {
            "verdict": "equivalent",
            "rationale": "ok",
            "source_citations": ["left:table-1", "left:table-1"],
        },
    ],
)
def test_invalid_responses_are_technical_failure(handler):
    """空白/重复/越界/占位引用与结构错误都记 technical_failure。"""
    adapter = MockAgentAdapter(handler)
    adjudicator = AuxiliaryAdjudicator(config=protocol_only_config(), adapter=adapter)
    entry = adjudicator.adjudicate(make_request())
    assert entry.status == "technical_failure"
    assert entry.failure_reason


def test_adjudicator_reuses_frozen_judgment_without_second_call(tmp_path: Path):
    adapter = MockAgentAdapter(lambda request: ok_response("equivalent"))
    store = FrozenDecisionStore(tmp_path / "frozen")
    adjudicator = AuxiliaryAdjudicator(
        config=protocol_only_config(), adapter=adapter, store=store
    )
    request = make_request()
    first = adjudicator.adjudicate(request)
    second = adjudicator.adjudicate(request)
    assert first.verdict == "equivalent" and second.verdict == "equivalent"
    assert len(adapter.calls) == 1
    assert adjudicator.uses[0].reused is False
    assert adjudicator.uses[1].reused is True

    adapter2 = MockAgentAdapter(lambda request: ok_response("different"))
    adjudicator2 = AuxiliaryAdjudicator(
        config=protocol_only_config(), adapter=adapter2, store=store
    )
    third = adjudicator2.adjudicate(request)
    assert third.verdict == "equivalent"
    assert adapter2.calls == []


def test_technical_failure_is_frozen_not_treated_as_different(tmp_path: Path):
    def boom(request):
        raise TimeoutError("synthetic timeout")

    adapter = MockAgentAdapter(boom)
    store = FrozenDecisionStore(tmp_path / "frozen")
    adjudicator = AuxiliaryAdjudicator(
        config=protocol_only_config(), adapter=adapter, store=store
    )
    request = make_request()
    entry = adjudicator.adjudicate(request)
    assert entry.status == "technical_failure" and entry.verdict is None
    again = adjudicator.adjudicate(request)
    assert again.status == "technical_failure"
    assert len(adapter.calls) == 1


def test_store_rejects_conflicting_content_for_same_context(tmp_path: Path):
    store = FrozenDecisionStore(tmp_path / "frozen")
    request = make_request()
    good = build_judgment(
        request,
        response_raw={
            "verdict": "equivalent",
            "rationale": "a",
            "source_citations": ["left:table-1"],
        },
        verdict="equivalent",
        rationale="a",
        source_citations=["left:table-1"],
        status="ok",
        failure_reason=None,
        recorded_at="2026-09-27T00:00:00+00:00",
    )
    store.record(good)
    conflicting = build_judgment(
        request,
        response_raw={
            "verdict": "different",
            "rationale": "b",
            "source_citations": ["left:table-1"],
        },
        verdict="different",
        rationale="b",
        source_citations=["left:table-1"],
        status="ok",
        failure_reason=None,
        recorded_at="2026-09-27T00:00:00+00:00",
    )
    with pytest.raises(FrozenStoreError):
        store.record(conflicting)
    assert store.record(good).entry_key == good.entry_key


def test_store_detects_top_level_field_tampering(tmp_path: Path):
    """顶层 verdict/依据/引用篡改必须被读取校验拒绝，不只是 raw 摘要。"""
    store = FrozenDecisionStore(tmp_path / "frozen")
    request = make_request()
    entry = build_judgment(
        request,
        response_raw={
            "verdict": "equivalent",
            "rationale": "a",
            "source_citations": ["left:table-1"],
        },
        verdict="equivalent",
        rationale="a",
        source_citations=["left:table-1"],
        status="ok",
        failure_reason=None,
        recorded_at="2026-09-27T00:00:00+00:00",
    )
    store.record(entry)
    path = tmp_path / "frozen" / f"{entry.entry_key}.json"
    for field, value in (
        ("verdict", "different"),
        ("rationale", "tampered"),
        ("source_citations", ["right:macro-def"]),
    ):
        payload = json.loads(path.read_text("utf-8"))
        payload[field] = value
        path.write_text(json.dumps(payload), "utf-8")
        with pytest.raises(FrozenStoreError):
            store.lookup(entry.entry_key)
        payload = json.loads(path.read_text("utf-8"))
        payload[field] = json.loads(path.read_text("utf-8"))[field]  # 保持篡改
    payload = json.loads(path.read_text("utf-8"))
    payload["verdict"] = "equivalent"
    payload["rationale"] = "a"
    payload["source_citations"] = ["left:table-1"]
    path.write_text(json.dumps(payload), "utf-8")
    assert store.lookup(entry.entry_key).verdict == "equivalent"


def test_store_detects_raw_response_tampering(tmp_path: Path):
    store = FrozenDecisionStore(tmp_path / "frozen")
    request = make_request()
    entry = build_judgment(
        request,
        response_raw={
            "verdict": "equivalent",
            "rationale": "a",
            "source_citations": ["left:table-1"],
        },
        verdict="equivalent",
        rationale="a",
        source_citations=["left:table-1"],
        status="ok",
        failure_reason=None,
        recorded_at="2026-09-27T00:00:00+00:00",
    )
    store.record(entry)
    path = tmp_path / "frozen" / f"{entry.entry_key}.json"
    payload = json.loads(path.read_text("utf-8"))
    payload["response_raw"]["verdict"] = "different"
    path.write_text(json.dumps(payload), "utf-8")
    with pytest.raises(FrozenStoreError):
        store.lookup(entry.entry_key)


def test_concurrent_record_conflict_has_single_winner(tmp_path: Path):
    """真正并发写同键不同判定：只有一个发布成功，另一个被拒绝且不覆盖。"""
    store = FrozenDecisionStore(tmp_path / "frozen")
    request = make_request()
    entry_a = build_judgment(
        request,
        response_raw={
            "verdict": "equivalent",
            "rationale": "a",
            "source_citations": ["left:table-1"],
        },
        verdict="equivalent",
        rationale="a",
        source_citations=["left:table-1"],
        status="ok",
        failure_reason=None,
        recorded_at="2026-09-27T00:00:00+00:00",
    )
    entry_b = build_judgment(
        request,
        response_raw={
            "verdict": "different",
            "rationale": "b",
            "source_citations": ["left:table-1"],
        },
        verdict="different",
        rationale="b",
        source_citations=["left:table-1"],
        status="ok",
        failure_reason=None,
        recorded_at="2026-09-27T00:00:00+00:00",
    )
    barrier = threading.Barrier(4)
    results: dict[str, object] = {}

    def worker(name: str, entry):
        barrier.wait()
        try:
            results[name] = store.record(entry).verdict
        except FrozenStoreError as error:
            results[name] = f"conflict: {error}"

    threads = [
        threading.Thread(target=worker, args=(f"a{i}", entry_a)) for i in range(2)
    ] + [threading.Thread(target=worker, args=(f"b{i}", entry_b)) for i in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    verdicts = [
        value for value in results.values() if value in {"equivalent", "different"}
    ]
    conflicts = [
        value for value in results.values() if str(value).startswith("conflict")
    ]
    assert len(verdicts) >= 1 and conflicts
    persisted = store.lookup(entry_a.entry_key)
    assert persisted.verdict in {"equivalent", "different"}
    files = [f.name for f in (tmp_path / "frozen").iterdir()]
    assert len([name for name in files if name.endswith(".json")]) == 1
    assert all(name.endswith(".json") for name in files)


def test_entry_key_binds_full_context():
    base = make_request()
    changed = make_request(
        context_digests={
            **base.context_digests,
            "science_rules": hashlib.sha256(b"other-rules").hexdigest(),
        }
    )
    assert judgment_entry_key(base) != judgment_entry_key(changed)
    changed_fragments = make_request(
        left_fragments=[fragment("other", "different evidence text")]
    )
    assert judgment_entry_key(base) != judgment_entry_key(changed_fragments)


def _validation_record(digests, *, rounds_count=2, all_pass=True, samples=None):
    samples = samples or [
        ValidationSample(
            sample_id="s-equiv",
            relation="printed_expression",
            left="km s⁻¹",
            right="\\mathrm{km\\,s^{-1}}",
            expected_verdict="equivalent",
        ),
        ValidationSample(
            sample_id="s-diff",
            relation="printed_expression",
            left="8.2",
            right="8200",
            expected_verdict="different",
        ),
        ValidationSample(
            sample_id="s-undecidable",
            relation="printed_expression",
            left="8.2",
            right="\\mystery{8.2}",
            expected_verdict="undecidable",
        ),
    ]
    rounds = []
    for index in range(rounds_count):
        outcomes = [
            SampleOutcome(
                sample_id=sample.sample_id,
                actual_verdict=(sample.expected_verdict if all_pass else "undecidable"),
                passed=(sample.expected_verdict if all_pass else "undecidable")
                == sample.expected_verdict,
            )
            for sample in samples
        ]
        rounds.append(ValidationRound(round_id=f"r{index + 1}", outcomes=outcomes))
    return AgentValidationRecord(
        test_set_id="synthetic-capability-set",
        test_set_fingerprint=hashlib.sha256(
            json.dumps(
                [s.model_dump(mode="json") for s in samples],
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
            ).encode("utf-8")
        ).hexdigest(),
        planned_round_count=rounds_count,
        samples=samples,
        rounds=rounds,
        **digests,
    )


def _digests():
    return {
        "model_config_digest": hashlib.sha256(b"model").hexdigest(),
        "prompt_digest": hashlib.sha256(b"prompt").hexdigest(),
        "params_digest": hashlib.sha256(b"params").hexdigest(),
        "budget_digest": hashlib.sha256(b"budget").hexdigest(),
    }


def test_production_fail_closed_even_with_fully_passed_bound_record(tmp_path: Path):
    config = AuxiliaryScoringConfig(
        mode="production",
        store_directory=str(tmp_path / "frozen"),
        validation=_validation_record(_digests()),
        **_digests(),
    )
    assert config.agent_allowed() is False
    assert "fail closed" in config.refusal_reason()
    with pytest.raises(AuxiliaryEnablementError):
        AuxiliaryAdjudicator(
            config=config, adapter=MockAgentAdapter(lambda r: ok_response())
        )


def test_validation_record_requires_content_fingerprint_match():
    digests = _digests()
    record = _validation_record(digests)
    with pytest.raises(ValueError):
        AgentValidationRecord.model_validate(
            {
                **record.model_dump(),
                "samples": [
                    s.model_copy(update={"left": "999"}).model_dump()
                    if s.sample_id == "s-diff"
                    else s.model_dump()
                    for s in record.samples
                ],
            }
        )


def test_validation_record_round_rules_still_enforced():
    digests = _digests()
    with pytest.raises(ValueError):
        _validation_record(digests, all_pass=False)
    record = _validation_record(digests)
    with pytest.raises(ValueError):
        AgentValidationRecord.model_validate(
            {
                **record.model_dump(),
                "planned_round_count": record.planned_round_count + 1,
            }
        )
    with pytest.raises(ValueError):
        AgentValidationRecord.model_validate(
            {**record.model_dump(), "test_set_fingerprint": "0" * 64}
        )


def test_protocol_only_rejects_non_mock_adapter():
    class NonMockAdapter:
        name = "unverified-real-adapter"

        def judge(self, request):
            return ok_response()

    with pytest.raises(AuxiliaryEnablementError, match="MockAgentAdapter"):
        AuxiliaryAdjudicator(config=protocol_only_config(), adapter=NonMockAdapter())


def test_score_batch_resolves_u1e_with_mock_and_freezes_judgment(tmp_path: Path):
    """证据充足变体：mock 引用实际片段时判定成立并被冻结复用。"""
    plan, gold, predictions, _ = load_bundle("synthetic-s07u1e")
    auxiliary = protocol_only_config(store_directory=str(tmp_path / "frozen"))
    adapter = MockAgentAdapter(lambda request: ok_response("equivalent"))
    report = score_batch(
        plan, gold, predictions, auxiliary=auxiliary, agent_adapter=adapter
    )
    paper = report.papers[0]
    assert paper.l2.correct == 1 and paper.l2.precision == 1.0
    assert report.batch_provisional is False
    assert report.auxiliary.enabled and report.auxiliary.protocol_only
    assert report.auxiliary.judgments[0].verdict == "equivalent"
    assert list((tmp_path / "frozen").glob("*.json"))

    adapter2 = MockAgentAdapter(lambda request: ok_response("different"))
    report2 = score_batch(
        plan, gold, predictions, auxiliary=auxiliary, agent_adapter=adapter2
    )
    assert report2.papers[0].l2.correct == 1
    assert adapter2.calls == []
    assert report2.auxiliary.judgments[0].reused is True


def test_score_batch_does_not_call_agent_for_material_insufficient_sample(tmp_path):
    """U1 固定样例材料不足：不调用 mock，保持 undecidable/provisional。"""
    plan, gold, predictions, _ = load_bundle("synthetic-s07u1")
    auxiliary = protocol_only_config(store_directory=str(tmp_path / "frozen"))
    adapter = MockAgentAdapter(lambda request: ok_response("equivalent"))
    report = score_batch(
        plan, gold, predictions, auxiliary=auxiliary, agent_adapter=adapter
    )
    paper = report.papers[0]
    assert adapter.calls == []
    assert paper.l2.correct == 0 and paper.l2.precision is None
    assert report.batch_provisional is True


def test_agent_explicit_different_is_determined_mismatch(tmp_path: Path):
    """Agent 明确 different：确定为不匹配，给出最终 0 率而非 unresolved。"""
    plan, gold, predictions, _ = load_bundle("synthetic-s07u1e")
    auxiliary = protocol_only_config(store_directory=str(tmp_path / "frozen"))
    adapter = MockAgentAdapter(
        lambda request: ok_response("different", citations=("right:macro-def",))
    )
    report = score_batch(
        plan, gold, predictions, auxiliary=auxiliary, agent_adapter=adapter
    )
    paper = report.papers[0]
    assert paper.l2.correct == 0
    assert paper.l2.precision == 0.0 and paper.l2.recall == 0.0
    assert report.batch_provisional is False
    assert any(row.status == "value_mismatch" for row in paper.pairings)
    assert not any(row.status == "unresolved_value" for row in paper.pairings)


def test_mock_undecidable_keeps_provisional(tmp_path: Path):
    plan, gold, predictions, _ = load_bundle("synthetic-s07u1e")
    auxiliary = protocol_only_config(store_directory=str(tmp_path / "frozen"))
    adapter = MockAgentAdapter(lambda request: ok_response("undecidable"))
    report = score_batch(
        plan, gold, predictions, auxiliary=auxiliary, agent_adapter=adapter
    )
    paper = report.papers[0]
    assert paper.l2.correct == 0 and paper.l2.precision is None
    assert report.batch_provisional is True


def test_program_determined_mismatch_is_never_sent_to_agent(tmp_path: Path):
    plan, gold, predictions, _ = load_bundle("synthetic-s06b2")
    auxiliary = protocol_only_config(store_directory=str(tmp_path / "frozen"))
    adapter = MockAgentAdapter(lambda request: ok_response("equivalent"))
    report = score_batch(
        plan, gold, predictions, auxiliary=auxiliary, agent_adapter=adapter
    )
    assert adapter.calls == []
    assert report.papers[0].l2.correct == 0
    assert report.batch_provisional is False


def test_input_change_invalidates_frozen_decisions(tmp_path: Path):
    plan, gold, predictions, _ = load_bundle("synthetic-s07u1e")
    auxiliary = protocol_only_config(store_directory=str(tmp_path / "frozen"))
    adapter = MockAgentAdapter(lambda request: ok_response("equivalent"))
    score_batch(plan, gold, predictions, auxiliary=auxiliary, agent_adapter=adapter)
    assert len(adapter.calls) == 1

    changed_plan = plan.model_copy(
        update={
            "materials_ref": plan.materials_ref.model_copy(
                update={
                    "fingerprint": hashlib.sha256(b"new-materials").hexdigest(),
                    "name": "other-materials",
                }
            )
        }
    )
    adapter2 = MockAgentAdapter(lambda request: ok_response("undecidable"))
    report = score_batch(
        changed_plan, gold, predictions, auxiliary=auxiliary, agent_adapter=adapter2
    )
    # 材料指纹变化 → 片段绑定不再匹配 → 材料不足，不调用也不复用旧判定
    assert adapter2.calls == []
    assert report.batch_provisional is True


def test_auxiliary_config_digest_excludes_store_directory():
    with_store = protocol_only_config(store_directory="/tmp/a")
    without_store = protocol_only_config()
    assert auxiliary_config_digest(with_store) == auxiliary_config_digest(without_store)
    other = protocol_only_config().model_copy(
        update={"model_config_digest": hashlib.sha256(b"other").hexdigest()}
    )
    assert auxiliary_config_digest(other) != auxiliary_config_digest(without_store)

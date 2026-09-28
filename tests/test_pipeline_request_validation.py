"""共享提取入口：严格请求与入口校验（C01）。

请求只接受强类型字段，没有 Gold、评分或答案路径参数，也不接受自由
metadata；未知字段与非法模型配置一律拒绝。日常与评测经同一入口建
立执行，输入快照同样不含答案通道。
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from hvs_catalog.pipeline import (
    PAPER_B,
    ExtractionRequest,
    PipelineRequestError,
    StepModel,
    UnknownMethodError,
    import_mock_source,
    start_extraction,
)
from hvs_catalog.records.common import RecordRef
from hvs_catalog.records.runs import InputSnapshot
from hvs_catalog.workspace import initialize_workspace


@pytest.fixture()
def space(tmp_path):
    workspace = initialize_workspace(tmp_path / "space")
    yield workspace
    workspace.close()


@pytest.fixture()
def source(space):
    return import_mock_source(space, PAPER_B)


def _request(source_ref, **overrides):
    payload = {
        "mode": "daily",
        "method": "direct",
        "source_refs": [source_ref],
        "models": [StepModel(model="mock-default")],
    }
    payload.update(overrides)
    return ExtractionRequest(**payload)


def test_request_fields_are_strict_and_closed():
    """字段集固定：无 Gold/score/答案路径，也无自由 metadata。"""
    fields = set(ExtractionRequest.model_fields)
    assert fields == {
        "mode",
        "method",
        "source_refs",
        "models",
        "expert_adjudication",
        "existing_decision_refs",
    }
    forbidden = ("gold", "gold_path", "answers", "answer_path", "score", "scoring")
    for name in forbidden:
        assert name not in fields
    # 未知/额外字段拒绝（extra=forbid），答案不能借道 metadata 偷渡
    with pytest.raises(ValidationError, match="metadata"):
        _request(None, metadata={"gold_answer": 42})
    with pytest.raises(ValidationError, match="gold_path"):
        _request(None, gold_path="/gold/answer.json")


def test_models_need_unique_default_and_unique_steps(source):
    with pytest.raises(ValidationError, match="默认模型"):
        _request(source, models=[StepModel(step_id="quantities", model="m1")])
    with pytest.raises(ValidationError, match="默认模型"):
        _request(
            source,
            models=[StepModel(model="d1"), StepModel(model="d2")],
        )
    with pytest.raises(ValidationError, match="重复"):
        _request(
            source,
            models=[
                StepModel(model="d1"),
                StepModel(step_id="quantities", model="m1"),
                StepModel(step_id="quantities", model="m2"),
            ],
        )


def test_unknown_step_model_rejected_before_execution(space, source):
    """给不存在或未启用的步骤设置模型时，运行前报错。"""
    request = _request(
        source, models=[StepModel(model="d1"), StepModel(step_id="nope", model="m")]
    )
    with pytest.raises(PipelineRequestError, match="没有步骤 'nope'"):
        start_extraction(space, request=request, request_key="bad-step")
    # 未建立任何执行
    assert space.store().list_executions() == []


def test_unknown_method_rejected_before_execution(space, source):
    request = _request(source, method="not-registered")
    with pytest.raises(UnknownMethodError):
        start_extraction(space, request=request, request_key="bad-method")


def test_preflight_rejects_unsupported_material(space, tmp_path):
    """模拟实现之外的论文在建立执行前拒绝，不退化为空结果。"""
    from datetime import UTC, datetime

    from hvs_catalog.records.common import build_record
    from hvs_catalog.records.source import (
        SourceFile,
        SourcePackageData,
        SourcePackageRecord,
    )

    ref = space.publish_source_file("candidate/other/paper.tex", b"fictional\n")
    source_record = build_record(
        SourcePackageRecord,
        record_id="src-unknown",
        created_at=datetime.now(UTC),
        execution_id=None,
        data=SourcePackageData(
            paper_key="FIC-UNKNOWN-1",
            origin={"kind": "synthetic"},
            source_version="fictional-v1",
            files=[
                SourceFile(
                    file_id="paper",
                    path=ref.path,
                    sha256=ref.sha256,
                    media_type="text/x-tex",
                    role="main_text",
                    read_state="ready",
                    derived_from=[],
                )
            ],
            gaps=[],
        ),
    )
    from hvs_catalog.workspace import CommitRequest

    space.store().commit(
        CommitRequest(
            operation_key="import:unknown",
            kind="material_import",
            new_records=[source_record],
            result_refs=[source_record.ref()],
        )
    )
    request = _request(source_record.ref())
    with pytest.raises(PipelineRequestError, match="不在模拟材料支持范围"):
        start_extraction(space, request=request, request_key="unknown-paper")
    assert space.store().list_executions() == []


def test_preflight_rejects_missing_source_and_multiple_sources(space):
    missing = RecordRef(record_id="src-missing", revision=1)
    with pytest.raises(PipelineRequestError, match="来源记录缺失"):
        start_extraction(
            space,
            request=_request(missing),
            request_key="missing-source",
        )
    real = import_mock_source(space, PAPER_B)
    with pytest.raises(PipelineRequestError, match="单一来源包"):
        start_extraction(
            space,
            request=_request(real, source_refs=[real, real]),
            request_key="two-sources",
        )


def test_preflight_rejects_missing_fixed_decision(space, source):
    request = _request(
        source,
        existing_decision_refs=[RecordRef(record_id="dec-missing", revision=1)],
    )
    with pytest.raises(PipelineRequestError, match="固定允许的决定缺失"):
        start_extraction(space, request=request, request_key="missing-decision")


def test_daily_and_evaluation_share_one_entry_and_snapshot_has_no_answer_channel(
    space, source
):
    """评测与日常经同一入口建立执行；快照字段固定、无答案通道。"""
    snapshot_fields = set(InputSnapshot.model_fields)
    assert snapshot_fields == {
        "source_refs",
        "rules",
        "method",
        "models",
        "code_version",
        "dependency_versions",
        "expert_adjudication",
        "prompt_refs",
        "example_refs",
        "precedent_refs",
        "existing_decision_refs",
    }
    for name in ("gold", "answers", "score", "metadata"):
        assert name not in snapshot_fields
    seen = []
    import hvs_catalog.pipeline.entry as entry_module

    original = entry_module._create_execution

    def spy(space_, *, request_key, input_snapshot, flow):
        seen.append((request_key, input_snapshot.method, flow.mode))
        return original(
            space_, request_key=request_key, input_snapshot=input_snapshot, flow=flow
        )

    entry_module._create_execution = spy
    try:
        daily = start_extraction(
            space, request=_request(source), request_key="spy-daily"
        )
        evaluation = start_extraction(
            space,
            request=_request(source, mode="evaluation"),
            request_key="spy-eval",
        )
    finally:
        entry_module._create_execution = original
    assert seen == [
        ("spy-daily", "direct", "daily"),
        ("spy-eval", "direct", "evaluation"),
    ]
    assert daily.mode == "daily"
    assert evaluation.mode == "evaluation"
    assert daily.input_digest != evaluation.input_digest


def test_expert_adjudication_defaults_to_true(source):
    request = _request(source)
    assert request.expert_adjudication is True
    with pytest.raises(ValidationError):
        _request(source, expert_adjudication="maybe")

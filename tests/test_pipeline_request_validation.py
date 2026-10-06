"""Shared extraction entry point: strict requests and entry validation (C01).

Accept strongly typed fields only, with no Gold/scoring/answer paths or free metadata.
Reject unknown fields and invalid model configuration. Ordinary/evaluation execution
uses one entry point with no answer channels in snapshots.
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
        "method": "roster",
        "source_refs": [source_ref],
        "models": [StepModel(model="mock-default")],
    }
    payload.update(overrides)
    return ExtractionRequest(**payload)


def test_request_fields_are_strict_and_closed():
    """Fixed fields exclude Gold/scoring/answer paths and free metadata."""
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
    # extra=forbid rejects unknown fields; metadata cannot smuggle answers.
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
    """Reject models assigned to nonexistent or disabled steps before running."""
    request = _request(
        source, models=[StepModel(model="d1"), StepModel(step_id="nope", model="m")]
    )
    with pytest.raises(PipelineRequestError, match="没有步骤 'nope'"):
        start_extraction(space, request=request, request_key="bad-step")
    # No execution was created.
    assert space.store().list_executions() == []


def test_unknown_method_rejected_before_execution(space, source):
    request = _request(source, method="not-registered")
    with pytest.raises(UnknownMethodError):
        start_extraction(space, request=request, request_key="bad-method")


def test_preflight_rejects_unsupported_material(space, tmp_path):
    """Reject papers outside the mock implementation before execution creation, without
    empty-result fallback.
    """
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
    """Evaluation and ordinary runs share execution creation with fixed snapshot fields and
    no answer channels.
    """
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
        "options",
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
        ("spy-daily", "roster", "daily"),
        ("spy-eval", "roster", "evaluation"),
    ]
    assert daily.mode == "daily"
    assert evaluation.mode == "evaluation"
    assert daily.input_digest != evaluation.input_digest


def test_expert_adjudication_defaults_to_false(source):
    request = _request(source)
    assert request.expert_adjudication is False
    with pytest.raises(ValidationError):
        _request(source, expert_adjudication="maybe")

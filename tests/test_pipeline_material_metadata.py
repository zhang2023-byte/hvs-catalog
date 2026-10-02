"""Shared extraction entry point: scientific source metadata and registration contract
(C05).

Require all source fields affecting scientific usability/interpretation to match
registered v1 semantics: read_state, derived_from chains, and source.gaps. Reject
unreadable files, unregistered derivations, or any gaps before service calls, through
shared creation/recovery/runtime checks. Require zero spy calls, unchanged snapshots,
and no mismatched outputs. Timestamps and execution bookkeeping
(created_at/execution_id) are not scientific input and need no mechanical equality.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from hvs_catalog.pipeline import (
    ExtractionRequest,
    PipelineRequestError,
    StepModel,
    get_extraction,
    resume_extraction,
    start_extraction,
)
from hvs_catalog.pipeline.flow import ExtractionFlow
from hvs_catalog.pipeline.methods import CANDIDATE_REVIEW_METHOD, DIRECT_METHOD
from hvs_catalog.pipeline.mock import PipelineMockService
from hvs_catalog.records.common import RecordRef, build_record
from hvs_catalog.records.runs import ModelSpec
from hvs_catalog.records.source import (
    AffectedItem,
    AffectedTargets,
    Gap,
    SourceFile,
    SourcePackageData,
    SourcePackageRecord,
)
from hvs_catalog.runtime import build_input_snapshot, create_execution, resume_execution
from hvs_catalog.synthetic import material as m
from hvs_catalog.workspace import CommitRequest, initialize_workspace


@pytest.fixture()
def space(tmp_path):
    workspace = initialize_workspace(tmp_path / "space")
    yield workspace
    workspace.close()


class SpyService(PipelineMockService):
    def __init__(self):
        super().__init__()
        self.requests: list[dict] = []

    def call(self, *, step, request, material):
        self.requests.append({"step": step, "request": request})
        return super().call(step=step, request=request, material=material)


def roster_gap(gap_id: str = "gap-roster-1") -> Gap:
    return Gap(
        gap_id=gap_id,
        reason="missing",
        description="A registered-looking gap affecting roster inclusion.",
        file_id=None,
        mention_evidence_refs=[],
        affected_targets=AffectedTargets(
            scope="known",
            items=[AffectedItem(item_id=None, quantity=None, judgment="inclusion")],
        ),
    )


def quantity_gap(gap_id: str = "gap-quantity-1") -> Gap:
    return Gap(
        gap_id=gap_id,
        reason="unparsed",
        description="A registered-looking gap affecting one quantity.",
        file_id="paper",
        mention_evidence_refs=[],
        affected_targets=AffectedTargets(
            scope="known",
            items=[
                AffectedItem(
                    item_id=None,
                    quantity="observed_phase_space.radial_velocity",
                    judgment=None,
                )
            ],
        ),
    )


def build_registered_like_b_source(
    space,
    *,
    read_state: str = "ready",
    derived_from: list[str] | None = None,
    gaps: list[Gap] | None = None,
    operation_key: str = "import:custom-b",
) -> RecordRef:
    """Build structurally valid src-r05-b v1 matching registered B materials except
    specified metadata.
    """
    file_ref = space.publish_source_file("r05/b/paper.tex", m.tex_bytes("B"))
    created_at = datetime.now(UTC)
    source = build_record(
        SourcePackageRecord,
        record_id="src-r05-b",
        created_at=created_at,
        execution_id=None,
        data=SourcePackageData(
            paper_key=m.PAPER_KEYS["B"],
            origin={"kind": "synthetic"},
            source_version="fictional-v1",
            files=[
                SourceFile(
                    file_id="paper",
                    path=file_ref.path,
                    sha256=file_ref.sha256,
                    media_type="text/x-tex",
                    role="main_text",
                    read_state=read_state,
                    derived_from=derived_from or [],
                )
            ],
            gaps=gaps or [],
        ),
    )
    _, evidences = m.build_source_and_evidence(
        "B", created_at=created_at, execution_id=None
    )
    space.store().commit(
        CommitRequest(
            operation_key=operation_key,
            kind="material_import",
            new_records=[source, *evidences],
            result_refs=[source.ref()],
        )
    )
    return source.ref()


def _start(space, source_ref, *, request_key, method="direct"):
    request = ExtractionRequest(
        mode="synthetic",
        method=method,
        source_refs=[source_ref],
        models=[StepModel(model="mock-default")],
        expert_adjudication=False,
    )
    return start_extraction(space, request=request, request_key=request_key)


def _bypass_execution(space, source_ref, *, method_spec, request_key):
    snapshot = build_input_snapshot(
        source_refs=[source_ref],
        method=method_spec.method_id,
        models=[
            ModelSpec(step_id=None, provider=None, model="mock-default", parameters={})
        ],
        expert_adjudication=False,
    )
    handle, _ = create_execution(
        space,
        request_key=request_key,
        input_snapshot=snapshot,
        flow=ExtractionFlow(method_spec, mode="synthetic"),
    )
    return handle


def _stored_source_refs(space, handle) -> list[dict]:
    run = space.store().read_record_current(handle.run_ref.record_id)
    return [ref.model_dump(mode="json") for ref in run.data.input_snapshot.source_refs]


# ---- Review reproducer 1: unreadable material must not complete ----


def test_unreadable_file_entry_start_rejected(space):
    """Reject the sole unreadable file before creating execution; never complete unreadable
    input.
    """
    source = build_registered_like_b_source(space, read_state="unreadable")
    with pytest.raises(PipelineRequestError, match="read_state"):
        _start(space, source, request_key="unreadable-entry")
    assert space.store().list_executions() == []


def test_unreadable_file_direct_runtime_rejected_before_service(space):
    """Direct runtime recovery fails with zero spy calls, unchanged snapshots, and no
    outputs.
    """
    source = build_registered_like_b_source(space, read_state="unreadable")
    handle = _bypass_execution(
        space, source, method_spec=DIRECT_METHOD, request_key="unreadable-rt"
    )
    service = SpyService()
    report = resume_execution(
        space,
        handle.execution_id,
        ExtractionFlow(DIRECT_METHOD, mode="synthetic"),
        service=service,
    )
    assert report.outcome == "failed"
    assert any("read_state" in error for error in report.errors)
    assert service.requests == []
    assert get_extraction(space, handle.execution_id)["result_refs"] == []
    assert _stored_source_refs(space, handle) == [
        {"record_id": "src-r05-b", "revision": 1}
    ]


def test_unreadable_file_entry_resume_rejected(space):
    """Entry-point recovery of bypass-created unreadable execution also fails without
    calls.
    """
    source = build_registered_like_b_source(space, read_state="unreadable")
    handle = _bypass_execution(
        space,
        source,
        method_spec=CANDIDATE_REVIEW_METHOD,
        request_key="unreadable-resume",
    )
    service = SpyService()
    report = resume_extraction(space, handle.execution_id, service=service)
    assert report.outcome == "failed"
    assert service.requests == []
    assert get_extraction(space, handle.execution_id)["result_refs"] == []


def test_unparsed_read_state_also_rejected(space):
    """unparsed is likewise unregistered; systematically cover every non-ready state."""
    source = build_registered_like_b_source(space, read_state="unparsed")
    with pytest.raises(PipelineRequestError, match="read_state"):
        _start(space, source, request_key="unparsed-entry")
    handle = _bypass_execution(
        space, source, method_spec=DIRECT_METHOD, request_key="unparsed-rt"
    )
    service = SpyService()
    report = resume_execution(
        space,
        handle.execution_id,
        ExtractionFlow(DIRECT_METHOD, mode="synthetic"),
        service=service,
    )
    assert report.outcome == "failed"
    assert service.requests == []


# ---- Review reproducer 2: source gaps must fail before calling services ----


def test_roster_affecting_gap_rejected_before_service(space):
    """Direct runtime rejects a valid roster-affecting gap before direct_roster."""
    source = build_registered_like_b_source(space, gaps=[roster_gap()])
    handle = _bypass_execution(
        space, source, method_spec=DIRECT_METHOD, request_key="gap-rt"
    )
    service = SpyService()
    report = resume_execution(
        space,
        handle.execution_id,
        ExtractionFlow(DIRECT_METHOD, mode="synthetic"),
        service=service,
    )
    assert report.outcome == "failed"
    assert any("缺口" in error for error in report.errors)
    assert service.requests == []
    assert get_extraction(space, handle.execution_id)["result_refs"] == []


def test_roster_affecting_gap_entry_start_rejected(space):
    source = build_registered_like_b_source(space, gaps=[roster_gap()])
    with pytest.raises(PipelineRequestError, match="缺口"):
        _start(space, source, request_key="gap-entry")
    assert space.store().list_executions() == []


def test_quantity_only_gap_also_rejected(space):
    """Quantity-only gaps are also unregistered semantics; registered v1 has no gaps."""
    source = build_registered_like_b_source(space, gaps=[quantity_gap()])
    with pytest.raises(PipelineRequestError, match="缺口"):
        _start(space, source, request_key="qgap-entry")
    handle = _bypass_execution(
        space, source, method_spec=DIRECT_METHOD, request_key="qgap-rt"
    )
    service = SpyService()
    report = resume_execution(
        space,
        handle.execution_id,
        ExtractionFlow(DIRECT_METHOD, mode="synthetic"),
        service=service,
    )
    assert report.outcome == "failed"
    assert service.requests == []


# ---- Derivation chains and combined states ----


def test_derived_from_mismatch_rejected(space):
    """Reject an unregistered derived_from chain referencing itself."""
    source = build_registered_like_b_source(space, derived_from=["paper"])
    with pytest.raises(PipelineRequestError, match="derived_from"):
        _start(space, source, request_key="derived-entry")
    handle = _bypass_execution(
        space, source, method_spec=DIRECT_METHOD, request_key="derived-rt"
    )
    service = SpyService()
    report = resume_execution(
        space,
        handle.execution_id,
        ExtractionFlow(DIRECT_METHOD, mode="synthetic"),
        service=service,
    )
    assert report.outcome == "failed"
    assert any("derived_from" in error for error in report.errors)
    assert service.requests == []


def test_combined_unreadable_and_gap_reports_both(space):
    """Report all combined-state diagnostics together while making zero calls."""
    source = build_registered_like_b_source(
        space, read_state="unreadable", gaps=[roster_gap()]
    )
    with pytest.raises(PipelineRequestError) as excinfo:
        _start(space, source, request_key="combined-entry")
    message = str(excinfo.value)
    assert "read_state" in message
    assert "缺口" in message


# ---- Valid registered materials remain supported ----


def test_registered_ready_material_still_runs(space):
    """Matching ready sources without gaps complete both methods normally."""
    source = build_registered_like_b_source(space)
    handle = _start(space, source, request_key="legal-b")
    service = SpyService()
    report = resume_extraction(space, handle.execution_id, service=service)
    assert report.outcome == "finished", report.errors
    assert len(service.requests) == 2

    source_c = _import_c(space)
    handle_c = _start(space, source_c, request_key="legal-c", method="candidate_review")
    report_c = resume_extraction(space, handle_c.execution_id, service=SpyService())
    assert report_c.outcome == "finished", report_c.errors


def _import_c(space) -> RecordRef:
    from hvs_catalog.pipeline import PAPER_C, import_mock_source

    return import_mock_source(space, PAPER_C)

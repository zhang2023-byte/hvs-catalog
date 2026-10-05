"""Shared extraction entry point: joint source and trusted mock-material binding (C05).

Membership alone does not establish binding. Snapshot source identity, exact supported
revision, paper identity, origin/version, and file manifest/roles/fingerprints must
match trusted mock registration. Reject changed keys/contents, unregistered revisions,
or other source identities before any service call, even for supported paper keys. Never
fall back to v1, redirect sources, or rewrite snapshots. Valid outputs reference the
frozen revision.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from hvs_catalog.pipeline import (
    PAPER_A,
    PAPER_B,
    PAPER_C,
    ExtractionRequest,
    PipelineRequestError,
    StepModel,
    get_extraction,
    import_mock_source,
    resume_extraction,
    start_extraction,
)
from hvs_catalog.pipeline.flow import ExtractionFlow
from hvs_catalog.pipeline.methods import ROSTER_METHOD
from hvs_catalog.pipeline.mock import PipelineMockService
from hvs_catalog.records.common import RecordRef, build_record
from hvs_catalog.records.paper import PaperRecord
from hvs_catalog.records.roster import RosterRecord
from hvs_catalog.records.runs import ModelSpec
from hvs_catalog.records.source import (
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


OTHER_CONTENT = (
    b"A different but correctly hashed synthetic manuscript about another"
    b" object sample.\n"
)


def _commit_new_revision(
    space,
    *,
    source_id: str,
    paper_key: str,
    content: bytes,
    file_rel: str,
    version: str = "fictional-v2",
) -> RecordRef:
    """Commit a source revision with changeable contents/identity and actual fixed hashes."""
    file_ref = space.publish_source_file(file_rel, content)
    record = build_record(
        SourcePackageRecord,
        record_id=source_id,
        revision=2,
        created_at=datetime.now(UTC),
        execution_id=None,
        data=SourcePackageData(
            paper_key=paper_key,
            origin={"kind": "synthetic"},
            source_version=version,
            files=[
                SourceFile(
                    file_id="paper",
                    path=file_ref.path,
                    sha256=file_ref.sha256,
                    media_type="text/x-tex",
                    role="main_text",
                    read_state="ready",
                    derived_from=[],
                )
            ],
            gaps=[],
        ),
    )
    space.store().commit(
        CommitRequest(
            operation_key=f"revise:{source_id}:2:{file_ref.sha256[:8]}",
            kind="material_import",
            new_records=[record],
            expected_revisions={source_id: 1},
            result_refs=[record.ref()],
        )
    )
    return RecordRef(record_id=source_id, revision=2)


def _start(space, source_ref, *, request_key, method="roster", expert=False):
    request = ExtractionRequest(
        mode="synthetic",
        method=method,
        source_refs=[source_ref],
        models=[StepModel(model="mock-default")],
        expert_adjudication=expert,
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
        flow=ExtractionFlow(method_spec, mode="daily"),
    )
    return handle


def _stored_source_refs(space, handle) -> list[dict]:
    run = space.store().read_record_current(handle.run_ref.record_id)
    return [ref.model_dump(mode="json") for ref in run.data.input_snapshot.source_refs]


# ---- Counterexample 1: A files with supported B paper_key ----


def test_identity_swap_entry_start_rejected(space):
    """Reject src-r05-a v2 retaining A files but claiming paper_key B before execution
    creation.
    """
    import_mock_source(space, PAPER_A)
    import_mock_source(space, PAPER_B)
    swapped = _commit_new_revision(
        space,
        source_id="src-r05-a",
        paper_key=PAPER_B,
        content=m.tex_bytes("A"),
        file_rel="r05/a/swapped.tex",
    )
    with pytest.raises(PipelineRequestError, match="登记"):
        _start(space, swapped, request_key="swap-entry")
    assert space.store().list_executions() == []


def test_identity_swap_direct_runtime_rejected_before_service(space):
    """Direct runtime recovery of the same mismatch fails with zero service calls and
    unchanged snapshots.
    """
    import_mock_source(space, PAPER_A)
    import_mock_source(space, PAPER_B)
    swapped = _commit_new_revision(
        space,
        source_id="src-r05-a",
        paper_key=PAPER_B,
        content=m.tex_bytes("A"),
        file_rel="r05/a/swapped.tex",
    )
    handle = _bypass_execution(
        space, swapped, method_spec=ROSTER_METHOD, request_key="swap-rt"
    )
    service = SpyService()
    report = resume_execution(
        space,
        handle.execution_id,
        ExtractionFlow(ROSTER_METHOD, mode="daily"),
        service=service,
    )
    assert report.outcome == "failed"
    assert any("登记" in error for error in report.errors)
    assert service.requests == []
    assert get_extraction(space, handle.execution_id)["result_refs"] == []
    assert _stored_source_refs(space, handle) == [
        {"record_id": "src-r05-a", "revision": 2}
    ]


def test_identity_swap_entry_resume_rejected(space):
    """Entry-point recovery of bypass-created mismatched execution also fails with zero
    calls.
    """
    import_mock_source(space, PAPER_A)
    import_mock_source(space, PAPER_B)
    swapped = _commit_new_revision(
        space,
        source_id="src-r05-a",
        paper_key=PAPER_B,
        content=m.tex_bytes("A"),
        file_rel="r05/a/swapped.tex",
    )
    handle = _bypass_execution(
        space, swapped, method_spec=ROSTER_METHOD, request_key="swap-resume"
    )
    service = SpyService()
    report = resume_extraction(space, handle.execution_id, service=service)
    assert report.outcome == "failed"
    assert service.requests == []
    assert get_extraction(space, handle.execution_id)["result_refs"] == []


# ---- Counterexample 2: unchanged paper_key with different correctly hashed contents ----


def test_same_paper_new_content_direct_runtime_rejected(space):
    """src-r05-b v2 with different synthetic contents must not fall back to B v1 static
    responses.
    """
    import_mock_source(space, PAPER_B)
    revised = _commit_new_revision(
        space,
        source_id="src-r05-b",
        paper_key=PAPER_B,
        content=OTHER_CONTENT,
        file_rel="r05/b/revised.tex",
    )
    handle = _bypass_execution(
        space, revised, method_spec=ROSTER_METHOD, request_key="content-rt"
    )
    service = SpyService()
    report = resume_execution(
        space,
        handle.execution_id,
        ExtractionFlow(ROSTER_METHOD, mode="daily"),
        service=service,
    )
    assert report.outcome == "failed"
    assert any("登记" in error for error in report.errors)
    assert service.requests == []
    assert get_extraction(space, handle.execution_id)["result_refs"] == []
    # Neither rewrite the snapshot nor fall back to v1.
    assert _stored_source_refs(space, handle) == [
        {"record_id": "src-r05-b", "revision": 2}
    ]


def test_same_paper_new_content_entry_start_rejected(space):
    import_mock_source(space, PAPER_B)
    revised = _commit_new_revision(
        space,
        source_id="src-r05-b",
        paper_key=PAPER_B,
        content=OTHER_CONTENT,
        file_rel="r05/b/revised.tex",
    )
    with pytest.raises(PipelineRequestError, match="登记"):
        _start(space, revised, request_key="content-entry")
    assert space.store().list_executions() == []


# ---- Even identical contents do not authorize static responses for an unregistered revision ----


def test_unregistered_revision_identical_content_rejected(space):
    """Reject unregistered src-r05-b v2 even if its bytes equal v1."""
    import_mock_source(space, PAPER_B)
    revised = _commit_new_revision(
        space,
        source_id="src-r05-b",
        paper_key=PAPER_B,
        content=m.tex_bytes("B"),
        file_rel=m.source_relative_path("B"),
        version="fictional-v1",
    )
    handle = _bypass_execution(
        space, revised, method_spec=ROSTER_METHOD, request_key="identical-rt"
    )
    service = SpyService()
    report = resume_execution(
        space,
        handle.execution_id,
        ExtractionFlow(ROSTER_METHOD, mode="daily"),
        service=service,
    )
    assert report.outcome == "failed"
    assert any("修订" in error for error in report.errors)
    assert service.requests == []
    with pytest.raises(PipelineRequestError, match="登记"):
        _start(space, revised, request_key="identical-entry")


# ---- Other source identities or file fingerprints under the same paper_key ----


def _fresh_source(space, *, record_id: str, paper_key: str, content: bytes, rel: str):
    file_ref = space.publish_source_file(rel, content)
    record = build_record(
        SourcePackageRecord,
        record_id=record_id,
        created_at=datetime.now(UTC),
        execution_id=None,
        data=SourcePackageData(
            paper_key=paper_key,
            origin={"kind": "synthetic"},
            source_version="fictional-v1",
            files=[
                SourceFile(
                    file_id="paper",
                    path=file_ref.path,
                    sha256=file_ref.sha256,
                    media_type="text/x-tex",
                    role="main_text",
                    read_state="ready",
                    derived_from=[],
                )
            ],
            gaps=[],
        ),
    )
    space.store().commit(
        CommitRequest(
            operation_key=f"import:{record_id}",
            kind="material_import",
            new_records=[record],
            result_refs=[record.ref()],
        )
    )
    return record.ref()


def test_other_source_identity_same_paper_key_rejected(space):
    """Reject src-candidate-c-alt claiming C's paper_key and valid files because source
    identity differs.
    """
    import_mock_source(space, PAPER_C)
    from hvs_catalog.pipeline.mock import c_tex_bytes

    fake = _fresh_source(
        space,
        record_id="src-candidate-c-alt",
        paper_key=PAPER_C,
        content=c_tex_bytes(),
        rel="candidate/c-alt/paper.tex",
    )
    with pytest.raises(PipelineRequestError, match="来源标识"):
        _start(space, fake, request_key="alt-id")
    assert space.store().list_executions() == []


def test_file_fingerprint_mismatch_rejected(space):
    """Reject a different correctly hashed file whose fingerprint differs from
    registration.
    """
    import_mock_source(space, PAPER_C)
    mismatched = _fresh_source(
        space,
        record_id="src-candidate-c-alt2",
        paper_key=PAPER_C,
        content=OTHER_CONTENT,
        rel="candidate/c-alt2/paper.tex",
    )
    with pytest.raises(PipelineRequestError, match="登记"):
        _start(space, mismatched, request_key="fingerprint")
    assert space.store().list_executions() == []


# ---- Registered materials: outputs reference the frozen execution revision ----


def test_legal_materials_bind_outputs_to_frozen_revision(space):
    """Normal A/B/C roster and paper source_refs equal the frozen execution revision."""
    source_a = import_mock_source(space, PAPER_A)
    handle_a = _start(space, source_a, request_key="legal-a")
    report = resume_extraction(
        space, handle_a.execution_id, service=PipelineMockService()
    )
    assert report.outcome == "finished"
    frozen = {"record_id": "src-r05-a", "revision": 1}
    roster = space.store().read_record_current(
        f"roster-fic-r05-a-{handle_a.execution_id}"
    )
    paper = space.store().read_record_current(
        f"paper-fic-r05-a-{handle_a.execution_id}"
    )
    assert roster.data.source_ref.model_dump(mode="json") == frozen
    assert paper.data.source_ref.model_dump(mode="json") == frozen

    source_c = import_mock_source(space, PAPER_C)
    handle_c = _start(space, source_c, request_key="legal-c", method="roster")
    report_c = resume_extraction(
        space, handle_c.execution_id, service=PipelineMockService()
    )
    assert report_c.outcome == "finished"
    frozen_c = {"record_id": "src-candidate-c", "revision": 1}
    roster_c = space.store().read_record_current(
        f"roster-fic-candidate-c-{handle_c.execution_id}"
    )
    paper_c = space.store().read_record_current(
        f"paper-fic-candidate-c-{handle_c.execution_id}"
    )
    assert roster_c.data.source_ref.model_dump(mode="json") == frozen_c
    assert paper_c.data.source_ref.model_dump(mode="json") == frozen_c
    assert isinstance(roster_c, RosterRecord) and isinstance(paper_c, PaperRecord)

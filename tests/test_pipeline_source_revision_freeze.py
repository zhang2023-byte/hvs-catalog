"""Shared extraction entry point: frozen source revisions and mock-material identity (C05).

Freeze complete RecordRef values in InputSnapshot.source_refs. Later revisions, even
replacing a source with another paper, cannot change initial execution, paused recovery,
or fresh-process recovery: read the exact frozen revision for roster/value outputs.
Reject missing revisions or mismatched mock identities before service calls; never read
current as fallback or rewrite old snapshots.
"""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

from hvs_catalog.pipeline import (
    PAPER_A,
    PAPER_B,
    PAPER_C,
    ExtractionRequest,
    StepModel,
    get_extraction,
    import_mock_source,
    pending_questions,
    resume_extraction,
    start_extraction,
    submit_expert_decision,
)
from hvs_catalog.pipeline.flow import ExtractionFlow
from hvs_catalog.pipeline.methods import ROSTER_METHOD
from hvs_catalog.pipeline.mock import PipelineMockService, material_spec
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


def _upgrade_source_to_other_paper(space, paper_key_from: str) -> RecordRef:
    """Commit a new revision under the same source identifier with another paper's contents
    to simulate mismatched upgrades.

    Match the review reproducer: change paper_key to A and reference newly published
    files while keeping v1 and its files unchanged.
    """
    source_id = material_spec(paper_key_from)["source_id"]
    spec = material_spec(PAPER_A)
    from hvs_catalog.synthetic import material as m

    file_ref = space.publish_source_file("candidate/upgraded/a.tex", m.tex_bytes("A"))
    upgraded = build_record(
        SourcePackageRecord,
        record_id=source_id,
        revision=2,
        created_at=datetime.now(UTC),
        execution_id=None,
        data=SourcePackageData(
            paper_key=PAPER_A,
            origin={"kind": "synthetic"},
            source_version="fictional-v2",
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
    del spec
    space.store().commit(
        CommitRequest(
            operation_key=f"upgrade:{source_id}",
            kind="material_import",
            new_records=[upgraded],
            expected_revisions={source_id: 1},
            result_refs=[upgraded.ref()],
        )
    )
    return upgraded.ref()


def _start(space, source, *, request_key, method="roster", expert=False, mode="daily"):
    request = ExtractionRequest(
        mode=mode,
        method=method,
        source_refs=[source],
        models=[StepModel(model="mock-default")],
        expert_adjudication=expert,
    )
    return start_extraction(space, request=request, request_key=request_key)


def _record(space, record_id):
    revision = space.store().current_revision(record_id)
    assert revision is not None
    record = space.store().read_record(
        RecordRef(record_id=record_id, revision=revision)
    )
    assert record is not None
    return record


# ---- Review reproducer: frozen v1 source later becomes another paper under the same ID ----


def test_frozen_b_v1_used_after_same_id_upgraded_to_a(space):
    """Initial execution must use frozen B v1, not A v2 under the same identifier."""
    source = import_mock_source(space, PAPER_B)
    handle = _start(space, source, request_key="freeze-b")
    upgraded = _upgrade_source_to_other_paper(space, PAPER_B)
    assert upgraded.revision == 2
    assert space.store().current_revision(material_spec(PAPER_B)["source_id"]) == 2

    service = SpyService()
    report = resume_extraction(space, handle.execution_id, service=service)
    assert report.outcome == "finished", report.errors
    # Roster and paper both come from frozen FIC-R05-B, never A.
    roster = _record(space, f"roster-fic-r05-b-{handle.execution_id}")
    assert isinstance(roster, RosterRecord)
    assert roster.data.paper_key == "FIC-R05-B"
    paper = _record(space, f"paper-fic-r05-b-{handle.execution_id}")
    assert isinstance(paper, PaperRecord)
    value = paper.data.object_contributions[0].quantities[0].values[0]
    assert value.value == "480"  # B's fixed value; A has 520.
    assert (
        space.store().current_revision(f"roster-fic-r05-a-{handle.execution_id}")
        is None
    )
    assert (
        space.store().current_revision(f"paper-fic-r05-a-{handle.execution_id}") is None
    )
    assert all(item["request"]["paper_key"] == "FIC-R05-B" for item in service.requests)
    # The execution input digest stays unchanged.
    assert get_extraction(space, handle.execution_id)["config"]["input_digest"] == (
        handle.input_digest
    )


def test_frozen_c_v1_pause_resume_after_same_id_upgraded_to_a(space):
    """Paused roster recovery still uses frozen C v1."""
    source = import_mock_source(space, PAPER_C)
    handle = _start(space, source, request_key="freeze-c", method="roster", expert=True)
    first = resume_extraction(space, handle.execution_id, service=SpyService())
    assert first.outcome == "paused"
    _upgrade_source_to_other_paper(space, PAPER_C)

    service = SpyService()
    question = pending_questions(space, handle.execution_id)[0]
    submit_expert_decision(
        space,
        execution_id=handle.execution_id,
        question_ref=question.question_ref,
        request_key="freeze-c-decision",
        choice="include",
    )
    second = resume_extraction(space, handle.execution_id, service=service)
    assert second.outcome == "finished", second.errors
    roster = _record(space, f"roster-fic-candidate-c-{handle.execution_id}")
    assert roster.data.paper_key == "FIC-CANDIDATE-C"
    assert {e.item_id for e in roster.data.entries} == {
        "cand-c-1",
        "cand-c-2",
        "cand-c-3",
    }
    paper = _record(space, f"paper-fic-candidate-c-{handle.execution_id}")
    assert [c.roster_item_id for c in paper.data.object_contributions] == [
        "cand-c-1",
        "cand-c-3",
    ]
    assert (
        space.store().current_revision(f"roster-fic-r05-a-{handle.execution_id}")
        is None
    )
    assert all(
        item["request"]["paper_key"] == "FIC-CANDIDATE-C"
        for item in service.requests
        if item["step"] in ("roster", "quantities")
    )


SUBPROCESS_DRIVER = r"""
import json
import sys
from pathlib import Path

sys.path.insert(0, __SRC__)

from hvs_catalog.pipeline import (
    PAPER_C, ExtractionRequest, PipelineMockService, StepModel,
    import_mock_source, pending_questions, resume_extraction,
    start_extraction, submit_expert_decision, get_extraction,
)
from hvs_catalog.workspace import open_workspace


def emit(payload):
    print(json.dumps(payload, ensure_ascii=False))
    sys.stdout.flush()


command = sys.argv[1]
root = Path(sys.argv[2])

if command == "run-to-pause":
    space = open_workspace(root)
    source = space.store().read_record_current("src-candidate-c")
    request = ExtractionRequest(
        mode="synthetic", method="roster",
        source_refs=[source.ref()],
        models=[StepModel(model="mock-default")], expert_adjudication=True,
    )
    handle = start_extraction(space, request=request, request_key="cross-freeze")
    report = resume_extraction(
        space, handle.execution_id, service=PipelineMockService()
    )
    emit({"execution_id": handle.execution_id, "outcome": report.outcome})
elif command == "resume":
    execution_id = sys.argv[3]
    space = open_workspace(root)
    question = pending_questions(space, execution_id)[0]
    submit_expert_decision(
        space, execution_id=execution_id, question_ref=question.question_ref,
        request_key="cross-decision", choice="include",
    )
    report = resume_extraction(
        space, execution_id, service=PipelineMockService()
    )
    status = get_extraction(space, execution_id)
    emit({
        "execution_id": execution_id,
        "outcome": report.outcome,
        "errors": list(report.errors),
        "digest": status["config"]["input_digest"],
    })
"""


def _run_driver(tmp_path, *args, timeout=90.0):
    script = tmp_path / "source_freeze_driver.py"
    script.write_text(
        SUBPROCESS_DRIVER.replace(
            "__SRC__", repr(str(Path(__file__).resolve().parents[1] / "src"))
        ),
        encoding="utf-8",
    )
    result = subprocess.run(
        [sys.executable, str(script), *map(str, args)],
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
        cwd=tmp_path,
    )
    lines = [line for line in result.stdout.splitlines() if line.strip()]
    payload = json.loads(lines[-1]) if lines else None
    assert result.returncode == 0, result.stderr
    return payload


@pytest.mark.integration
def test_new_process_recovery_uses_frozen_source_revision(tmp_path):
    """Fresh-process recovery completes with frozen C v1 despite source upgrades, retaining
    the digest.
    """
    from hvs_catalog.workspace import initialize_workspace

    space = initialize_workspace(tmp_path / "space")
    import_mock_source(space, PAPER_C)
    space.close()

    first = _run_driver(tmp_path, "run-to-pause", tmp_path / "space")
    assert first["outcome"] == "paused"
    execution_id = first["execution_id"]

    # After the subprocess exits, upgrade the source to another paper's v2 in the same workspace.
    space = __import__(
        "hvs_catalog.workspace", fromlist=["open_workspace"]
    ).open_workspace(tmp_path / "space")
    try:
        _upgrade_source_to_other_paper(space, PAPER_C)
    finally:
        space.close()

    second = _run_driver(tmp_path, "resume", tmp_path / "space", execution_id)
    assert second["outcome"] == "finished", second["errors"]

    space = __import__(
        "hvs_catalog.workspace", fromlist=["open_workspace"]
    ).open_workspace(tmp_path / "space")
    try:
        roster = _record(space, f"roster-fic-candidate-c-{execution_id}")
        assert roster.data.paper_key == "FIC-CANDIDATE-C"
        assert (
            space.store().current_revision(f"roster-fic-r05-a-{execution_id}") is None
        )
    finally:
        space.close()


# ---- Reject mismatches and missing exact revisions before service calls ----


def _bypass_execution(space, source_ref, *, method_spec=ROSTER_METHOD, request_key):
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


def test_missing_exact_source_revision_rejected_before_service(space):
    """Missing frozen revisions block recovery with zero calls and no fallback to current."""
    from hvs_catalog.workspace import RecoveryInconsistentError

    source = import_mock_source(space, PAPER_B)
    _upgrade_source_to_other_paper(space, PAPER_B)  # current=v2(A)
    stale = RecordRef(record_id=source.record_id, revision=99)
    handle = _bypass_execution(space, stale, request_key="missing-rev")
    service = SpyService()
    with pytest.raises(RecoveryInconsistentError, match="不能用当前新版替代原输入"):
        resume_execution(
            space,
            handle.execution_id,
            ExtractionFlow(ROSTER_METHOD, mode="daily"),
            service=service,
        )
    assert service.requests == []
    # Produce no scientific records.
    status = get_extraction(space, handle.execution_id)
    assert status["result_refs"] == []
    # Entry-point recovery likewise rejects.
    with pytest.raises(RecoveryInconsistentError):
        resume_extraction(space, handle.execution_id, service=SpyService())


def test_identity_mismatch_with_registered_mock_rejected(space):
    """An existing exact revision outside registered mock papers rejects before any service
    call.
    """

    source_id = "src-unsupported-freeze"
    file_ref = space.publish_source_file("candidate/unsupported/x.tex", b"fictional\n")
    record = build_record(
        SourcePackageRecord,
        record_id=source_id,
        created_at=datetime.now(UTC),
        execution_id=None,
        data=SourcePackageData(
            paper_key="FIC-UNKNOWN-9",
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
            operation_key="import:unsupported-freeze",
            kind="material_import",
            new_records=[record],
            result_refs=[record.ref()],
        )
    )
    handle = _bypass_execution(space, record.ref(), request_key="mismatch")
    service = SpyService()
    report = resume_execution(
        space,
        handle.execution_id,
        ExtractionFlow(ROSTER_METHOD, mode="daily"),
        service=service,
    )
    assert report.outcome == "failed"
    assert any("不在模拟材料支持范围" in error for error in report.errors)
    assert service.requests == []
    assert get_extraction(space, handle.execution_id)["result_refs"] == []


def test_stale_revision_pointer_rejected_even_though_current_valid(space):
    """Never substitute current when a snapshot references an older revision superseded by
    a later one.
    """
    source = import_mock_source(space, PAPER_B)  # v1
    _upgrade_source_to_other_paper(space, PAPER_B)  # v2=A
    # Build execution referencing v1; read B v1 even when current is A v2.
    handle = _bypass_execution(space, source, request_key="stale-v1")
    service = SpyService()
    report = resume_execution(
        space,
        handle.execution_id,
        ExtractionFlow(ROSTER_METHOD, mode="daily"),
        service=service,
    )
    assert report.outcome == "finished", report.errors
    paper = _record(space, f"paper-fic-r05-b-{handle.execution_id}")
    assert paper.data.paper_key == "FIC-R05-B"
    assert all(item["request"]["paper_key"] == "FIC-R05-B" for item in service.requests)


# ---- Valid sources without upgrades remain supported ----


def test_non_source_record_at_frozen_revision_rejected(space):
    """An exact revision that is not a source package, such as evidence, blocks recovery
    without service calls.
    """
    from hvs_catalog.workspace import RecoveryInconsistentError

    import_mock_source(space, PAPER_B)
    evidence_ref = RecordRef(record_id="ev-r05-b-1", revision=1)
    handle = _bypass_execution(space, evidence_ref, request_key="non-source")
    service = SpyService()
    with pytest.raises(RecoveryInconsistentError, match="不是来源包"):
        resume_execution(
            space,
            handle.execution_id,
            ExtractionFlow(ROSTER_METHOD, mode="daily"),
            service=service,
        )
    assert service.requests == []
    assert get_extraction(space, handle.execution_id)["result_refs"] == []


def test_unupgraded_source_resume_unchanged(space):
    """Unchanged sources retain normal frozen-execution completion and reuse."""
    source = import_mock_source(space, PAPER_B)
    handle = _start(space, source, request_key="no-upgrade")
    service = SpyService()
    report = resume_extraction(space, handle.execution_id, service=service)
    assert report.outcome == "finished"
    assert report.errors == []
    again = resume_extraction(space, handle.execution_id, service=service)
    assert again.outcome == "finished"
    assert again.mode == "already-finished"

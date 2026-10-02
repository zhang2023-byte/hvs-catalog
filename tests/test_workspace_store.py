"""Business store: exact-revision reads/saves, atomicity, operation-key replay, and
consistent current output (A05/A06/A14).

Records use fictional shared fixtures whose rewritten paths are published under
workspace sources/.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from records_helpers import load_fixture
from workspace_helpers import publish_fixture_collection

from hvs_catalog.records import build_record, current_rules_ref
from hvs_catalog.records.runs import InputSnapshot, RunData, RunRecord, Usage
from hvs_catalog.workspace import (
    CommitRequest,
    OperationKeyConflictError,
    RevisionStaleError,
    WorkspaceIntegrityError,
    initialize_workspace,
)

STAMP = datetime(2026, 9, 21, tzinfo=UTC)


@pytest.fixture()
def space(tmp_path):
    space = initialize_workspace(tmp_path / "space")
    yield space
    space.close()


def _commit(space, records, key, **kwargs):
    # Commit question records and step bindings together; tests default to a fixed step name.
    kwargs.setdefault(
        "question_steps",
        {r.record_id: "fixture-step" for r in records if r.RECORD_NAME == "question"},
    )
    request = CommitRequest(
        operation_key=key,
        kind=kwargs.pop("kind", "manual"),
        new_records=records,
        **kwargs,
    )
    return space.store().commit(request)


def _collection(space, names=("gap-conflict", "numeric-forms")):
    records: list = []
    for name in names:
        records.extend(publish_fixture_collection(space, load_fixture(name)))
    return records


def test_commit_and_read_back_all_ten_record_types_by_exact_revision(space):
    records = _collection(space)
    receipt = _commit(space, records, "op-init")
    assert not receipt.replayed
    assert receipt.operation_key == "op-init"
    store = space.store()
    types = {record.RECORD_NAME for record in records}
    assert {
        "source_package",
        "evidence",
        "paper",
        "roster",
        "question",
        "decision",
        "object",
        "external_record",
        "run",
        "release",
    } == types & {
        "source_package",
        "evidence",
        "paper",
        "roster",
        "question",
        "decision",
        "object",
        "external_record",
        "run",
        "release",
    }
    for record in records:
        loaded = store.read_record(record.ref())
        assert loaded == record
    latest: dict[str, int] = {}
    for record in records:
        latest[record.record_id] = max(latest.get(record.record_id, 0), record.revision)
    for record_id, revision in latest.items():
        assert store.current_revision(record_id) == revision
    # Missing revisions never implicitly fall back to latest.
    missing = store.read_record(
        records[0].ref().model_copy(update={"revision": records[0].revision + 5})
    )
    assert missing is None


def test_old_revision_and_its_references_are_preserved_after_new_revision(space):
    records = _collection(space, names=("gap-conflict",))
    _commit(space, records, "op-init")
    store = space.store()
    roster = next(r for r in records if r.RECORD_NAME == "roster")
    papers = sorted(
        (r for r in records if r.RECORD_NAME == "paper"), key=lambda r: r.revision
    )
    paper = papers[
        -1
    ]  # Fixture papers already have two revisions; continue from current.
    paper_current = store.current_revision(paper.record_id)
    assert paper_current == paper.revision

    # A new roster revision without an updated paper rejects atomically for inconsistent current output.
    roster_v2 = roster.model_copy(update={"revision": 2, "created_at": STAMP})
    with pytest.raises(WorkspaceIntegrityError, match="当前输出不一致"):
        _commit(
            space,
            [roster_v2],
            "op-roster-v2-alone",
            expected_revisions={roster.record_id: 1},
        )
    paper_v_next = paper.model_copy(
        update={
            "revision": paper_current + 1,
            "created_at": STAMP,
            "data": paper.data.model_copy(
                update={
                    "roster_ref": paper.data.roster_ref.model_copy(
                        update={"revision": 2}
                    )
                }
            ),
        }
    )
    receipt = _commit(
        space,
        [roster_v2, paper_v_next],
        "op-roster-v2-with-paper",
        expected_revisions={roster.record_id: 1, paper.record_id: paper_current},
    )
    assert not receipt.replayed

    # Retain old revisions verbatim; new revisions never replace old value strings or quotes.
    old_roster = store.read_record(roster.ref())
    assert old_roster == roster
    old_paper = store.read_record(paper.ref())
    assert old_paper.data.roster_ref.revision == paper.data.roster_ref.revision
    # Exact old references return old contents without implicit latest-version substitution.
    assert store.read_record_current(roster.record_id).revision == 2
    assert store.read_record_current(paper.record_id).revision == paper_current + 1


def test_batch_is_atomic_when_validation_fails(space):
    records = _collection(space, names=("gap-conflict",))
    paper = next(r for r in records if r.RECORD_NAME == "paper")
    # A broken paper references a nonexistent roster revision.
    broken = paper.model_copy(
        update={
            "record_id": "broken-paper",
            "revision": 1,
            "data": paper.data.model_copy(
                update={
                    "roster_ref": paper.data.roster_ref.model_copy(
                        update={"revision": 99}
                    )
                }
            ),
        }
    )
    broken_records = list(records) + [broken]
    with pytest.raises(WorkspaceIntegrityError, match="未通过校验"):
        _commit(space, broken_records, "op-broken")
    # Atomic rollback leaves no records or receipts in the database.
    store = space.store()
    assert store.list_records() == []
    assert store.find_receipt("op-broken") is None


def test_expected_revision_stale_rejects_and_keeps_state(space):
    records = _collection(space, names=("gap-conflict",))
    _commit(space, records, "op-init")
    evidence = next(r for r in records if r.RECORD_NAME == "evidence")
    evidence_v2 = evidence.model_copy(update={"revision": 2, "created_at": STAMP})
    with pytest.raises(RevisionStaleError, match="预期当前修订"):
        _commit(
            space,
            [evidence_v2],
            "op-stale",
            expected_revisions={evidence.record_id: 5},
        )
    assert space.store().current_revision(evidence.record_id) == 1
    ok = _commit(
        space,
        [evidence_v2],
        "op-fresh",
        expected_revisions={evidence.record_id: 1},
    )
    assert not ok.replayed
    # Reject concurrent updates expecting an old revision when current is already two; never overwrite newer contents.
    with pytest.raises(RevisionStaleError):
        _commit(
            space,
            [evidence.model_copy(update={"revision": 3, "created_at": STAMP})],
            "op-late",
            expected_revisions={evidence.record_id: 1},
        )


def test_same_operation_key_replay_returns_original_receipt(space):
    records = _collection(space, names=("gap-conflict",))
    first = _commit(space, records, "op-key-1")
    replay = _commit(space, records, "op-key-1")
    assert replay.replayed
    assert replay.created_at == first.created_at
    assert replay.content_digest == first.content_digest
    # Replay creates no new revision.
    assert len(space.store().list_records()) == len(records)
    # Reject same-key/different-content requests.
    changed = [records[0].model_copy(update={"record_id": "other"})]
    with pytest.raises(OperationKeyConflictError, match="不同内容"):
        _commit(space, changed, "op-key-1")
    # Same contents under another operation key returns a no-change receipt when records already match.
    duplicate = _commit(space, records, "op-key-2")
    assert duplicate.replayed
    assert len(space.store().list_records()) == len(records)
    # Identical identifiers/revisions can save only identical contents.
    tampered = [
        record.model_copy(
            update={
                "data": record.data.model_copy(
                    update={
                        next(
                            field
                            for field in ("paper_key", "prompt", "execution_id")
                            if hasattr(record.data, field)
                        ): "tampered"
                    }
                )
            }
        )
        if record.record_id == records[0].record_id
        else record
        for record in records
    ]
    with pytest.raises(WorkspaceIntegrityError, match="相同内容"):
        _commit(space, tampered, "op-tamper")


def test_roster_revision_requires_referencing_paper_update(space):
    """A14 storage: update new rosters together with referencing papers; either missing
    side rejects atomically.
    """
    records = _collection(space, names=("gap-conflict",))
    _commit(space, records, "op-init")
    roster = next(r for r in records if r.RECORD_NAME == "roster")

    roster_v2 = roster.model_copy(update={"revision": 2, "created_at": STAMP})
    with pytest.raises(WorkspaceIntegrityError, match="当前输出不一致"):
        _commit(
            space,
            [roster_v2],
            "op-missing-paper",
            expected_revisions={roster.record_id: 1},
        )
    # Rejection persists no roster revision; old records cannot bypass correspondence checks.
    assert space.store().current_revision(roster.record_id) == 1
    assert space.store().find_receipt("op-missing-paper") is None


def test_run_record_artifact_references_are_verified(space):
    """This layer verifies run archive references and rejects commits for missing files or
    changed hashes.
    """
    snapshot = InputSnapshot(
        source_refs=[],
        rules=current_rules_ref(),
        method=None,
        models=[],
        code_version="test-code",
        dependency_versions={},
        expert_adjudication=True,
        prompt_refs=[
            {
                "path": "runs/exec-x/missing-prompt.md",
                "sha256": "0" * 64,
            }
        ],
        example_refs=[],
        precedent_refs=[],
        existing_decision_refs=[],
    )
    run = build_record(
        RunRecord,
        record_id="run-broken",
        created_at=STAMP,
        execution_id="exec-x",
        data=RunData(
            execution_id="exec-x",
            mode="synthetic",
            input_snapshot=snapshot,
            state="created",
            steps=[],
            result_refs=[],
            model_judgments=[],
            usage=Usage(tokens=None, cost=None, currency=None),
            errors=[],
        ),
    )
    with pytest.raises(WorkspaceIntegrityError, match="缺失或不可读"):
        _commit(space, [run], "op-run-missing")
    # The file exists but its hash differs.
    ref = space.publish_run_file("exec-x", "missing-prompt.md", b"actual prompt")
    snapshot_ok = snapshot.model_copy(
        update={"prompt_refs": [ref.model_copy(update={"sha256": "1" * 64})]}
    )
    run_wrong_hash = run.model_copy(
        update={"data": run.data.model_copy(update={"input_snapshot": snapshot_ok})}
    )
    with pytest.raises(WorkspaceIntegrityError, match="哈希与记录不符"):
        _commit(space, [run_wrong_hash], "op-run-hash")
    good = run.model_copy(
        update={
            "data": run.data.model_copy(
                update={
                    "input_snapshot": snapshot.model_copy(update={"prompt_refs": [ref]})
                }
            )
        }
    )
    receipt = _commit(space, [good], "op-run-good")
    assert not receipt.replayed

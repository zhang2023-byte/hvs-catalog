"""Shared transactions preserve exact revisions, references, and replay receipts."""

from datetime import UTC, datetime

import pytest

from hvs_catalog.records import build_record
from hvs_catalog.records.common import ArtifactRef
from hvs_catalog.records.source import (
    EvidenceData,
    EvidenceRecord,
    SourcePackageData,
    SourcePackageRecord,
)
from hvs_catalog.workspace import (
    CommitRequest,
    OperationKeyConflictError,
    RevisionStaleError,
    WorkspaceIntegrityError,
    initialize_workspace,
)


@pytest.fixture
def collection(tmp_path):
    space = initialize_workspace(tmp_path / "space")
    ref = space.publish_file(
        "sources/synthetic/text.txt", b"A is a candidate.\nAlias-A refers to A."
    )
    source = build_record(
        SourcePackageRecord,
        record_id="source",
        created_at=datetime(2026, 10, 16, tzinfo=UTC),
        execution_id=None,
        data=SourcePackageData(
            paper_key="synthetic",
            origin={"kind": "synthetic"},
            source_version="1",
            files=[
                {
                    "file_id": "text",
                    "path": ref.path,
                    "sha256": ref.sha256,
                    "media_type": "text/plain",
                    "role": "text",
                    "read_state": "ready",
                    "derived_from": [],
                }
            ],
        ),
    )
    evidence = build_record(
        EvidenceRecord,
        record_id="evidence",
        created_at=source.created_at,
        execution_id=None,
        data=EvidenceData(
            paper_key="synthetic",
            source_ref=source.ref(),
            file_id="text",
            locator={"kind": "text", "start_line": 1, "end_line": 1},
            excerpt="A is a candidate.",
        ),
    )
    yield space, source, evidence
    space.close()


def commit(space, records, key, **kwargs):
    return space.store().commit(
        CommitRequest(operation_key=key, kind="manual", new_records=records, **kwargs)
    )


def test_replay_and_conflict_are_atomic(collection):
    space, source, evidence = collection
    receipt = commit(space, [source, evidence], "first")
    replay = commit(space, [source, evidence], "first")
    assert replay.replayed and replay.content_digest == receipt.content_digest
    assert replay.created_at == receipt.created_at
    assert commit(space, [source, evidence], "duplicate").replayed
    with pytest.raises(OperationKeyConflictError):
        commit(space, [source], "first")
    changed = source.model_copy(
        update={"data": source.data.model_copy(update={"paper_key": "different"})}
    )
    with pytest.raises(WorkspaceIntegrityError):
        commit(space, [changed], "overwrite")
    assert len(space.store().list_records()) == 2
    assert space.store().read_record(source.ref()) == source


def test_stale_revision_and_exact_reference(collection):
    space, source, evidence = collection
    commit(space, [source, evidence], "first")
    next_source = source.model_copy(update={"revision": 2})
    with pytest.raises(RevisionStaleError):
        commit(space, [next_source], "stale", expected_revisions={"source": 5})
    assert space.store().find_receipt("stale") is None
    commit(space, [next_source], "fresh", expected_revisions={"source": 1})
    assert space.store().read_record(source.ref()) == source
    assert space.store().read_record_current("source") == next_source
    assert space.store().read_record(evidence.ref()).data.source_ref == source.ref()
    assert (
        space.store().read_record(source.ref().model_copy(update={"revision": 99}))
        is None
    )
    with pytest.raises(RevisionStaleError):
        commit(
            space,
            [next_source.model_copy(update={"revision": 3})],
            "race",
            expected_revisions={"source": 1},
        )


@pytest.mark.parametrize("fault", ["reference", "paper", "file", "quote", "location"])
def test_bad_evidence_rolls_back_whole_transaction(collection, fault):
    space, source, evidence = collection
    update = {
        "reference": {"source_ref": source.ref().model_copy(update={"revision": 99})},
        "paper": {"paper_key": "another-paper"},
        "file": {"file_id": "missing"},
        "quote": {"excerpt": "An altered quotation."},
        "location": {
            "locator": evidence.data.locator.model_copy(
                update={"start_line": 99, "end_line": 99}
            )
        },
    }[fault]
    bad = evidence.model_copy(update={"data": evidence.data.model_copy(update=update)})
    with pytest.raises(WorkspaceIntegrityError):
        commit(space, [source, bad], "bad")
    assert space.store().list_records() == []
    assert space.store().find_receipt("bad") is None


@pytest.mark.parametrize("fault", ["missing", "changed", "symlink"])
def test_material_integrity_precedes_commit(collection, tmp_path, fault):
    space, source, evidence = collection
    path = space.root / source.data.files[0].path
    if fault == "missing":
        path.unlink()
    elif fault == "changed":
        path.write_text("changed")
    else:
        outside = tmp_path / "outside.txt"
        outside.write_bytes(path.read_bytes())
        path.unlink()
        path.symlink_to(outside)
    with pytest.raises(WorkspaceIntegrityError):
        commit(space, [source, evidence], "bad")
    assert space.store().list_records() == []
    with pytest.raises(WorkspaceIntegrityError):
        space.read_file(
            ArtifactRef(
                path=source.data.files[0].path, sha256=source.data.files[0].sha256
            )
        )


def test_old_preparation_diagnostics_are_read_without_mutating_input(collection):
    import copy

    from hvs_catalog.records.serialize import parse_record

    _, source, _ = collection
    old = source.model_dump(mode="json", by_alias=True)
    old["data"].pop("preparation_issues")
    old["data"]["gaps"] = [
        {"gap_id": "synthetic", "description": "Parser omitted PDF page 2."}
    ]
    before = copy.deepcopy(old)
    loaded = parse_record(old)
    assert loaded.data.preparation_issues == ["Parser omitted PDF page 2."]
    assert old == before
    old["schema"]["name"] = "roster"
    with pytest.raises(ValueError, match="未知记录类型"):
        parse_record(old)

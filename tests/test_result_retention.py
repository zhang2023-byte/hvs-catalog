"""Execution close preserves response history and shared reasoning references."""

import base64
import json
import sqlite3

import pytest
from test_roster import run, submission

from hvs_catalog.pipeline import (
    continue_format_corrections,
    get_extraction,
    resume_extraction,
    retry_network_failures,
)
from hvs_catalog.workspace import WorkspaceIntegrityError
from hvs_catalog.workspace.archives import publish_json, read_json
from hvs_catalog.workspace.retention import close_extraction, retained_result


def test_shared_text_and_images_reconstruct_exactly(prepared):
    space = prepared[0]
    value = {
        "messages": [
            {
                "reasoning_content": "Short reasoning.",
                "content": "Synthetic shared input Ω\n" * 20000,
            }
        ],
        "image": "data:image/png;base64,"
        + base64.b64encode(b"synthetic image" * 1000).decode(),
    }
    first = publish_json(space, "runs/inputs/first.json", value)
    second = publish_json(space, "runs/inputs/second.json", value)
    assert read_json(space, first) == read_json(space, second) == value
    shared = list((space.root / "runs/inputs/shared").glob("*.txt"))
    assert len(shared) == 2
    assert len(list((space.root / "sources/shared-images").glob("*.bin"))) == 1
    assert (space.root / first.path).stat().st_size < 1024
    shared[0].write_text("damaged")
    with pytest.raises(WorkspaceIntegrityError):
        read_json(space, first)


def test_failed_execution_keeps_history_until_explicit_close(prepared):
    handle, report, adapter, _ = run(prepared, [{"stars": [], "notes": None}])
    space = prepared[0]
    assert report.outcome == "finished"
    assert report.result_status != "complete"
    assert retained_result(space, handle.execution_id) is None
    with pytest.raises(ValueError, match="明确结束"):
        close_extraction(space, handle.execution_id)
    closed = close_extraction(space, handle.execution_id, end=True)
    assert closed["status"]["state"] == "finished"
    assert len(closed["calls"]) == len(adapter.seen) == 3
    assert get_extraction(space, handle.execution_id)["resumable"] is False
    assert resume_extraction(space, handle.execution_id).outcome == "finished"
    assert close_extraction(space, handle.execution_id, end=True) == closed
    for action in (continue_format_corrections, retry_network_failures):
        with pytest.raises(ValueError, match="已明确结束"):
            action(space, handle.execution_id, request_key="must-not-reopen")
    assert retained_result(space, handle.execution_id) == closed


def test_closed_result_detects_changed_scientific_record(prepared):
    handle, _, _, _ = run(prepared, [submission()])
    space = prepared[0]
    result = retained_result(space, handle.execution_id)
    payload = next(
        p for p in result["records"] if p["schema"]["name"] == "roster_snapshot"
    )
    changed = {**payload, "created_at": "2000-01-01T00:00:00Z"}
    with sqlite3.connect(space.root / "business.db") as conn:
        conn.execute(
            "UPDATE records SET payload=? WHERE record_id=? AND revision=?",
            (json.dumps(changed), payload["record_id"], payload["revision"]),
        )
    with pytest.raises(WorkspaceIntegrityError, match="科学记录"):
        retained_result(space, handle.execution_id)


def test_publication_failure_does_not_remove_recovery_history(prepared, monkeypatch):
    space = prepared[0]
    original = space.store().commit

    def fail(request):
        if any(key.startswith("retained_result:") for key in request.meta_updates):
            raise RuntimeError("synthetic crash before result marker")
        return original(request)

    monkeypatch.setattr(space.store(), "commit", fail)
    with pytest.raises(RuntimeError, match="synthetic crash"):
        run(prepared, [submission()])
    # Preparation is the first execution. The failed close belongs to the second.
    execution_id = "exec-000002"
    assert retained_result(space, execution_id) is None
    with sqlite3.connect(space.root / "checkpoint.db") as conn:
        assert (
            conn.execute(
                "SELECT count(*) FROM checkpoints WHERE thread_id=?", (execution_id,)
            ).fetchone()[0]
            > 0
        )
    monkeypatch.setattr(space.store(), "commit", original)
    assert close_extraction(space, execution_id)["status"]["state"] == "finished"
    closed = retained_result(space, execution_id)
    assert (
        closed["calls"][0]["response"]["exchange"]["body"]["choices"][0]["message"][
            "reasoning_content"
        ]
        == "Synthetic reasoning for call 1"
    )

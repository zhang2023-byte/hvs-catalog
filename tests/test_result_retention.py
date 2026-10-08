"""Compact results preserve science and submissions while ending recovery history."""

import base64
import json
import sqlite3
from contextlib import closing

import pytest
import test_pdf_quantities as fixtures
from pydantic import SecretStr

from hvs_catalog.llm.managed import CallService
from hvs_catalog.pipeline import resume_extraction, start_pdf_extraction
from hvs_catalog.workspace import WorkspaceIntegrityError, open_workspace
from hvs_catalog.workspace.archives import publish_json, read_json
from hvs_catalog.workspace.backup import backup_workspace, restore_workspace
from hvs_catalog.workspace.retention import close_extraction, retained_result


@pytest.fixture
def prepared(tmp_path):
    yield from fixtures.prepared_pdf.__wrapped__(tmp_path)


def run(prepared, respond):
    space, prep, config = prepared
    handle = start_pdf_extraction(
        space,
        preparation_execution_id=prep,
        configuration=config,
        request_key="retention",
    )
    adapter = fixtures.Adapter(respond, names=("A",))
    report = resume_extraction(
        space,
        handle.execution_id,
        service=CallService(
            config.runtime, credential=SecretStr("synthetic"), adapter=adapter
        ),
    )
    return space, handle, adapter, report


def test_shared_inputs_reconstruct_exactly_and_detect_corruption(prepared):
    space, _, _ = prepared
    text = "Synthetic shared input Ω\n" * 20000
    value = {"payload": {"messages": [{"content": text}]}}
    first = publish_json(space, "runs/inputs/first.json", value)
    second = publish_json(space, "runs/inputs/second.json", value)
    shared = list((space.root / "runs/inputs/shared").glob("*.txt"))
    assert len(shared) == 1
    assert (space.root / first.path).stat().st_size < 512
    assert read_json(space, first) == read_json(space, second) == value
    shared[0].write_text("damaged")
    with pytest.raises(WorkspaceIntegrityError):
        read_json(space, first)


def test_shared_images_are_saved_once(prepared):
    space, _, _ = prepared
    value = {
        "image": "data:image/png;base64,"
        + base64.b64encode(b"synthetic image" * 1000).decode()
    }
    first = publish_json(space, "runs/inputs/image-first.json", value)
    second = publish_json(space, "runs/inputs/image-second.json", value)
    assert read_json(space, first) == read_json(space, second) == value
    assert len(list((space.root / "sources/shared-images").glob("*.bin"))) == 1


def test_finished_result_keeps_each_correction_and_supports_new_experiment(
    prepared, tmp_path
):
    def respond(tool, _):
        if tool["name"] == "repair_quantities":
            return {key: fixtures.cite() for key in tool["parameters"]["properties"]}
        value = fixtures.quantities(tool["_object_id"])
        value["measurements"][0]["evidence"][0]["quote"] = "Incorrect quotation"
        return value

    space, handle, adapter, report = run(prepared, respond)
    assert report.outcome == "finished", report.errors
    result = retained_result(space, handle.execution_id)
    assert len(result["calls"]) == len(adapter.seen) == 3
    functions = {
        function["name"]: json.loads(function["arguments"])
        for call in result["calls"]
        for function in [
            call["response"]["exchange"]["body"]["choices"][0]["message"]["tool_calls"][
                0
            ]["function"]
        ]
    }
    assert (
        functions["submit_quantities"]["measurements"][0]["evidence"][0]["quote"]
        == "Incorrect quotation"
    )
    assert all(
        value == fixtures.cite() for value in functions["repair_quantities"].values()
    )
    with sqlite3.connect(space.root / "business.db") as conn:
        assert (
            conn.execute(
                "SELECT count(*) FROM records WHERE schema_name='run' AND execution_id=?",
                (handle.execution_id,),
            ).fetchone()[0]
            == 1
        )
    with sqlite3.connect(space.root / "checkpoint.db") as conn:
        assert (
            conn.execute(
                "SELECT count(*) FROM checkpoints WHERE thread_id=?",
                (handle.execution_id,),
            ).fetchone()[0]
            == 0
        )
    assert not list(space.root.rglob("*.raw"))
    archived_requests = []
    for call in result["calls"]:
        from hvs_catalog.records.common import ArtifactRef

        archived_requests.append(
            read_json(space, ArtifactRef.model_validate(call["facts"]["request"]))[
                "payload"
            ]
        )
    assert sorted(json.dumps(p, sort_keys=True) for p in archived_requests) == sorted(
        json.dumps(p, sort_keys=True) for p in adapter.seen
    )
    assert resume_extraction(space, handle.execution_id).outcome == "finished"
    assert len(adapter.seen) == 3
    repeated = start_pdf_extraction(
        space,
        preparation_execution_id=prepared[1],
        configuration=prepared[2],
        request_key="retention",
    )
    assert repeated.execution_id == handle.execution_id
    backup_workspace(space, tmp_path / "backup")
    restore_workspace(tmp_path / "backup", tmp_path / "restored")
    with closing(open_workspace(tmp_path / "restored")) as restored:
        assert retained_result(restored, handle.execution_id) == result
    fresh = start_pdf_extraction(
        space,
        preparation_execution_id=prepared[1],
        configuration=prepared[2],
        request_key="next",
    )
    assert fresh.execution_id != handle.execution_id


def test_failed_execution_retains_recovery_until_explicit_end(prepared):
    space, handle, adapter, report = run(
        prepared, lambda *_: {"not_a_submission": True}
    )
    assert report.outcome == "failed"
    assert retained_result(space, handle.execution_id) is None
    with pytest.raises(ValueError, match="明确结束"):
        close_extraction(space, handle.execution_id)
    closed = close_extraction(space, handle.execution_id, end=True)
    assert closed["status"]["state"] == "failed"
    assert len(closed["calls"]) == len(adapter.seen)
    from hvs_catalog.pipeline import get_extraction

    assert get_extraction(space, handle.execution_id)["resumable"] is False
    assert resume_extraction(space, handle.execution_id).outcome == "failed"
    assert close_extraction(space, handle.execution_id, end=True) == closed
    from hvs_catalog.pipeline import continue_format_corrections, retry_network_failures

    for action in (continue_format_corrections, retry_network_failures):
        with pytest.raises(ValueError, match="已明确结束"):
            action(space, handle.execution_id, request_key="must-not-reopen")
    assert retained_result(space, handle.execution_id) == closed


def test_damaged_retained_result_cannot_bypass_verification(prepared, tmp_path):
    space, handle, _, report = run(
        prepared, lambda tool, _: fixtures.quantities(tool["_object_id"])
    )
    assert report.outcome == "finished", report.errors
    (space.root / "runs" / handle.execution_id / "result.json").write_text("{}")
    with pytest.raises(WorkspaceIntegrityError):
        resume_extraction(space, handle.execution_id)
    with pytest.raises(WorkspaceIntegrityError):
        backup_workspace(space, tmp_path / "backup")


def test_closed_result_detects_changed_scientific_record(prepared):
    space, handle, _, report = run(
        prepared, lambda tool, _: fixtures.quantities(tool["_object_id"])
    )
    assert report.outcome == "finished"
    result = retained_result(space, handle.execution_id)
    payload = next(p for p in result["records"] if p["schema"]["name"] == "paper")
    changed = {**payload, "created_at": "2000-01-01T00:00:00Z"}
    with sqlite3.connect(space.root / "business.db") as conn:
        conn.execute(
            "UPDATE records SET payload=? WHERE record_id=? AND revision=?",
            (json.dumps(changed), payload["record_id"], payload["revision"]),
        )
    with pytest.raises(WorkspaceIntegrityError, match="科学记录"):
        retained_result(space, handle.execution_id)


def test_publication_failure_does_not_remove_recovery_history(prepared, monkeypatch):
    space, prep, config = prepared
    handle = start_pdf_extraction(
        space, preparation_execution_id=prep, configuration=config, request_key="crash"
    )
    original = space.store().set_meta

    def fail(key, value):
        if key.startswith("retained_result:"):
            raise RuntimeError("synthetic crash before result marker")
        return original(key, value)

    monkeypatch.setattr(space.store(), "set_meta", fail)
    with pytest.raises(RuntimeError, match="synthetic crash"):
        resume_extraction(
            space,
            handle.execution_id,
            service=CallService(
                config.runtime,
                credential=SecretStr("synthetic"),
                adapter=fixtures.Adapter(
                    lambda tool, _: fixtures.quantities(tool["_object_id"])
                ),
            ),
        )
    assert retained_result(space, handle.execution_id) is None
    with sqlite3.connect(space.root / "checkpoint.db") as conn:
        assert (
            conn.execute(
                "SELECT count(*) FROM checkpoints WHERE thread_id=?",
                (handle.execution_id,),
            ).fetchone()[0]
            > 0
        )
    monkeypatch.setattr(space.store(), "set_meta", original)
    assert close_extraction(space, handle.execution_id)["status"]["state"] == "finished"


def test_legacy_compaction_survives_failure_before_result_marker(prepared, monkeypatch):
    from hvs_catalog.records.common import canonical_json
    from hvs_catalog.workspace import CommitRequest, retention

    with monkeypatch.context() as patch:
        patch.setattr(retention, "close_extraction", lambda *a, **kw: None)
        space, handle, _, report = run(
            prepared, lambda tool, _: fixtures.quantities(tool["_object_id"])
        )
    assert report.outcome == "finished"
    current = space.store().read_record_current(
        space.store().get_execution(handle.execution_id).run_record_id
    )
    step = next(s for s in current.data.steps if s.attempts)
    attempt = step.attempts[0]
    inline = read_json(space, attempt.request)
    legacy = space.publish_run_file(
        handle.execution_id, "requests/legacy.json", canonical_json(inline).encode()
    )
    replacement = current.model_copy(
        update={
            "revision": current.revision + 1,
            "data": current.data.model_copy(
                update={
                    "steps": [
                        s.model_copy(
                            update={
                                "attempts": [
                                    attempt.model_copy(update={"request": legacy}),
                                    *step.attempts[1:],
                                ]
                            }
                        )
                        if s.step_id == step.step_id
                        else s
                        for s in current.data.steps
                    ]
                }
            ),
        }
    )
    space.store().commit(
        CommitRequest(
            operation_key="synthetic-legacy",
            kind="retention",
            new_records=[replacement],
            execution_id=handle.execution_id,
            expected_revisions={current.record_id: current.revision},
        )
    )
    original = space.store().set_meta

    def fail(key, value):
        if key.startswith("retained_result:"):
            raise RuntimeError("synthetic marker failure")
        return original(key, value)

    monkeypatch.setattr(space.store(), "set_meta", fail)
    with pytest.raises(RuntimeError, match="synthetic marker"):
        close_extraction(space, handle.execution_id)
    assert (space.root / legacy.path).is_file()
    monkeypatch.setattr(space.store(), "set_meta", original)
    closed = close_extraction(space, handle.execution_id)
    assert closed["status"]["state"] == "finished"
    assert not (space.root / legacy.path).exists()

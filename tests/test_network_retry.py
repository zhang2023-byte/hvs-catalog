"""Roster network recovery reuses submissions without restoring correction allowance."""

import pytest
from pydantic import SecretStr
from test_roster import ScriptedAdapter, run, submission

from hvs_catalog.llm.managed import CallService
from hvs_catalog.llm.transport import TransportFailure
from hvs_catalog.pipeline import (
    get_extraction,
    resume_extraction,
    retry_network_failures,
)
from hvs_catalog.pipeline.roster import saved_roster
from hvs_catalog.workspace import backup_workspace, restore_workspace


def failure(unsent):
    return TransportFailure(
        "completion_unknown",
        uncertain=True,
        exchange={
            "transport": {
                "phase": "connect" if unsent else "response_headers",
                "response_bytes": 0,
                "request_started": not unsent,
                "exceptions": [{"type": "ReadTimeout"}],
            }
        },
    )


def test_retry_of_correction_reuses_initial_response_after_backup(prepared, tmp_path):
    space, _, default = prepared
    config = default.model_copy(
        update={
            "runtime": default.runtime.model_copy(
                update={"max_attempts": 1, "retry_post_send_network_errors": False}
            )
        }
    )
    bad = submission()
    bad["notes"] = None
    handle, report, adapter, _ = run(prepared, [bad, failure(True)], config=config)
    assert report.outcome == "failed" and len(adapter.seen) == 2
    intent = retry_network_failures(space, handle.execution_id, request_key="retry")
    assert intent["targets"][0]["first_round"] == 1
    assert retry_network_failures(space, handle.execution_id, request_key="retry")[
        "reused"
    ]
    backup_workspace(space, tmp_path / "backup")
    restored = restore_workspace(tmp_path / "backup", tmp_path / "restored")
    try:
        corrected = ScriptedAdapter([bad, submission()])
        service = CallService(
            config.runtime, credential=SecretStr("synthetic"), adapter=corrected
        )
        report = resume_extraction(restored, handle.execution_id, service=service)
        assert report.outcome == "finished", report.errors
        assert [len(p["messages"]) for p in corrected.seen] == [5, 8]
        assert (
            corrected.seen[0]["messages"][2]["reasoning_content"]
            == "Synthetic reasoning for call 1"
        )
        assert saved_roster(restored, handle.execution_id).data.source_round == 2
        assert (
            resume_extraction(restored, handle.execution_id, service=service).outcome
            == "finished"
        )
        assert len(corrected.seen) == 2
    finally:
        restored.close()


@pytest.mark.parametrize("payload", [failure(False), {"stars": [], "notes": 42}])
def test_unknown_or_format_failure_cannot_be_reopened_as_network_failure(
    prepared, payload
):
    space, _, default = prepared
    config = default.model_copy(
        update={
            "runtime": default.runtime.model_copy(
                update={"max_attempts": 1, "retry_post_send_network_errors": False}
            )
        }
    )
    handle, report, _, _ = run(prepared, [payload], config=config)
    assert report.outcome == ("finished" if isinstance(payload, dict) else "failed")
    before = get_extraction(space, handle.execution_id)
    with pytest.raises(ValueError, match="proven-unsent"):
        retry_network_failures(space, handle.execution_id, request_key="unsafe")
    assert get_extraction(space, handle.execution_id) == before


def test_object_retry_is_disabled_before_workspace_access():
    with pytest.raises(TypeError, match="object_ids"):
        retry_network_failures(None, "unused", request_key="blocked", object_ids=["A"])

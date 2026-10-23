"""Synthetic service facts stay outside the workspace and survive its rollback."""

import json

import pytest
from llm_helpers import completion, endpoint
from pydantic import SecretStr
from test_roster import entry, submission

from hvs_catalog.llm.managed import CallService
from hvs_catalog.pipeline import resume_extraction, start_extraction
from hvs_catalog.pipeline.results import current_result
from hvs_catalog.workspace import backup_workspace, restore_workspace


@pytest.mark.integration
@pytest.mark.parametrize("known_usage", [True, False])
def test_service_facts_survive_client_restore(prepared, tmp_path, known_usage):
    space, prep, config = prepared
    facts = tmp_path / "external-calls.jsonl"

    def reply(request, number):
        with facts.open("a") as stream:
            stream.write(json.dumps({"number": number, "request": request}) + "\n")
        name = request["tools"][0]["function"]["name"]
        payload = (
            submission([entry("A")])
            if name == "submit_roster"
            else {"quantities": [], "notes": ""}
        )
        response = completion(request, usage=known_usage)
        response["choices"][0]["message"]["tool_calls"][0]["function"]["arguments"] = (
            json.dumps(payload)
        )
        return {"body": response}

    with endpoint(reply) as (url, seen, _):
        config = config.model_copy(
            update={"runtime": config.runtime.model_copy(update={"base_url": url})}
        )
        handle = start_extraction(
            space,
            preparation_execution_id=prep,
            request_key="synthetic",
            configuration=config,
        )
        report = resume_extraction(
            space,
            handle.execution_id,
            service=CallService(config.runtime, credential=SecretStr("synthetic-key")),
        )
        assert report.result_status == "complete"
        _, result = current_result(space, handle.execution_id)
        assert result.usage.total_tokens == (20 if known_usage else None)
        backup_workspace(space, tmp_path / "backup")
        restored = restore_workspace(tmp_path / "backup", tmp_path / "restored")
        try:
            assert (
                resume_extraction(restored, handle.execution_id).result_status
                == "complete"
            )
            assert (
                current_result(restored, handle.execution_id)[0]
                == current_result(space, handle.execution_id)[0]
            )
            assert len(seen) == len(facts.read_text().splitlines()) == 2
        finally:
            restored.close()

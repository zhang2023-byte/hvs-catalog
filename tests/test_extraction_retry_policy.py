"""Default extraction policy preserves format failures and retries transport only."""

import pytest
import test_pdf_quantities as fixtures
from llm_helpers import completion, endpoint, sse
from test_llm_managed import run_call

from hvs_catalog.llm import ModelConfig, RuntimeConfig, resume_call
from hvs_catalog.llm.transport import network_retry_allowed
from hvs_catalog.pipeline import ExtractionConfig


def test_default_efforts_and_legacy_policy():
    config = ExtractionConfig()
    assert config.model_for("roster").parameters["reasoning_effort"] == "max"
    assert config.model_for("quantities").parameters["reasoning_effort"] == "low"
    assert config.max_format_corrections == 2
    assert config.runtime.retry_post_send_network_errors
    legacy = config.model_dump()
    del legacy["max_format_corrections"]
    del legacy["runtime"]["retry_post_send_network_errors"]
    restored = ExtractionConfig.model_validate(legacy, context={"historical": True})
    assert restored.max_format_corrections == 2
    assert not restored.runtime.retry_post_send_network_errors
    explicit = ExtractionConfig(default_model=ModelConfig(model="mimo-v2.6-flash"))
    assert explicit.model_for("quantities").model == "mimo-v2.6-flash"


@pytest.mark.parametrize(
    "error",
    [
        "invalid_tool_arguments",
        "invalid_response",
        "response_limit",
        "worker_failure:OSError",
    ],
)
def test_non_network_failure_is_never_retried(error, tmp_path):
    runtime = RuntimeConfig(
        quota_store=tmp_path / "quota", retry_post_send_network_errors=True
    )
    assert not network_retry_allowed({"error": error, "uncertain": True}, runtime)


@pytest.mark.integration
@pytest.mark.parametrize("mode", ["headers", "stream", "server"])
def test_post_send_network_retry_retains_attempt_and_terminal_resume(tmp_path, mode):
    def action(payload, number):
        if number > 1:
            return {}
        if mode == "headers":
            return {"delay": 0.15}
        if mode == "server":
            return {"status": 503}
        return {"bytes": sse(completion(payload), done=False)}

    with endpoint(action) as (url, seen, _):
        space, handle, report = run_call(
            tmp_path,
            url,
            tool=True,
            retry_post_send_network_errors=True,
            idle_timeout=0.05,
            total_timeout=2,
        )
        try:
            assert report["state"] == "finished", report
            assert len(seen) == 2 and len(report["attempts"]) == 2
            assert report["attempts"][0]["state"] == "pending"
            assert report["attempts"][0]["response"] is not None
            assert resume_call(space, handle.execution_id)["state"] == "finished"
            assert len(seen) == 2
        finally:
            space.close()


@pytest.mark.integration
def test_network_attempt_limit_and_model_output_are_distinct(tmp_path):
    with endpoint(lambda *_: {"status": 503}) as (url, seen, _):
        space, handle, report = run_call(
            tmp_path, url, retry_post_send_network_errors=True
        )
        try:
            assert report["state"] == "failed" and len(seen) == 3
            assert resume_call(space, handle.execution_id)["state"] == "failed"
            assert len(seen) == 3
        finally:
            space.close()


@pytest.mark.integration
def test_zero_format_corrections_preserves_valid_object_without_recalling(tmp_path):
    generator = fixtures.prepared_pdf.__wrapped__(tmp_path)
    space, prep, config = next(generator)
    config = config.model_copy(update={"max_format_corrections": 0})

    def response(tool, _):
        target = fixtures.object_id(tool)
        raw = fixtures.quantities(target)
        if target.endswith("-2"):
            raw["measurements"][0]["interval_kind"] = "credible"
        return raw

    adapter = fixtures.Adapter(response)
    try:
        handle, report, service = fixtures.start((space, prep, config), adapter)
        assert report.outcome == "failed" and len(adapter.seen) == 3
        assert fixtures.paper(space, handle.execution_id).data.object_contributions
        from hvs_catalog.pipeline import resume_extraction

        assert (
            resume_extraction(space, handle.execution_id, service=service).outcome
            == "failed"
        )
        assert len(adapter.seen) == 3
    finally:
        generator.close()


@pytest.mark.integration
@pytest.mark.parametrize("recover", [True, False])
def test_parallel_retry_targets_only_failed_object(tmp_path, recover):
    from pydantic import SecretStr

    from hvs_catalog.llm.managed import CallService
    from hvs_catalog.llm.transport import TransportFailure
    from hvs_catalog.pipeline import (
        ExtractionTask,
        resume_extractions,
        start_pdf_extraction,
    )

    generator = fixtures.prepared_pdf.__wrapped__(tmp_path)
    space, prep, config = next(generator)
    config = config.model_copy(
        update={
            "max_format_corrections": 0,
            "runtime": config.runtime.model_copy(
                update={"retry_post_send_network_errors": True}
            ),
        }
    )
    calls = []

    def response(tool, _):
        target = fixtures.object_id(tool)
        calls.append(target)
        if target.endswith("-2") and (not recover or calls.count(target) < 3):
            raise TransportFailure(
                "completion_unknown",
                uncertain=True,
                exchange={
                    "transport": {
                        "phase": "read",
                        "exceptions": [{"type": "ReadTimeout"}],
                    },
                },
            )
        return fixtures.quantities(target)

    adapter = fixtures.Adapter(response)
    try:
        handle = start_pdf_extraction(
            space,
            preparation_execution_id=prep,
            request_key="parallel-retry",
            configuration=config,
        )
        service = CallService(
            config.runtime, credential=SecretStr("synthetic"), adapter=adapter
        )
        reports = resume_extractions(
            [ExtractionTask(space, handle.execution_id, service)], concurrency=4
        )
        assert reports[0].outcome == ("finished" if recover else "failed")
        assert sum(target.endswith("-1") for target in calls) == 1
        assert sum(target.endswith("-2") for target in calls) == 3
        before = list(calls)
        resume_extractions(
            [ExtractionTask(space, handle.execution_id, service)], concurrency=4
        )
        assert calls == before
    finally:
        generator.close()

"""Configured mock execution: actual requests, immutable inputs, archives and recovery."""

import json

import pytest

from hvs_catalog.llm import ModelConfig
from hvs_catalog.pipeline import (
    PAPER_A,
    PAPER_B,
    PAPER_C,
    ExtractionConfig,
    PipelineMockService,
    get_extraction,
    import_mock_source,
    resume_extraction,
    start_configured_extraction,
)
from hvs_catalog.runtime import get_execution
from hvs_catalog.workspace import (
    OperationKeyConflictError,
    RecoveryInconsistentError,
    initialize_workspace,
    open_workspace,
)


class RequestsService(PipelineMockService):
    def __init__(self):
        super().__init__()
        self.requests = []

    def call(self, *, step, request, material):
        self.requests.append(request)
        return super().call(step=step, request=request, material=material)


@pytest.fixture
def space(tmp_path):
    workspace = initialize_workspace(tmp_path / "space")
    yield workspace
    workspace.close()


@pytest.mark.parametrize("mode", ["daily", "evaluation"])
def test_model_combinations_actual_requests_and_per_step_usage(space, mode):
    config = ExtractionConfig(
        mode=mode,
        step_models={"quantities": ModelConfig(model="mimo-v2.6-flash")},
    )
    source = import_mock_source(space, PAPER_B)
    handle = start_configured_extraction(
        space, configuration=config, source_refs=[source], request_key="combination"
    )
    service = RequestsService()
    report = resume_extraction(space, handle.execution_id, service=service)
    assert report.outcome == "finished", report.errors
    for request in service.requests:
        call = request["call_request"]
        assert len(call["messages"]) == 2 and call["tool"] is not None
        assert "480" in call["messages"][1]["content"]
        assert "contributions" not in call["messages"][1]["content"]
        chosen = call["model"]
        if request["step"] == "quantities":
            assert chosen["model"] == "mimo-v2.6-flash"
            assert "max_tokens" not in chosen["parameters"]
        else:
            assert chosen["model"] == "deepseek-v4.1-flash"
        payload = request["call_prepared"]["payload"]
        assert payload["provider"] == {
            "only": [chosen["provider"]],
            "allow_fallbacks": False,
        }
    status = get_extraction(space, handle.execution_id)
    tokens = {step["step_id"]: step["tokens"] for step in status["steps"]}
    assert tokens["quantities"] == 200
    assert tokens["finalize"] is None
    assert status["usage"]["tokens"] == 320
    before = len(service.requests)
    assert (
        resume_extraction(space, handle.execution_id, service=service).outcome
        == "finished"
    )
    assert len(service.requests) == before


def test_ai_interpretation_is_recorded_without_expert_and_remains_pending(space):
    source = import_mock_source(space, PAPER_C)
    config = ExtractionConfig(mode="evaluation")
    handle = start_configured_extraction(
        space, configuration=config, source_refs=[source], request_key="unresolved-ai"
    )
    service = RequestsService()
    report = resume_extraction(space, handle.execution_id, service=service)
    assert report.outcome == "finished", report.errors
    assert report.model_judgments >= 1 and report.open_questions == 0
    assert not any(r["step"] == "interpret_ambiguity" for r in service.requests)
    assert get_extraction(space, handle.execution_id)["questions"] == []
    quantities = next(r for r in service.requests if r["step"] == "quantities")
    assert len(quantities["object_calls"]) == 2
    assert "call_request" not in quantities
    for unit in quantities["object_calls"]:
        call = unit["call_request"]
        context = json.loads(
            call["messages"][1]["content"].split("===== SUPPLIED CONTEXT =====\n", 1)[1]
        )
        assert len(context["targets"]) == 1
        assert "measurements" in call["tool"]["parameters"]["properties"]


def test_completed_reentry_uses_archives_after_prompt_sources_change(
    space, monkeypatch
):
    source = import_mock_source(space, PAPER_A)
    config = ExtractionConfig()
    handle = start_configured_extraction(
        space, configuration=config, source_refs=[source], request_key="fixed"
    )
    service = RequestsService()
    assert (
        resume_extraction(space, handle.execution_id, service=service).outcome
        == "finished"
    )
    before = len(service.requests)
    config.default_model = ModelConfig(model="qwen3.8-flash")
    config.examples["quantities"] = []
    from hvs_catalog.llm import prompts

    def changed():
        raise AssertionError("recovery must not rebuild current prompt sources")

    monkeypatch.setattr(prompts, "science_content", changed)
    monkeypatch.setattr(prompts, "prompt_resources", changed)
    assert (
        resume_extraction(space, handle.execution_id, service=service).outcome
        == "finished"
    )
    assert len(service.requests) == before


def test_configuration_or_actual_english_prompt_change_requires_new_execution(
    space, monkeypatch
):
    source = import_mock_source(space, PAPER_B)
    config = ExtractionConfig()
    first = start_configured_extraction(
        space, configuration=config, source_refs=[source], request_key="original"
    )
    assert start_configured_extraction(
        space, configuration=config, source_refs=[source], request_key="original"
    ).reused
    from hvs_catalog.llm import prompts

    data = prompts.prompt_resources()
    data["steps"]["roster"]["task"] += " Check every named table member."
    monkeypatch.setattr(prompts, "prompt_resources", lambda: data)
    with pytest.raises(OperationKeyConflictError):
        start_configured_extraction(
            space, configuration=config, source_refs=[source], request_key="original"
        )
    second = start_configured_extraction(
        space, configuration=config, source_refs=[source], request_key="changed"
    )
    assert (
        first.execution_id != second.execution_id
        and first.input_digest != second.input_digest
    )


def test_archived_response_reused_after_failure_before_result_commit(space):
    from hvs_catalog.pipeline import ROSTER_METHOD, ExtractionFlow
    from hvs_catalog.runtime import resume_execution

    source = import_mock_source(space, PAPER_B)
    handle = start_configured_extraction(
        space,
        configuration=ExtractionConfig(),
        source_refs=[source],
        request_key="archive-window",
    )
    service = RequestsService()

    def fail():
        raise RuntimeError("synthetic failure after archive")

    result = resume_execution(
        space,
        handle.execution_id,
        ExtractionFlow(
            ROSTER_METHOD,
            mode="daily",
            fault_hooks={"after_archive:roster": fail},
        ),
        service=service,
    )
    assert result.outcome == "failed"
    assert (
        resume_extraction(space, handle.execution_id, service=service).outcome
        == "finished"
    )
    assert service.calls_for_step("roster") == 1


def test_missing_frozen_prompt_blocks_resume_without_service_calls(space):
    source = import_mock_source(space, PAPER_B)
    handle = start_configured_extraction(
        space,
        configuration=ExtractionConfig(),
        source_refs=[source],
        request_key="missing-archive",
    )
    frozen = get_execution(space, handle.execution_id).input_snapshot
    (space.root / frozen.prompt_refs[0].path).unlink()
    service = RequestsService()
    with pytest.raises(RecoveryInconsistentError, match="提示归档缺失"):
        resume_extraction(space, handle.execution_id, service=service)
    assert not service.requests


def test_archives_and_complete_request_survive_workspace_reopen(space):
    source = import_mock_source(space, PAPER_B)
    handle = start_configured_extraction(
        space,
        configuration=ExtractionConfig(),
        source_refs=[source],
        request_key="reopen",
    )
    service = RequestsService()
    assert (
        resume_extraction(space, handle.execution_id, service=service).outcome
        == "finished"
    )
    root = space.root
    space.close()
    reopened = open_workspace(root)
    try:
        frozen = get_execution(reopened, handle.execution_id).input_snapshot
        assert (
            json.loads(reopened.read_file(frozen.example_refs[0]))["roster"][0]["id"]
            == "E01"
        )
        run = reopened.store().read_record_current(
            get_execution(reopened, handle.execution_id).run_ref.record_id
        )
        request = json.loads(
            reopened.read_file(
                next(step for step in run.data.steps if step.step_id == "roster")
                .attempts[0]
                .request
            )
        )
        assert request["call_request"]["tool"]["name"] == "submit_roster"
        assert (
            next(
                step
                for step in get_extraction(reopened, handle.execution_id)["steps"]
                if step["step_id"] == "roster"
            )["tokens"]
            == 120
        )
    finally:
        reopened.close()


def test_backup_restores_frozen_prompt_and_example_artifacts(space, tmp_path):
    from hvs_catalog.workspace import backup_workspace, restore_workspace

    source = import_mock_source(space, PAPER_B)
    handle = start_configured_extraction(
        space,
        configuration=ExtractionConfig(),
        source_refs=[source],
        request_key="backup-prompts",
    )
    frozen = get_execution(space, handle.execution_id).input_snapshot
    assert (
        resume_extraction(space, handle.execution_id, service=RequestsService()).outcome
        == "finished"
    )
    backup_workspace(space, tmp_path / "backup")
    restored = restore_workspace(tmp_path / "backup", tmp_path / "restored")
    try:
        assert restored.read_file(frozen.prompt_refs[0]) == space.read_file(
            frozen.prompt_refs[0]
        )
        assert restored.read_file(frozen.example_refs[0]) == space.read_file(
            frozen.example_refs[0]
        )
        report = resume_extraction(
            restored, handle.execution_id, service=RequestsService()
        )
        assert report.outcome == "finished", report.errors
    finally:
        restored.close()

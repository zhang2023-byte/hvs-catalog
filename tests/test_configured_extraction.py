"""Configured mock execution: actual requests, immutable inputs, archives and recovery."""

import json

import pytest

from hvs_catalog.llm import ModelConfig
from hvs_catalog.llm.prompts import PrecedentInput
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
    submit_expert_decision,
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


@pytest.mark.parametrize("method", ["direct", "candidate_review"])
@pytest.mark.parametrize("expert", [True, False])
def test_model_combinations_actual_requests_and_per_step_usage(space, method, expert):
    config = ExtractionConfig(
        method=method,
        mode="evaluation",
        expert_adjudication=expert,
        step_models={"quantities": ModelConfig(model="mimo-v2.6-flash")},
    )
    if method == "candidate_review":
        config.step_models["discover_candidates"] = ModelConfig(model="qwen3.8-flash")
        config.step_models["review_candidates"] = ModelConfig(model="glm-5.3-flash")
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
        elif request["step"] == "review_candidates":
            assert chosen["model"] == "glm-5.3-flash"
            assert "reasoning_effort" in chosen["parameters"]
        elif request["step"] == "discover_candidates":
            assert chosen["model"] == "qwen3.8-flash"
            assert "reasoning_effort" not in chosen["parameters"]
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
    assert status["usage"]["tokens"] == (320 if method == "direct" else 370)
    before = len(service.requests)
    assert (
        resume_extraction(space, handle.execution_id, service=service).outcome
        == "finished"
    )
    assert len(service.requests) == before


@pytest.mark.parametrize("method", ["direct", "candidate_review"])
def test_ai_interpretation_is_recorded_without_expert_and_remains_pending(
    space, method
):
    source = import_mock_source(space, PAPER_C)
    config = ExtractionConfig(
        method=method, mode="evaluation", expert_adjudication=False
    )
    handle = start_configured_extraction(
        space, configuration=config, source_refs=[source], request_key="unresolved-ai"
    )
    service = RequestsService()
    report = resume_extraction(space, handle.execution_id, service=service)
    assert report.outcome == "finished", report.errors
    assert report.model_judgments >= 1 and report.open_questions >= 1
    interpretations = [
        r for r in service.requests if r["step"] == "interpret_ambiguity"
    ]
    assert interpretations
    for request in interpretations:
        assert (
            "Do not request or wait for an expert"
            in request["call_request"]["messages"][0]["content"]
        )
        context = request["call_request"]["messages"][1]["content"]
        assert "open_questions" in context
        assert request["question_ref"]["record_id"] in context
        assert '"target_ids"' in context and '"evidence"' in context
        assert "fingerprint" not in context and "rules_ref" not in context


def test_paused_recovery_uses_archives_after_source_prompts_change(space, monkeypatch):
    source = import_mock_source(space, PAPER_A)
    precedent = PrecedentInput(
        precedent_id="SYN-P1",
        version="synthetic-v1",
        approved=True,
        explanation="Record an explicit author synthesis.",
        conditions="An explicit synthesis is present.",
        boundaries="Do not infer a synthesis from counts of models.",
    )
    config = ExtractionConfig(precedents=[precedent])
    handle = start_configured_extraction(
        space, configuration=config, source_refs=[source], request_key="fixed"
    )
    service = RequestsService()
    report = resume_extraction(space, handle.execution_id, service=service)
    assert report.outcome == "paused", report.errors
    original_prompt = service.requests[0]["call_request"]["messages"][0]["content"]
    config.default_model = ModelConfig(model="qwen3.8-flash")
    config.examples["quantities"] = []
    precedent.explanation = "An unrelated later library revision."
    from hvs_catalog.llm import prompts

    def changed():
        raise AssertionError("recovery must not rebuild current prompt/science sources")

    monkeypatch.setattr(prompts, "science_content", changed)
    monkeypatch.setattr(prompts, "prompt_resources", changed)
    from hvs_catalog.records.common import RecordRef

    submit_expert_decision(
        space,
        execution_id=handle.execution_id,
        question_ref=RecordRef.model_validate(report.pending_question["question_ref"]),
        request_key="fixed-decision",
        choice="no_overall_conclusion",
    )
    report = resume_extraction(space, handle.execution_id, service=service)
    assert report.outcome == "finished", report.errors
    assert (
        service.requests[0]["call_request"]["messages"][0]["content"] == original_prompt
    )
    assert all(
        r["call_request"]["model"]["model"] == "deepseek-v4.1-flash"
        for r in service.requests
    )
    assert (
        "Record an explicit author synthesis"
        in service.requests[-1]["call_request"]["messages"][1]["content"]
    )


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
    data["steps"]["direct_roster"]["task"] += " Check every named table member."
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
    from hvs_catalog.pipeline import DIRECT_METHOD, ExtractionFlow
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
            DIRECT_METHOD,
            mode="daily",
            fault_hooks={"after_archive:direct_roster": fail},
        ),
        service=service,
    )
    assert result.outcome == "failed"
    assert (
        resume_extraction(space, handle.execution_id, service=service).outcome
        == "finished"
    )
    assert service.calls_for_step("direct_roster") == 1


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
            json.loads(reopened.read_file(frozen.example_refs[0]))["direct_roster"][0][
                "id"
            ]
            == "E01"
        )
        run = reopened.store().read_record_current(
            get_execution(reopened, handle.execution_id).run_ref.record_id
        )
        request = json.loads(reopened.read_file(run.data.steps[0].attempts[0].request))
        assert request["call_request"]["tool"]["name"] == "submit_direct_roster"
        assert (
            get_extraction(reopened, handle.execution_id)["steps"][0]["tokens"] == 120
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

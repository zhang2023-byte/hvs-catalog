"""Demo uses the same scientific records and fixed extraction inputs."""

import pytest

from hvs_catalog.demo import runner
from hvs_catalog.pipeline.mock import PAPER_A, paper_record_id
from hvs_catalog.workspace import open_workspace


def test_shared_author_judgment_preserves_values_conditions_evidence(tmp_path):
    root = tmp_path / "demo"
    paused = runner.run(root)
    assert paused["config"]["method"] == "direct"
    assert paused["implementation"] == "synthetic-mock"
    done = runner.decide(root, choice="no_overall_conclusion")
    assert done["report"]["outcome"] == "finished"
    space = open_workspace(root)
    try:
        record_id = paper_record_id(PAPER_A, done["execution_id"])
        old = space.store().read_record_current(record_id)
        contribution = old.data.object_contributions[0]
        assert old.revision == 2
        assert contribution.paper_boundness.status == "no_overall_conclusion"
        assert len(contribution.conditional_assessments) == 2
        value = contribution.quantities[0].values[0]
        assert (value.value, value.error, value.unit, value.condition) == (
            "520",
            "10",
            "km/s",
            "New spectrum; heliocentric frame.",
        )
        assert value.direct_evidence
    finally:
        space.close()


def test_answer_can_remain_unresolved(tmp_path):
    root = tmp_path / "demo"
    runner.run(root)
    report = runner.decide(root, choice="keep_pending")
    assert report["report"]["outcome"] == "paused"
    assert report["pending"][0]["answered_outcome"] == "unresolved"
    assert report["external_calls"]["calls"] == 2


def test_unsaved_response_recovery_uses_frozen_material(tmp_path, monkeypatch):
    import copy

    from shared_scenarios import direct_flow, open_service, start_execution

    from hvs_catalog.pipeline import mock
    from hvs_catalog.runtime import resume_execution
    from hvs_catalog.workspace import initialize_workspace

    space = initialize_workspace(tmp_path / "space")
    try:
        service = open_service(tmp_path / "calls.jsonl")

        def fail():
            raise RuntimeError("unsaved response")

        execution, flow, _ = start_execution(
            space, paper="B", fault_hooks={"after_response:direct_roster": fail}
        )
        assert (
            resume_execution(
                space, execution.execution_id, flow, service=service
            ).outcome
            == "failed"
        )
        changed = copy.deepcopy(mock.material_spec(mock.PAPER_B))
        changed["contributions"][0]["values"][0]["value"] = "999"
        monkeypatch.setitem(mock._MOCK_PAPERS, mock.PAPER_B, changed)
        report = resume_execution(
            space, execution.execution_id, direct_flow(), service=service
        )
        assert report.outcome == "finished"
        paper = space.store().read_record_current(
            mock.paper_record_id(mock.PAPER_B, execution.execution_id)
        )
        assert paper.data.object_contributions[0].quantities[0].values[0].value == "480"
    finally:
        space.close()


def test_demo_restart_never_rebuilds_current_inputs(tmp_path, monkeypatch):
    root = tmp_path / "demo"
    first = runner.run(root)

    def forbid(*args, **kwargs):
        pytest.fail("restart must use saved execution, not build today's inputs")

    monkeypatch.setattr(runner, "start_extraction", forbid)
    monkeypatch.setattr(runner, "import_mock_source", forbid)
    resumed = runner.run(root)
    assert resumed["input_digest"] == first["input_digest"]
    assert resumed["external_calls"]["calls"] == 2


def test_unknown_choice_rejected(tmp_path):
    root = tmp_path / "demo"
    runner.run(root)
    with pytest.raises(ValueError, match="未知模拟决定"):
        runner.decide(root, choice="merge")

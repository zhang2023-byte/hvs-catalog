"""Quantity branches share immutable rosters and retain independent results."""

import threading

import pytest
import test_pdf_quantities as fixtures
from pydantic import SecretStr

from hvs_catalog.llm import ModelConfig
from hvs_catalog.llm.managed import CallService
from hvs_catalog.pipeline import (
    ExtractionTask,
    resume_extraction,
    resume_extractions,
    start_pdf_extraction,
    start_roster_extraction,
)
from hvs_catalog.runtime import get_execution
from hvs_catalog.workspace import open_workspace
from hvs_catalog.workspace.backup import backup_workspace, restore_workspace


@pytest.fixture
def prepared(tmp_path):
    yield from fixtures.prepared_pdf.__wrapped__(tmp_path)


def service(config, adapter):
    return CallService(
        config.runtime, credential=SecretStr("synthetic"), adapter=adapter
    )


def test_branches_reuse_exact_roster_with_independent_quantities_and_resume(
    prepared, tmp_path
):
    space, prep, config = prepared
    source_adapter = fixtures.Adapter(
        lambda *_: pytest.fail("roster run requested quantities")
    )
    source = start_roster_extraction(
        space, preparation_execution_id=prep, configuration=config, request_key="source"
    )
    assert (
        resume_extraction(
            space, source.execution_id, service=service(config, source_adapter)
        ).outcome
        == "finished"
    )
    branches, adapters = [], []
    for effort, error in [("low", "12"), ("high", "15")]:
        branch_config = config.model_copy(
            update={
                "step_models": {
                    "quantities": ModelConfig(
                        model="deepseek-v4.1-flash",
                        parameters={"reasoning_effort": effort},
                    )
                }
            }
        )
        branch = start_pdf_extraction(
            space,
            preparation_execution_id=prep,
            roster_execution_id=source.execution_id,
            configuration=branch_config,
            request_key=effort,
        )
        adapter = fixtures.Adapter(
            lambda tool, _, error=error: fixtures.quantities(
                fixtures.object_id(tool),
                values=[
                    {
                        "quantity": "observed_phase_space.radial_velocity",
                        "values": [
                            fixtures.value(error=error, index=1 if error == "15" else 0)
                        ],
                    }
                ],
            )
        )
        assert (
            resume_extraction(
                space, branch.execution_id, service=service(branch_config, adapter)
            ).outcome
            == "finished"
        )
        branches.append(branch)
        adapters.append(adapter)
    assert len(source_adapter.seen) == 1
    assert [len(a.seen) for a in adapters] == [2, 2]
    assert all(
        p["tools"][0]["function"]["name"] == "submit_quantities"
        for a in adapters
        for p in a.seen
    )
    papers = [fixtures.paper(space, b.execution_id) for b in branches]
    assert papers[0].data.roster_ref != papers[1].data.roster_ref
    assert [i.value for i in papers[0].data.object_contributions[0].identifiers] == [
        i.value for i in papers[1].data.object_contributions[0].identifiers
    ]
    assert [
        p.data.object_contributions[0].quantities[0].values[0].error for p in papers
    ] == ["12", "15"]
    source_run = space.store().read_record(
        get_execution(space, source.execution_id).run_ref
    )
    assert source_run.data.state == "finished" and len(source_run.data.steps) == 1
    backup_workspace(space, tmp_path / "backup")
    restore_workspace(tmp_path / "backup", tmp_path / "restored")
    restored = open_workspace(tmp_path / "restored")
    for branch, adapter in zip(branches, adapters, strict=True):
        assert (
            resume_extraction(
                restored, branch.execution_id, service=service(config, adapter)
            ).outcome
            == "finished"
        )
        assert len(adapter.seen) == 2
    restored.close()


@pytest.mark.integration
def test_dynamic_quantity_branches_start_before_other_roster_finishes(prepared):
    space, prep, config = prepared
    numeric = threading.Event()
    configs, handles, adapters = [], [], []

    class Adapter(fixtures.Adapter):
        def send(self, payload, runtime, credential):
            if payload["tools"][0]["function"]["name"] == "submit_roster":
                if payload.get("reasoning_effort") == "max":
                    assert numeric.wait(10), "quantity branches waited for every roster"
            else:
                numeric.set()
            return super().send(payload, runtime, credential)

    for effort in ("high", "max"):
        fixed = config.model_copy(
            update={
                "step_models": {
                    "roster": ModelConfig(
                        model="deepseek-v4.1-flash",
                        parameters={"reasoning_effort": effort},
                    )
                }
            }
        )
        configs.append(fixed)
        handles.append(
            start_roster_extraction(
                space,
                preparation_execution_id=prep,
                configuration=fixed,
                request_key=effort,
            )
        )
        adapters.append(
            Adapter(
                lambda tool, _: fixtures.quantities(fixtures.object_id(tool)),
                names=("A",),
            )
        )
    branch_adapters = []

    def finished(index, report):
        if index >= 2:
            return []
        assert report.outcome == "finished", report.errors
        tasks = []
        for effort in ("low", "high"):
            fixed = configs[index].model_copy(
                update={
                    "step_models": {
                        **configs[index].step_models,
                        "quantities": ModelConfig(
                            model="deepseek-v4.1-flash",
                            parameters={"reasoning_effort": effort},
                        ),
                    }
                }
            )
            handle = start_pdf_extraction(
                space,
                preparation_execution_id=prep,
                roster_execution_id=handles[index].execution_id,
                configuration=fixed,
                request_key=str(index) + effort,
            )
            adapter = Adapter(
                lambda tool, _: fixtures.quantities(fixtures.object_id(tool))
            )
            branch_adapters.append(adapter)
            tasks.append(
                ExtractionTask(space, handle.execution_id, service(fixed, adapter))
            )
        return tasks

    reports = resume_extractions(
        [
            ExtractionTask(space, h.execution_id, service(c, a))
            for h, c, a in zip(handles, configs, adapters, strict=True)
        ],
        concurrency=4,
        on_report=finished,
    )
    assert len(reports) == 6 and all(r.outcome == "finished" for r in reports)
    assert sum(len(a.seen) for a in [*adapters, *branch_adapters]) == 6


def test_uncommitted_roster_is_rejected_before_branch_calls(prepared):
    space, prep, config = prepared
    source = start_roster_extraction(
        space, preparation_execution_id=prep, configuration=config, request_key="source"
    )
    with pytest.raises(ValueError, match="finished automatic"):
        start_pdf_extraction(
            space,
            preparation_execution_id=prep,
            configuration=config,
            roster_execution_id=source.execution_id,
            request_key="branch",
        )

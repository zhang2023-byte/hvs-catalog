"""Synthetic materials CLI tests for prepare/status/resume/consume and exit codes."""

from __future__ import annotations

import json

from materials_helpers import (
    PAPER,
    FakeConverterRunner,
    good_latexml_html,
    prepared_space,
)

from hvs_catalog.cli import main
from hvs_catalog.materials.service import MaterialsService


def _run_cli(space, service_runner, *args):
    """Run the CLI with an injected fake converter by replacing the default runner in the
    test environment.
    """
    from hvs_catalog import cli_materials
    from hvs_catalog.materials import service as service_mod

    original = service_mod.MaterialsService

    class InjectedService(original):
        def __init__(self, space_, *, runner=None):
            super().__init__(space_, runner=service_runner)

    service_mod.MaterialsService = InjectedService
    cli_materials.MaterialsService = InjectedService
    try:
        code = main(
            (
                "materials",
                *args,
                "--space",
                str(space.root),
            )
        )
    finally:
        service_mod.MaterialsService = original
        cli_materials.MaterialsService = original
    return code


def test_cli_prepare_status_resume_consume_flow(tmp_path, capsys):
    space, imported = prepared_space(tmp_path)
    runner = FakeConverterRunner(
        latexml_html=good_latexml_html(
            [("FIC-A", "525", "10"), ("FIC-B", "-430", "12")]
        )
    )
    code = _run_cli(
        space,
        runner,
        "prepare",
        "--paper",
        imported.canonical_id,
        "--request-key",
        "cli-1",
        "--json",
    )
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    execution_id = payload["handle"]["execution_id"]
    assert payload["report"]["outcome"] == "finished"

    code = _run_cli(space, runner, "status", "--execution", execution_id, "--json")
    assert code == 0
    status = json.loads(capsys.readouterr().out)
    assert status["state"] == "finished"
    assert status["config"]["request"]["paper_key"] == imported.canonical_id

    code = _run_cli(space, runner, "resume", "--execution", execution_id, "--json")
    assert code == 5  # Idempotent reentry after execution completes.
    resumed = json.loads(capsys.readouterr().out)
    assert resumed["mode"] == "already-finished"

    code = _run_cli(space, runner, "consume", "--execution", execution_id, "--json")
    assert code == 0
    consumed = json.loads(capsys.readouterr().out)
    assert consumed["ok"] is True
    space.close()


def test_cli_prepare_pauses_with_exit_three_and_resumes_after_decision(
    tmp_path, capsys
):
    space, imported = prepared_space(tmp_path)
    runner = FakeConverterRunner(
        latexml_html=good_latexml_html([("FIC-A", "525", "10")])
    )
    code = _run_cli(
        space,
        runner,
        "prepare",
        "--paper",
        imported.canonical_id,
        "--request-key",
        "cli-q",
        "--expert-adjudication",
        "--range",
        "1-5|sec1.tex|2|2|candidates:A=HVS",
        "--json",
    )
    assert code == 3
    payload = json.loads(capsys.readouterr().out)
    assert payload["report"]["outcome"] == "paused"
    execution_id = payload["handle"]["execution_id"]

    # An expert submits a decision through the R05 interface, explicitly giving candidate code A.
    from hvs_catalog.records.common import RecordRef
    from hvs_catalog.records.questions import Actor
    from hvs_catalog.review import ReviewService
    from hvs_catalog.review.service import DecisionSubmission

    review = ReviewService(space)
    view = review.get_question(
        RecordRef(**payload["report"]["pending_question"]["question_ref"])
    )
    review.submit_decision(
        execution_id=execution_id,
        step_id=view.step_id,
        request_key="dec-cli",
        submission=DecisionSubmission(
            question_ref=view.question_ref,
            fingerprint=view.record.data.fingerprint,
            actor=Actor(kind="human", id="expert-synthetic"),
            outcome="resolved",
            answer_original="内部编号即 HVS 对象。",
            answer_en="A",
            reason="合成 CLI 决定。",
        ),
    )
    code = _run_cli(space, runner, "resume", "--execution", execution_id, "--json")
    assert code == 0
    resumed = json.loads(capsys.readouterr().out)
    assert resumed["outcome"] == "finished"

    code = _run_cli(space, runner, "consume", "--execution", execution_id, "--json")
    consumed = json.loads(capsys.readouterr().out)
    assert consumed["ok"]
    assert consumed["ranges"][0]["status"] == "expert-resolved"
    space.close()


def test_cli_prepare_without_material_exits_four(tmp_path, capsys):
    space, _imported = prepared_space(tmp_path)
    runner = FakeConverterRunner()
    code = _run_cli(
        space,
        runner,
        "prepare",
        "--paper",
        "2501.09999",
        "--request-key",
        "x",
        "--json",
    )
    assert code == 4
    assert "没有已完成的选定材料" in capsys.readouterr().err
    space.close()


def test_cli_status_lists_preparations(tmp_path, capsys):
    space, _imported = prepared_space(tmp_path)
    runner = FakeConverterRunner(available={"latexmlc": False, "pandoc": False})
    service = MaterialsService(space, runner=runner)
    handle, report = service.prepare_materials(request_key="rk", paper=PAPER)
    assert report.outcome == "finished"
    code = _run_cli(space, runner, "status", "--json")
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["executions"][0]["execution_id"] == handle.execution_id
    assert payload["executions"][0]["request_key"] == "rk"
    space.close()

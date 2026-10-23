"""The formal extraction path feeds frozen scoring without new model requests."""

import json
import subprocess
import sys

import pytest
from materials_helpers import PAPER
from scoring_helpers import document, group, plan, prediction, reference, star
from test_quantities import quantities, run_full, value
from test_roster import ScriptedAdapter, submission

from hvs_catalog.evaluation.adapt import prediction_from_execution
from hvs_catalog.evaluation.bundle import (
    load_scoring_bundle,
    save_scoring_bundle,
    score_scoring_bundle,
)
from hvs_catalog.evaluation.saved import score_saved_manifest
from hvs_catalog.evaluation.scoring import score_batch
from hvs_catalog.llm.managed import CallService
from hvs_catalog.pipeline import resume_extraction, start_extraction
from hvs_catalog.workspace import WorkspaceIntegrityError


def saved_reference(pred):
    # Scientific expectations are explicit and do not come from prediction.content.
    answer = document(
        [star("A", [group("radial_velocity", {"value": "520", "unit": "km/s"})])],
        paper=PAPER,
    )
    return reference(answer, materials=pred.materials)


def write_manifest(tmp_path, space, execution_id, ref, scope="full"):
    path = tmp_path / "reference.json"
    path.write_text(ref.model_dump_json(indent=2))
    manifest = tmp_path / "score.json"
    manifest.write_text(
        json.dumps(
            {
                "format": "hvs.scoring-manifest",
                "version": 1,
                "scope": scope,
                **({"quantities": []} if scope == "roster" else {}),
                "papers": [
                    {
                        "arxiv_id": PAPER,
                        "reference": path.name,
                        "workspace": str(space.root),
                        "execution_id": execution_id,
                    }
                ],
            }
        )
    )
    return manifest


def test_full_extraction_scores_and_replays_without_sources(prepared, tmp_path):
    handle, _, adapter, _ = run_full(prepared, [submission(), quantities()])
    space = prepared[0]
    pred = prediction_from_execution(space, handle.execution_id)
    assert pred.completion == "complete" and pred.delivered and pred.format_valid
    assert pred.evidence_checks["checked"] == 3
    path = write_manifest(tmp_path, space, handle.execution_id, saved_reference(pred))
    report = score_saved_manifest(path, tmp_path / "scored")
    assert report.micro.l1.tp == report.micro.l2.correct == 1
    assert report.micro.l2.f1 == 1
    assert len(adapter.seen) == 2
    (tmp_path / "reference.json").unlink()
    path.unlink()
    space.close()
    space.root.rename(tmp_path / "unavailable-workspace")
    assert score_scoring_bundle(tmp_path / "scored/scoring-inputs.json") == report
    for filename in (
        "papers.csv",
        "quantities.csv",
        "differences.csv",
        "scoring-report.json",
    ):
        assert (tmp_path / "scored" / filename).is_file()


def test_partial_numerical_failure_keeps_l1_and_gold_denominator(prepared):
    handle, _, _, _ = run_full(
        prepared, [submission(), quantities([value(4)])], limit=0
    )
    pred = prediction_from_execution(prepared[0], handle.execution_id)
    result = score_batch(plan(PAPER), [saved_reference(pred)], [pred]).papers[0]
    assert pred.completion == "partial" and pred.format_valid
    assert result.l1.tp == 1
    assert (result.l2.correct, result.l2.predicted, result.l2.gold, result.l2.f1) == (
        0,
        0,
        1,
        0,
    )


def test_partial_snapshot_scores_only_retained_values(prepared):
    handle, _, _, _ = run_full(
        prepared,
        [submission(), quantities([value(), value("999", "not in paper")])],
        limit=0,
    )
    pred = prediction_from_execution(prepared[0], handle.execution_id)
    result = score_batch(plan(PAPER), [saved_reference(pred)], [pred]).papers[0]
    assert result.l2.correct == result.l2.predicted == result.l2.gold == 1
    assert pred.completion == "partial"


@pytest.mark.parametrize("valid", [True, False])
def test_saved_empty_and_failed_remain_distinct(prepared, valid):
    handle, _, _, _ = run_full(
        prepared, [{"stars": [], "notes": "" if valid else None}], limit=0
    )
    pred = prediction_from_execution(prepared[0], handle.execution_id)
    ref = reference(document(paper=PAPER), materials=pred.materials)
    row = score_batch(plan(PAPER), [ref], [pred]).papers[0]
    assert row.correct_empty is valid
    assert row.l0.delivered == int(valid)
    assert row.l0.completion_counts == {"complete" if valid else "failed": 1}


def test_interrupt_after_roster_keeps_l1_without_publishing(prepared):
    from pydantic import SecretStr

    space, prep, config = prepared
    handle = start_extraction(
        space,
        preparation_execution_id=prep,
        request_key="interrupted",
        configuration=config,
    )

    class StopAtQuantities(ScriptedAdapter):
        def send(self, payload, runtime, credential):
            if payload["tools"][0]["function"]["name"] == "submit_quantities":
                raise RuntimeError("synthetic unknown-completion interruption")
            return super().send(payload, runtime, credential)

    service = CallService(
        config.runtime,
        credential=SecretStr("synthetic"),
        adapter=StopAtQuantities([submission()]),
    )
    resume_extraction(space, handle.execution_id, service=service)
    pred = prediction_from_execution(space, handle.execution_id)
    assert not pred.delivered and pred.completion == "unfinished"
    row = score_batch(plan(PAPER), [saved_reference(pred)], [pred]).papers[0]
    assert row.l1.tp == 1 and row.l2.gold == 1 and row.l2.predicted == 0


def test_saved_roster_scope_and_scoring_after_extraction_code_changes(
    prepared, monkeypatch
):
    from test_roster import run

    from hvs_catalog.pipeline import inputs

    handle, _, _, _ = run(prepared, [submission()])
    monkeypatch.setattr(inputs, "code_fingerprint", lambda: "different-code")
    pred = prediction_from_execution(prepared[0], handle.execution_id)
    row = score_batch(
        plan(PAPER, scope="roster"), [saved_reference(pred)], [pred]
    ).papers[0]
    assert row.l1.tp == 1 and row.l2 is None and row.l0.delivered == 1


def test_new_execution_without_snapshots_is_undelivered(prepared):
    space, prep, config = prepared
    handle = start_extraction(
        space,
        preparation_execution_id=prep,
        request_key="not-run",
        configuration=config,
    )
    pred = prediction_from_execution(space, handle.execution_id)
    assert (
        pred.completion == "unfinished"
        and pred.content.stars == []
        and not pred.delivered
    )


def test_explicit_reused_roster_is_read_from_its_original_execution(prepared):
    from pydantic import SecretStr
    from test_roster import run

    from hvs_catalog.pipeline.roster import saved_roster

    space, _, config = prepared
    original, _, _, _ = run(prepared, [submission()])
    roster = saved_roster(space, original.execution_id)
    handle = start_extraction(
        space, roster_ref=roster.ref(), request_key="score-reuse", configuration=config
    )
    adapter = ScriptedAdapter([quantities()])
    resume_extraction(
        space,
        handle.execution_id,
        service=CallService(
            config.runtime, credential=SecretStr("synthetic"), adapter=adapter
        ),
    )
    pred = prediction_from_execution(space, handle.execution_id)
    row = score_batch(plan(PAPER), [saved_reference(pred)], [pred]).papers[0]
    assert row.l1.tp == row.l2.correct == 1
    assert (
        pred.provenance["run"]["stages"]["roster"]["source_execution"]
        == original.execution_id
    )
    assert len(adapter.seen) == 1


def test_generated_scoring_contracts_match_current_models():
    from pathlib import Path

    from hvs_catalog.evaluation.generate_scoring import check_scoring_outputs

    assert (
        check_scoring_outputs(Path(__file__).parents[1] / "contracts/generated/scoring")
        == []
    )


def test_corrupt_saved_science_is_not_silently_rescored(prepared):
    handle, _, _, _ = run_full(prepared, [submission(), quantities()])
    space = prepared[0]
    pred = prediction_from_execution(space, handle.execution_id)
    ref = pred.provenance["run"]["result_ref"]
    (space.root / ref["path"]).write_text("{}")
    with pytest.raises(WorkspaceIntegrityError, match="哈希|fingerprint|hash"):
        prediction_from_execution(space, handle.execution_id)


@pytest.mark.parametrize(
    "field", ["predictions", "gold", "plan", "program", "version", "sha256"]
)
def test_bundle_tampering_is_rejected(tmp_path, field):
    doc = document([star()])
    path = tmp_path / "inputs.json"
    save_scoring_bundle(
        path, plan=plan(), gold=[reference(doc)], predictions=[prediction(doc)]
    )
    data = json.loads(path.read_text())
    if field == "predictions":
        data[field][0]["content"]["stars"] = []
    elif field == "gold":
        data[field][0]["stars"] = []
    elif field == "plan":
        data[field]["quantities"] = ["distance"]
    elif field == "program":
        data["plan"]["program"]["sha256"] = "0" * 64
    elif field == "version":
        data[field] = 1
    else:
        data[field] = "0" * 64
    path.write_text(json.dumps(data))
    with pytest.raises(ValueError):
        load_scoring_bundle(path)


def test_existing_outputs_are_preserved_and_json_is_strict(tmp_path):
    doc = document([star()])
    path = tmp_path / "inputs.json"
    kwargs = {
        "plan": plan(),
        "gold": [reference(doc)],
        "predictions": [prediction(doc)],
    }
    save_scoring_bundle(path, **kwargs)
    before = path.read_bytes()
    with pytest.raises(FileExistsError):
        save_scoring_bundle(path, **kwargs)
    assert path.read_bytes() == before
    path.write_text('{"version":2,"version":2}')
    with pytest.raises(ValueError, match="Duplicate JSON key"):
        load_scoring_bundle(path)


@pytest.mark.integration
def test_score_cli_uses_saved_execution_and_offline_replay(prepared, tmp_path):
    handle, _, adapter, _ = run_full(prepared, [submission(), quantities()])
    space = prepared[0]
    pred = prediction_from_execution(space, handle.execution_id)
    manifest = write_manifest(
        tmp_path, space, handle.execution_id, saved_reference(pred)
    )
    output = tmp_path / "cli-report"
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "hvs_catalog",
            "score",
            "run",
            "--manifest",
            str(manifest),
            "--output",
            str(output),
        ],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "C=1 P=1 G=1" in result.stdout and "F1=1.0000" in result.stdout
    again = subprocess.run(
        [
            sys.executable,
            "-m",
            "hvs_catalog",
            "score",
            "replay",
            "--inputs",
            str(output / "scoring-inputs.json"),
        ],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert again.returncode == 0, again.stderr
    assert result.stdout == again.stdout
    assert len(adapter.seen) == 2


def test_direct_annotation_provenance_records_exact_bytes_and_git_status(tmp_path):
    import hashlib

    from hvs_catalog.evaluation.saved import prepare_saved_scoring

    def git(*args):
        return subprocess.run(
            ["git", "-C", str(tmp_path), *args],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()

    git("init", "-q")
    annotation = tmp_path / "annotation.json"
    annotation.write_text(reference(document()).model_dump_json(indent=2) + "\n")
    git("add", "annotation.json")
    git(
        "-c",
        "user.name=Synthetic",
        "-c",
        "user.email=synthetic@example.invalid",
        "commit",
        "-qm",
        "Synthetic reference",
    )
    manifest = tmp_path / "score.json"
    manifest.write_text(
        json.dumps(
            {"papers": [{"arxiv_id": "2601.99999", "reference": "annotation.json"}]}
        )
    )
    for dirty in (False, True):
        if dirty:
            annotation.write_text(annotation.read_text() + "\n")
        frozen, gold, predictions = prepare_saved_scoring(manifest)
        provenance = frozen.provenance["reference_files"][0]
        assert (
            provenance["sha256"] == hashlib.sha256(annotation.read_bytes()).hexdigest()
        )
        assert provenance["git"] == {
            "commit": git("rev-parse", "HEAD"),
            "path": "annotation.json",
            "tracked": True,
            "dirty": dirty,
        }
        assert gold[0].metadata.arxiv_id == "2601.99999" and predictions == []

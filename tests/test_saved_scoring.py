"""Saved full PDF extraction, independent Gold, read-only scoring and portable replay."""

import hashlib
import json
import sqlite3
import subprocess
import sys

import pytest
from evaluation_helpers import build_fresh_draft
from gold_save_helpers import authorize
from materials_helpers import PAPER
from test_pdf_quantities import (
    Adapter,
    object_id,
    quantities,
    start,
)
from test_pdf_quantities import prepared_pdf as _prepared_pdf_fixture

from hvs_catalog.cli import main
from hvs_catalog.evaluation import (
    GoldTarget,
    approved_save,
    check_save,
    compact_gold_annotation,
    parse_gold_annotation,
    write_compact_gold_pair,
)
from hvs_catalog.evaluation.adapt import prediction_from_execution
from hvs_catalog.evaluation.saved import SavedScoringInput, prepare_saved_scoring
from hvs_catalog.workspace import open_workspace

prepared_pdf = _prepared_pdf_fixture


@pytest.fixture
def manifest(prepared_pdf, tmp_path):
    space, _, _ = prepared_pdf
    adapter = Adapter(lambda tool, _: quantities(object_id(tool)), names=("A",))
    handle, result, _ = start(prepared_pdf, adapter)
    assert result.outcome == "finished"
    repo = tmp_path / "gold"
    material = repo / "materials"
    material.mkdir(parents=True)
    pdf = (space.root.parent / "paper.pdf").read_bytes()
    (material / "paper.pdf").write_bytes(pdf)
    raw = build_fresh_draft(
        pdf_sha256=hashlib.sha256(pdf).hexdigest(),
        value="-430",
        error="12",
        page=1,
        include_quote=False,
    )
    raw["paper"]["arxiv_id"] = PAPER
    raw["paper"]["version"] = "v1"
    raw["contributions"][0]["identifiers"][0]["value"] = "A"
    raw["contributions"][0]["paper_boundness"]["evidence"][0]["page"] = 1
    draft = parse_gold_annotation(raw)
    pair = compact_gold_annotation(
        draft,
        title="Synthetic full extraction",
        original_annotator="synthetic-human",
        reviewer="synthetic-human",
        material_review={
            "value": "checked",
            "basis": "Synthetic same PDF",
            "record_sha256": "a" * 64,
        },
        scope_review={
            "value": "complete",
            "basis": "Synthetic closed scope",
            "record_sha256": "a" * 64,
        },
    )
    pair_dir = repo / "pair"
    write_compact_gold_pair(pair, pair_dir)
    target = GoldTarget(repo / "data" / PAPER)
    checked = check_save(pair_path=pair_dir, material_root=material, target=target)
    approved_save(
        pair_path=pair_dir,
        material_root=material,
        target=target,
        approval=authorize(checked.binding),
    )
    for args in (
        ("init", "-q"),
        ("add", "data"),
        (
            "-c",
            "user.name=Synthetic",
            "-c",
            "user.email=synthetic@example.invalid",
            "commit",
            "-qm",
            "Synthetic Gold",
        ),
    ):
        subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)
    path = tmp_path / "manifest.json"
    path.write_text(
        json.dumps(
            {
                "gold_repository": str(repo),
                "papers": [
                    {
                        "paper_key": PAPER,
                        "gold_target": str(target.root),
                        "material_root": str(material),
                        "workspace": str(space.root),
                        "execution_id": handle.execution_id,
                    }
                ],
            }
        )
    )
    return path, space, handle, adapter


@pytest.mark.integration
def test_saved_full_scoring_cli_and_fresh_process_replay(manifest, tmp_path, capsys):
    path, space, _, adapter = manifest
    before = (space.root / "business.db").read_bytes()
    calls = len(adapter.seen)
    output = tmp_path / "scoring"
    assert main(["score", "run", "--manifest", str(path), "--output", str(output)]) == 0
    stdout = capsys.readouterr().out
    assert "L2 C=1 P=1 G=1" in stdout
    assert "整批 final" in stdout
    assert len(adapter.seen) == calls
    assert (space.root / "business.db").read_bytes() == before
    report = json.loads((output / "scoring-report.json").read_text())
    assert len(report["papers"][0]["quantity_breakdown"]) == 18
    assert {p.name for p in output.iterdir()} == {
        "evaluation-inputs.json",
        "scoring-report.json",
        "metrics.csv",
        "delivery.csv",
        "pairings.csv",
        "diagnostics.csv",
    }
    replay = tmp_path / "replay"
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "hvs_catalog",
            "score",
            "replay",
            "--inputs",
            str(output / "evaluation-inputs.json"),
            "--output",
            str(replay),
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads((replay / "scoring-report.json").read_text()) == report
    assert main(["score", "run", "--manifest", str(path), "--output", str(output)]) == 2
    assert "非空" in capsys.readouterr().err


def test_read_only_business_store_blocks_writes(manifest):
    _, space, handle, _ = manifest
    reader = open_workspace(space.root, read_only=True)
    try:
        assert prediction_from_execution(reader, handle.execution_id).objects
        with (
            pytest.raises(sqlite3.OperationalError, match="readonly"),
            reader.store().transaction() as tx,
        ):
            tx.conn.execute("UPDATE meta SET value='forbidden'")
    finally:
        reader.close()


def test_saved_scoring_rejects_changed_extraction_material(manifest):
    path, space, _, _ = manifest
    data = SavedScoringInput.model_validate_json(path.read_bytes())
    raw = next((space.root / "sources/packages").glob("*.bin"))
    raw.write_bytes(raw.read_bytes() + b"changed")
    with pytest.raises(ValueError, match="指纹不符"):
        prepare_saved_scoring(data)


def test_gold_material_gap_is_reported_without_hiding_delivery(manifest, tmp_path):
    path, _, _, _ = manifest
    payload = json.loads(path.read_text())
    payload["papers"][0]["material_root"] = str(tmp_path / "missing-pdf")
    plan, gold, preds = prepare_saved_scoring(SavedScoringInput.model_validate(payload))
    from hvs_catalog.evaluation.scoring import score_batch

    report = score_batch(plan, gold, preds)
    assert report.papers[0].l0.delivered == 1
    assert report.papers[0].readiness == "blocked"
    assert report.papers[0].l2 is None and report.batch_provisional


def test_numeric_replay_rejects_other_program_version(manifest, tmp_path, monkeypatch):
    from hvs_catalog.evaluation import saved
    from hvs_catalog.evaluation.bundle import (
        ScoringBundleError,
        save_scoring_bundle,
        score_scoring_bundle,
    )
    from hvs_catalog.evaluation.scoring import numeric_scoring_config

    path, _, _, _ = manifest
    plan, gold, preds = prepare_saved_scoring(
        SavedScoringInput.model_validate_json(path.read_bytes())
    )
    bundle = tmp_path / "bundle.json"
    save_scoring_bundle(
        bundle,
        plan=plan,
        gold=gold,
        predictions=preds,
        auxiliary=numeric_scoring_config(),
    )
    other = plan.program_ref.model_copy(update={"fingerprint": "a" * 64})
    monkeypatch.setattr(saved, "program_reference", lambda: other)
    with pytest.raises(ScoringBundleError, match="程序与当前安装不符"):
        score_scoring_bundle(bundle)

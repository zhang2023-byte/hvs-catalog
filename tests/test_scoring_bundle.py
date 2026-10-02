"""Scoring input files reject malformed containers before producing a report."""

import json
import os
import subprocess
import sys

import pytest
from scoring_sample_helpers import load_bundle

from hvs_catalog.evaluation.bundle import (
    ScoringBundleError,
    load_scoring_bundle,
    save_scoring_bundle,
)


@pytest.fixture
def bundle_path(tmp_path):
    plan, gold, predictions, auxiliary = load_bundle("synthetic-s01")
    path = tmp_path / "inputs.json"
    save_scoring_bundle(
        path, plan=plan, gold=gold, predictions=predictions, auxiliary=auxiliary
    )
    return path


@pytest.mark.parametrize("field", ["gold", "predictions"])
@pytest.mark.parametrize("value", [None, 5, {}, "not a list"])
def test_bundle_rejects_non_list_entries(bundle_path, field, value):
    payload = json.loads(bundle_path.read_text())
    payload[field] = value
    bundle_path.write_text(json.dumps(payload))
    before = bundle_path.read_bytes()
    with pytest.raises(ScoringBundleError, match=field):
        load_scoring_bundle(bundle_path)
    assert bundle_path.read_bytes() == before


@pytest.mark.parametrize("field", ["gold", "predictions"])
def test_bundle_rejects_missing_entries(bundle_path, field):
    payload = json.loads(bundle_path.read_text())
    del payload[field]
    bundle_path.write_text(json.dumps(payload))
    with pytest.raises(ScoringBundleError, match=f"缺少字段：{field}"):
        load_scoring_bundle(bundle_path)


@pytest.mark.parametrize(
    "change, message",
    [
        ({"schema": {"name": "other", "version": 1}}, "schema.name"),
        ({"schema": {"name": "hvs.evaluation.scoring-bundle", "version": 2}}, "版本"),
        ({"digests": None}, "摘要"),
    ],
)
def test_bundle_rejects_invalid_envelope(bundle_path, change, message):
    payload = json.loads(bundle_path.read_text())
    payload.update(change)
    bundle_path.write_text(json.dumps(payload))
    with pytest.raises(ScoringBundleError, match=message):
        load_scoring_bundle(bundle_path)


@pytest.mark.parametrize("content", ['{"schema":', "[]"])
def test_bundle_rejects_unreadable_payload(bundle_path, content):
    bundle_path.write_text(content)
    with pytest.raises(ScoringBundleError):
        load_scoring_bundle(bundle_path)


@pytest.mark.integration
def test_score_command_rejects_null_entries_without_report(bundle_path, tmp_path):
    payload = json.loads(bundle_path.read_text())
    payload["gold"] = None
    bundle_path.write_text(json.dumps(payload))
    before = bundle_path.read_bytes()
    report = tmp_path / "report.json"
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "hvs_catalog",
            "score",
            "demo",
            "--inputs",
            str(bundle_path),
            "--output",
            str(report),
        ],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert result.returncode == 2, result.stderr
    assert "[拒绝]" in result.stderr and "gold" in result.stderr
    assert "Traceback" not in result.stderr
    assert not report.exists()
    assert bundle_path.read_bytes() == before

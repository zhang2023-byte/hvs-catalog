"""Supported extraction-demo unresolved and independent-output paths."""

import json

from hvs_catalog.cli import main


def test_extract_demo_keep_pending_saves_unresolved_output(tmp_path):
    output = tmp_path / "output"
    assert (
        main(
            [
                "extract",
                "demo",
                "--space",
                str(tmp_path / "space"),
                "--paper",
                "FIC-R05-A",
                "--decision",
                "keep_pending",
                "--output",
                str(output),
            ]
        )
        == 0
    )
    result = json.loads((output / "extraction-report.json").read_text())
    assert result["report"]["outcome"] == "paused"
    assert result["status"]["questions"][0]["answered_outcome"] == "unresolved"
    assert (output / "evaluation-inputs.json").exists()


def test_extract_demo_rejects_call_log_inside_client_space_before_writes(tmp_path):
    space = tmp_path / "space"
    assert (
        main(
            [
                "extract",
                "demo",
                "--space",
                str(space),
                "--paper",
                "FIC-R05-B",
                "--output",
                str(space / "outputs"),
            ]
        )
        == 2
    )
    assert not space.exists()

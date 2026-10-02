"""Real process interruption with frozen configured prompts and archived-response reuse."""

import json
import subprocess
import sys

import pytest

pytestmark = pytest.mark.integration

DRIVER = r"""
import json, os, sys
from pathlib import Path
from hvs_catalog.llm import ModelConfig
from hvs_catalog.pipeline import (
    PAPER_B, DIRECT_METHOD, ExtractionConfig, ExtractionFlow, PipelineMockService,
    get_extraction, import_mock_source, resume_extraction, start_configured_extraction,
)
from hvs_catalog.runtime import resume_execution
from hvs_catalog.workspace import initialize_workspace, open_workspace

root, phase = Path(sys.argv[1]), sys.argv[2]
journal = root.parent / "calls.jsonl"
if phase == "crash":
    space = initialize_workspace(root)
    source = import_mock_source(space, PAPER_B)
    config = ExtractionConfig(step_models={"quantities": ModelConfig(model="mimo-v2.6-flash")})
    handle = start_configured_extraction(space, configuration=config, source_refs=[source], request_key="process-inputs")
    (root.parent / "execution.json").write_text(json.dumps({"execution_id": handle.execution_id}))
    flow = ExtractionFlow(DIRECT_METHOD, mode="daily", fault_hooks={"after_archive:direct_roster": lambda: os._exit(9)})
    resume_execution(space, handle.execution_id, flow, service=PipelineMockService(journal))
else:
    space = open_workspace(root)
    eid = json.loads((root.parent / "execution.json").read_text())["execution_id"]
    report = resume_extraction(space, eid, service=PipelineMockService(journal))
    print(json.dumps({"outcome": report.outcome, "status": get_extraction(space, eid)}))
    space.close()
"""


def test_new_process_recovers_original_configuration_and_single_archived_call(tmp_path):
    root = tmp_path / "space"
    first = subprocess.run(
        [sys.executable, "-c", DRIVER, str(root), "crash"],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert first.returncode == 9, first.stderr
    second = subprocess.run(
        [sys.executable, "-c", DRIVER, str(root), "resume"],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert second.returncode == 0, second.stderr
    result = json.loads(second.stdout)
    assert result["outcome"] == "finished"
    assert (
        result["status"]["config"]["configuration"]["step_models"]["quantities"][
            "model"
        ]
        == "mimo-v2.6-flash"
    )
    calls = [
        json.loads(line) for line in (tmp_path / "calls.jsonl").read_text().splitlines()
    ]
    assert len(calls) == 2
    assert [row["step"] for row in calls] == ["direct_roster", "quantities"]

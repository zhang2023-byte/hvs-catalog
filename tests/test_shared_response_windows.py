"""Both method/question kinds reenter archived and question-save death windows."""

import json
import subprocess
import sys

import pytest

from hvs_catalog.pipeline import (
    ExtractionRequest,
    StepModel,
    resume_extraction,
    start_extraction,
)
from hvs_catalog.pipeline.mock import PipelineMockService, import_mock_source
from hvs_catalog.workspace import initialize_workspace

DRIVER = """
import os, sys
from hvs_catalog.pipeline import resume_extraction
from hvs_catalog.pipeline.flow import ExtractionFlow
from hvs_catalog.pipeline.methods import DIRECT_METHOD, CANDIDATE_REVIEW_METHOD
from hvs_catalog.pipeline.mock import PipelineMockService, mock_preflight
from hvs_catalog.pipeline.registry import PipelineRegistry, MethodRegistration
from hvs_catalog.workspace import open_workspace
spec = DIRECT_METHOD if sys.argv[3] == 'direct' else CANDIDATE_REVIEW_METHOD
registry = PipelineRegistry()
registry.register(MethodRegistration(spec=spec, preflight=mock_preflight,
    build_flow=lambda spec, mode: ExtractionFlow(spec, mode=mode,
        fault_hooks={sys.argv[4]: lambda: os._exit(73)})))
space = open_workspace(sys.argv[1])
resume_extraction(space, 'exec-000001', service=PipelineMockService(sys.argv[2]), registry=registry)
raise AssertionError('expected death')
"""


@pytest.mark.parametrize("method", ["direct", "candidate_review"])
@pytest.mark.parametrize("paper", ["FIC-R05-A", "FIC-CANDIDATE-C"])
@pytest.mark.parametrize("window", ["after_archive", "after_question_create"])
def test_shared_response_and_question_save_windows(tmp_path, method, paper, window):
    space = initialize_workspace(tmp_path / "space")
    journal = tmp_path / "external.jsonl"
    try:
        start_extraction(
            space,
            request=ExtractionRequest(
                source_refs=[import_mock_source(space, paper)],
                method=method,
                mode="synthetic",
                models=[StepModel(model="synthetic-mock")],
                expert_adjudication=True,
            ),
            request_key="windows",
        )
        step = (
            "quantities"
            if paper == "FIC-R05-A"
            else ("direct_roster" if method == "direct" else "review_candidates")
        )
        hook = window if window == "after_question_create" else f"{window}:{step}"
        driver = tmp_path / "die.py"
        driver.write_text(DRIVER)
        result = subprocess.run(
            [sys.executable, str(driver), str(space.root), str(journal), method, hook],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        assert result.returncode == 73, result.stdout + result.stderr
        before = [json.loads(line) for line in journal.read_text().splitlines()]
        report = resume_extraction(
            space, "exec-000001", service=PipelineMockService(journal)
        )
        assert report.outcome == "paused", report.errors
        after = [json.loads(line) for line in journal.read_text().splitlines()]
        # No already-archived request was repeated, even if later candidates still run.
        for entry in before:
            assert (
                sum(call["request_digest"] == entry["request_digest"] for call in after)
                == 1
            )
        if window == "after_question_create":
            assert after == before
    finally:
        space.close()

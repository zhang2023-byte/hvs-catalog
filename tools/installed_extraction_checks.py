"""Exercise configuration/prompts and saved mock recovery from an installed wheel."""

from __future__ import annotations

import json
import os
import subprocess

DRIVER = r"""
import json, sys
from pathlib import Path
from hvs_catalog.llm import ModelConfig
from hvs_catalog.llm.prompts import PromptInput
from pydantic import ValidationError
from hvs_catalog.pipeline import (
    PAPER_A, PAPER_B, ExtractionConfig, PipelineMockService, check_extraction_config,
    get_extraction, import_mock_source, preview_extraction_prompt, resume_extraction,
    start_configured_extraction, submit_expert_decision,
)
from hvs_catalog.workspace import initialize_workspace, open_workspace

root, phase = Path(sys.argv[1]), sys.argv[2]
if phase == "create":
    try:
        ExtractionConfig(precedents=[{"precedent_id": "SYN-P1", "version": "synthetic-1", "explanation": "Record the explicit author synthesis.", "conditions": "An explicit synthesis is present.", "boundaries": "Do not infer it by counting models."}])
    except ValidationError:
        pass
    else:
        raise AssertionError("A precedent requires an explicit approved declaration.")
    config = ExtractionConfig(step_models={
        "roster": ModelConfig(model="qwen3.8-flash"),
        "quantities": ModelConfig(model="mimo-v2.6-flash"),
    })
    assert len(check_extraction_config(config)["steps"]) == 2
    preview = preview_extraction_prompt(ExtractionConfig(), "roster", PromptInput(manuscript="FILE synthetic.tex\n1|Synthetic A is unbound."))
    assert preview["prepared"]["payload"]["provider"]["only"] == ["deepseek"]
    assert "S07" in preview["request"]["messages"][0]["content"]
    (root.parent / "config.json").write_text(config.model_dump_json())
    (root.parent / "input.json").write_text('{"manuscript":"FILE synthetic.tex\\n1|A is unbound."}')
    space = initialize_workspace(root)
    source = import_mock_source(space, PAPER_B)
    handle = start_configured_extraction(space, configuration=config, source_refs=[source], request_key="installed-combination")
    assert resume_extraction(space, handle.execution_id, service=PipelineMockService()).outcome == "finished"
    status = get_extraction(space, handle.execution_id)
    assert status["usage"]["tokens"] == 320
    source = import_mock_source(space, PAPER_A)
    pending = start_configured_extraction(space, configuration=ExtractionConfig(), source_refs=[source], request_key="installed-pause")
    assert resume_extraction(space, pending.execution_id, service=PipelineMockService()).outcome == "paused"
    print(json.dumps({"execution_id": pending.execution_id}))
    space.close()
else:
    space = open_workspace(root)
    eid = sys.argv[3]
    from hvs_catalog.pipeline import pending_questions
    question = pending_questions(space, eid)[0]
    submit_expert_decision(space, execution_id=eid, question_ref=question.question_ref, request_key="installed-decision", choice="no_overall_conclusion")
    assert resume_extraction(space, eid, service=PipelineMockService()).outcome == "finished"
    assert get_extraction(space, eid)["usage"]["tokens"] == 320
    space.close()
"""


def verify_installed_extraction(venv_dir, work_dir, script) -> list[str]:
    python = venv_dir / ("Scripts" if os.name == "nt" else "bin") / "python"
    root = work_dir / "configured-extraction" / "space"
    root.parent.mkdir()
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)

    def run(args):
        result = subprocess.run(
            args,
            cwd=work_dir,
            env=env,
            capture_output=True,
            text=True,
            check=False,
            timeout=120,
        )
        if result.returncode:
            raise ValueError(result.stderr or result.stdout)
        return json.loads(result.stdout) if result.stdout.strip() else None

    try:
        created = run([str(python), "-c", DRIVER, str(root), "create"])
        run([str(python), "-c", DRIVER, str(root), "resume", created["execution_id"]])
        default = run([str(script), "extract", "config"])
        assert default["default_model"]["model"] == "deepseek-v4.1-flash"
        checked = run(
            [
                str(script),
                "extract",
                "check",
                "--config",
                str(root.parent / "config.json"),
            ]
        )
        assert "method" not in checked["configuration"]
        preview = run(
            [
                str(script),
                "extract",
                "preview",
                "--step",
                "roster",
                "--input",
                str(root.parent / "input.json"),
            ]
        )
        assert preview["request"]["tool"]["name"] == "submit_roster"
        assert "A is unbound" in preview["request"]["messages"][1]["content"]
    except (ValueError, AssertionError, subprocess.TimeoutExpired) as exc:
        return [f"Installed extraction configuration/preview/recovery failed: {exc}"]
    return []

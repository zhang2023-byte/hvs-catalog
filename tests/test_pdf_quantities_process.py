"""Exit after archiving a numerical response, then resume in an independent process."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

TOOLS = Path(__file__).resolve().parents[1] / "tools"


@pytest.mark.integration
@pytest.mark.parametrize("parallel", [False, True])
def test_archived_numeric_response_reused_after_real_process_exit(tmp_path, parallel):
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "installed_extraction_checks", TOOLS / "installed_extraction_checks.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    root = tmp_path / "space"
    for phase, expected in (
        ("create", 0),
        ("crash", 73),
        ("backup", 0),
        ("recover", 0),
        ("verify-backup", 0),
    ):
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                module.PDF_DRIVER,
                str(root),
                phase,
                *(["parallel"] if parallel else []),
            ],
            cwd=tmp_path,
            env=env,
            capture_output=True,
            text=True,
            check=False,
            timeout=90,
        )
        assert result.returncode == expected, result.stderr + result.stdout

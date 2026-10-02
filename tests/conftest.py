"""Allow tests to import development check tools from the tools directory."""

import os
import sys
from pathlib import Path

import pytest

_TOOLS_DIR = Path(__file__).resolve().parents[1] / "tools"
if str(_TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(_TOOLS_DIR))


@pytest.fixture(autouse=True)
def subprocess_test_helpers(monkeypatch):
    """Repository subprocess tests can import the same small trusted helpers.

    Distribution checks explicitly remove PYTHONPATH and never import tests.
    """
    helpers = str(Path(__file__).resolve().parent)
    current = os.environ.get("PYTHONPATH")
    monkeypatch.setenv(
        "PYTHONPATH", helpers + (os.pathsep + current if current else "")
    )

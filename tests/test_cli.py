"""CLI behavior tests: help, version, and consistency with the sole pyproject version
source.
"""

import tomllib
from importlib.metadata import version as installed_version
from pathlib import Path

import pytest

from hvs_catalog import cli

REPO_ROOT = Path(__file__).resolve().parents[1]


def test_version_prints_installed_version(capsys):
    with pytest.raises(SystemExit) as excinfo:
        cli.main(["--version"])
    assert excinfo.value.code == 0
    output = capsys.readouterr().out
    assert output.strip() == f"hvs-catalog {installed_version('hvs-catalog')}"


def test_help_exits_zero_and_shows_usage(capsys):
    with pytest.raises(SystemExit) as excinfo:
        cli.main(["--help"])
    assert excinfo.value.code == 0
    assert "usage: hvs-catalog" in capsys.readouterr().out


def test_without_arguments_prints_help(capsys):
    assert cli.main([]) == 0
    assert "usage: hvs-catalog" in capsys.readouterr().out


def test_installed_version_matches_pyproject():
    pyproject = tomllib.loads(
        (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    )
    assert installed_version("hvs-catalog") == pyproject["project"]["version"]

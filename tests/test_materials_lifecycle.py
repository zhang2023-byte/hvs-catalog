"""Synthetic cancellation, frozen parsing and failed-run restore regressions."""

from __future__ import annotations

import os
import platform
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest
from materials_helpers import FakeConverterRunner, make_tar_gz, prepared_space

from hvs_catalog.materials.flow import MaterialsFlow, parser_fingerprint
from hvs_catalog.materials.service import MaterialsService
from hvs_catalog.runtime.execution import get_execution
from hvs_catalog.workspace import (
    RecoveryInconsistentError,
    backup_workspace,
    open_workspace,
    restore_workspace,
)


@pytest.mark.skipif(
    platform.system() != "Darwin", reason="Only macOS isolation has been verified"
)
def test_real_sigint_cleans_separate_converter_process_group(tmp_path):
    script = r"""
import sys
from pathlib import Path
from hvs_catalog.materials.sandbox import SandboxedConverterRunner
try:
    SandboxedConverterRunner().run('pandoc', ['/bin/sh', '-c', 'sleep 30 & echo $! > child.pid; echo ready > ready.txt; wait'], workdir=Path(sys.argv[1]), timeout=60)
except KeyboardInterrupt:
    raise SystemExit(73)
"""
    parent = subprocess.Popen(
        [sys.executable, "-c", script, str(tmp_path)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        deadline = time.monotonic() + 20
        while not (tmp_path / "ready.txt").exists():
            assert parent.poll() is None
            assert time.monotonic() < deadline
            time.sleep(0.05)
        child = int((tmp_path / "child.pid").read_text())
        os.kill(parent.pid, signal.SIGINT)
        out, err = parent.communicate(timeout=15)
        assert parent.returncode == 73, (out, err)
        deadline = time.monotonic() + 5
        while True:
            state = subprocess.run(
                ["ps", "-o", "stat=", "-p", str(child)],
                capture_output=True,
                text=True,
                check=False,
            ).stdout.strip()
            if not state or state.startswith("Z"):
                break
            assert time.monotonic() < deadline, state
            time.sleep(0.05)
    finally:
        if parent.poll() is None:
            parent.kill()
        parent.communicate(timeout=10)


def test_observer_bytes_are_part_of_frozen_parser_fingerprint(monkeypatch):
    original = Path.read_bytes
    before = parser_fingerprint()

    def changed(path):
        value = original(path)
        return (
            value + b"\n# synthetic observer revision\n"
            if path.name == "source_trace.ltxml"
            else value
        )

    monkeypatch.setattr(Path, "read_bytes", changed)
    assert parser_fingerprint() != before


def test_changed_parser_blocks_resume_of_saved_failed_run(tmp_path, monkeypatch):
    space, imported = prepared_space(tmp_path)

    def fail(*_args):
        raise RuntimeError("synthetic failure before conversion")

    monkeypatch.setattr(MaterialsFlow, "_convert_tables", fail)
    service = MaterialsService(space, runner=FakeConverterRunner())
    try:
        handle, first = service.prepare_materials(
            request_key="frozen", paper=imported.canonical_id
        )
        assert first.outcome == "failed"
        monkeypatch.setattr(
            "hvs_catalog.materials.flow.parser_fingerprint", lambda: "0" * 64
        )
        with pytest.raises(RecoveryInconsistentError, match="解析代码"):
            service.resume_preparation(handle.execution_id)
    finally:
        space.close()


def test_repeated_failure_backup_restore_keeps_errors_then_finishes(
    tmp_path, monkeypatch
):
    source = r"\documentclass{article}\begin{document}\begin{tabular}{ll}FIC-A & 00525 \\ \end{tabular}\end{document}"
    space, imported = prepared_space(
        tmp_path, make_tar_gz({"main.tex": source.encode()})
    )
    original = MaterialsFlow._convert_tables

    def fail(*_args):
        raise RuntimeError("synthetic repeatable failure")

    monkeypatch.setattr(MaterialsFlow, "_convert_tables", fail)
    service = MaterialsService(space, runner=FakeConverterRunner())
    try:
        handle, first = service.prepare_materials(
            request_key="restore", paper=imported.canonical_id
        )
        assert (
            first.outcome
            == service.resume_preparation(handle.execution_id).outcome
            == "failed"
        )
        backup_workspace(space, tmp_path / "backup")
        restore_workspace(tmp_path / "backup", tmp_path / "restored")
    finally:
        space.close()
    monkeypatch.setattr(MaterialsFlow, "_convert_tables", original)
    restored = open_workspace(tmp_path / "restored")
    try:
        execution = get_execution(restored, handle.execution_id)
        run = restored.store().read_record_current(execution.run_ref.record_id)
        assert (
            len(run.data.errors)
            == len({error.error_id for error in run.data.errors})
            == 2
        )
        service = MaterialsService(restored, runner=FakeConverterRunner())
        result = service.resume_preparation(handle.execution_id)
        assert result.outcome == "finished", result.errors
        consumed = service.consume_prepared_input(handle.execution_id)
        assert consumed["ok"] and consumed["materials_complete"]
    finally:
        restored.close()

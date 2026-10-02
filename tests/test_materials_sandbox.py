"""Synthetic and native converter-isolation tests; use macOS sandbox-exec when available,
otherwise skip.

On macOS with /usr/bin/sandbox-exec and latexmlc, verify no network, writes confined to
conversion directories, and denied package .ltxml/.pm reads. Elsewhere verify
unavailable reporting without unisolated execution, without claiming platform support.
"""

from __future__ import annotations

import platform
import shutil
import subprocess
from pathlib import Path

import pytest

from hvs_catalog.materials.sandbox import (
    SandboxedConverterRunner,
    sandbox_profile,
)

SANDBOX_EXEC = Path("/usr/bin/sandbox-exec")
REAL_SANDBOX = platform.system() == "Darwin" and SANDBOX_EXEC.is_file()
REAL_LATEXML = REAL_SANDBOX and shutil.which("latexmlc") is not None


def test_profile_limits_reads_to_conversion_dir_and_system(tmp_path):
    profile = sandbox_profile(tmp_path)
    assert "(deny network*)" in profile
    assert f'(allow file-write* (subpath "{tmp_path.resolve()}"))' in profile
    assert "(deny file-write*)" in profile
    # Explicit read allowlist: deny file-read-data globally, permitting only conversion directories and
    # trusted system dependency trees; directory metadata/root reads are needed for process bootstrap.
    assert "(deny file-read-data)" in profile
    assert '(allow file-read-data (literal "/"))' in profile
    assert "(allow file-read-metadata)" in profile
    assert f'(allow file-read-data (subpath "{tmp_path.resolve()}"))' in profile
    assert "(ltxml|pm)" in profile  # Deny reads of executable package bindings.


@pytest.mark.integration
@pytest.mark.skipif(not REAL_SANDBOX, reason="需要本机 macOS sandbox-exec")
def test_sandbox_denies_network_local_endpoint_and_outside_access(tmp_path):
    """Verify denied network access against a new local synthetic endpoint, never an
    external service.
    """
    import http.server
    import socket
    import threading

    server = http.server.HTTPServer(
        ("127.0.0.1", 0), http.server.BaseHTTPRequestHandler
    )
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=5):
            pass  # Confirm the endpoint works from an unisolated environment.
        completed = subprocess.run(
            [
                str(SANDBOX_EXEC),
                "-p",
                sandbox_profile(tmp_path),
                "/usr/bin/curl",
                "-sS",
                "-m",
                "5",
                f"http://127.0.0.1:{port}/synthetic",
            ],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        combined = completed.stdout + completed.stderr
        assert "LOCAL-ENDPOINT-REACHED" not in combined
        assert completed.returncode != 0 or "Failed to connect" in combined
    finally:
        server.shutdown()
        thread.join(timeout=5)

    # Deny reads of newly created synthetic private files outside the package; no real papers are involved.
    private = tmp_path.parent / "sandbox-private-probe.txt"
    private.write_text("SYNTH-PRIVATE-ONLY", encoding="utf-8")
    completed = subprocess.run(
        [
            str(SANDBOX_EXEC),
            "-p",
            sandbox_profile(tmp_path),
            "/bin/cat",
            str(private),
        ],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert "SYNTH-PRIVATE-ONLY" not in completed.stdout
    assert completed.returncode != 0

    # Deny writes outside the directory while allowing reads/writes inside it.
    outside = tmp_path.parent / "sandbox-outside-probe.txt"
    completed = subprocess.run(
        [
            str(SANDBOX_EXEC),
            "-p",
            sandbox_profile(tmp_path),
            "sh",
            "-c",
            f"echo x > {outside} && echo WRITE-ALLOWED || echo WRITE-DENIED",
        ],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert "WRITE-DENIED" in completed.stdout
    assert not outside.exists()
    inside = tmp_path / "inside.txt"
    (tmp_path / "material.txt").write_text("SYNTH-MATERIAL", encoding="utf-8")
    completed = subprocess.run(
        [
            str(SANDBOX_EXEC),
            "-p",
            sandbox_profile(tmp_path),
            "sh",
            "-c",
            f"cat {tmp_path / 'material.txt'} > {inside} && echo READ-WRITE-OK",
        ],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert "READ-WRITE-OK" in completed.stdout
    assert inside.read_text().strip() == "SYNTH-MATERIAL"


@pytest.mark.integration
@pytest.mark.skipif(not REAL_LATEXML, reason="需要本机 latexmlc 实测最小隔离")
def test_real_latexml_under_sandbox_converts_table(tmp_path):
    runner = SandboxedConverterRunner()
    availability = runner.availability("latexmlc")
    assert availability.available
    assert availability.version and "LaTeXML" in availability.version
    workdir = tmp_path / "conv"
    workdir.mkdir()
    (workdir / "input.tex").write_text(
        "\\documentclass{article}\n"
        "\\newcommand{\\obj}[1]{#1}\n"
        "\\begin{document}\n"
        "\\begin{tabular}{lc}\\hline Obj & RV \\\\\\hline \\obj{FIC-A} & 525 \\\\\\hline\\end{tabular}\n"
        "\\end{document}\n",
        encoding="utf-8",
    )
    latexmlc = shutil.which("latexmlc")
    outcome = runner.run(
        "latexmlc",
        [
            latexmlc,
            "--format=html5",
            "--nographicimages",
            "--nopictureimages",
            "--nosvg",
            "--destination=latexml.html",
            "--timeout=60",
            "input.tex",
        ],
        workdir=workdir,
        timeout=120,
    )
    assert not outcome.timed_out
    assert outcome.returncode == 0, outcome.stderr.decode("utf-8", "replace")[:400]
    stderr = outcome.stderr.decode("utf-8", "replace")
    assert "Status:conversion:0" in stderr
    html = (workdir / "latexml.html").read_text(encoding="utf-8")
    assert "ltx_ERROR" not in html
    assert "FIC-A" in html


@pytest.mark.integration
@pytest.mark.skipif(not REAL_LATEXML, reason="需要本机 latexmlc 实测最小隔离")
def test_real_latexml_missing_macro_reports_conversion_error(tmp_path):
    """Missing macros can yield exit code zero with nonzero diagnostics/error nodes,
    requiring joint checks.
    """
    runner = SandboxedConverterRunner()
    workdir = tmp_path / "conv"
    workdir.mkdir()
    (workdir / "input.tex").write_text(
        "\\documentclass{article}\n"
        "\\begin{document}\n"
        "\\begin{tabular}{lc}\\hline Obj & RV \\\\\\hline \\obj{FIC-A} & 525 \\\\\\hline\\end{tabular}\n"
        "\\end{document}\n",
        encoding="utf-8",
    )
    latexmlc = shutil.which("latexmlc")
    outcome = runner.run(
        "latexmlc",
        [
            latexmlc,
            "--format=html5",
            "--nographicimages",
            "--nopictureimages",
            "--nosvg",
            "--destination=latexml.html",
            "--timeout=60",
            "input.tex",
        ],
        workdir=workdir,
        timeout=120,
    )
    assert (
        outcome.returncode == 0
    )  # Native probing established that exit code zero alone does not imply success.
    stderr = outcome.stderr.decode("utf-8", "replace")
    assert "Status:conversion:2" in stderr
    html = (workdir / "latexml.html").read_text(encoding="utf-8")
    assert "ltx_ERROR" in html


def test_missing_sandbox_reports_unavailable_and_refuses(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "hvs_catalog.materials.sandbox._sandbox_ready",
        lambda: "当前平台没有经过实测的隔离方案",
    )
    runner = SandboxedConverterRunner()
    availability = runner.availability("latexmlc")
    assert not availability.available
    assert "不裸跑" in (availability.reason or "") or "隔离" in (
        availability.reason or ""
    )
    outcome = runner.run(
        "latexmlc", ["/nonexistent/latexmlc", "--help"], workdir=tmp_path
    )
    assert outcome.returncode is None
    assert "拒绝运行" in outcome.error


@pytest.mark.integration
def test_timeout_kills_process(tmp_path):
    import time

    runner = SandboxedConverterRunner()
    if not REAL_SANDBOX:
        pytest.skip("需要本机 sandbox-exec")
    start = time.monotonic()
    outcome = runner.run(
        "latexmlc",
        ["/bin/sleep", "30"],
        workdir=tmp_path,
        timeout=2,
    )
    assert outcome.timed_out
    assert time.monotonic() - start < 20


@pytest.mark.integration
@pytest.mark.skipif(not REAL_SANDBOX, reason="需要本机 sandbox-exec")
def test_output_limit_fails_generation_and_kills_group(tmp_path):
    """Excess output during generation fails explicitly and terminates the process group
    before filling disk.
    """
    runner = SandboxedConverterRunner()
    workdir = tmp_path / "conv"
    workdir.mkdir()
    outcome = runner.run(
        "pandoc",
        [
            "/bin/sh",
            "-c",
            "yes 0123456789abcdef | head -c 9000000; echo DONE-MARKER",
        ],
        workdir=workdir,
        timeout=60,
    )
    assert outcome.exceeded_output
    assert "上限" in outcome.error
    assert len(outcome.stdout) <= 8 * 1024 * 1024
    # Termination after exceeding the limit prevents completion of DONE-MARKER; head was killed.
    assert b"DONE-MARKER" not in outcome.stdout


@pytest.mark.integration
@pytest.mark.skipif(not REAL_SANDBOX, reason="需要本机 sandbox-exec")
def test_timeout_kills_process_group_including_children(tmp_path):
    """Timeout terminates the whole process group, preventing synthetic descendants from
    writing markers afterward.
    """
    import time

    runner = SandboxedConverterRunner()
    workdir = tmp_path / "conv"
    workdir.mkdir()
    marker = workdir / "child-completed.txt"
    start = time.monotonic()
    outcome = runner.run(
        "pandoc",
        [
            "/bin/sh",
            "-c",
            f"sleep 30; touch {marker}",
        ],
        workdir=workdir,
        timeout=2,
    )
    assert outcome.timed_out
    assert "进程组" in outcome.error
    assert time.monotonic() - start < 20
    time.sleep(1.5)
    assert not marker.exists(), "超时后受控后代不应继续运行并写出标记"

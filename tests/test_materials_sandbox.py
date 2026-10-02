"""外部转换隔离的合成与实测测试（macOS sandbox-exec 实测，缺失时跳过）。

本机（macOS 且存在 /usr/bin/sandbox-exec 与 latexmlc）实测最小隔离：
禁网络、只写转换目录、包内 .ltxml/.pm 拒读；其他环境只验证"报告不可
用、不裸跑"的行为，不冒称已验证平台支持。
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
    # 读取采用明确允许范围：全盘拒绝 file-read-data，仅允许转换目录与
    # 可信系统依赖树；目录元数据与根目录本身的读取是进程引导必要条件。
    assert "(deny file-read-data)" in profile
    assert '(allow file-read-data (literal "/"))' in profile
    assert "(allow file-read-metadata)" in profile
    assert f'(allow file-read-data (subpath "{tmp_path.resolve()}"))' in profile
    assert "(ltxml|pm)" in profile  # 包内可执行绑定拒读


@pytest.mark.integration
@pytest.mark.skipif(not REAL_SANDBOX, reason="需要本机 macOS sandbox-exec")
def test_sandbox_denies_network_local_endpoint_and_outside_access(tmp_path):
    """网络拒绝用新建本地合成端点验证，不访问任何外部服务。"""
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
            pass  # 未隔离环境可连，确认端点本身可用
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

    # 包外私有文件读取被拒（本机新建合成内容，不涉及真实论文）。
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

    # 目录外写入被拒；目录内读写正常。
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
    """缺宏时退出码 0 但诊断非零、输出含错误节点（联合判定依据）。"""
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
    assert outcome.returncode == 0  # 本机探查已证明退出码 0 不是成功依据
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
    """生成期间超限明确失败并终止进程组，不先写满磁盘再截断。"""
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
    # 超限后进程组被终止：DONE-MARKER 不应写完（head 已被杀）。
    assert b"DONE-MARKER" not in outcome.stdout


@pytest.mark.integration
@pytest.mark.skipif(not REAL_SANDBOX, reason="需要本机 sandbox-exec")
def test_timeout_kills_process_group_including_children(tmp_path):
    """超时终止整个进程组：合成后代进程不能在超时后继续写出标记。"""
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

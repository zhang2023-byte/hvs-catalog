"""独立进程验证保存中断窗口与恢复（场景八）。

中断用故障窗口钩子在准确时点 ``os._exit`` 结束子进程，等价于进程死亡：
操作日志、已发布文件与版本入口等已提交事实保留，未完成部分丢弃。父
进程随后核对实际保存内容与版本入口，不只检查返回码。
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
from evaluation_helpers import (
    build_approval_payload,
    build_fresh_draft,
    write_synthetic_pdf,
)

from hvs_catalog.evaluation import (
    GoldTarget,
    approved_save,
    prepare_save,
    query_operation,
)

HELPERS = Path(__file__).parent / "evaluation_helpers.py"


def run_scenario(name: str, *args: object, timeout: float = 60.0):
    result = subprocess.run(
        [sys.executable, str(HELPERS), name, *map(str, args)],
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    payload = None
    lines = [line for line in result.stdout.splitlines() if line.strip()]
    if lines:
        try:
            payload = json.loads(lines[-1])
        except json.JSONDecodeError:
            payload = None
    return result.returncode, payload


def _setup_flow(tmp_path):
    material = tmp_path / "materials"
    digest = write_synthetic_pdf(material / "paper.pdf")
    draft = build_fresh_draft(pdf_sha256=digest)
    draft_path = tmp_path / "draft.json"
    draft_path.write_text(json.dumps(draft, ensure_ascii=False), encoding="utf-8")
    target = GoldTarget(tmp_path / "target")
    target.initialize()
    preparation = prepare_save(
        draft_path=draft_path,
        material_root=material,
        work_dir=tmp_path / "work",
        target=target,
    )
    approval_path = tmp_path / "approval.json"
    approval_path.write_text(
        json.dumps(build_approval_payload(preparation), ensure_ascii=False),
        encoding="utf-8",
    )
    return draft_path, material, target, preparation, approval_path


def _published_files(target: GoldTarget) -> list[Path]:
    annotations = target.root / "annotations"
    if not annotations.exists():
        return []
    return sorted(p for p in annotations.rglob("*") if p.is_file())


@pytest.mark.parametrize(
    "window,expected_query",
    [
        ("after_log", "pending"),
        ("after_stage", "pending"),
        ("after_file:answer.json", "recoverable"),
        ("after_files", "recoverable"),
        ("after_entry", "saved"),
    ],
)
def test_crash_window_then_recover(tmp_path, window, expected_query):
    """保存链路各中断窗口退出后：先核对实际结果，再恢复到一致状态。"""
    draft_path, material, target, preparation, approval_path = _setup_flow(tmp_path)
    code, payload = run_scenario(
        "save-exit",
        draft_path,
        material,
        target.root,
        preparation.directory,
        approval_path,
        "op-crash",
        window,
    )
    assert code == 9, payload  # 子进程在故障窗口硬退出

    status = query_operation(operation_id="op-crash", target=target)
    assert status.state == expected_query, (window, status.detail)

    # 中断后入口未发布时不算正式答案
    entry_before = target.read_entry("gold-synth-001")
    if expected_query in ("pending", "recoverable"):
        assert entry_before is None

    # 用同一操作标识与原批准重试：核对原批准、文件和目标后继续或确认
    receipt = approved_save(
        draft_path=draft_path,
        material_root=material,
        target=target,
        preparation_dir=preparation.directory,
        approval=approval_path,
        operation_id="op-crash",
    )
    assert receipt.version == 1
    entry = target.read_entry("gold-synth-001")
    assert entry is not None and entry.version == 1
    assert entry.operation_id == "op-crash"
    # 三个正式文件齐备且哈希一致（读取者只接受完整一致的入口）
    assert len(entry.files) == 3
    target.read_current_answer("gold-synth-001")
    # 只有一个版本目录，成功保存仅一份
    versions = sorted(
        p.name for p in (target.root / "annotations" / "gold-synth-001").iterdir()
    )
    assert versions == ["v1"]


def test_entry_published_but_receipt_lost_no_second_version(tmp_path):
    """入口已更新但回执未返回：重试据入口确认已保存，不发布第二份。"""
    draft_path, material, target, preparation, approval_path = _setup_flow(tmp_path)
    code, _ = run_scenario(
        "save-exit",
        draft_path,
        material,
        target.root,
        preparation.directory,
        approval_path,
        "op-receipt-lost",
        "after_entry",
    )
    assert code == 9
    again = approved_save(
        draft_path=draft_path,
        material_root=material,
        target=target,
        preparation_dir=preparation.directory,
        approval=approval_path,
        operation_id="op-receipt-lost",
    )
    assert again.replayed and again.recovered
    assert again.version == 1
    versions = sorted(
        p.name for p in (target.root / "annotations" / "gold-synth-001").iterdir()
    )
    assert versions == ["v1"]
    status = query_operation(operation_id="op-receipt-lost", target=target)
    assert status.state == "saved"


def test_recovered_save_in_subprocess(tmp_path):
    """中断后在独立子进程内完成恢复保存（另一个进程接手）。"""
    draft_path, material, target, preparation, approval_path = _setup_flow(tmp_path)
    code, _ = run_scenario(
        "save-exit",
        draft_path,
        material,
        target.root,
        preparation.directory,
        approval_path,
        "op-cross",
        "after_file:answer.json",
    )
    assert code == 9
    code, payload = run_scenario(
        "save",
        draft_path,
        material,
        target.root,
        preparation.directory,
        approval_path,
        "op-cross",
    )
    assert code == 0, payload
    assert payload["version"] == 1
    assert payload["recovered"] is True or payload["replayed"] is True
    target.read_current_answer("gold-synth-001")


def test_recovery_keeps_old_input_untouched(tmp_path):
    """恢复保存过程中旧输入与材料保持只读。"""
    draft_path, material, target, preparation, approval_path = _setup_flow(tmp_path)
    legacy = tmp_path / "legacy.json"
    legacy_payload = {"placeholder": True}
    legacy.write_text(json.dumps(legacy_payload), encoding="utf-8")
    material_bytes = (material / "paper.pdf").read_bytes()
    code, _ = run_scenario(
        "save-exit",
        draft_path,
        material,
        target.root,
        preparation.directory,
        approval_path,
        "op-ro",
        "after_files",
    )
    assert code == 9
    approved_save(
        draft_path=draft_path,
        material_root=material,
        target=target,
        preparation_dir=preparation.directory,
        approval=approval_path,
        operation_id="op-ro",
    )
    assert legacy.read_bytes() == json.dumps(legacy_payload).encode()
    assert (material / "paper.pdf").read_bytes() == material_bytes

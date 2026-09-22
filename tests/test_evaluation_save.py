"""受控保存：完整流程、批准绑定、目标前提、幂等重试与资料保护。"""

from __future__ import annotations

import json

import pytest
from evaluation_helpers import (
    add_synthetic_metadata,
    build_approval_payload,
    build_fresh_draft,
    build_legacy_v4_doc,
    write_synthetic_pdf,
)

from hvs_catalog.evaluation import (
    GoldApprovalError,
    GoldCheckError,
    GoldOperationConflictError,
    GoldTarget,
    GoldTargetError,
    SaveApproval,
    approved_save,
    check_gold_annotation,
    load_preparation,
    manifest_sha256,
    migrate_legacy_annotation,
    parse_gold_annotation,
    prepare_save,
    query_operation,
)
from hvs_catalog.workspace.errors import WorkspaceBusyError


def _material(tmp_path, pages=None, name="paper.pdf"):
    material = tmp_path / "materials"
    digest = write_synthetic_pdf(material / name, pages=pages)
    return material, digest


def _write_draft(tmp_path, draft, name="draft.json"):
    path = tmp_path / name
    path.write_text(json.dumps(draft, ensure_ascii=False), encoding="utf-8")
    return path


def _full_fresh_flow(tmp_path, *, value="612.0", operation_id="op-1", document_id=None):
    material, digest = _material(tmp_path)
    draft = build_fresh_draft(pdf_sha256=digest, value=value)
    if document_id:
        draft["document_id"] = document_id
    draft_path = _write_draft(tmp_path, draft)
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
    return draft_path, material, target, preparation, approval_path, operation_id


def test_scenario_6_fresh_draft_full_flow_saves(tmp_path):
    """全新草稿提供完整合成 PDF 及标注证据：无需旧答案即可检查、模拟审定并保存。"""
    draft_path, material, target, preparation, approval_path, op = _full_fresh_flow(
        tmp_path
    )
    report = check_gold_annotation(
        parse_gold_annotation(draft_path), material_root=material
    )
    assert report.blocking_count == 0
    receipt = approved_save(
        draft_path=draft_path,
        material_root=material,
        target=target,
        preparation_dir=preparation.directory,
        approval=approval_path,
        operation_id=op,
    )
    assert receipt.version == 1
    assert not receipt.replayed
    entry, annotation = target.read_current_answer("gold-synth-001")
    assert entry.version == 1
    assert annotation.kind == "answer"
    assert annotation.contributions[0].quantities[0].values[0].value == "612.0"
    assert annotation.pending == []  # 正式答案不携带待办
    # 入口引用的三个文件全部存在且一致
    assert len(entry.files) == 3
    target.read_entry("gold-synth-001")


def test_migrated_draft_saves_with_accepted_pending(tmp_path):
    """既有答案迁移：检查后按批准接受科学待核查项，完成模拟获准保存。"""
    legacy = tmp_path / "legacy.json"
    legacy.write_text(
        json.dumps(build_legacy_v4_doc(), ensure_ascii=False), encoding="utf-8"
    )
    material, _ = _material(tmp_path)
    result = migrate_legacy_annotation(
        legacy_path=legacy,
        draft_dir=tmp_path / "draft",
        known_pdfs=[material / "paper.pdf"],
        material_root=material,
    )
    assert result.check.blocking_count == 3  # 本版作者、论文版本、材料对应尚待补
    completed = add_synthetic_metadata(
        result.draft.model_dump(mode="json", by_alias=True)
    )
    result.draft_path.write_text(json.dumps(completed), encoding="utf-8")
    assert (
        check_gold_annotation(
            parse_gold_annotation(completed), material_root=material
        ).blocking_count
        == 0
    )
    assert result.check.pending_ids()  # 但仍有科学待核查项
    target = GoldTarget(tmp_path / "target")
    target.initialize()
    preparation = prepare_save(
        draft_path=result.draft_path,
        material_root=material,
        work_dir=tmp_path / "work",
        target=target,
    )
    assert preparation.manifest.science_pending  # 批准须显式接受这些类别
    approval_path = tmp_path / "approval.json"
    approval_path.write_text(
        json.dumps(build_approval_payload(preparation), ensure_ascii=False),
        encoding="utf-8",
    )
    receipt = approved_save(
        draft_path=result.draft_path,
        material_root=material,
        target=target,
        preparation_dir=preparation.directory,
        approval=approval_path,
        operation_id="op-mig",
    )
    assert receipt.version == 1
    _, annotation = target.read_current_answer(result.draft.document_id)
    assert annotation.provenance.origin == "migration"
    assert annotation.provenance.legacy.sha256 == result.report.legacy_sha256


def test_scenario_7_approval_does_not_survive_draft_change(tmp_path):
    """批准 612.0 后将草稿改成 613.0：原批准不再适用，拒绝保存。"""
    draft_path, material, target, preparation, approval_path, op = _full_fresh_flow(
        tmp_path
    )
    draft = json.loads(draft_path.read_text(encoding="utf-8"))
    draft["contributions"][0]["quantities"][0]["values"][0]["value"] = "613.0"
    draft_path.write_text(json.dumps(draft, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(GoldApprovalError, match="批准后发生了变化"):
        approved_save(
            draft_path=draft_path,
            material_root=material,
            target=target,
            preparation_dir=preparation.directory,
            approval=approval_path,
            operation_id=op,
        )
    assert target.read_entry("gold-synth-001") is None  # 未保存任何版本


def test_approval_binding_failures(tmp_path):
    draft_path, material, target, preparation, _, op = _full_fresh_flow(tmp_path)
    payload = build_approval_payload(preparation)
    # 绑定的清单哈希不对
    bad_hash = dict(payload, manifest_sha256="f" * 64)
    with pytest.raises(GoldApprovalError, match="清单"):
        approved_save(
            draft_path=draft_path,
            material_root=material,
            target=target,
            preparation_dir=preparation.directory,
            approval=SaveApproval.model_validate(bad_hash),
            operation_id=op,
        )
    # 没有审定依据
    no_basis = dict(payload, basis="")
    with pytest.raises(GoldApprovalError, match="审定依据"):
        approved_save(
            draft_path=draft_path,
            material_root=material,
            target=target,
            preparation_dir=preparation.directory,
            approval=SaveApproval.model_validate(no_basis),
            operation_id=op,
        )
    # 未接受的待核查项（对迁移类内容才会出现；这里人为制造不一致）
    if payload["accepted_pending"]:
        wrong = dict(payload, accepted_pending=[])
        with pytest.raises(GoldApprovalError, match="待核查项"):
            approved_save(
                draft_path=draft_path,
                material_root=material,
                target=target,
                preparation_dir=preparation.directory,
                approval=SaveApproval.model_validate(wrong),
                operation_id=op,
            )


def test_prepare_refuses_blocking_problems(tmp_path):
    material, _ = _material(tmp_path)
    draft = build_fresh_draft(pdf_sha256=None)  # 哈希待补
    draft_path = _write_draft(tmp_path, draft)
    target = GoldTarget(tmp_path / "target")
    target.initialize()
    with pytest.raises(GoldCheckError, match="阻断"):
        prepare_save(
            draft_path=draft_path,
            material_root=material,
            work_dir=tmp_path / "work",
            target=target,
        )
    assert target.read_entry("gold-synth-001") is None


def test_scenario_9_refuses_save_when_pdf_replaced(tmp_path):
    """PDF 被替换：报告文件错误，拒绝按原批准保存。"""
    draft_path, material, target, preparation, approval_path, op = _full_fresh_flow(
        tmp_path
    )
    write_synthetic_pdf(material / "paper.pdf", pages=[["replaced"], ["x"], ["y"]])
    with pytest.raises(GoldCheckError, match="哈希"):
        approved_save(
            draft_path=draft_path,
            material_root=material,
            target=target,
            preparation_dir=preparation.directory,
            approval=approval_path,
            operation_id=op,
        )
    assert target.read_entry("gold-synth-001") is None


def test_scenario_9_page_out_of_range_blocks_prepare(tmp_path):
    material, digest = _material(tmp_path)  # 3 页
    draft = build_fresh_draft(pdf_sha256=digest, page=9)
    draft_path = _write_draft(tmp_path, draft)
    target = GoldTarget(tmp_path / "target")
    target.initialize()
    with pytest.raises(GoldCheckError):
        prepare_save(
            draft_path=draft_path,
            material_root=material,
            work_dir=tmp_path / "work",
            target=target,
        )


def test_new_save_requires_absent_target_version(tmp_path):
    draft_path, material, target, preparation, approval_path, op = _full_fresh_flow(
        tmp_path
    )
    approved_save(
        draft_path=draft_path,
        material_root=material,
        target=target,
        preparation_dir=preparation.directory,
        approval=approval_path,
        operation_id=op,
    )
    # 同一文档再次以“新建”准备：目标已有版本，拒绝
    with pytest.raises(GoldTargetError, match="已有"):
        prepare_save(
            draft_path=draft_path,
            material_root=material,
            work_dir=tmp_path / "work2",
            target=target,
        )


def test_revision_keeps_old_version_and_checks_target_change(tmp_path):
    draft_path, material, target, preparation, approval_path, op = _full_fresh_flow(
        tmp_path
    )
    approved_save(
        draft_path=draft_path,
        material_root=material,
        target=target,
        preparation_dir=preparation.directory,
        approval=approval_path,
        operation_id=op,
    )
    v1_entry = target.read_entry("gold-synth-001")

    # 修订：草稿改为 613.0，按预期旧版本 v1 准备
    draft = json.loads(draft_path.read_text(encoding="utf-8"))
    draft["contributions"][0]["quantities"][0]["values"][0]["value"] = "613.0"
    draft_path.write_text(json.dumps(draft, ensure_ascii=False), encoding="utf-8")
    preparation2 = prepare_save(
        draft_path=draft_path,
        material_root=material,
        work_dir=tmp_path / "work",
        target=target,
        expected_old_version=1,
    )
    approval2 = tmp_path / "approval2.json"
    approval2.write_text(
        json.dumps(build_approval_payload(preparation2), ensure_ascii=False),
        encoding="utf-8",
    )
    # 目标版本变化：另一操作先完成修订后，按 v1 前提获准的操作被拒绝
    draft3 = json.loads(draft_path.read_text(encoding="utf-8"))
    draft3["document_id"] = "gold-synth-001"
    draft3["contributions"][0]["quantities"][0]["values"][0]["value"] = "614.0"
    draft3_path = _write_draft(tmp_path, draft3, name="draft3.json")
    preparation3 = prepare_save(
        draft_path=draft3_path,
        material_root=material,
        work_dir=tmp_path / "work3",
        target=target,
        expected_old_version=1,
    )
    approval3 = tmp_path / "approval3.json"
    approval3.write_text(
        json.dumps(build_approval_payload(preparation3), ensure_ascii=False),
        encoding="utf-8",
    )
    receipt2 = approved_save(
        draft_path=draft_path,
        material_root=material,
        target=target,
        preparation_dir=preparation2.directory,
        approval=approval2,
        operation_id="op-2",
    )
    assert receipt2.version == 2
    with pytest.raises(GoldTargetError, match="预期旧版本"):
        approved_save(
            draft_path=draft3_path,
            material_root=material,
            target=target,
            preparation_dir=preparation3.directory,
            approval=approval3,
            operation_id="op-3",
        )
    assert target.read_entry("gold-synth-001").version == 2
    entry2 = target.read_entry("gold-synth-001")
    assert entry2.previous.version == 1
    assert entry2.previous.manifest_sha256 == v1_entry.manifest_sha256
    # 旧版本文件保留且未被改动
    for item in v1_entry.files:
        from hvs_catalog.evaluation.store import resolve_within

        path = resolve_within(target.root, item.path)
        assert path.is_file()
        import hashlib

        assert hashlib.sha256(path.read_bytes()).hexdigest() == item.sha256


def test_same_operation_replays_and_conflicting_content_rejected(tmp_path):
    draft_path, material, target, preparation, approval_path, op = _full_fresh_flow(
        tmp_path
    )
    first = approved_save(
        draft_path=draft_path,
        material_root=material,
        target=target,
        preparation_dir=preparation.directory,
        approval=approval_path,
        operation_id=op,
    )
    again = approved_save(
        draft_path=draft_path,
        material_root=material,
        target=target,
        preparation_dir=preparation.directory,
        approval=approval_path,
        operation_id=op,
    )
    assert again.replayed and again.recovered
    assert again.version == first.version
    assert again.manifest_sha256 == first.manifest_sha256
    assert target.read_entry("gold-synth-001").version == 1  # 只有一份

    # 同操作标识、不同内容：拒绝
    draft = json.loads(draft_path.read_text(encoding="utf-8"))
    draft["contributions"][0]["quantities"][0]["values"][0]["value"] = "613.0"
    draft_path.write_text(json.dumps(draft, ensure_ascii=False), encoding="utf-8")
    preparation2 = prepare_save(
        draft_path=draft_path,
        material_root=material,
        work_dir=tmp_path / "work",
        target=target,
        expected_old_version=1,
    )
    approval2 = tmp_path / "approval2.json"
    approval2.write_text(
        json.dumps(build_approval_payload(preparation2), ensure_ascii=False),
        encoding="utf-8",
    )
    with pytest.raises(GoldOperationConflictError, match="不同内容"):
        approved_save(
            draft_path=draft_path,
            material_root=material,
            target=target,
            preparation_dir=preparation2.directory,
            approval=approval2,
            operation_id=op,
        )


def test_concurrent_save_reports_busy(tmp_path, monkeypatch):
    import subprocess
    import sys
    from pathlib import Path

    helpers = Path(__file__).parent / "evaluation_helpers.py"
    draft_path, material, target, preparation, approval_path, op = _full_fresh_flow(
        tmp_path
    )
    signal = tmp_path / "held.signal"
    release = tmp_path / "release.signal"
    holder = subprocess.Popen(
        [
            sys.executable,
            str(helpers),
            "hold-target-lock",
            str(target.root),
            str(signal),
            str(release),
        ],
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        deadline_wait(signal, holder)
        with pytest.raises(WorkspaceBusyError):
            approved_save(
                draft_path=draft_path,
                material_root=material,
                target=target,
                preparation_dir=preparation.directory,
                approval=approval_path,
                operation_id=op,
            )
    finally:
        release.write_text("go", encoding="utf-8")
        holder.communicate(timeout=30)
    # 持锁者退出后可以保存
    receipt = approved_save(
        draft_path=draft_path,
        material_root=material,
        target=target,
        preparation_dir=preparation.directory,
        approval=approval_path,
        operation_id=op,
    )
    assert receipt.version == 1


def deadline_wait(path, proc, timeout=30.0):
    import time

    deadline = time.monotonic() + timeout
    while not path.exists():
        assert proc.poll() is None, f"子进程提前退出（{proc.returncode}）"
        assert time.monotonic() < deadline, f"等待信号文件超时：{path}"
        time.sleep(0.02)


def test_target_initialization_protects_existing_data(tmp_path):
    target_root = tmp_path / "target"
    target_root.mkdir()
    (target_root / "existing-notes.txt").write_text("重要资料", encoding="utf-8")
    with pytest.raises(GoldTargetError, match="非托管"):
        GoldTarget(target_root).initialize()
    assert (target_root / "existing-notes.txt").read_text() == "重要资料"


def test_target_rejects_unsafe_document_id(tmp_path):
    target = GoldTarget(tmp_path / "target")
    target.initialize()
    with pytest.raises(GoldTargetError, match="document_id"):
        target.read_entry("../escape")


def test_query_unknown_operation(tmp_path):
    target = GoldTarget(tmp_path / "target")
    target.initialize()
    status = query_operation(operation_id="op-x", target=target)
    assert status.state == "unknown"


def test_tampered_published_file_detected(tmp_path):
    draft_path, material, target, preparation, approval_path, op = _full_fresh_flow(
        tmp_path
    )
    approved_save(
        draft_path=draft_path,
        material_root=material,
        target=target,
        preparation_dir=preparation.directory,
        approval=approval_path,
        operation_id=op,
    )
    entry = target.read_entry("gold-synth-001")
    answer_path = target.root / entry.files[0].path
    answer_path.write_text(
        answer_path.read_text(encoding="utf-8").replace("612.0", "999.0"),
        encoding="utf-8",
    )
    from hvs_catalog.evaluation import GoldIntegrityError

    with pytest.raises(GoldIntegrityError):
        target.read_entry("gold-synth-001")


def test_preparation_content_bound_and_reloadable(tmp_path):
    draft_path, material, target, preparation, _, _ = _full_fresh_flow(tmp_path)
    loaded = load_preparation(preparation.directory)
    assert loaded.manifest.model_dump() == preparation.manifest.model_dump()
    assert manifest_sha256(loaded.manifest) == manifest_sha256(preparation.manifest)
    # 相同草稿重新准备得到同一清单
    again = prepare_save(
        draft_path=draft_path,
        material_root=material,
        work_dir=tmp_path / "work",
        target=target,
    )
    assert again.manifest.prep_id == preparation.manifest.prep_id

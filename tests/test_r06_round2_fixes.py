"""第二轮独立复验的回归：真实合成 PDF、保存前提及跨中断组合路径。"""

import copy
import hashlib
import json
import subprocess
import sys

import pytest
from evaluation_helpers import (
    build_approval_payload,
    build_fresh_draft,
    build_legacy_v4_doc,
)
from reportlab.pdfgen.canvas import Canvas

from hvs_catalog.evaluation import (
    GoldApprovalError,
    GoldCheckError,
    GoldFormatError,
    GoldIntegrityError,
    GoldTarget,
    SaveApproval,
    approved_save,
    check_gold_annotation,
    compare_legacy_with_current,
    migrate_legacy_annotation,
    parse_gold_annotation,
    prepare_save,
    query_operation,
)
from hvs_catalog.evaluation.pdfcheck import analyze_pdf, quote_result


def write(path, data):
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


@pytest.fixture
def flow(tmp_path):
    pdf = tmp_path / "paper.pdf"
    canvas = Canvas(str(pdf), pageCompression=1)
    for _ in range(3):
        canvas.drawString(30, 700, "Synthetic independent regression material.")
        canvas.showPage()
    canvas.save()
    (tmp_path / "paper.tex").write_bytes(b"Synthetic source material.")
    data = build_fresh_draft(
        pdf_sha256=hashlib.sha256(pdf.read_bytes()).hexdigest(), include_quote=False
    )
    path = write(tmp_path / "draft.json", data)
    target = GoldTarget(tmp_path / "target")
    prep = prepare_save(
        draft_path=path,
        material_root=tmp_path,
        target=target,
        work_dir=tmp_path / "work",
    )
    return data, {
        "draft_path": path,
        "material_root": tmp_path,
        "target": target,
        "preparation_dir": prep.directory,
        "approval": SaveApproval.model_validate(build_approval_payload(prep)),
        "operation_id": "op-1",
    }


def snapshot(root):
    return {
        str(p.relative_to(root)): (p.read_bytes(), p.stat().st_mtime_ns)
        for p in root.rglob("*")
        if p.is_file() and not p.is_symlink()
    }


@pytest.mark.parametrize(
    "relative",
    [
        "staging/op-1",
        "staging/op-1/answer.json",
        "staging/op-1/approval.json",
        "staging/op-1/provenance.json",
    ],
)
def test_f10_staging_links_rejected_before_external_write(flow, tmp_path, relative):
    _, args = flow
    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / "answer.json"
    sentinel.write_text("unrelated data")
    before = snapshot(outside)
    link = args["target"].root / relative
    link.parent.mkdir(parents=True, exist_ok=True)
    link.symlink_to(outside if relative == "staging/op-1" else sentinel)
    with pytest.raises(GoldIntegrityError):
        approved_save(**args)
    assert snapshot(outside) == before
    assert args["target"].read_entry("gold-synth-001") is None


@pytest.mark.parametrize("replay_first", [False, True])
def test_f11_after_entry_crash_then_revisions_keep_original_receipt(
    flow, tmp_path, replay_first
):
    data, args = flow
    serialized = {k: str(v) for k, v in args.items() if k not in {"approval", "target"}}
    serialized.update(
        target=str(args["target"].root),
        approval=args["approval"].model_dump(mode="json"),
    )
    script = """import json,os,sys
from hvs_catalog.evaluation import approved_save,GoldTarget,SaveApproval
a=json.loads(sys.argv[1]);a['target']=GoldTarget(a['target']);a['approval']=SaveApproval.model_validate(a['approval'])
approved_save(**a,fault_hooks={'after_entry':lambda:os._exit(23)})
"""
    result = subprocess.run(
        [sys.executable, "-c", script, json.dumps(serialized)],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 23, result.stderr
    target = args["target"]
    before_query = snapshot(target.root)
    original = query_operation(operation_id="op-1", target=target).receipt
    assert original.version == 1
    assert snapshot(target.root) == before_query  # query is read-only
    if replay_first:
        assert approved_save(**args).created_at == original.created_at
    for version in (2, 3):
        data["contributions"][0]["quantities"][0]["values"][0]["value"] = str(
            610 + version
        )
        path = write(tmp_path / f"draft-{version}.json", data)
        prep = prepare_save(
            draft_path=path,
            material_root=tmp_path,
            target=target,
            expected_old_version=version - 1,
            work_dir=tmp_path / "work",
        )
        approved_save(
            **dict(
                args,
                draft_path=path,
                preparation_dir=prep.directory,
                approval=SaveApproval.model_validate(build_approval_payload(prep)),
                operation_id=f"op-{version}",
            )
        )
    status = query_operation(operation_id="op-1", target=target)
    assert status.state == "saved"
    assert status.receipt.created_at == original.created_at
    assert status.receipt.files == original.files
    assert approved_save(**args).created_at == original.created_at
    assert target.read_entry("gold-synth-001").version == 3
    # Historical damage must still reject replay and preserve the scene.
    (target.root / original.files[0].path).write_text("damaged")
    before = snapshot(target.root)
    assert query_operation(operation_id="op-1", target=target).state == "inconsistent"
    with pytest.raises(GoldIntegrityError):
        approved_save(**args)
    assert snapshot(target.root) == before


@pytest.mark.parametrize("index", [None, 0, 999])
@pytest.mark.parametrize("insert_first", [False, True])
def test_f12_every_extra_contribution_is_reported(tmp_path, index, insert_first):
    old = write(tmp_path / "old.json", build_legacy_v4_doc())
    migrated = migrate_legacy_annotation(
        legacy_path=old, draft_dir=tmp_path / "migration"
    )
    data = migrated.draft.model_dump(mode="json", by_alias=True)
    extra = copy.deepcopy(data["contributions"][0])
    extra.update(contribution_id="extra", legacy_index=index)
    extra["identifiers"][0]["value"] = "EXTRA-OBJECT"
    extra["quantities"][0]["values"][0]["value"] = "999.99"
    data["contributions"].insert(0 if insert_first else 1, extra)
    new = write(tmp_path / "new.json", data)
    before = (old.read_bytes(), new.read_bytes())
    report = compare_legacy_with_current(legacy_path=old, current_path=new)
    assert "extra" in report.unmatched_new or any(
        "extra" in item.subject and item.status != "identical" for item in report.items
    )
    assert before == (old.read_bytes(), new.read_bytes())


def inject_evidence(data, location, evidence):
    contribution = data["contributions"][0]
    value = contribution["quantities"][0]["values"][0]
    if location == "exclusion":
        data["reviewed_exclusions"] = [
            {
                "exclusion_id": "x1",
                "reason": "Synthetic exclusion",
                "evidence": [evidence],
            }
        ]
    elif location == "identifier":
        contribution["identifiers"][0]["evidence"] = [evidence]
    else:
        alternative = copy.deepcopy(value)
        if location == "context":
            alternative["context_evidence"] = [evidence]
        elif location == "component":
            alternative["error_components"] = [
                {
                    "name": "statistical",
                    "error": "1.0",
                    "lower_error": None,
                    "upper_error": None,
                    "evidence": [evidence],
                }
            ]
        else:
            alternative.update(
                value=None,
                error=None,
                limit_kind="range",
                range_lower="600.0",
                range_upper="700.0",
                interval_kind="credible",
            )
            alternative["coverage"] = {
                "value": "0.68",
                "unit": "fraction",
                "evidence": [evidence],
            }
        second = copy.deepcopy(alternative)
        if location == "coverage":
            second["range_upper"] = "702.20"
        else:
            second["value"] = "702.20"
        contribution["quantities"] = []
        contribution["quantity_conflicts"] = [
            {
                "conflict_id": "k1",
                "quantity": "observed_phase_space.radial_velocity",
                "reason": "Synthetic contradiction",
                "evidence": [
                    value["evidence"][0],
                    dict(value["evidence"][0], location="Section S"),
                ],
                "alternatives": [alternative, second],
            }
        ]


@pytest.mark.parametrize(
    "location", ["exclusion", "identifier", "context", "component", "coverage"]
)
@pytest.mark.parametrize("defect", ["page", "unbound", "unknown", "valid"])
def test_f13_all_evidence_paths_block_invalid_material(
    flow, tmp_path, location, defect
):
    data, args = flow
    approved_save(**args)
    before = snapshot(args["target"].root)
    evidence = {
        "file_id": "pdf-1",
        "location": "Synthetic locator",
        "page": 2,
        "quote": "Synthetic independent regression material.",
    }
    if defect == "page":
        evidence["page"] = 999
    elif defect == "unbound":
        evidence["file_id"] = None
    elif defect == "unknown":
        evidence["file_id"] = "unknown"
    inject_evidence(data, location, evidence)
    if defect == "valid":
        report = check_gold_annotation(
            parse_gold_annotation(data), material_root=tmp_path
        )
        assert report.blocking_count == 0
        assert any(
            q.quote == evidence["quote"] and q.result == "matched"
            for q in report.quotes
        )
        path = write(tmp_path / "valid-revision.json", data)
        prep = prepare_save(
            draft_path=path,
            material_root=tmp_path,
            target=args["target"],
            expected_old_version=1,
            work_dir=tmp_path / "work",
        )
        receipt = approved_save(
            **dict(
                args,
                draft_path=path,
                preparation_dir=prep.directory,
                approval=SaveApproval.model_validate(build_approval_payload(prep)),
                operation_id="op-2",
            )
        )
        assert receipt.version == 2
        return
    if defect == "unknown":
        with pytest.raises(GoldFormatError, match="未登记"):
            parse_gold_annotation(data)
    else:
        report = check_gold_annotation(
            parse_gold_annotation(data), material_root=tmp_path
        )
        assert report.blocking_count > 0
        assert any(
            p.code == ("page_out_of_range" if defect == "page" else "evidence_unbound")
            for p in report.problems
        )
    path = write(tmp_path / "invalid.json", data)
    with pytest.raises((GoldCheckError, GoldFormatError)):
        prepare_save(
            draft_path=path,
            material_root=tmp_path,
            target=args["target"],
            expected_old_version=1,
            work_dir=tmp_path / "work",
        )
    assert snapshot(args["target"].root) == before


def test_f14_formal_files_retain_full_pending_and_bind_description(flow, tmp_path):
    data, args = flow
    pending = {
        "item_id": "q1",
        "category": "science_review",
        "subject": "contributions/c1/identifiers",
        "description": "Confirm which source in Table S identifies SYN-A.",
    }
    data["pending"] = [pending]
    write(args["draft_path"], data)
    prep = prepare_save(
        draft_path=args["draft_path"],
        material_root=tmp_path,
        target=args["target"],
        work_dir=tmp_path / "work",
    )
    args.update(
        preparation_dir=prep.directory,
        approval=SaveApproval.model_validate(build_approval_payload(prep)),
    )
    data["pending"][0]["description"] += " Additional unresolved detail."
    write(args["draft_path"], data)
    with pytest.raises(GoldApprovalError):
        approved_save(**args)
    pending["description"] = "Confirm which source in Table S identifies SYN-A."
    write(args["draft_path"], data)
    receipt = approved_save(**args)
    provenance = next(f for f in receipt.files if f.path.endswith("provenance.json"))
    payload = (args["target"].root / provenance.path).read_bytes()
    assert hashlib.sha256(payload).hexdigest() == provenance.sha256
    assert json.loads(payload)["check_summary"]["accepted_draft_pending"] == [pending]
    assert args["target"].read_current_answer("gold-synth-001")[1].pending == []


def test_f15_mixed_pdf_reports_unextractable_scope(tmp_path):
    pdf = tmp_path / "mixed.pdf"
    canvas = Canvas(str(pdf))
    canvas.drawString(30, 700, "Synthetic text")
    canvas.showPage()
    canvas.rect(10, 10, 100, 100)
    canvas.showPage()
    canvas.save()
    result = analyze_pdf(pdf.read_bytes())
    assert result.openable and result.page_count == 2
    assert (
        quote_result(result.page_texts, page=2, quote="quote")[0] == "not_extractable"
    )
    assert (
        quote_result(result.page_texts, page=None, quote="quote")[0]
        == "not_extractable"
    )
    assert quote_result(result.page_texts, page=1, quote="quote")[0] == "not_found"
    assert (
        quote_result(result.page_texts, page=None, quote="Synthetic text")[0]
        == "matched"
    )
    assert quote_result(result.page_texts, page=2, quote="")[0] == "skipped"

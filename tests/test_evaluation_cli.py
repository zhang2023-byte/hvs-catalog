"""Gold CLI 薄封装：六个操作的实际流程与退出码。"""

from __future__ import annotations

import json

from evaluation_helpers import (
    build_approval_payload,
    build_fresh_draft,
    build_legacy_v4_doc,
    write_synthetic_pdf,
)

from hvs_catalog.cli import main


def _write(tmp_path, name, payload):
    path = tmp_path / name
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


def test_gold_cli_migrate_check_compare(tmp_path, capsys):
    legacy = _write(tmp_path, "legacy.json", build_legacy_v4_doc())
    draft_dir = tmp_path / "draft"
    code = main(
        [
            "gold",
            "migrate",
            "--legacy",
            str(legacy),
            "--draft-dir",
            str(draft_dir),
            "--material-root",
            str(tmp_path),
        ]
    )
    assert code == 0
    out = capsys.readouterr().out
    assert "[迁移]" in out and "[待办]" in out
    assert (draft_dir / "draft.json").is_file()

    code = main(
        [
            "gold",
            "check",
            "--draft",
            str(draft_dir / "draft.json"),
            "--material-root",
            str(tmp_path),
        ]
    )
    assert code == 2  # 未绑定材料的草稿有阻断问题
    assert "[拒绝]" not in capsys.readouterr().out  # 检查结果不是拒绝
    report_path = tmp_path / "report.json"
    code = main(
        [
            "gold",
            "check",
            "--draft",
            str(draft_dir / "draft.json"),
            "--material-root",
            str(tmp_path),
            "--report-json",
            str(report_path),
        ]
    )
    assert code == 2
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["problems"]

    code = main(
        [
            "gold",
            "compare",
            "--legacy",
            str(legacy),
            "--current",
            str(draft_dir / "draft.json"),
        ]
    )
    assert code == 0
    out = capsys.readouterr().out
    assert "一致" in out


def test_gold_cli_prepare_save_query_full_flow(tmp_path, capsys):
    material = tmp_path / "materials"
    digest = write_synthetic_pdf(material / "paper.pdf")
    draft = build_fresh_draft(pdf_sha256=digest)
    draft_path = _write(tmp_path, "draft.json", draft)
    target = tmp_path / "target"
    work = tmp_path / "work"

    code = main(
        [
            "gold",
            "prepare",
            "--draft",
            str(draft_path),
            "--material-root",
            str(material),
            "--target",
            str(target),
            "--work-dir",
            str(work),
        ]
    )
    assert code == 0, capsys.readouterr()
    out = capsys.readouterr().out
    assert "清单哈希" in out
    prep_dir = next((work / "preparations").iterdir())

    from hvs_catalog.evaluation import load_preparation

    preparation = load_preparation(prep_dir)
    approval_path = _write(
        tmp_path, "approval.json", build_approval_payload(preparation)
    )
    code = main(
        [
            "gold",
            "save",
            "--draft",
            str(draft_path),
            "--material-root",
            str(material),
            "--target",
            str(target),
            "--preparation",
            str(prep_dir),
            "--approval",
            str(approval_path),
            "--operation-id",
            "op-cli",
        ]
    )
    assert code == 0, capsys.readouterr()
    out = capsys.readouterr().out
    assert "v1" in out

    code = main(
        [
            "gold",
            "query",
            "--operation-id",
            "op-cli",
            "--target",
            str(target),
        ]
    )
    assert code == 0
    out = capsys.readouterr().out
    assert "saved" in out


def test_gold_cli_save_rejects_stale_approval(tmp_path, capsys):
    material = tmp_path / "materials"
    digest = write_synthetic_pdf(material / "paper.pdf")
    draft = build_fresh_draft(pdf_sha256=digest)
    draft_path = _write(tmp_path, "draft.json", draft)
    target = tmp_path / "target"
    work = tmp_path / "work"
    code = main(
        [
            "gold",
            "prepare",
            "--draft",
            str(draft_path),
            "--material-root",
            str(material),
            "--target",
            str(target),
            "--work-dir",
            str(work),
        ]
    )
    assert code == 0
    prep_dir = next((work / "preparations").iterdir())
    from hvs_catalog.evaluation import load_preparation

    preparation = load_preparation(prep_dir)
    # 批准绑定错误清单哈希
    payload = build_approval_payload(preparation, manifest_sha256="0" * 64)
    approval_path = _write(tmp_path, "approval.json", payload)
    code = main(
        [
            "gold",
            "save",
            "--draft",
            str(draft_path),
            "--material-root",
            str(material),
            "--target",
            str(target),
            "--preparation",
            str(prep_dir),
            "--approval",
            str(approval_path),
            "--operation-id",
            "op-bad",
        ]
    )
    assert code == 2
    err = capsys.readouterr().err
    assert "[拒绝]" in err


def test_gold_cli_migrate_rejects_unknown_version(tmp_path, capsys):
    document = build_legacy_v4_doc()
    document["schema"]["version"] = 3
    legacy = _write(tmp_path, "legacy.json", document)
    code = main(
        [
            "gold",
            "migrate",
            "--legacy",
            str(legacy),
            "--draft-dir",
            str(tmp_path / "draft"),
        ]
    )
    assert code == 2
    assert "不在受支持范围" in capsys.readouterr().err

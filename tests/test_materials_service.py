"""End-to-end synthetic tests for material preparation, recovery, reuse, and consumption."""

from __future__ import annotations

import json

import pytest
from materials_helpers import (
    DRAFT_TEX,
    FakeConverterRunner,
    good_latexml_html,
    prepared_space,
    synthetic_package,
)

from hvs_catalog.materials.service import (
    MaterialsPreparationError,
    MaterialsService,
)
from hvs_catalog.records.questions import Actor
from hvs_catalog.review import ReviewService
from hvs_catalog.review.service import DecisionSubmission
from hvs_catalog.workspace import (
    OperationKeyConflictError,
    backup_workspace,
    restore_workspace,
)


def _runner():
    return FakeConverterRunner(
        latexml_html=good_latexml_html(
            [("FIC-A", "525", "10"), ("FIC-B", "-430", "12")]
        )
    )


def test_prepare_convert_and_consume_roundtrip(tmp_path):
    space, imported = prepared_space(tmp_path)
    service = MaterialsService(space, runner=_runner())
    handle, report = service.prepare_materials(
        request_key="rk-1", paper=imported.canonical_id
    )
    assert report.outcome == "finished"
    assert report.summary["body_status"] == "organized"
    # Four converted tables plus one standalone in-package table; prepare backing.csv through shared readers.
    assert report.summary["table_count"] == 5
    assert report.summary["table_success"] == 5
    bundle = service.get_prepared_input(handle.execution_id)
    # Preserve body inclusion locations and original line numbers.
    files = {f["name"]: f for f in bundle["body"]["files"]}
    assert files["sec1.tex"]["included_from"] == "main.tex"
    assert files["sec1.tex"]["included_at_line"] == 7
    # Preserve table identities, environments, and source line ranges.
    by_env = {t["environment"]: t for t in bundle["tables"]}
    assert by_env["pgfplotstable"]["method"] == "pgfplotstable_csv"
    assert by_env["pgfplotstable"]["start_line"] >= 1
    assert by_env["tabular"]["label"] == "tab:rv"
    # Shared readers prepare standalone CSV supplements, preserving original column names without assigning science meaning.
    attached = by_env["attached"]
    assert attached["method"] == "member_direct"
    assert attached["file"] == "backing.csv"
    assert attached["source_file_id"].startswith("ecsv:att-")
    # Neutral ECSV column names.
    ecsv = space.read_file(
        __import__("hvs_catalog.records.common", fromlist=["ArtifactRef"]).ArtifactRef(
            path=by_env["tabular"]["ecsv_path"], sha256=by_env["tabular"]["ecsv_sha256"]
        )
    ).decode("utf-8")
    assert "col_001" in ecsv
    # A synthetic consumer verifies all references without a model.
    result = service.consume_prepared_input(handle.execution_id)
    assert result["ok"], result["problems"]
    # Commit the prepared source revision and retain the original package.
    store = space.store()
    prepared = store.read_record(
        __import__("hvs_catalog.records.common", fromlist=["RecordRef"]).RecordRef(
            **bundle["prepared_source_ref"]
        )
    )
    assert prepared.revision == 2
    roles = {f.role for f in prepared.data.files}
    assert {"raw_package", "member"} <= roles
    space.close()


def test_replay_same_request_reuses_and_same_input_refuses_change(tmp_path):
    space, imported = prepared_space(tmp_path)
    service = MaterialsService(space, runner=_runner())
    handle1, _report1 = service.prepare_materials(
        request_key="rk", paper=imported.canonical_id
    )
    handle2, report2 = service.prepare_materials(
        request_key="rk", paper=imported.canonical_id
    )
    assert handle2.reused is True
    assert handle2.execution_id == handle1.execution_id
    assert report2.mode in ("already-finished", "resumed")
    # Reject changed inputs under the same request key; input changes require a new key.
    with pytest.raises(OperationKeyConflictError):
        service.prepare_materials(
            request_key="rk", paper=imported.canonical_id, root_file="main.tex"
        )
    # A new key creates a new execution without repeating conversion; execution caches reuse steps.
    handle3, _ = service.prepare_materials(
        request_key="rk-2", paper=imported.canonical_id
    )
    assert handle3.execution_id != handle1.execution_id
    space.close()


def test_multiple_roots_require_explicit_root_and_new_execution(tmp_path):
    space, imported = prepared_space(
        tmp_path, synthetic_package({"draft/old.tex": DRAFT_TEX.encode()})
    )
    service = MaterialsService(space, runner=_runner())
    handle, report = service.prepare_materials(
        request_key="rk", paper=imported.canonical_id
    )
    assert report.outcome == "finished"
    bundle = service.get_prepared_input(handle.execution_id)
    assert bundle["body"]["status"] == "unavailable"
    assert any("多个有效主稿" in g["description"] for g in bundle["gaps"])
    # Without a selected main document, tables belong to no body: produce no converted tables or false success;
    # standalone supplementary members are still prepared independently of main selection.
    assert [t for t in bundle["tables"] if t["environment"] != "attached"] == []
    assert [t["environment"] for t in bundle["tables"]] == ["attached"]
    # Explicit main selection creates a fresh execution/key and retains the selected package contents.
    handle2, report2 = service.prepare_materials(
        request_key="rk-root", paper=imported.canonical_id, root_file="main.tex"
    )
    assert report2.outcome == "finished"
    bundle2 = service.get_prepared_input(handle2.execution_id)
    assert bundle2["body"]["status"] == "organized"
    assert bundle2["body"]["root"] == "main.tex"
    assert bundle2["summary"]["table_success"] >= 1
    assert "draft/old.tex" in bundle2["body"]["excluded"]
    space.close()


def test_pdf_only_material_unavailable_not_empty_success(tmp_path):
    # PDF-only packages leave the body unavailable and PDF unprocessed, without claiming a successful empty roster.
    members = {"paper.pdf": b"%PDF-1.4 synthetic"}
    from materials_helpers import make_tar_gz

    space, imported = prepared_space(tmp_path, make_tar_gz(members))
    service = MaterialsService(space, runner=_runner())
    handle, report = service.prepare_materials(
        request_key="rk", paper=imported.canonical_id
    )
    assert report.outcome == "finished"
    bundle = service.get_prepared_input(handle.execution_id)
    assert bundle["body"]["status"] == "unavailable"
    # Preserve unprocessed scope accurately: no PDF parsing or false successful empty roster.
    descriptions = " ".join(g["description"] for g in bundle["gaps"])
    assert "未支持的附带材料格式" in descriptions
    assert "没有可识别的主稿" in descriptions
    assert bundle["tables"] == []
    result = service.consume_prepared_input(handle.execution_id)
    assert result["ok"]
    space.close()


def test_ambiguous_range_pauses_for_expert_then_applies_once(tmp_path):
    space, imported = prepared_space(tmp_path)
    service = MaterialsService(space, runner=_runner())
    handle, report = service.prepare_materials(
        request_key="rk",
        paper=imported.canonical_id,
        expert_adjudication=True,
        range_notations=[
            {
                "notation": "1-5",
                "file": "sec1.tex",
                "start_line": 2,
                "end_line": 2,
                "candidates": [
                    {
                        "code": "A",
                        "kind": "object_range",
                        "prefix": "HVS",
                        "description": "内部编号即 HVS 对象编号",
                    }
                ],
            }
        ],
    )
    assert report.outcome == "paused"
    assert report.pending_question is not None
    qref = report.pending_question["question_ref"]
    assert "候选解释" in report.pending_question["prompt"]
    # Unresolved state is not a complete roster; preparation input does not exist yet.
    with pytest.raises(MaterialsPreparationError):
        service.get_prepared_input(handle.execution_id)
    # Expert decision explicitly supplies code A.
    review = ReviewService(space)
    view = review.get_question(
        __import__("hvs_catalog.records.common", fromlist=["RecordRef"]).RecordRef(
            **qref
        )
    )
    review.submit_decision(
        execution_id=handle.execution_id,
        step_id=view.step_id,
        request_key="dec-1",
        submission=DecisionSubmission(
            question_ref=view.question_ref,
            fingerprint=view.record.data.fingerprint,
            actor=Actor(kind="human", id="expert-synthetic"),
            outcome="resolved",
            answer_original="内部编号 1-5 即对象 HVS1 到 HVS5。",
            answer_en="A",
            reason="合成专家决定：候选解释 A。",
        ),
    )
    resumed = service.resume_preparation(handle.execution_id)
    assert resumed.outcome == "finished"
    bundle = service.get_prepared_input(handle.execution_id)
    entry = bundle["ranges"][0]
    assert entry["status"] == "expert-resolved"
    assert entry["identifiers"] == ["HVS1", "HVS2", "HVS3", "HVS4", "HVS5"]
    assert entry["decision_ref"]["record_id"].startswith("dec-")
    # Repeated recovery reenters completed execution idempotently without applying twice.
    again = service.resume_preparation(handle.execution_id)
    assert again.mode == "already-finished"
    receipts = space.store().find_receipts(execution_id=handle.execution_id)
    applied = [r for r in receipts if r.kind == "decision_apply"]
    assert len(applied) == 1
    space.close()


def test_unresolved_or_offmenu_decision_keeps_range_open(tmp_path):
    space, imported = prepared_space(tmp_path)
    service = MaterialsService(space, runner=_runner())
    handle, report = service.prepare_materials(
        request_key="rk",
        paper=imported.canonical_id,
        expert_adjudication=True,
        range_notations=[
            {
                "notation": "1-5",
                "file": "sec1.tex",
                "start_line": 2,
                "end_line": 2,
                "candidates": [
                    {
                        "code": "A",
                        "kind": "object_range",
                        "prefix": "HVS",
                        "description": "A",
                    }
                ],
            }
        ],
    )
    assert report.outcome == "paused"
    review = ReviewService(space)
    from hvs_catalog.records.common import RecordRef

    qref = RecordRef(**report.pending_question["question_ref"])
    view = review.get_question(qref)
    review.submit_decision(
        execution_id=handle.execution_id,
        step_id=view.step_id,
        request_key="dec-unresolved",
        submission=DecisionSubmission(
            question_ref=qref,
            fingerprint=view.record.data.fingerprint,
            actor=Actor(kind="human", id="expert-synthetic"),
            outcome="unresolved",
            answer_original="证据不足，暂不能判定。",
            answer_en="cannot decide",
            reason="合成未决答复。",
        ),
    )
    resumed = service.resume_preparation(handle.execution_id)
    # Unresolved question application remains blocked; execution waits rather than treating the roster as complete.
    assert resumed.outcome == "paused"
    bundle_pending = service.get_preparation(handle.execution_id)
    assert bundle_pending["state"] == "waiting_review"
    space.close()


def test_range_without_adjudication_stays_pending(tmp_path):
    space, imported = prepared_space(tmp_path)
    service = MaterialsService(space, runner=_runner())
    handle, report = service.prepare_materials(
        request_key="rk",
        paper=imported.canonical_id,
        range_notations=[
            {
                "notation": "1-5",
                "file": "sec1.tex",
                "start_line": 2,
                "end_line": 2,
                "candidates": [
                    {
                        "code": "A",
                        "kind": "object_range",
                        "prefix": "HVS",
                        "description": "A",
                    }
                ],
            },
            {
                "notation": "HVS1--HVS5",
                "file": "sec1.tex",
                "start_line": 1,
                "end_line": 1,
            },
        ],
    )
    assert report.outcome == "finished"
    bundle = service.get_prepared_input(handle.execution_id)
    statuses = {r["notation"]: r["status"] for r in bundle["ranges"]}
    assert statuses["1-5"] == "ambiguous"
    assert statuses["HVS1--HVS5"] == "expanded"
    space.close()


def test_converter_version_change_blocks_conversion(tmp_path):
    """Reject conversion when converter version differs from fixed input; input changes
    require a new execution.
    """
    from hvs_catalog.materials.flow import _verify_frozen_converters
    from hvs_catalog.materials.sandbox import ConverterAvailability
    from hvs_catalog.workspace import WorkspaceIntegrityError

    frozen = {"latexmlc": {"available": True, "version": "0.8.8", "reason": None}}
    current = {
        "latexmlc": ConverterAvailability(
            "latexmlc", available=True, executable="/x", version="9.9-changed"
        ),
        "pandoc": ConverterAvailability("pandoc", available=False, reason="absent"),
    }
    with pytest.raises(WorkspaceIntegrityError, match="转换器"):
        _verify_frozen_converters({"request": {"converters": frozen}}, current)
    # Matching versions, including matching unavailability, do not block.
    same = {
        "latexmlc": ConverterAvailability(
            "latexmlc", available=True, executable="/x", version="0.8.8"
        ),
        "pandoc": ConverterAvailability("pandoc", available=False, reason="absent"),
    }
    ok_frozen = {
        "latexmlc": {"available": True, "version": "0.8.8", "reason": None},
        "pandoc": {"available": False, "version": None, "reason": "absent"},
    }
    _verify_frozen_converters({"request": {"converters": ok_frozen}}, same)


def test_missing_tool_falls_back_with_skipped_diagnostics(tmp_path):
    from materials_helpers import MAIN_TEX

    literal = MAIN_TEX.replace(r"\objname{FIC-A}", "FIC-A")
    space, imported = prepared_space(
        tmp_path, synthetic_package({"main.tex": literal.encode()})
    )
    runner = FakeConverterRunner(available={"latexmlc": False, "pandoc": False})
    service = MaterialsService(space, runner=runner)
    handle, report = service.prepare_materials(
        request_key="rk", paper=imported.canonical_id
    )
    assert report.outcome == "finished"
    bundle = service.get_prepared_input(handle.execution_id)
    # Internal-parser fallback keeps simple tables usable and retains skipped-converter diagnostics.
    statuses = {t["environment"]: (t["status"], t["method"]) for t in bundle["tables"]}
    assert statuses["tabular"][0] == "success"
    assert statuses["tabular"][1] == "internal"
    prep = service.get_preparation(handle.execution_id)
    assert prep["config"]["request"]["converters"]["latexmlc"]["available"] is False
    space.close()


def test_backup_restore_preserves_prepared_input(tmp_path):
    space, imported = prepared_space(tmp_path)
    service = MaterialsService(space, runner=_runner())
    handle, report = service.prepare_materials(
        request_key="rk", paper=imported.canonical_id
    )
    assert report.outcome == "finished"
    backup_workspace(space, tmp_path / "backup")
    restored_root = tmp_path / "restored"
    restore_workspace(tmp_path / "backup", restored_root)
    from hvs_catalog.workspace import open_workspace

    restored = open_workspace(restored_root)
    restored_service = MaterialsService(restored, runner=_runner())
    result = restored_service.consume_prepared_input(handle.execution_id)
    assert result["ok"], result["problems"]
    status = restored_service.get_preparation(handle.execution_id)
    assert status["state"] == "finished"
    restored.close()
    space.close()


def test_prepare_requires_available_material(tmp_path):
    space, _imported = prepared_space(tmp_path)
    service = MaterialsService(space, runner=_runner())
    with pytest.raises(MaterialsPreparationError, match="没有已完成的选定材料"):
        service.prepare_materials(request_key="rk", paper="2501.09999")
    space.close()


def test_external_link_mention_kept_as_gap_not_fetched(tmp_path):
    # Retain external links as mentions without fetching them or claiming material availability.
    main = (
        "\\documentclass{article}\n\\begin{document}\n"
        "Data available at \\url{https://example.org/external.csv}.\n"
        "\\end{document}\n"
    )
    from materials_helpers import make_tar_gz

    space, imported = prepared_space(tmp_path, make_tar_gz({"main.tex": main.encode()}))
    service = MaterialsService(space, runner=_runner())
    handle, report = service.prepare_materials(
        request_key="rk", paper=imported.canonical_id
    )
    assert report.outcome == "finished"
    bundle = service.get_prepared_input(handle.execution_id)
    assert bundle["body"]["status"] == "organized"
    assert bundle["tables"] == []
    # Unfetched external links produce no tables or material declarations; register explicit mentions as gaps,
    # never as processed scope. The initial-review counterexample previously had empty gaps.
    external_gaps = [
        g for g in bundle["gaps"] if g["gap_id"].startswith("gap-external")
    ]
    assert any("external.csv" in g["description"] for g in external_gaps)
    result = service.consume_prepared_input(handle.execution_id)
    assert result["ok"]
    space.close()


@pytest.mark.integration
def test_real_latexml_end_to_end_when_available(tmp_path):
    """Run the real conversion chain when latexmlc is installed locally; otherwise skip
    without claiming verification.
    """
    import platform
    import shutil

    from hvs_catalog.materials.sandbox import SandboxedConverterRunner

    if platform.system() != "Darwin" or shutil.which("latexmlc") is None:
        pytest.skip("需要本机 macOS sandbox-exec 与 latexmlc 实测")
    space, imported = prepared_space(tmp_path)
    runner = SandboxedConverterRunner()
    availability = runner.availability("latexmlc")
    if not availability.available:
        pytest.skip(f"latexmlc 不可用：{availability.reason}")
    service = MaterialsService(space, runner=runner)
    handle, report = service.prepare_materials(
        request_key="rk-real", paper=imported.canonical_id
    )
    assert report.outcome == "finished", report.errors
    bundle = service.get_prepared_input(handle.execution_id)
    # Real tables succeed with latexml; pgfplotstable uses backing CSV.
    by_method = {t["method"] for t in bundle["tables"] if t["status"] == "success"}
    assert "latexml" in by_method
    assert "pgfplotstable_csv" in by_method
    result = service.consume_prepared_input(handle.execution_id)
    assert result["ok"], result["problems"]
    space.close()


def test_consumer_reports_corrupted_or_missing_artifacts(tmp_path):
    """Corrupted ECSV attachments fail consumer validation rather than counting as usable
    materials.
    """

    space, imported = prepared_space(tmp_path)
    service = MaterialsService(space, runner=_runner())
    handle, report = service.prepare_materials(
        request_key="rk", paper=imported.canonical_id
    )
    assert report.outcome == "finished"
    bundle = service.get_prepared_input(handle.execution_id)
    ecsv_table = next(t for t in bundle["tables"] if t["environment"] == "tabular")
    target = space.root / ecsv_table["ecsv_path"]
    original = target.read_bytes()
    # Tamper with ECSV so the hash differs; choose the table containing 525 to ensure changed bytes.
    assert b"525" in original
    target.write_bytes(original.replace(b"525", b"526"))
    result = service.consume_prepared_input(handle.execution_id)
    assert result["ok"] is False
    assert any(ecsv_table["table_id"] in p for p in result["problems"])
    # Missing attachments are also reported accurately.
    target.unlink()
    result = service.consume_prepared_input(handle.execution_id)
    assert result["ok"] is False
    assert any("缺失" in p or "哈希" in p for p in result["problems"])
    space.close()


def test_table_footnotes_preserved_in_bundle(tmp_path):
    """Preserve tablenotetext footnotes and their markers verbatim in input attachments."""
    space, imported = prepared_space(tmp_path)
    service = MaterialsService(space, runner=_runner())
    handle, report = service.prepare_materials(
        request_key="rk", paper=imported.canonical_id
    )
    assert report.outcome == "finished"
    bundle = service.get_prepared_input(handle.execution_id)
    deluxe = next(t for t in bundle["tables"] if t["environment"] == "deluxetable")
    marks = {note["mark"]: note["text"] for note in deluxe["footnotes"]}
    assert marks["a"].startswith("Synthetic")
    assert marks["b"] == r"\emph{Second} synthetic note"
    space.close()


def test_body_range_discovery_and_registration_verification(tmp_path):
    """Discover explicit body notation within bounds; validate registered source locations
    (review range/list example).
    """
    space, imported = prepared_space(tmp_path)
    service = MaterialsService(space, runner=_runner())
    handle, report = service.prepare_materials(
        request_key="rk",
        paper=imported.canonical_id,
        range_notations=[
            # Invalid location: the file is outside body flow (review example absent.tex, line 999).
            {
                "notation": "HVS1--HVS5",
                "file": "absent.tex",
                "start_line": 999,
                "end_line": 1000,
            }
        ],
    )
    assert report.outcome == "finished"
    bundle = service.get_prepared_input(handle.execution_id)
    by_origin = {}
    for entry in bundle["ranges"]:
        by_origin.setdefault(entry["origin"], []).append(entry)
    # Discover HVS1--HVS5 in sec1.tex with its actual location.
    discovered = by_origin.get("discovered", [])
    assert any(
        item["notation"] == "HVS1--HVS5"
        and item["file"] == "sec1.tex"
        and item["status"] == "expanded"
        and item["identifiers"] == ["HVS1", "HVS2", "HVS3", "HVS4", "HVS5"]
        for item in discovered
    )
    # Invalid registered location produces a gap without expanding members.
    invalid = by_origin.get("registered", [])
    assert len(invalid) == 1
    assert invalid[0]["status"] == "invalid-registration"
    assert invalid[0]["identifiers"] == []
    assert any("出处无效" in gap["description"] for gap in bundle["gaps"])
    space.close()


def test_expert_interpretation_applies_to_full_notation(tmp_path):
    """Apply candidate interpretations to the entire original notation, without truncating
    lists (review range-list example).
    """
    space, imported = prepared_space(tmp_path)
    service = MaterialsService(space, runner=_runner())
    handle, report = service.prepare_materials(
        request_key="rk",
        paper=imported.canonical_id,
        expert_adjudication=True,
        range_notations=[
            {
                "notation": "1,3,5",
                "file": "sec1.tex",
                "start_line": 3,
                "end_line": 3,
                "candidates": [
                    {
                        "code": "A",
                        "kind": "object_range",
                        "prefix": "HVS",
                        "description": "内部编号即 HVS 对象编号",
                    }
                ],
            }
        ],
    )
    assert report.outcome == "paused"
    review = ReviewService(space)
    from hvs_catalog.records.common import RecordRef

    qref = RecordRef(**report.pending_question["question_ref"])
    view = review.get_question(qref)
    review.submit_decision(
        execution_id=handle.execution_id,
        step_id=view.step_id,
        request_key="dec-full",
        submission=DecisionSubmission(
            question_ref=qref,
            fingerprint=view.record.data.fingerprint,
            actor=Actor(kind="human", id="expert-synthetic"),
            outcome="resolved",
            answer_original="内部编号 1,3,5 即 HVS1、HVS3、HVS5。",
            answer_en="A",
            reason="合成决定：完整列表按前缀展开。",
        ),
    )
    resumed = service.resume_preparation(handle.execution_id)
    assert resumed.outcome == "finished"
    bundle = service.get_prepared_input(handle.execution_id)
    entry = next(r for r in bundle["ranges"] if r["origin"] == "registered")
    assert entry["status"] == "expert-resolved"
    assert entry["identifiers"] == ["HVS1", "HVS3", "HVS5"]
    space.close()


def test_same_notation_different_lines_get_distinct_questions(tmp_path):
    """Identical notation on different original lines creates independent question
    identities (review collision example).
    """
    from hvs_catalog.records.common import sha256_of

    def digest(notation, file, start, end, candidates):
        return sha256_of(
            {
                "notation": notation,
                "file": file,
                "start_line": start,
                "end_line": end,
                "candidates": candidates,
            }
        )[:16]

    candidates = [{"code": "A", "kind": "object_range", "prefix": "HVS"}]
    a = digest("HVS1-5", "sec1.tex", 2, 2, candidates)
    b = digest("HVS1-5", "sec1.tex", 8, 8, candidates)
    c = digest(
        "HVS1-5",
        "sec1.tex",
        2,
        2,
        [{"code": "B", "kind": "object_range", "prefix": "HVS"}],
    )
    assert a != b  # Different original lines.
    assert a != c  # Different candidate interpretations.


def test_dat_member_and_external_url_registered_as_gaps(tmp_path):
    """Decoded in-package DAT files are not thereby processed; retain linked mentions as
    gaps (review example).
    """
    from materials_helpers import make_tar_gz

    main = (
        "\\documentclass{article}\n\\begin{document}\n"
        "Measurements listed in measurements.dat; "
        "mirror at \\url{https://example.org/measurements.dat}.\n"
        "\\end{document}\n"
    )
    package = make_tar_gz(
        {
            "main.tex": main.encode(),
            "measurements.dat": b"col1 col2\n1 2\n",
        }
    )
    space, imported = prepared_space(tmp_path, package)
    service = MaterialsService(space, runner=_runner())
    handle, report = service.prepare_materials(
        request_key="rk", paper=imported.canonical_id
    )
    assert report.outcome == "finished"
    bundle = service.get_prepared_input(handle.execution_id)
    descriptions = [gap["description"] for gap in bundle["gaps"]]
    assert any("measurements.dat" in d and "未支持" in d for d in descriptions)
    assert any("https://example.org/measurements.dat" in d for d in descriptions)
    # DAT members remain undecoded; read_state is not ready text.
    dat_member = next(m for m in bundle["members"] if m["name"] == "measurements.dat")
    assert dat_member.get("encoding") is None
    space.close()


def test_same_notation_at_two_lines_two_decisions_no_conflict(tmp_path):
    """Identical notation on different lines creates independent questions whose separate
    decisions do not conflict.
    """
    from materials_helpers import make_tar_gz

    main = (
        "\\documentclass{article}\n\\begin{document}\n"
        "Sample A spans 1-3 here.\n"
        "Sample B spans 1-3 again.\n"
        "\\end{document}\n"
    )
    space, imported = prepared_space(tmp_path, make_tar_gz({"main.tex": main.encode()}))
    service = MaterialsService(space, runner=_runner())
    candidates = [
        {"code": "A", "kind": "object_range", "prefix": "HVS", "description": "A"}
    ]
    handle, report = service.prepare_materials(
        request_key="rk",
        paper=imported.canonical_id,
        expert_adjudication=True,
        range_notations=[
            {
                "notation": "1-3",
                "file": "main.tex",
                "start_line": 3,
                "end_line": 3,
                "candidates": candidates,
            },
            {
                "notation": "1-3",
                "file": "main.tex",
                "start_line": 4,
                "end_line": 4,
                "candidates": candidates,
            },
        ],
    )
    assert report.outcome == "paused"
    review = ReviewService(space)
    from hvs_catalog.records.common import RecordRef

    for index in range(2):
        prep = service.get_preparation(handle.execution_id)
        open_questions = [q for q in prep["questions"] if q["status"] == "open"]
        assert open_questions, "两个问题都应逐个等待决定"
        qref = RecordRef(**open_questions[0]["question_ref"])
        view = review.get_question(qref)
        review.submit_decision(
            execution_id=handle.execution_id,
            step_id=view.step_id,
            request_key=f"dec-{index}",
            submission=DecisionSubmission(
                question_ref=qref,
                fingerprint=view.record.data.fingerprint,
                actor=Actor(kind="human", id="expert-synthetic"),
                outcome="resolved",
                answer_original="内部编号即 HVS 对象。",
                answer_en="A",
                reason="合成决定。",
            ),
        )
        resumed = service.resume_preparation(handle.execution_id)
        if index == 0:
            assert resumed.outcome == "paused"
        else:
            assert resumed.outcome == "finished"
    bundle = service.get_prepared_input(handle.execution_id)
    resolved = [r for r in bundle["ranges"] if r["status"] == "expert-resolved"]
    assert len(resolved) == 2
    space.close()


def test_tampered_bundle_rejected_by_consume_resume_and_backup(tmp_path):
    """Tampered bundles/ECSV fail consumption and recovery and prevent completed backup
    publication (review example).
    """
    from hvs_catalog.workspace import (
        WorkspaceIntegrityError,
        backup_workspace,
    )

    space, imported = prepared_space(tmp_path)
    service = MaterialsService(space, runner=_runner())
    handle, report = service.prepare_materials(
        request_key="rk", paper=imported.canonical_id
    )
    assert report.outcome == "finished"
    bundle_path = (
        space.root / "runs" / handle.execution_id / "outputs" / "prepared-input.json"
    )
    payload = json.loads(bundle_path.read_text(encoding="utf-8"))
    payload["ranges"] = [{"status": "expanded", "identifiers": ["SYNTH-FORGED"]}]
    payload.pop("tables", None)
    bundle_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    # Consumption verifies saved references and rejects rewritten contents.
    result = service.consume_prepared_input(handle.execution_id)
    assert result["ok"] is False
    # Completed recovery verifies checkpoint attachment hashes and rejects mismatches.
    from hvs_catalog.runtime.recovery import RecoveryInconsistentError

    with pytest.raises(RecoveryInconsistentError):
        service.resume_preparation(handle.execution_id)
    # Full backup fails consistency checks and does not publish completion.
    with pytest.raises(WorkspaceIntegrityError):
        backup_workspace(space, tmp_path / "backup")
    space.close()


def test_prepare_to_records_evidence_roundtrip(tmp_path):
    """Prepared results enter records directly: save table_cell evidence and validate the
    collection (derived-source review example).
    """
    space, imported = prepared_space(tmp_path)
    service = MaterialsService(space, runner=_runner())
    handle, report = service.prepare_materials(
        request_key="rk", paper=imported.canonical_id
    )
    assert report.outcome == "finished"
    bundle = service.get_prepared_input(handle.execution_id)
    table = next(
        t
        for t in bundle["tables"]
        if t["environment"] == "tabular"
        and t["status"] == "success"
        and t.get("source_file_id")
    )
    # prepared_source_ref supplies file_id; ECSV is registered as a derived file in the source revision.
    assert table.get("source_file_id")
    prep_ref = bundle["prepared_source_ref"]
    from hvs_catalog.records.common import RecordRef
    from hvs_catalog.records.validate import validate_collection

    source = space.store().read_record(RecordRef.model_validate(prep_ref))
    ecsv_entry = next(
        f for f in source.data.files if f.file_id == table["source_file_id"]
    )
    assert ecsv_entry.path == table["ecsv_path"]
    # Build and check table_cell evidence from actual execution output through the same interface as manual ECSV.
    locator = {
        "kind": "table_cell",
        "row_key": {"col_001": "FIC-A"},
        "column": "col_002",
        "row_number": None,
    }
    from hvs_catalog.records.validate import CollectionValidationError

    records = []
    evidence = None
    from hvs_catalog.records.common import build_record
    from hvs_catalog.records.source import EvidenceRecord

    evidence = build_record(
        EvidenceRecord,
        record_id="ev-prepare-1",
        created_at=__import__("datetime").datetime(
            2026, 10, 1, tzinfo=__import__("datetime").UTC
        ),
        execution_id=handle.execution_id,
        data={
            "paper_key": imported.canonical_id,
            "source_ref": prep_ref,
            "file_id": table["source_file_id"],
            "locator": locator,
            "excerpt": "525",
        },
    )
    records = [source, evidence]
    try:
        issues = validate_collection(records, materials_root=space.root)
        assert issues == []
    except CollectionValidationError as exc:
        pytest.fail(f"准备产物应可直接作为证据来源：{exc.issues}")
    # Consumption still succeeds after full backup/restore.
    from hvs_catalog.workspace import backup_workspace, restore_workspace

    backup_workspace(space, tmp_path / "backup")
    restore_workspace(tmp_path / "backup", tmp_path / "restored")
    from hvs_catalog.workspace import open_workspace

    restored = open_workspace(tmp_path / "restored")
    restored_service = MaterialsService(restored, runner=_runner())
    result = restored_service.consume_prepared_input(handle.execution_id)
    assert result["ok"], result["problems"]
    space.close()
    restored.close()


def test_crash_after_revision_commit_resumes_without_conflict(tmp_path, monkeypatch):
    """Interrupt after preparation revision commit but before advancing the step; recovery
    reuses the saved receipt.
    """
    import hvs_catalog.materials.service as service_module
    from hvs_catalog.materials.flow import MaterialsFlow

    space, imported = prepared_space(tmp_path)
    runner = _runner()

    def crash_after_commit() -> None:
        raise RuntimeError("synthetic crash after revision commit")

    flow_crash = MaterialsFlow(
        runner=runner,
        fault_hooks={"after_revision_commit": crash_after_commit},
    )
    monkeypatch.setattr(service_module, "MaterialsFlow", lambda **kwargs: flow_crash)
    service = MaterialsService(space, runner=runner)
    handle, report = service.prepare_materials(
        request_key="rk", paper=imported.canonical_id
    )
    assert report.outcome == "failed"
    # The revision transaction committed, but execution failed with its checkpoint before conversion.
    receipts = space.store().find_receipts(execution_id=handle.execution_id)
    assert any(r.kind == "materials_prepared_revision" for r in receipts)
    # Normal flow recovery reuses the committed revision without recomputing revision number or timestamp.
    monkeypatch.setattr(
        service_module,
        "MaterialsFlow",
        lambda **kwargs: MaterialsFlow(runner=kwargs.get("runner")),
    )
    resumed = service.resume_preparation(handle.execution_id)
    assert resumed.outcome == "finished", resumed.errors
    bundle = service.get_prepared_input(handle.execution_id)
    prep_ref = bundle["prepared_source_ref"]
    assert prep_ref["revision"] == imported.material_ref.revision + 1
    from hvs_catalog.records.common import RecordRef

    source = space.store().read_record(RecordRef.model_validate(prep_ref))
    assert source.execution_id == handle.execution_id
    # Recovery creates no second preparation revision for the same source record.
    revisions = [
        r
        for r in space.store().list_records()
        if getattr(r, "record_id", None) == prep_ref["record_id"]
    ]
    assert max(r.revision for r in revisions) == prep_ref["revision"]
    result = service.consume_prepared_input(handle.execution_id)
    assert result["ok"], result["problems"]
    space.close()

"""Single-file Gold boundaries, complete evidence checks and atomic publication."""

import hashlib
import json
import shutil

import pytest
from gold_save_helpers import flow, make_annotation, revision

from hvs_catalog.evaluation.generate import build_gold_outputs
from hvs_catalog.evaluation.gold import (
    check_annotation,
    read_annotation,
    write_annotation,
)
from hvs_catalog.evaluation.gold_compare import compare_annotations
from hvs_catalog.evaluation.store import inspect_save, read_saved, save_annotation
from hvs_catalog.records.pdf_contribution import GoldAnnotation


def test_single_file_roundtrip_and_no_text_extraction(tmp_path, monkeypatch):
    value, root = make_annotation(tmp_path)
    from hvs_catalog.evaluation import gold

    original = gold.analyze_pdf

    def checked(data, *, extract_text):
        assert extract_text is False
        return original(data, extract_text=False)

    monkeypatch.setattr(gold, "analyze_pdf", checked)
    assert check_annotation(value, material_root=root).ready
    assert read_annotation(root) == value
    assert {p.name for p in root.iterdir()} == {"annotation.json", "paper.pdf"}
    with pytest.raises(FileExistsError):
        write_annotation(value, root / "annotation.json")


@pytest.mark.parametrize(
    "field,value",
    [
        ("quote", ""),
        ("quote", "Original text"),
        ("file_id", "paper"),
        ("page", 0),
        ("page", True),
        ("location", " "),
    ],
)
def test_invalid_or_retired_evidence_fields_rejected(tmp_path, field, value):
    annotation, _ = make_annotation(tmp_path)
    raw = annotation.model_dump(mode="json")
    raw["stars"][0]["identifiers"][0]["evidence"][0][field] = value
    with pytest.raises(ValueError):
        GoldAnnotation.model_validate(raw)


@pytest.mark.parametrize("group", ["identifier", "contribution", "value"])
def test_every_evidence_group_checks_page_bounds(tmp_path, group):
    annotation, root = make_annotation(tmp_path)
    star = annotation.stars[0]
    item = {
        "identifier": star.identifiers[0],
        "contribution": star.contribution_type,
        "value": star.quantities[0].values[0],
    }[group]
    item.evidence[0].page = 3
    report = check_annotation(annotation, material_root=root)
    assert not report.ready and "exceeds 2" in report.errors[0]


@pytest.mark.parametrize("change", ["missing", "hash", "invalid", "symlink", "rules"])
def test_fixed_pdf_and_rules_fail_closed(tmp_path, change):
    annotation, root = make_annotation(tmp_path)
    pdf = root / "paper.pdf"
    if change == "missing":
        pdf.unlink()
    if change == "hash":
        pdf.write_bytes(pdf.read_bytes() + b"changed")
    if change == "invalid":
        pdf.write_bytes(b"not a PDF")
        annotation.metadata.material.sha256 = hashlib.sha256(
            pdf.read_bytes()
        ).hexdigest()
    if change == "symlink":
        other = root / "other.pdf"
        pdf.rename(other)
        pdf.symlink_to(other)
    if change == "rules":
        annotation.metadata.rules.version = "old"
    assert not check_annotation(annotation, material_root=root).ready


@pytest.mark.parametrize(
    "key,value",
    [
        ("annotation", {}),
        ("coverage", "partial"),
        ("unresolved", []),
        ("revision", {}),
        ("answer_sha256", "0" * 64),
        ("materials", []),
    ],
)
def test_removed_process_metadata_is_not_silently_discarded(tmp_path, key, value):
    annotation, _ = make_annotation(tmp_path)
    raw = annotation.model_dump(mode="json")
    raw["metadata"][key] = value
    with pytest.raises(ValueError):
        GoldAnnotation.model_validate(raw)


def test_one_adjacent_material_and_shared_numeric_validation(tmp_path):
    annotation, _ = make_annotation(tmp_path)
    raw = annotation.model_dump(mode="json")
    raw["metadata"]["material"]["path"] = "../other.pdf"
    with pytest.raises(ValueError):
        GoldAnnotation.model_validate(raw)
    raw = annotation.model_dump(mode="json")
    raw["stars"][0]["quantities"][0]["values"][0]["lower_bound"] = "2"
    with pytest.raises(ValueError):
        GoldAnnotation.model_validate(raw)


def test_save_retry_revision_and_stale_candidate_guards(tmp_path):
    args = flow(tmp_path)
    result = save_annotation(**args)
    assert result["already_present"] is False
    assert read_saved(args["target"])["annotation_sha256"] == args["expected_new"]
    assert save_annotation(**args)["already_present"] is True
    assert {p.name for p in args["target"].iterdir()} == {
        "paper.pdf",
        "annotation.json",
    }
    new = revision(args, tmp_path / "new")
    with pytest.raises(ValueError, match="expected_old"):
        save_annotation(**{k: v for k, v in new.items() if k != "expected_old"})
    with pytest.raises(ValueError, match="expected_new"):
        save_annotation(**{**new, "expected_new": "0" * 64})
    assert save_annotation(**new)["already_present"] is False
    with pytest.raises(ValueError, match="expected_old"):
        save_annotation(**args)


def test_unknown_layout_wrong_paper_and_changed_material_rejected(tmp_path):
    args = flow(tmp_path)
    wrong = args["target"].with_name("2601.99998")
    with pytest.raises(ValueError, match="paper"):
        save_annotation(**{**args, "target": wrong})
    args["target"].mkdir()
    (args["target"] / "answer.json").write_text("{}")
    with pytest.raises(ValueError, match="layout"):
        save_annotation(**args)
    shutil.rmtree(args["target"])
    save_annotation(**args)
    other, root = make_annotation(tmp_path / "different")
    pdf = root / "paper.pdf"
    pdf.write_bytes(pdf.read_bytes() + b"\nchanged fixed bytes\n")
    other.metadata.material.sha256 = hashlib.sha256(pdf.read_bytes()).hexdigest()
    (root / "annotation.json").unlink()
    write_annotation(other, root / "annotation.json")
    with pytest.raises(ValueError, match="material migration"):
        inspect_save(
            annotation=root, target=args["target"], expected_old=args["expected_new"]
        )


def test_compare_keeps_conditions_evidence_notes_and_ambiguous_rows(tmp_path):
    annotation, root = make_annotation(tmp_path)
    new = tmp_path / "new.json"
    annotation.stars[0].quantities[1].values[0].condition = "Model B"
    annotation.notes = ["New note"]
    write_annotation(annotation, new)
    changes = compare_annotations(root, new)["changes"]
    assert {x["path"] for x in changes} == {
        "notes",
        "stars[0] -> stars[0].quantities.distance",
    }
    annotation.stars.append(annotation.stars[0].model_copy(deep=True))
    new.unlink()
    write_annotation(annotation, new)
    diff = compare_annotations(root, new)
    assert diff["matches"] == [] and diff["unmatched_after"] == [0, 1]


def test_compare_reordering_values_does_not_create_changes(tmp_path):
    annotation, root = make_annotation(tmp_path)
    annotation.stars[0].quantities.reverse()
    new = tmp_path / "new.json"
    write_annotation(annotation, new)
    assert compare_annotations(root, new)["changes"] == []


def test_generated_annotation_has_only_reference_content():
    outputs = build_gold_outputs()
    raw = json.loads(outputs["templates/annotation.json"])
    GoldAnnotation.model_validate(raw)
    assert set(raw) == {"metadata", "stars", "notes"}
    assert set(outputs) == {
        "schemas/annotation.json",
        "templates/annotation.json",
        "README.md",
        "gold-format.md",
    }


def test_publication_rejects_symlink_targets_and_annotation_sources(tmp_path):
    args = flow(tmp_path)
    real = args["target"].with_name("actual")
    real.mkdir()
    args["target"].symlink_to(real, target_is_directory=True)
    with pytest.raises(ValueError, match="symbolic"):
        save_annotation(**args)
    args["target"].unlink()
    source = args["annotation"]
    saved = source.with_name("real.json")
    source.rename(saved)
    source.symlink_to(saved)
    with pytest.raises(ValueError, match="symbolic"):
        save_annotation(**args)
    assert not args["target"].exists()


def test_source_change_during_staging_does_not_publish(tmp_path, monkeypatch):
    args = flow(tmp_path)
    from hvs_catalog.evaluation import store

    original = store.shutil.copyfile

    def changed(source, destination):
        result = original(source, destination)
        if destination.name == "annotation.json":
            raw = json.loads(destination.read_text())
            raw["notes"] = ["Unreviewed content change."]
            destination.write_text(json.dumps(raw))
        return result

    monkeypatch.setattr(store.shutil, "copyfile", changed)
    with pytest.raises(ValueError, match="changed while staging"):
        save_annotation(**args)
    assert not args["target"].exists()

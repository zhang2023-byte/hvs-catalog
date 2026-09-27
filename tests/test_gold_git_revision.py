"""In-place Gold correction checks use synthetic materials only."""

import json
import subprocess
import sys

import pytest
from evaluation_helpers import build_approval_payload
from test_gold_compact import _pair

from hvs_catalog.cli import main
from hvs_catalog.evaluation import (
    GoldCheckError,
    GoldIntegrityError,
    GoldTarget,
    GoldTargetError,
    SaveApproval,
    approved_save,
    prepare_save,
    query_operation,
    write_compact_gold_pair,
)
from hvs_catalog.evaluation.git_revision import refresh_gold_in_place

DOC = "gold-2501.00001"


def _saved(tmp_path):
    material, _, pair = _pair(tmp_path)
    answer, _ = write_compact_gold_pair(pair, tmp_path / "pair")
    target = GoldTarget(tmp_path / "target")
    prep = prepare_save(
        draft_path=answer,
        material_root=material,
        work_dir=tmp_path / "work",
        target=target,
    )
    approval = SaveApproval.model_validate(
        {**build_approval_payload(prep), "scientific_review": True}
    )
    kwargs = {
        "draft_path": answer,
        "material_root": material,
        "target": target,
        "preparation_dir": prep.directory,
        "approval": approval,
        "operation_id": "baseline",
    }
    approved_save(**kwargs)
    return target, material, kwargs


def _answer(target):
    return target.root / "annotations" / DOC / "v1" / "answer.json"


def _edit(target, value="613.0"):
    answer = _answer(target)
    data = json.loads(answer.read_text())
    data["contributions"][0]["quantities"][0]["values"][0]["value"] = value
    answer.write_text(json.dumps(data))


def _refresh(target, material):
    return refresh_gold_in_place(
        target=target,
        document_id=DOC,
        material_root=material,
        revised_by="synthetic-preparer",
        decided_by="synthetic-human",
        basis="Synthetic human decision: correct transcription",
    )


def test_in_place_correction_keeps_paths_and_old_approval_blocks_old_save(tmp_path):
    target, material, kwargs = _saved(tmp_path)
    before_paths = {
        str(p.relative_to(target.root)) for p in target.root.rglob("*.json")
    }
    before_records = {
        str(p.relative_to(target.root)): p.read_bytes()
        for p in (target.root / "operations").glob("*.json")
    }
    old_entry = target.read_entry(DOC)
    _edit(target)
    with pytest.raises(GoldIntegrityError):
        target.read_current_answer(DOC)
    updated = _refresh(target, material)
    assert updated.version == 1 and updated.storage_mode == "git"
    assert {
        str(p.relative_to(target.root)) for p in target.root.rglob("*.json")
    } == before_paths
    assert not (target.root / "current").exists()
    for path, content in before_records.items():
        assert (target.root / path).read_bytes() == content
    entry, current = target.read_current_answer(DOC)
    assert entry == updated
    assert current.contributions[0].quantities[0].values[0].value == "613.0"
    metadata = json.loads(_answer(target).with_name("metadata.json").read_text())
    assert metadata["revision"]["previous_answer_sha256"] == next(
        f.sha256 for f in old_entry.files if f.path.endswith("/answer.json")
    )
    assert metadata["revision"]["revised_by"] == "synthetic-preparer"
    assert metadata["revision"]["decided_by"] == "synthetic-human"
    assert metadata["annotation"]["reviewer"] == "synthetic-human"
    status = query_operation(target=target, operation_id="baseline")
    assert status.state == "historical"
    assert status.receipt is None
    assert "does not verify historical content" in status.detail
    with pytest.raises(GoldTargetError, match="corrected in place"):
        approved_save(**kwargs)
    with pytest.raises(GoldTargetError, match="corrected in place"):
        prepare_save(
            draft_path=kwargs["draft_path"],
            material_root=material,
            work_dir=tmp_path / "new-work",
            target=target,
            expected_old_version=1,
        )
    # A second authorized correction reuses all paths and version 1.
    _edit(target, "614.0")
    _refresh(target, material)
    assert (
        target.read_current_answer(DOC)[1]
        .contributions[0]
        .quantities[0]
        .values[0]
        .value
        == "614.0"
    )


def test_bad_material_or_identity_cannot_refresh(tmp_path):
    target, material, _ = _saved(tmp_path)
    _edit(target)
    index_before = target.entry_path(DOC).read_bytes()
    (material / "paper.pdf").write_bytes(b"broken")
    with pytest.raises(GoldCheckError):
        _refresh(target, material)
    assert target.entry_path(DOC).read_bytes() == index_before
    with pytest.raises(GoldIntegrityError):
        target.read_current_answer(DOC)
    answer = _answer(target)
    answer.write_text(answer.read_text().replace("2501.00001", "2501.00002"))
    with pytest.raises(GoldIntegrityError, match="identity"):
        _refresh(target, material)


def test_original_path_symlink_is_rejected(tmp_path):
    target, material, _ = _saved(tmp_path)
    answer = _answer(target)
    answer.unlink()
    answer.symlink_to(tmp_path / "outside.json")
    with pytest.raises(GoldIntegrityError):
        _refresh(target, material)


def test_in_place_cli_and_no_start_command(tmp_path, capsys):
    target, material, _ = _saved(tmp_path)
    _edit(target)
    common = [
        "--target",
        str(target.root),
        "--document-id",
        DOC,
        "--material-root",
        str(material),
    ]
    assert (
        main(
            [
                "gold",
                "git-refresh",
                *common,
                "--revised-by",
                "preparer",
                "--decided-by",
                "human",
                "--basis",
                "test decision",
            ]
        )
        == 0
    )
    assert '"storage_mode": "git"' in capsys.readouterr().out
    with pytest.raises(SystemExit) as exc:
        main(["gold", "git-start", *common])
    assert exc.value.code == 2


@pytest.mark.parametrize("failed_name", ["metadata.json", f"{DOC}.json"])
def test_interrupted_refresh_closes_read_until_retry(
    tmp_path, monkeypatch, failed_name
):
    from hvs_catalog.evaluation import git_revision

    target, material, _ = _saved(tmp_path)
    _edit(target)
    real_write, real_json = git_revision._atomic_write, git_revision._atomic_write_json

    def fail(path, data):
        if path.name == failed_name:
            raise OSError("synthetic interrupted write")
        return real_write(path, data)

    def fail_json(path, data):
        if path.name == failed_name:
            raise OSError("synthetic interrupted write")
        return real_json(path, data)

    monkeypatch.setattr(git_revision, "_atomic_write", fail)
    monkeypatch.setattr(git_revision, "_atomic_write_json", fail_json)
    with pytest.raises(OSError):
        _refresh(target, material)
    with pytest.raises(GoldIntegrityError):
        target.read_current_answer(DOC)
    monkeypatch.setattr(git_revision, "_atomic_write", real_write)
    monkeypatch.setattr(git_revision, "_atomic_write_json", real_json)
    _refresh(target, material)
    assert (
        target.read_current_answer(DOC)[1]
        .contributions[0]
        .quantities[0]
        .values[0]
        .value
        == "613.0"
    )


def test_refresh_process_exit_then_fresh_process_recovery(tmp_path):
    target, material, _ = _saved(tmp_path)
    _edit(target, "616.0")
    crash = """
import os, sys
from hvs_catalog.evaluation import GoldTarget
from hvs_catalog.evaluation import git_revision
original = git_revision._atomic_write_json
def terminate(path, data):
    if path.name == sys.argv[2] + ".json":
        os._exit(77)
    original(path, data)
git_revision._atomic_write_json = terminate
git_revision.refresh_gold_in_place(target=GoldTarget(sys.argv[1]), document_id=sys.argv[2], material_root=sys.argv[3], revised_by="preparer", decided_by="human", basis="authorized synthetic correction")
"""
    args = [str(target.root), DOC, str(material)]
    assert (
        subprocess.run([sys.executable, "-c", crash, *args], check=False).returncode
        == 77
    )
    verify = """
import sys
from hvs_catalog.evaluation import GoldTarget, GoldIntegrityError
try:
    GoldTarget(sys.argv[1]).read_current_answer(sys.argv[2])
except GoldIntegrityError:
    sys.exit(0)
sys.exit(1)
"""
    assert (
        subprocess.run([sys.executable, "-c", verify, *args], check=False).returncode
        == 0
    )
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "hvs_catalog",
            "gold",
            "git-refresh",
            "--target",
            args[0],
            "--document-id",
            DOC,
            "--material-root",
            args[2],
            "--revised-by",
            "preparer",
            "--decided-by",
            "human",
            "--basis",
            "authorized synthetic correction",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert (
        target.read_current_answer(DOC)[1]
        .contributions[0]
        .quantities[0]
        .values[0]
        .value
        == "616.0"
    )

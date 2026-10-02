"""Real process death: reject incomplete saves; manual rollback, not replay."""

import hashlib
import json
import os
import shutil
import subprocess
import sys
import time

import pytest
from gold_save_helpers import approve_revision, flow, revise, snapshot, write_approval

from hvs_catalog.evaluation import (
    GoldIntegrityError,
    GoldTargetError,
    SaveApproval,
    approved_save,
    check_compact_gold_pair,
    read_compact_gold_pair,
)
from hvs_catalog.workspace import SpaceLock
from hvs_catalog.workspace.errors import WorkspaceBusyError

pytestmark = pytest.mark.integration

CHILD = """
import json, os, sys
from pathlib import Path
from hvs_catalog.evaluation import GoldTarget, SaveApproval, approved_save
pair, material, root, approval, window = sys.argv[1:]
approved_save(pair_path=pair, material_root=material, target=GoldTarget(root),
              approval=SaveApproval.model_validate_json(Path(approval).read_bytes()),
              fault_hooks={window: lambda: os._exit(9)})
raise AssertionError("missed explicit fault window")
"""


PRE_REPLACE_CHILD = """
import hashlib, json, os, sys
from pathlib import Path
from hvs_catalog.evaluation import GoldTarget, SaveApproval, approved_save, store
pair, material, root, approval, filename = sys.argv[1:]
original_replace = store.os.replace

def stop_before_replace(source, destination):
    if Path(destination) == Path(root) / filename:
        # _atomic_write has closed/fsynced the new temporary file, but has not
        # replaced the old file. Record the exact residue, not a glob selection.
        data = Path(source).read_bytes()
        assert data == (Path(root) / ".save-backup" / "new" / filename).read_bytes()
        print(json.dumps({"temporary_path": str(source),
                          "sha256": hashlib.sha256(data).hexdigest()}), flush=True)
        os._exit(19)
    return original_replace(source, destination)

store.os.replace = stop_before_replace
approved_save(pair_path=pair, material_root=material, target=GoldTarget(root),
              approval=SaveApproval.model_validate_json(Path(approval).read_bytes()))
raise AssertionError("missed precise pre-replace window")
"""


def child(args, tmp_path, window, *, script=CHILD, exit_code=9):
    approval = write_approval(args, tmp_path / "permission.json")
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            script,
            str(args["pair_path"]),
            str(args["material_root"]),
            str(args["target"].root),
            str(approval),
            window,
        ],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == exit_code, result.stdout + result.stderr
    return result


@pytest.mark.parametrize("revision", [False, True])
@pytest.mark.parametrize(
    "window",
    ["after_backup", "after_answer.json", "after_metadata.json", "after_approval.json"],
)
@pytest.mark.parametrize("restore", ["old", "new"])
def test_crash_refusal_and_complete_manual_restore(tmp_path, revision, window, restore):
    args = flow(tmp_path)
    if revision:
        approved_save(**args)
    old = snapshot(args["target"].root)
    if revision:
        args = approve_revision(revise(args))
    child(args, tmp_path, window)
    backup = args["target"].root / ".save-backup"
    assert backup.is_dir()
    assert {p.name for p in (backup / "new").iterdir()} == {
        "answer.json",
        "metadata.json",
        "approval.json",
    }
    if revision:
        assert {p.name: p.read_bytes() for p in (backup / "old").iterdir()} == {
            name: old[name]
            for name in ("answer.json", "metadata.json", "approval.json")
        }
    with pytest.raises(GoldIntegrityError, match="Interrupted"):
        args["target"].read_current()
    with pytest.raises(GoldIntegrityError, match="Interrupted"):
        approved_save(**args)
    # Explicit manual restoration while holding the documented existing lock.
    with SpaceLock(args["target"].root):
        chosen = backup / restore
        for name in ("answer.json", "metadata.json", "approval.json"):
            source = chosen / name
            destination = args["target"].root / name
            if source.exists():
                shutil.copyfile(source, destination)
            elif destination.exists():
                destination.unlink()  # synthetic empty rollback only
        if restore == "new" or revision:
            restored = read_compact_gold_pair(args["target"].root)
            approval = SaveApproval.model_validate_json(
                (args["target"].root / "approval.json").read_bytes()
            )
            assert (
                approval.scientific_review
                and approval.binding_sha256 == approval.binding.sha256
            )
            assert approval.binding.target_path == str(args["target"].root)
            for name, digest in (
                ("answer.json", approval.binding.answer_sha256),
                ("metadata.json", approval.binding.metadata_sha256),
            ):
                assert (
                    hashlib.sha256(
                        (args["target"].root / name).read_bytes()
                    ).hexdigest()
                    == digest
                )
            checked = check_compact_gold_pair(
                restored, material_root=args["material_root"]
            )
            assert checked.blocking_count == 0
            assert sorted(approval.accepted_pending) == sorted(checked.pending_ids())
        shutil.rmtree(backup)
    # A fresh process verifies the restored target, not the prior process cache.
    verify = subprocess.run(
        [
            sys.executable,
            "-m",
            "hvs_catalog",
            "gold",
            "read",
            "--target",
            str(args["target"].root),
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert verify.returncode == (2 if restore == "old" and not revision else 0), (
        verify.stderr
    )
    if restore == "new":
        assert (
            json.loads(verify.stdout)["pair_sha256"]
            == args["approval"].binding.pair_sha256
        )
        assert approved_save(**args).already_present
    elif revision:
        assert (
            snapshot(args["target"].root) == old
        )  # uncommitted previous result retained
    else:
        assert args["target"].read_current() is None


@pytest.mark.parametrize("restore", ["old", "new"])
def test_pre_replace_residue_preserved_and_manually_moved_out(tmp_path, restore):
    args = flow(tmp_path)
    approved_save(**args)  # Previous result is deliberately never Git-committed.
    root = args["target"].root
    old = snapshot(root)
    old_approval = args["target"].read_current()[1]
    old_fingerprint = args["target"].fingerprint()
    args = approve_revision(revise(args))
    result = child(
        args, tmp_path, "metadata.json", script=PRE_REPLACE_CHILD, exit_code=19
    )
    evidence = json.loads(result.stdout)
    residue = root / os.path.basename(evidence["temporary_path"])
    assert str(residue) == evidence["temporary_path"]
    assert residue.name.startswith(".metadata.json.tmp-")
    residue_bytes = residue.read_bytes()
    assert hashlib.sha256(residue_bytes).hexdigest() == evidence["sha256"]
    backup = root / ".save-backup"
    names = ("answer.json", "metadata.json", "approval.json")
    assert {name: (backup / "old" / name).read_bytes() for name in names} == {
        name: old[name] for name in names
    }
    assert residue_bytes == (backup / "new" / "metadata.json").read_bytes()
    assert (root / "metadata.json").read_bytes() == old["metadata.json"]
    assert (root / "answer.json").read_bytes() == (
        backup / "new" / "answer.json"
    ).read_bytes()
    with pytest.raises(GoldTargetError, match="layout"):
        args["target"].read_current()
    with pytest.raises(GoldTargetError, match="layout"):
        approved_save(**args)

    # Follow the documentation: hold the existing lock, preserve the complete
    # incident, restore/check one full set, then move the confirmed residue out.
    incident = tmp_path / "incident"
    incident.mkdir()
    before_recovery = snapshot(root)
    expected = dict(old)
    expected_approval = old_approval if restore == "old" else args["approval"]
    with SpaceLock(root):
        shutil.copytree(root, incident / "target-before-recovery")
        assert snapshot(incident / "target-before-recovery") == before_recovery
        for name in names:
            expected[name] = (backup / restore / name).read_bytes()
            shutil.copyfile(backup / restore / name, root / name)
        restored = read_compact_gold_pair(root)
        approval = SaveApproval.model_validate_json(
            (root / "approval.json").read_bytes()
        )
        assert approval == expected_approval and approval.scientific_review
        assert approval.binding_sha256 == approval.binding.sha256
        assert approval.binding.target_path == str(root)
        for name, digest in (
            ("answer.json", approval.binding.answer_sha256),
            ("metadata.json", approval.binding.metadata_sha256),
        ):
            assert hashlib.sha256((root / name).read_bytes()).hexdigest() == digest
        checked = check_compact_gold_pair(restored, material_root=args["material_root"])
        assert checked.blocking_count == 0
        assert sorted(approval.accepted_pending) == sorted(checked.pending_ids())
        # Restoring valid files alone cannot bypass the unknown-file protection.
        with pytest.raises(GoldTargetError, match="layout"):
            args["target"].read_current()
        assert residue.read_bytes() == residue_bytes
        assert (
            snapshot(root)[str(residue.relative_to(root))]
            == before_recovery[residue.name]
        )
        retained_dir = incident / "confirmed-atomic-write-residue"
        retained_dir.mkdir()
        retained = retained_dir / residue.name
        assert not retained.exists()
        residue.rename(retained)  # Exact identified file, preserved, not deleted.
        assert retained.read_bytes() == residue_bytes and not residue.exists()
        with pytest.raises(GoldIntegrityError, match="Interrupted"):
            args["target"].read_current()
        shutil.rmtree(backup)  # Only the new synthetic invocation's verified backup.

    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    verify = subprocess.run(
        [sys.executable, "-m", "hvs_catalog", "gold", "read", "--target", str(root)],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert verify.returncode == 0, verify.stdout + verify.stderr
    fingerprint = json.loads(verify.stdout)["pair_sha256"]
    assert fingerprint == expected_approval.binding.pair_sha256
    if restore == "old":
        assert fingerprint == old_fingerprint
    assert snapshot(root) == expected  # Old rollback restores all uncommitted bytes.
    assert retained.read_bytes() == residue_bytes
    assert snapshot(incident / "target-before-recovery") == before_recovery


def test_response_lost_completed_retry_no_second_result(tmp_path):
    args = flow(tmp_path)
    child(args, tmp_path, "after_complete")
    before = snapshot(args["target"].root)
    assert approved_save(**args).already_present
    assert before == snapshot(args["target"].root)


def test_other_process_lock_busy_no_queue(tmp_path):
    args = flow(tmp_path)
    args["target"].initialize()
    signal = tmp_path / "held"
    release = tmp_path / "release"
    code = """
import sys, time
from pathlib import Path
from hvs_catalog.workspace import SpaceLock
with SpaceLock(Path(sys.argv[1])):
    Path(sys.argv[2]).write_text("held")
    while not Path(sys.argv[3]).exists():
        time.sleep(.01)
"""
    proc = subprocess.Popen(
        [
            sys.executable,
            "-c",
            code,
            str(args["target"].root),
            str(signal),
            str(release),
        ],
        cwd=tmp_path,
    )
    try:
        deadline = time.monotonic() + 30
        while not signal.exists():
            assert proc.poll() is None and time.monotonic() < deadline
            time.sleep(0.01)
        with pytest.raises(WorkspaceBusyError):
            approved_save(**args)
    finally:
        release.write_text("done")
        proc.wait(timeout=30)
    assert proc.returncode == 0
    assert not approved_save(**args).already_present

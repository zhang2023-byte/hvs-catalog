"""Archive integrity checks retain every expected hash and detect file changes."""

import hashlib
import json
import os
from types import SimpleNamespace

import pytest

from hvs_catalog.records.file_checks import file_content, verification_scope
from hvs_catalog.workspace import WorkspaceIntegrityError, archives, layout
from hvs_catalog.workspace.store import verify_artifact_refs


def test_archive_references_check_each_hash_and_detect_same_size_changes(tmp_path):
    path = tmp_path / "response.json"
    original = b'{"value": "first"}'
    path.write_bytes(original)
    digest = hashlib.sha256(original).hexdigest()

    def record(expected):
        return SimpleNamespace(
            record_id="archive-test",
            revision=1,
            model_dump=lambda **_: {
                "response": {"path": "response.json", "sha256": expected}
            },
        )

    valid = record(digest)
    with verification_scope():
        assert verify_artifact_refs([valid], tmp_path) == []
        assert len(verify_artifact_refs([valid, record("0" * 64)], tmp_path)) == 1
        stat = path.stat()
        path.write_bytes(b'{"value": "other"}')
        os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns))
        assert len(verify_artifact_refs([valid], tmp_path)) == 1
    path.write_bytes(original)
    assert verify_artifact_refs([valid], tmp_path) == []


def archive_record(refs, revision=1):
    return {"record_id": "archive-test", "revision": revision, "artifacts": refs}


def save_ref(root, name, value):
    data = json.dumps(value).encode() if isinstance(value, dict) else value
    (root / name).write_bytes(data)
    return {"path": name, "sha256": hashlib.sha256(data).hexdigest()}


def test_repeated_history_checks_distinct_references_once(tmp_path, monkeypatch):
    ref = save_ref(tmp_path, "source.txt", b"fixed source")
    records = [archive_record([ref] * 20, revision) for revision in range(1, 101)]
    calls = []
    original = layout.resolve_within

    def resolve(root, relative):
        calls.append(relative)
        return original(root, relative)

    monkeypatch.setattr(layout, "resolve_within", resolve)
    assert verify_artifact_refs(records, tmp_path) == []
    # History size must not multiply filesystem traversal for one immutable file.
    assert 0 < len(calls) <= 3
    (tmp_path / ref["path"]).unlink()
    errors = verify_artifact_refs(records, tmp_path)
    assert len(errors) == 2000
    assert "v1 " in errors[0] and "v100 " in errors[-1]
    assert all("缺失或不可读" in error for error in errors)


def test_each_expected_hash_keeps_its_own_diagnostic(tmp_path):
    ref = save_ref(tmp_path, "source.txt", b"fixed source")
    bad = {**ref, "sha256": "0" * 64}
    records = [archive_record([ref, bad], 1), archive_record([bad, ref], 2)]
    assert verify_artifact_refs(records, tmp_path) == [
        f"记录 archive-test v{revision} 引用的 source.txt 哈希与记录不符"
        for revision in (1, 2)
    ]


@pytest.mark.parametrize("change", ["content", "missing", "symlink"])
def test_shared_dependency_changes_rechecked_within_scope(tmp_path, change):
    child = save_ref(tmp_path, "source.txt", b"first")
    ref = save_ref(
        tmp_path,
        "request.json",
        {"format": "hvs.shared-json", "version": 1, "value": {"$hvs_text": child}},
    )
    records = [archive_record([ref], revision) for revision in (1, 2)]
    with verification_scope():
        assert verify_artifact_refs(records, tmp_path) == []
        path = tmp_path / child["path"]
        stat = path.stat()
        if change == "content":
            path.write_bytes(b"other")
            os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns))
        else:
            path.unlink()
            if change == "symlink":
                (tmp_path / "other.txt").write_bytes(b"first")
                path.symlink_to(tmp_path / "other.txt")
        if change == "symlink":
            with pytest.raises(WorkspaceIntegrityError, match="符号链接"):
                verify_artifact_refs(records, tmp_path)
            return
        errors = verify_artifact_refs(records, tmp_path)
        assert len(errors) == 2
        assert "v1 " in errors[0] and "v2 " in errors[1]


@pytest.mark.parametrize("kind", ["escape", "symlink", "directory_symlink"])
def test_repeated_refs_cannot_bypass_path_checks(tmp_path, kind):
    root = tmp_path / "space"
    root.mkdir()
    ref = save_ref(tmp_path, "outside.txt", b"fixed source")
    if kind == "escape":
        ref["path"] = "../outside.txt"
    elif kind == "symlink":
        (root / "source.txt").symlink_to(tmp_path / "outside.txt")
        ref["path"] = "source.txt"
    else:
        (root / "sources").symlink_to(tmp_path, target_is_directory=True)
        ref["path"] = "sources/outside.txt"
    errors = verify_artifact_refs([archive_record([ref, ref])], root)
    assert len(errors) == 2 and errors[0] == errors[1]


def test_reference_results_are_not_reused_across_roots(tmp_path):
    left, right = tmp_path / "left", tmp_path / "right"
    left.mkdir()
    right.mkdir()
    ref = save_ref(left, "source.txt", b"first")
    save_ref(right, "source.txt", b"other")
    with verification_scope():
        assert verify_artifact_refs([archive_record([ref])], left) == []
        assert len(verify_artifact_refs([archive_record([ref])], right)) == 1


def test_shared_children_are_checked_once_across_distinct_parents(
    tmp_path, monkeypatch
):
    child = save_ref(tmp_path, "source.txt", b"fixed source")
    refs = [
        save_ref(
            tmp_path,
            f"request-{i}.json",
            {"format": "hvs.shared-json", "version": 1, "value": {"child": child}},
        )
        for i in range(20)
    ]
    calls = []
    original = archives.resolve_within

    def resolve(root, relative):
        calls.append(relative)
        return original(root, relative)

    monkeypatch.setattr(archives, "resolve_within", resolve)
    assert verify_artifact_refs([archive_record(refs)], tmp_path) == []
    assert calls.count("source.txt") == 1
    bad = save_ref(
        tmp_path,
        "bad.json",
        {
            "format": "hvs.shared-json",
            "version": 1,
            "value": {"child": {**child, "sha256": "0" * 64}},
        },
    )
    errors = verify_artifact_refs([archive_record([*refs, bad])], tmp_path)
    assert len(errors) == 1 and "bad.json" in errors[0]


def test_file_content_cache_detects_symlink_retarget(tmp_path):
    first, second = tmp_path / "first.txt", tmp_path / "second.txt"
    first.write_bytes(b"first")
    second.write_bytes(b"other")
    link = tmp_path / "link.txt"
    link.symlink_to(first)
    with verification_scope():
        assert file_content(link)[0] == b"first"
        link.unlink()
        link.symlink_to(second)
        assert file_content(link)[0] == b"other"


def test_schema_generation_is_scoped_and_returned_values_are_independent(monkeypatch):
    from hvs_catalog.records.quantity_input import (
        QuantitySubmission,
        quantity_input_schema,
    )

    expected = quantity_input_schema()
    original = QuantitySubmission.model_json_schema
    calls = []

    def generate(cls, *args, **kwargs):
        calls.append(cls)
        return original(*args, **kwargs)

    monkeypatch.setattr(QuantitySubmission, "model_json_schema", classmethod(generate))
    with verification_scope():
        changed = quantity_input_schema()
        changed["$defs"]["Value"]["properties"]["unit"]["description"] = "changed"
        assert quantity_input_schema() == expected
        assert calls == [QuantitySubmission]
    with verification_scope():
        assert quantity_input_schema() == expected
        assert calls == [QuantitySubmission, QuantitySubmission]


@pytest.mark.parametrize(
    "change",
    [
        "snapshot",
        "roster",
        "source",
        "request",
        "response",
        "shared",
        "missing",
        "symlink",
    ],
)
def test_snapshot_reuse_rechecks_files_and_record_inputs(prepared, monkeypatch, change):
    from test_quantities import quantities, run_full
    from test_roster import submission

    from hvs_catalog.records import validate
    from hvs_catalog.records.quantities import QuantityRecord
    from hvs_catalog.records.roster import RosterRecord
    from hvs_catalog.records.source import SourcePackageRecord
    from hvs_catalog.workspace.store import record_closure

    space = prepared[0]
    handle, _, _, _ = run_full(prepared, [submission(), quantities()])
    run = space.store().read_record_current(handle.run_ref.record_id)
    records = record_closure([run], space.store().read_record)
    snapshot = next(r for r in records if isinstance(r, QuantityRecord))
    roster = next(r for r in records if isinstance(r, RosterRecord))
    original = validate._check_quantity_snapshot
    calls = []

    def check(*args):
        calls.append(args[0].record_id)
        return original(*args)

    monkeypatch.setattr(validate, "_check_quantity_snapshot", check)
    with verification_scope():
        validate.validate_collection(records, space.root)
        validate.validate_collection(records, space.root)
        assert len(calls) == 1
    with verification_scope():
        validate.validate_collection(records, space.root)
        assert len(calls) == 2
        response = space.root / snapshot.data.response_ref.path
        request = space.root / snapshot.data.response_ref.path.replace(
            "/responses/", "/requests/", 1
        )
        if change == "snapshot":
            values = snapshot.data.quantities[0].values
            values[0] = values[0].model_copy(update={"value": "999"})
        elif change == "roster":
            ids = roster.data.stars[0].identifiers
            ids[0] = ids[0].model_copy(update={"value": "Other"})
        elif change == "source":
            source = next(r for r in records if isinstance(r, SourcePackageRecord))
            file = source.data.files[0]
            alternate = file.path + ".copy"
            (space.root / alternate).write_bytes((space.root / file.path).read_bytes())
            source.data.files[0] = file.model_copy(update={"path": alternate})
            validate.validate_collection(records, space.root)
            assert len(calls) == 3
            return
        elif change == "missing":
            request.unlink()
        elif change == "symlink":
            target = request.with_suffix(".saved")
            request.rename(target)
            request.symlink_to(target)
        elif change in {"request", "response"}:
            path = request if change == "request" else response
            payload = json.loads(path.read_bytes())
            payload["value"]["changed"] = True
            path.write_text(json.dumps(payload))
        else:
            ref = {
                "path": str(request.relative_to(space.root)),
                "sha256": hashlib.sha256(request.read_bytes()).hexdigest(),
            }
            dependencies = archives.shared_dependencies(space.root, ref)
            child = next(d for d in dependencies if d["path"].endswith(".txt"))
            path = space.root / child["path"]
            stat, data = path.stat(), path.read_bytes()
            path.write_bytes(b"!" + data[1:])
            os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns))
        with pytest.raises(
            (validate.CollectionValidationError, WorkspaceIntegrityError)
        ):
            validate.validate_collection(records, space.root)

"""Archive integrity checks retain every expected hash and detect file changes."""

import hashlib
import os
from types import SimpleNamespace

from hvs_catalog.records.file_checks import verification_scope
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

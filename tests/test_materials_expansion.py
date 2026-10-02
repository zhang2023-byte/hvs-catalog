"""Synthetic source-package expansion and text-decoding tests."""

from __future__ import annotations

import io
import tarfile

import pytest
from materials_helpers import (
    BACKING_CSV,
    MEMBERS,
    make_tar_gz,
    prepared_space,
    synthetic_package,
)

from hvs_catalog.materials.expansion import (
    MaterialExpansionError,
    expand_source_package,
)


def test_expand_members_publishes_content_addressed_files(tmp_path):
    space, _ = prepared_space(tmp_path)
    package = synthetic_package()
    expanded = expand_source_package(space, package)
    names = {m.name for m in expanded.members}
    assert names == set(MEMBERS)
    by_name = {m.name: m for m in expanded.members}
    csv_member = by_name["backing.csv"]
    assert csv_member.sha256 == __import__("hashlib").sha256(BACKING_CSV).hexdigest()
    # Publish content-addressed members under sources/ and read them back.
    from hvs_catalog.records.common import ArtifactRef

    data = space.read_file(
        ArtifactRef(
            path=f"sources/materials/members/{csv_member.sha256}.bin",
            sha256=csv_member.sha256,
        )
    )
    assert data == BACKING_CSV
    # Decode text members while preserving their original expression.
    assert by_name["backing.csv"].text is not None
    assert by_name["backing.csv"].encoding == "utf-8"
    # Retain nontext members as binary.
    fits = by_name["payloads/data.fits"]
    assert fits.text is None
    assert fits.decode_note is None


def test_latin1_member_derives_utf8_mapping(tmp_path):
    raw = "Gravi%t\\'e für ölaf\n".encode("latin-1")
    package = make_tar_gz(
        {
            "main.tex": b"\\documentclass{article}\n\\begin{document}x\n\\end{document}\n",
            "notes_l1.tex": raw,
        }
    )
    space, _ = prepared_space(tmp_path, package)
    # Without a declaration or fixed choice, do not guess latin-1; leave the member unprocessed.
    strict = expand_source_package(space, package)
    strict_member = next(m for m in strict.members if m.name == "notes_l1.tex")
    assert strict_member.text is None
    assert strict_member.decode_note and "保持未处理" in strict_member.decode_note
    # Derive mapped text only after the operator fixes latin-1 in the input snapshot.
    expanded = expand_source_package(space, package, fixed_encoding="latin-1")
    member = next(m for m in expanded.members if m.name == "notes_l1.tex")
    assert member.encoding == "latin-1"
    assert member.text is not None
    # Derived UTF-8 lines correspond one-to-one with original bytes.
    derived_bytes = member.derived_text.encode("utf-8")
    assert derived_bytes == raw.decode("latin-1").encode("utf-8")
    assert (
        member.derived_sha256 == __import__("hashlib").sha256(derived_bytes).hexdigest()
    )
    # The original byte hash remains unchanged.
    assert member.sha256 == __import__("hashlib").sha256(raw).hexdigest()


def test_utf16_member_stays_unparsed(tmp_path):
    raw = "document".encode("utf-16")
    package = make_tar_gz({"main.tex": b"\\documentclass{article}x", "u16.tex": raw})
    space, _ = prepared_space(tmp_path, package)
    expanded = expand_source_package(space, package)
    member = next(m for m in expanded.members if m.name == "u16.tex")
    assert member.text is None
    assert member.decode_note and "UTF-16" in member.decode_note


def test_single_tex_package_member_named_main(tmp_path):
    payload = b"\\documentclass{article}\\begin{document}single\\end{document}"
    space, _ = prepared_space(tmp_path, payload)
    expanded = expand_source_package(space, payload)
    assert [m.name for m in expanded.members] == ["main.tex"]
    assert expanded.package_format == "single_tex"


def test_unsafe_package_rejected_without_expansion(tmp_path):
    # Build a tar with an absolute-path member: inspect_package rejects it and expand reports an engineering error.
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w") as archive:
        info = tarfile.TarInfo("/etc/evil.tex")
        info.size = 3
        archive.addfile(info, io.BytesIO(b"xxx"))
    space, _ = prepared_space(tmp_path)
    with pytest.raises(MaterialExpansionError, match="安全检查"):
        expand_source_package(space, raw.getvalue())
    # Write no member files.
    members_dir = space.root / "sources" / "materials" / "members"
    assert not members_dir.exists() or not any(members_dir.iterdir())


def test_declared_unsupported_encoding_stays_unparsed(tmp_path):
    """Leave members unprocessed when their declared encoding is unsupported; do not guess
    latin-1.
    """
    raw = b"% !TeX encoding = windows-1252\nFIC-A\x96FIC-B\n"
    package = make_tar_gz(
        {
            "main.tex": b"\\documentclass{article}x",
            "cp1252.tex": raw,
        }
    )
    space, _ = prepared_space(tmp_path, package)
    expanded = expand_source_package(space, package)
    member = next(m for m in expanded.members if m.name == "cp1252.tex")
    assert member.text is None
    assert member.encoding is None
    assert member.decode_note and "windows-1252" in member.decode_note


def test_declared_latin1_encoding_derives_mapping(tmp_path):
    """A member explicitly declaring latin-1 derives mapped UTF-8 text."""
    raw = b"% !TeX encoding = latin-1\nGravi%t f\xfcr \xf6laf\n"
    package = make_tar_gz(
        {
            "main.tex": b"\\documentclass{article}x",
            "declared.tex": raw,
        }
    )
    space, _ = prepared_space(tmp_path, package)
    expanded = expand_source_package(space, package)
    member = next(m for m in expanded.members if m.name == "declared.tex")
    assert member.encoding == "latin-1"
    assert member.text is not None
    assert member.derived_sha256
    space.close()

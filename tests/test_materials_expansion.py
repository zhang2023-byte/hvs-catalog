"""来源包展开与文本解码的合成测试。"""

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
    # 成员按内容寻址发布到 sources/，可读回
    from hvs_catalog.records.common import ArtifactRef

    data = space.read_file(
        ArtifactRef(
            path=f"sources/materials/members/{csv_member.sha256}.bin",
            sha256=csv_member.sha256,
        )
    )
    assert data == BACKING_CSV
    # 文本成员可解码并保留原表达
    assert by_name["backing.csv"].text is not None
    assert by_name["backing.csv"].encoding == "utf-8"
    # 非文本成员按二进制保留
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
    # 未声明且未固定选择时不猜 latin-1，保持未处理。
    strict = expand_source_package(space, package)
    strict_member = next(m for m in strict.members if m.name == "notes_l1.tex")
    assert strict_member.text is None
    assert strict_member.decode_note and "保持未处理" in strict_member.decode_note
    # 操作者固定选择 latin-1（进入固定输入）才按映射派生。
    expanded = expand_source_package(space, package, fixed_encoding="latin-1")
    member = next(m for m in expanded.members if m.name == "notes_l1.tex")
    assert member.encoding == "latin-1"
    assert member.text is not None
    # 派生 UTF-8 文本行数与原字节一一对应
    derived_bytes = member.derived_text.encode("utf-8")
    assert derived_bytes == raw.decode("latin-1").encode("utf-8")
    assert (
        member.derived_sha256 == __import__("hashlib").sha256(derived_bytes).hexdigest()
    )
    # 原字节哈希仍是原字节
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
    # 构造带绝对路径成员的 tar：inspect_package 拒绝，expand 报工程错误。
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w") as archive:
        info = tarfile.TarInfo("/etc/evil.tex")
        info.size = 3
        archive.addfile(info, io.BytesIO(b"xxx"))
    space, _ = prepared_space(tmp_path)
    with pytest.raises(MaterialExpansionError, match="安全检查"):
        expand_source_package(space, raw.getvalue())
    # 不写入任何成员文件
    members_dir = space.root / "sources" / "materials" / "members"
    assert not members_dir.exists() or not any(members_dir.iterdir())


def test_declared_unsupported_encoding_stays_unparsed(tmp_path):
    """成员自身声明的编码不在受支持集时保持未处理，不猜 latin-1。"""
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
    """成员自身声明 latin-1 时按声明派生 UTF-8 映射。"""
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

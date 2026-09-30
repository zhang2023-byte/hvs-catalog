"""源码包工程安全检查的合成测试（Lane B：archive 模块）。

全部离线：包字节在测试内构造（tarfile/gzip/纯文本），不触网、不落
盘解压。护栏常量按测试需要缩小（monkeypatch），生产默认值不改动。
"""

from __future__ import annotations

import io
import tarfile

import pytest

import hvs_catalog.pipeline.acquisition.archive as archive_mod
from hvs_catalog.pipeline.acquisition.archive import (
    PackageIntegrityError,
    UnsupportedPackageError,
    bounded_gunzip,
    inspect_package,
    sniff_package_format,
)


def make_gzip_tar(members: dict[str, bytes]) -> bytes:
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w:gz") as archive:
        for name, data in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
    return raw.getvalue()


def make_plain_tar(members: dict[str, bytes]) -> bytes:
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w") as archive:
        for name, data in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
    return raw.getvalue()


def make_member(name: str, data: bytes, *, type_: bytes, linkname: str = "") -> bytes:
    """构造含非常规成员的单成员 tar（type 用原始字节指定）。"""
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w") as archive:
        info = tarfile.TarInfo(name)
        info.type = type_
        info.linkname = linkname
        info.size = 0 if type_ != tarfile.REGTYPE else len(data)
        if type_ == tarfile.REGTYPE:
            archive.addfile(info, io.BytesIO(data))
        else:
            archive.addfile(info)
    return raw.getvalue()


SINGLE_TEX = b"\\documentclass{article}\n\\begin{document}\nhello\n\\end{document}\n"


# ---------------------------------------------------------------- 形态识别


def test_gzip_tar_inspected_with_member_hashes():
    payload = make_gzip_tar({"main.tex": b"a", "data/table.dat": b"12345"})
    inspection = inspect_package(payload)
    assert inspection.format == "tar_gzip"
    assert [member.name for member in inspection.members] == [
        "main.tex",
        "data/table.dat",
    ]
    import hashlib

    assert inspection.members[0].sha256 == hashlib.sha256(b"a").hexdigest()
    assert inspection.members[1].size == 5
    assert inspection.total_decompressed == 6
    assert inspection.members[0].media_type == "text/x-tex"
    assert inspection.members[1].media_type == "application/octet-stream"


def test_plain_tar_inspected():
    payload = make_plain_tar({"main.tex": SINGLE_TEX})
    inspection = inspect_package(payload)
    assert inspection.format == "tar_plain"
    assert len(inspection.members) == 1
    assert inspection.total_decompressed == len(SINGLE_TEX)


def test_single_tex_file_inspected():
    inspection = inspect_package(SINGLE_TEX)
    assert inspection.format == "single_tex"
    assert inspection.members[0].name == "main.tex"
    assert inspection.members[0].media_type == "text/x-tex"


def test_directory_entries_skipped_but_files_kept():
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w") as archive:
        directory = tarfile.TarInfo("subdir/")
        directory.type = tarfile.DIRTYPE
        archive.addfile(directory)
        info = tarfile.TarInfo("subdir/main.tex")
        info.size = 3
        archive.addfile(info, io.BytesIO(b"abc"))
    inspection = inspect_package(raw.getvalue())
    assert [member.name for member in inspection.members] == ["subdir/main.tex"]


# ---------------------------------------------------------------- 不支持形态


def test_pdf_only_rejected_as_unsupported():
    with pytest.raises(UnsupportedPackageError, match="PDF"):
        inspect_package(b"%PDF-1.4 fake pdf bytes")


def test_html_rejected_as_unsupported():
    with pytest.raises(UnsupportedPackageError, match="HTML"):
        inspect_package(b"<!DOCTYPE html><html><body>err</body></html>")
    with pytest.raises(UnsupportedPackageError, match="HTML"):
        inspect_package(b"\xef\xbb\xbf<html><body></body></html>")


def test_binary_single_file_rejected_as_unsupported():
    with pytest.raises(UnsupportedPackageError, match="NUL"):
        inspect_package(b"\x00\x01\x02binary")


def test_sniff_format_variants():
    assert sniff_package_format(SINGLE_TEX) == "single_tex"
    assert sniff_package_format(make_gzip_tar({"a": b"b"})) == "tar_gzip"
    with pytest.raises(UnsupportedPackageError):
        sniff_package_format(b"%PDF-x")


# ---------------------------------------------------------------- 安全拒绝


@pytest.mark.parametrize(
    "name",
    [
        "../escape.tex",
        "a/../../escape.tex",
        "/etc/passwd",
        "C:\\evil.tex",
        "a\\b.tex",
        "C:evil.tex",
        "a//b.tex",
        "a/./b.tex",
        "bad name.tex",
        "~home.tex",
    ],
)
def test_unsafe_member_paths_rejected(name):
    payload = make_plain_tar({name: b"x"})
    with pytest.raises(PackageIntegrityError, match="路径"):
        inspect_package(payload)


def test_symlink_member_rejected():
    payload = make_member(
        "link.tex", b"", type_=tarfile.SYMTYPE, linkname="/etc/passwd"
    )
    with pytest.raises(PackageIntegrityError, match="链接"):
        inspect_package(payload)


def test_hardlink_member_rejected():
    payload = make_member("hard.tex", b"", type_=tarfile.LNKTYPE, linkname="main.tex")
    with pytest.raises(PackageIntegrityError, match="链接"):
        inspect_package(payload)


def test_device_member_rejected():
    payload = make_member("dev", b"", type_=tarfile.CHRTYPE)
    with pytest.raises(PackageIntegrityError, match="设备"):
        inspect_package(payload)


def test_duplicate_member_names_rejected():
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w") as archive:
        for _ in range(2):
            info = tarfile.TarInfo("main.tex")
            info.size = 1
            archive.addfile(info, io.BytesIO(b"x"))
    with pytest.raises(PackageIntegrityError, match="重名"):
        inspect_package(raw.getvalue())


def test_case_alias_members_rejected():
    payload = make_plain_tar({"Main.tex": b"x", "main.tex": b"y"})
    with pytest.raises(PackageIntegrityError, match="别名"):
        inspect_package(payload)


def test_empty_package_rejected():
    with pytest.raises(PackageIntegrityError, match="空"):
        inspect_package(b"")


def test_archive_without_regular_files_rejected():
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w") as archive:
        directory = tarfile.TarInfo("only-dir/")
        directory.type = tarfile.DIRTYPE
        archive.addfile(directory)
    with pytest.raises(PackageIntegrityError, match="没有任何常规文件"):
        inspect_package(raw.getvalue())


# ---------------------------------------------------------------- 工程护栏


def test_member_count_cap(monkeypatch):
    monkeypatch.setattr(archive_mod, "MAX_ARCHIVE_MEMBERS", 2)
    payload = make_plain_tar({"a.tex": b"1", "b.tex": b"2", "c.tex": b"3"})
    with pytest.raises(PackageIntegrityError, match="物理头部数"):
        inspect_package(payload)


def test_total_decompressed_cap(monkeypatch):
    monkeypatch.setattr(archive_mod, "MAX_TOTAL_DECOMPRESSED_BYTES", 8)
    payload = make_plain_tar({"a.tex": b"12345", "b.tex": b"12345"})
    with pytest.raises(PackageIntegrityError, match="总量"):
        inspect_package(payload)


def test_package_size_cap(monkeypatch):
    monkeypatch.setattr(archive_mod, "MAX_PACKAGE_BYTES", 4)
    with pytest.raises(PackageIntegrityError, match="原始字节"):
        inspect_package(SINGLE_TEX)


def test_gzip_bomb_bounded_before_expansion(monkeypatch):
    """gzip 炸弹在流式有界解压处即被拦截：上限先于展开，而非解压完再检查。"""
    monkeypatch.setattr(archive_mod, "MAX_TOTAL_DECOMPRESSED_BYTES", 10)
    payload = make_gzip_tar({"a": b"123456", "b": b"123456"})
    with pytest.raises(PackageIntegrityError, match="有界上限"):
        inspect_package(payload)


def test_total_cap_boundary_exact_fit_accepted(monkeypatch):
    """解压总量恰好等于上限时放行：护栏是严格大于，不是大于等于。"""
    monkeypatch.setattr(archive_mod, "MAX_TOTAL_DECOMPRESSED_BYTES", 10)
    payload = make_plain_tar({"a": b"12345", "b": b"12345"})
    inspection = inspect_package(payload)
    assert inspection.total_decompressed == 10


def test_gzip_single_tex_official_form_supported():
    """arXiv 官方形态：gzip 压缩的单 TeX 文件（并非每个 gzip 都是 tar）。"""
    import gzip
    import hashlib

    payload = gzip.compress(SINGLE_TEX)
    inspection = inspect_package(payload)
    assert inspection.format == "single_tex_gzip"
    assert len(inspection.members) == 1
    member = inspection.members[0]
    assert member.name == "main.tex"
    assert member.size == len(SINGLE_TEX)
    assert member.sha256 == hashlib.sha256(SINGLE_TEX).hexdigest()
    assert inspection.total_decompressed == len(SINGLE_TEX)


def test_gzip_trailing_data_rejected():
    import gzip

    payload = gzip.compress(b"just text") + b"trailing junk"
    with pytest.raises(PackageIntegrityError, match="尾随"):
        inspect_package(payload)


def test_gzip_multimember_rejected():
    import gzip

    payload = gzip.compress(b"member one text") + gzip.compress(b"member two")
    with pytest.raises(PackageIntegrityError, match="尾随"):
        inspect_package(payload)


def test_truncated_gzip_rejected():
    import gzip

    payload = gzip.compress(b"some tex text")[:-4]
    with pytest.raises(PackageIntegrityError, match="截断"):
        inspect_package(payload)


def test_gzip_pdf_inside_rejected():
    """gzip 里包着 PDF-only 也按形态不支持拒绝，且字节可被调用方保留。"""
    import gzip

    payload = gzip.compress(b"%PDF-1.4 fake pdf body")
    with pytest.raises(UnsupportedPackageError, match="PDF"):
        inspect_package(payload)


def test_gzip_bomb_allocation_bounded_before_expansion():
    """解压炸弹的内存分配受上限约束：峰值远小于展开后的完整尺寸。

    审核反例：约 4 KiB 的合成 gzip 可展开为 4 MiB；若先解压再检查，
    拒绝前的内存峰值接近完整展开。这里用 tracemalloc 实测峰值必须
    保持在与上限同量级，而不是与展开尺寸同量级。
    """
    import gzip as gzip_mod
    import tracemalloc

    payload = gzip_mod.compress(b"A" * (4 * 1024 * 1024))
    assert len(payload) < 64 * 1024  # 压缩载荷确实远小于展开尺寸
    tracemalloc.start()
    try:
        with pytest.raises(PackageIntegrityError, match="有界上限"):
            bounded_gunzip(payload, 64 * 1024)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    # 峰值与 64 KiB 上限同量级（含解释器/读取缓冲开销），必须远小于
    # 4 MiB 的完整展开；放宽到 8×上限以防环境抖动，但比旧实现的
    # ~2.5×展开尺寸小两个数量级。
    assert peak < 64 * 1024 * 8, f"解压拒绝前分配了 {peak} 字节"


def test_gzip_output_at_cap_boundary_accepted():
    """输出恰好等于上限的 gzip 正常解出：护栏是分配约束不是差一错误。"""
    import gzip as gzip_mod

    body = b"B" * 1000
    payload = gzip_mod.compress(body)
    assert bounded_gunzip(payload, 1000) == body


def test_tar_member_cap_stops_parsing_before_all_headers(monkeypatch):
    """成员数上限在逐项解析处立即停止：不会先构造全部 TarInfo。

    审核反例：上限设 2 时旧实现先 getmembers() 构造 100 项才拒绝。
    这里直接统计 TarInfo 构造次数：拒绝发生时构造数必须与上限同量级。
    """

    members = {f"file{i:03d}.tex": b"x" * 10 for i in range(200)}
    payload = make_plain_tar(members)
    monkeypatch.setattr(archive_mod, "MAX_ARCHIVE_MEMBERS", 5)
    constructed = 0
    original_frombuf = tarfile.TarInfo.frombuf.__func__

    def counting_frombuf(cls, buf, encoding, errors):
        nonlocal constructed
        constructed += 1
        return original_frombuf(cls, buf, encoding, errors)

    monkeypatch.setattr(tarfile.TarInfo, "frombuf", classmethod(counting_frombuf))
    with pytest.raises(PackageIntegrityError, match="物理头部数"):
        inspect_package(payload)
    # 逐项解析在第 6 个头部（上限 5 + 1）处立即停止，而不是 200。
    assert constructed <= 8, f"拒绝前解析了 {constructed} 个头部"


def test_gzip_bomb_inside_tar_rejected_with_bounded_allocation():
    """gzip→tar 双层炸弹同样在解压层被拦：不进入 tar 解析。"""
    import gzip as gzip_mod

    inner = make_plain_tar({f"f{i:02d}.tex": b"A" * 4096 for i in range(20)})
    payload = gzip_mod.compress(inner)
    assert len(payload) < 32 * 1024
    with pytest.raises(PackageIntegrityError):
        bounded_gunzip(payload, 1024)


def test_gnu_sparse_member_rejected():
    """GNU sparse 成员显式拒绝（扩展头处理后的安全护栏）。"""
    # GNU old-style sparse：类型 'S'。手工构造最小头部。
    info = tarfile.TarInfo("sparse.bin")
    info.type = b"S"
    info.size = 0
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w") as archive:
        archive.addfile(info)
    with pytest.raises(PackageIntegrityError, match="sparse"):
        inspect_package(raw.getvalue())


def _make_tar_bytes(
    members: list[tuple[str, bytes, bytes]], *, pax_format=False
) -> bytes:
    """手工拼物理 tar 字节：[(name, data, typeflag)]，不做安全包装。"""
    import io as _io

    raw = _io.BytesIO()

    def header(name: bytes, size: int, typeflag: bytes) -> bytes:
        block = bytearray(512)
        block[0 : len(name)] = name
        size_field = f"{size:011o}".encode() + b"\0"
        block[124 : 124 + len(size_field)] = size_field
        block[156:157] = typeflag
        block[257:262] = b"ustar"
        block[263:265] = b"00"
        block[148:156] = b" " * 8  # 校验和字段光空格参与计算
        chksum = sum(block)
        field = f"{chksum:06o}".encode() + b"\0 "
        block[148 : 148 + len(field)] = field
        return bytes(block)

    for name, data, typeflag in members:
        raw.write(header(name.encode(), len(data), typeflag))
        if data:
            raw.write(data)
            pad = (-len(data)) % 512
            raw.write(b"\0" * pad)
    raw.write(b"\0" * 1024)
    return raw.getvalue()


def test_tar_big_pax_extension_preflight_rejects_before_parse(monkeypatch):
    """大 PAX 扩展负载在 tarfile 解析前被物理预检拒绝。"""
    monkeypatch.setattr(archive_mod, "MAX_TOTAL_DECOMPRESSED_BYTES", 1024)
    # 400016 字节的 PAX 扩展负载 + 1 字节真实文件。
    pax_payload = b"400016 path=evil.txt\n" + b"x" * (400016 - 20)
    payload = _make_tar_bytes(
        [
            ("PaxHeader", pax_payload, b"x"),
            ("real.tex", b"A", b"0"),
        ]
    )
    with pytest.raises(PackageIntegrityError, match="扩展头"):
        inspect_package(payload)


def test_tar_long_extension_chain_preflight_no_recursion(monkeypatch):
    """1200 个成员各带一个 PAX 头的小包在物理预检处被截断，不触发递归。"""
    monkeypatch.setattr(archive_mod, "MAX_ARCHIVE_MEMBERS", 2)
    entries: list[tuple[str, bytes, bytes]] = []
    for _ in range(600):
        entries.append(("PaxHeader", b"11 mtime=0\n", b"x"))
        entries.append(("m.tex", b"hi", b"0"))
    payload = _make_tar_bytes(entries)
    with pytest.raises(PackageIntegrityError, match="物理头部数"):
        inspect_package(payload)


def test_tar_preflight_counts_before_parser_allocation(monkeypatch):
    """量度证据：预检拒绝时 tarfile 一个头部都没解析（零 TarInfo）。"""
    constructed = 0
    original_frombuf = tarfile.TarInfo.frombuf.__func__

    def counting_frombuf(cls, buf, encoding, errors):
        nonlocal constructed
        constructed += 1
        return original_frombuf(cls, buf, encoding, errors)

    monkeypatch.setattr(tarfile.TarInfo, "frombuf", classmethod(counting_frombuf))
    monkeypatch.setattr(archive_mod, "MAX_ARCHIVE_MEMBERS", 3)
    entries: list[tuple[str, bytes, bytes]] = []
    for _ in range(25):
        entries.append(("PaxHeader", b"11 mtime=0\n", b"x"))
        entries.append(("m.tex", b"hi", b"0"))
    payload = _make_tar_bytes(entries)
    with pytest.raises(PackageIntegrityError, match="物理头部数"):
        inspect_package(payload)
    assert constructed == 0  # 预检先于解析：tarfile 未被触及


def test_tar_legitimate_pax_extension_accepted():
    """合法有界 PAX 扩展（长文件名）正常接受。"""
    long_name = "figures/" + "x" * 140 + ".pdf"
    payload = make_plain_tar({long_name: b"%PDF-fake"})
    inspection = inspect_package(payload)
    assert any(m.name == long_name for m in inspection.members)


def test_tar_pax_path_override_traversal_rejected():
    """PAX path 覆盖成越界路径时，逐成员校验在解析层拒绝。"""
    pax_payload = b"20 path=../evil.txt\n"
    payload = _make_tar_bytes(
        [
            ("PaxHeader", pax_payload, b"x"),
            ("safe.tex", b"ok", b"0"),
        ]
    )
    with pytest.raises(PackageIntegrityError, match="越界|绝对路径|上跳"):
        inspect_package(payload)


def test_gzip_block_boundary_trailing_rejected(monkeypatch):
    """压缩流恰落在输入块边界：EOF 后的尾随数据仍被拒绝。"""
    import gzip as gzip_mod

    monkeypatch.setattr(archive_mod, "_GUNZIP_CHUNK", 16)
    body = b"z" * 200
    payload = gzip_mod.compress(body)
    assert len(payload) > 16
    # 找到输出恰好跨块的设置：直接构造尾随 EXTRA 并验证拒绝。
    with pytest.raises(PackageIntegrityError, match="尾随"):
        bounded_gunzip(payload + b"EXTRA", 4096)


def test_gzip_output_exactly_cap_with_trailer_next_block_accepted(monkeypatch):
    """输出恰等于额度、CRC/结束标记在下一输入块：合法边界材料被接受。"""
    import gzip as gzip_mod

    monkeypatch.setattr(archive_mod, "_GUNZIP_CHUNK", 16)
    body = b"y" * 64
    payload = gzip_mod.compress(body)
    result = bounded_gunzip(payload, 64)
    assert result == body


def test_gzip_extra_output_after_cap_rejected(monkeypatch):
    import gzip as gzip_mod

    monkeypatch.setattr(archive_mod, "_GUNZIP_CHUNK", 16)
    body = b"w" * 128
    payload = gzip_mod.compress(body)
    with pytest.raises(PackageIntegrityError, match="有界上限"):
        bounded_gunzip(payload, 64)


def test_gzip_truncated_trailer_rejected(monkeypatch):
    import gzip as gzip_mod

    monkeypatch.setattr(archive_mod, "_GUNZIP_CHUNK", 16)
    payload = gzip_mod.compress(b"trailer case")[:-3]
    with pytest.raises(PackageIntegrityError, match="截断"):
        bounded_gunzip(payload, 4096)


def test_gzip_concatenated_members_rejected_small_chunk(monkeypatch):
    import gzip as gzip_mod

    monkeypatch.setattr(archive_mod, "_GUNZIP_CHUNK", 16)
    payload = gzip_mod.compress(b"first member") + gzip_mod.compress(b"second")
    with pytest.raises(PackageIntegrityError, match="尾随"):
        bounded_gunzip(payload, 4096)


def test_gzip_boundary_minus_one_and_exact_split(monkeypatch):
    """块边界前后（-1 / 恰好 / +1）的正常流全部接受。"""
    import gzip as gzip_mod

    monkeypatch.setattr(archive_mod, "_GUNZIP_CHUNK", 16)
    for body in (b"a" * 15, b"a" * 16, b"a" * 17, b"a" * 100):
        payload = gzip_mod.compress(body)
        assert bounded_gunzip(payload, 4096) == body


def _pax_entry(name: str, records: bytes) -> tuple[str, bytes, bytes]:
    """构造 PAX 扩展头条目（records 为 'NN key=value\n' 负载）。"""
    return name, records, b"x"


def _tar_header_raw(name: str, declared_size: int, typeflag: bytes) -> bytes:
    """只写头部块（不写数据），尺寸字段可独立于物理数据。"""
    import io as _io

    raw = _io.BytesIO()
    block = bytearray(512)
    encoded = name.encode()
    block[0 : len(encoded)] = encoded
    size_field = f"{declared_size:011o}".encode() + b"\0"
    block[124 : 124 + len(size_field)] = size_field
    block[156:157] = typeflag
    block[257:262] = b"ustar"
    block[263:265] = b"00"
    block[148:156] = b" " * 8
    chksum = sum(block)
    field = f"{chksum:06o}".encode() + b"\0 "
    block[148 : 148 + len(field)] = field
    raw.write(bytes(block))
    return raw.getvalue()


def _pad_data(data: bytes) -> bytes:
    return data + b"\0" * ((-len(data)) % 512)


def _pax_size_record(value: int) -> bytes:
    """构造自洽长度的 PAX ``size=NN`` 记录（前缀数字含自身）。"""
    rec = f"size={value}\n".encode()
    for width in range(1, 6):
        total = width + 1 + len(rec)
        if len(str(total)) == width:
            return f"{total} ".encode() + rec
    raise AssertionError("unreachable")


def test_tar_local_pax_size_record_unsupported(monkeypatch):
    """收敛边界：局部 PAX ``size`` 记录（含缩至 0）明确不支持。

    R4 审核反例的处置方式：不在自定义解析器里仿真 tarfile 的覆盖
    语义（重复字段/局部链/数值容错会打开分歧窗口），布局改变型变
    体一律在进入 tarfile 前报告“不支持”，原始字节由调用方保留。
    """
    monkeypatch.setattr(archive_mod, "MAX_TOTAL_DECOMPRESSED_BYTES", 1024)
    crafted = b"".join(
        [
            _tar_header_raw("PaxHeader", 9, b"x") + _pad_data(b"9 size=0\n"),
            _tar_header_raw("big.bin", 4096, b"0"),  # 声明 4096，物理 0 块
            _tar_header_raw("PaxHidden", 3000, b"x") + b"H" * 3000 + _pad_data(b""),
            _tar_header_raw("real.tex", 2, b"0") + _pad_data(b"hi"),
            b"\0" * 1024,
        ]
    )
    with pytest.raises(UnsupportedPackageError, match="size"):
        inspect_package(crafted)
    # 展示为获取结果时是“不支持”而非“损坏”：字节保留（lifecycle 层覆盖）。


def test_tar_local_pax_grow_size_record_unsupported():
    """局部 PAX 放大尺寸（size=2048）同样明确不支持。"""
    crafted = b"".join(
        [
            _tar_header_raw("PaxHeader", len(_pax_size_record(2048)), b"x")
            + _pad_data(_pax_size_record(2048)),
            _tar_header_raw("grown.bin", 2048, b"0") + b"B" * 2048,
            _tar_header_raw("real.tex", 2, b"0") + _pad_data(b"hi"),
            b"\0" * 1024,
        ]
    )
    with pytest.raises(UnsupportedPackageError, match="size"):
        inspect_package(crafted)


def test_tar_global_pax_header_unsupported(monkeypatch):
    """全局 PAX 头（``g``）整体明确不支持，无论记录内容。"""
    monkeypatch.setattr(archive_mod, "MAX_TOTAL_DECOMPRESSED_BYTES", 1024)
    crafted = b"".join(
        [
            _tar_header_raw("GlobalHeader", 9, b"g") + _pad_data(b"9 size=0\n"),
            _tar_header_raw("zeroed.bin", 512, b"0"),
            _tar_header_raw("real.tex", 2, b"0") + _pad_data(b"hi"),
            b"\0" * 1024,
        ]
    )
    with pytest.raises(UnsupportedPackageError, match="全局 PAX"):
        inspect_package(crafted)


def test_tar_malformed_pax_payload_unsupported():
    """无法完整切分的 PAX 负载明确不支持（不猜测 tarfile 容错）。"""
    crafted = b"".join(
        [
            _tar_header_raw("PaxHeader", 16, b"x") + _pad_data(b"not a record\0\n"),
            _tar_header_raw("real.tex", 2, b"0") + _pad_data(b"hi"),
            b"\0" * 1024,
        ]
    )
    with pytest.raises(UnsupportedPackageError, match="不支持"):
        inspect_package(crafted)


def test_tar_consecutive_extension_chain_unsupported():
    """连续扩展头链（x 后再 x）在第二个头部即被拒，无递归。"""
    entries = [("PaxHeader", b"11 mtime=0\n", b"x"), ("PaxNext", b"5 c=d\n", b"x")]
    payload = _make_tar_bytes(entries + [("real.tex", b"hi", b"0")])
    with pytest.raises(UnsupportedPackageError, match="连续扩展头链"):
        inspect_package(payload)


def test_tar_production_chain_depth_900_unsupported_no_recursion():
    """生产默认上限下 900 个连续有效 PAX 头：链变体先于递归拒绝。"""
    entries = [("PaxHeader", b"11 mtime=0\n", b"x")] * 900
    payload = _make_tar_bytes(entries + [("real.tex", b"hi", b"0")])
    assert len(payload) < 2 * 1024 * 1024  # 复现材料保持小体积
    with pytest.raises(UnsupportedPackageError, match="连续扩展头链"):
        inspect_package(payload)


def test_tar_legitimate_short_extension_chain_accepted():
    """正常 PAX/GNU 长名（每成员单个扩展头）不受子集边界影响。"""
    long_name = "figs/" + "n" * 130 + ".eps"
    deep = {f"{long_name}.{i}": b"x" * 4 for i in range(5)}
    payload = make_plain_tar(deep)
    inspection = inspect_package(payload)
    assert len(inspection.members) == 5


# ---------------------------------------------------------------- R extra1：PAX 允许清单


def _pax_record(key: str, value: str) -> bytes:
    """构造自洽长度且键值明确的 PAX 记录。"""
    rec = f"{key}={value}\n".encode()
    for width in range(1, 7):
        total = width + 1 + len(rec)
        if len(str(total)) == width:
            return f"{total} ".encode() + rec
    raise AssertionError("unreachable")


def test_pax_sparse_map_field_unsupported_before_tarfile():
    """审核反例：有效定界的 ``GNU.sparse.map=garbage`` 在 tarfile 之前拒绝。"""
    crafted = b"".join(
        [
            _tar_header_raw(
                "PaxHeader", len(_pax_record("GNU.sparse.map", "garbage")), b"x"
            )
            + _pad_data(_pax_record("GNU.sparse.map", "garbage")),
            _tar_header_raw("real.tex", 2, b"0") + _pad_data(b"hi"),
            b"\0" * 1024,
        ]
    )
    with pytest.raises(UnsupportedPackageError, match="GNU.sparse|允许清单"):
        inspect_package(crafted)


def test_pax_sparse_major_minor_unsupported():
    """``GNU.sparse.major/minor`` 稀疏版本字段同样在解析前拒绝。"""
    records = _pax_record("GNU.sparse.major", "1") + _pax_record(
        "GNU.sparse.minor", "0"
    )
    crafted = b"".join(
        [
            _tar_header_raw("PaxHeader", len(records), b"x") + _pad_data(records),
            _tar_header_raw("real.tex", 2, b"0") + _pad_data(b"hi"),
            b"\0" * 1024,
        ]
    )
    with pytest.raises(UnsupportedPackageError, match="允许清单"):
        inspect_package(crafted)


def test_pax_unknown_field_unsupported():
    """未知字段（含 SCHILY.* 等未承诺命名空间）不在允许清单内。"""
    crafted = b"".join(
        [
            _tar_header_raw(
                "PaxHeader", len(_pax_record("SCHILY.xattr.bogus", "x")), b"x"
            )
            + _pad_data(_pax_record("SCHILY.xattr.bogus", "x")),
            _tar_header_raw("real.tex", 2, b"0") + _pad_data(b"hi"),
            b"\0" * 1024,
        ]
    )
    with pytest.raises(UnsupportedPackageError, match="允许清单"):
        inspect_package(crafted)


def test_pax_allowed_metadata_keys_accepted():
    """允许清单内的纯元数据键正常接受（mtime/path/comment 等）。"""
    records = (
        _pax_record("mtime", "1234567890")
        + _pax_record("comment", "ordinary")
        + _pax_record("uname", "build")
    )
    crafted = b"".join(
        [
            _tar_header_raw("PaxHeader", len(records), b"x") + _pad_data(records),
            _tar_header_raw("real.tex", 2, b"0") + _pad_data(b"hi"),
            b"\0" * 1024,
        ]
    )
    inspection = inspect_package(crafted)
    assert [m.name for m in inspection.members] == ["real.tex"]


def test_supported_key_numeric_garbage_no_plaintext_fallback():
    """支持键上的数值垃圾（mtime=garbage）绝不回退为普通文本形态。

    本运行时的 tarfile 对允许键上的垃圾值整体容忍（记录保留为字符
    串、元数据取默认值），包按 tar 正常接受；若其他运行时在数值转
    换处抛 ValueError，预检后的解析层把它分类为
    ``TarMemberMetadataError``（完整性错误的子类，可观察、可审计），
    同样不会退化为单文件文本。这里验证可观察行为：tar 结构被识别
    并按 tar 检查（成员 real.tex 进入清单）。
    """
    records = _pax_record("mtime", "garbage")
    crafted = b"".join(
        [
            _tar_header_raw("PaxHeader", len(records), b"x") + _pad_data(records),
            _tar_header_raw("real.tex", 2, b"0") + _pad_data(b"hi"),
            b"\0" * 1024,
        ]
    )
    from hvs_catalog.pipeline.acquisition.archive import (
        sniff_package_format,
    )

    assert sniff_package_format(crafted) == "tar_plain"
    inspection = inspect_package(crafted)
    assert [m.name for m in inspection.members] == ["real.tex"]

"""Synthetic source-package safety tests (Lane B: archive module).

All tests are offline: tarfile/gzip/plain-text bytes are built in memory, without
network access or disk extraction. Monkeypatch limits for test cases; production
defaults remain unchanged.
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
    """Build a single-member tar with an unusual member type supplied as raw bytes."""
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


# ---------------------------------------------------------------- Format recognition


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


# ---------------------------------------------------------------- Unsupported formats


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


# ---------------------------------------------------------------- Safety rejection


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


# ---------------------------------------------------------------- Engineering limits


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
    """Reject a gzip bomb during bounded streaming decompression, before expansion
    completes.
    """
    monkeypatch.setattr(archive_mod, "MAX_TOTAL_DECOMPRESSED_BYTES", 10)
    payload = make_gzip_tar({"a": b"123456", "b": b"123456"})
    with pytest.raises(PackageIntegrityError, match="有界上限"):
        inspect_package(payload)


def test_total_cap_boundary_exact_fit_accepted(monkeypatch):
    """Accept decompressed size exactly at the limit: reject only strictly greater sizes."""
    monkeypatch.setattr(archive_mod, "MAX_TOTAL_DECOMPRESSED_BYTES", 10)
    payload = make_plain_tar({"a": b"12345", "b": b"12345"})
    inspection = inspect_package(payload)
    assert inspection.total_decompressed == 10


def test_gzip_single_tex_official_form_supported():
    """Official arXiv format: a single gzip-compressed TeX file; gzip does not always
    contain tar.
    """
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
    """Reject gzip-wrapped PDF-only data as unsupported while allowing the caller to retain
    the bytes.
    """
    import gzip

    payload = gzip.compress(b"%PDF-1.4 fake pdf body")
    with pytest.raises(UnsupportedPackageError, match="PDF"):
        inspect_package(payload)


def test_gzip_bomb_allocation_bounded_before_expansion():
    """Bound decompression-bomb allocations well below the full expanded size.

    A roughly 4 KiB synthetic gzip expands to 4 MiB. Decompressing before checking would
    allocate near the full size before rejection. Use tracemalloc to require a peak
    comparable to the output limit rather than the expanded size.
    """
    import gzip as gzip_mod
    import tracemalloc

    payload = gzip_mod.compress(b"A" * (4 * 1024 * 1024))
    assert (
        len(payload) < 64 * 1024
    )  # The compressed payload is much smaller than its expanded size.
    tracemalloc.start()
    try:
        with pytest.raises(PackageIntegrityError, match="有界上限"):
            bounded_gunzip(payload, 64 * 1024)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    # The peak should be comparable to the 64 KiB limit, including interpreter and read buffers,
    # and far below the full 4 MiB expansion. Allow 8 times the limit for environment variation;
    # this is two orders of magnitude below the old implementation's roughly 2.5 times expanded size.
    assert peak < 64 * 1024 * 8, f"解压拒绝前分配了 {peak} 字节"


def test_gzip_output_at_cap_boundary_accepted():
    """Accept gzip output exactly at the limit; the allocation bound has no off-by-one
    rejection.
    """
    import gzip as gzip_mod

    body = b"B" * 1000
    payload = gzip_mod.compress(body)
    assert bounded_gunzip(payload, 1000) == body


def test_tar_member_cap_stops_parsing_before_all_headers(monkeypatch):
    """Stop parsing at the member limit instead of constructing every TarInfo first.

    With a limit of 2, do not use getmembers() to construct 100 entries before
    rejection. Count TarInfo constructions and require the count at rejection to be
    comparable to the limit.
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
    # Stop at header 6 (limit 5 plus one), rather than parsing all 200.
    assert constructed <= 8, f"拒绝前解析了 {constructed} 个头部"


def test_gzip_bomb_inside_tar_rejected_with_bounded_allocation():
    """Reject a nested gzip/tar bomb during decompression, before tar parsing."""
    import gzip as gzip_mod

    inner = make_plain_tar({f"f{i:02d}.tex": b"A" * 4096 for i in range(20)})
    payload = gzip_mod.compress(inner)
    assert len(payload) < 32 * 1024
    with pytest.raises(PackageIntegrityError):
        bounded_gunzip(payload, 1024)


def test_gnu_sparse_member_rejected():
    """Explicitly reject GNU sparse members after extension-header handling."""
    # GNU old-style sparse type 'S': construct a minimal header manually.
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
    """Assemble physical tar bytes from (name, data, typeflag) entries without safety
    wrappers.
    """
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
        block[148:156] = (
            b" " * 8
        )  # Treat checksum-field bytes as spaces while calculating the checksum.
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
    """Reject large PAX extension payloads in physical preflight before tarfile parses
    them.
    """
    monkeypatch.setattr(archive_mod, "MAX_TOTAL_DECOMPRESSED_BYTES", 1024)
    # A 400016-byte PAX extension payload plus a one-byte real file.
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
    """Stop a small archive with 1200 members and one PAX header each during physical
    preflight, avoiding recursion.
    """
    monkeypatch.setattr(archive_mod, "MAX_ARCHIVE_MEMBERS", 2)
    entries: list[tuple[str, bytes, bytes]] = []
    for _ in range(600):
        entries.append(("PaxHeader", b"11 mtime=0\n", b"x"))
        entries.append(("m.tex", b"hi", b"0"))
    payload = _make_tar_bytes(entries)
    with pytest.raises(PackageIntegrityError, match="物理头部数"):
        inspect_package(payload)


def test_tar_preflight_counts_before_parser_allocation(monkeypatch):
    """Verify that preflight rejection precedes every tarfile header parse: zero TarInfo
    objects.
    """
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
    assert constructed == 0  # Preflight runs before parsing: tarfile is never reached.


def test_tar_legitimate_pax_extension_accepted():
    """Accept a valid bounded PAX extension for a long filename."""
    long_name = "figures/" + "x" * 140 + ".pdf"
    payload = make_plain_tar({long_name: b"%PDF-fake"})
    inspection = inspect_package(payload)
    assert any(m.name == long_name for m in inspection.members)


def test_tar_pax_path_override_traversal_rejected():
    """Reject a PAX path override that escapes the root during per-member validation."""
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
    """Reject data after EOF even when the compressed stream ends on an input-chunk
    boundary.
    """
    import gzip as gzip_mod

    monkeypatch.setattr(archive_mod, "_GUNZIP_CHUNK", 16)
    body = b"z" * 200
    payload = gzip_mod.compress(body)
    assert len(payload) > 16
    # Construct trailing EXTRA at the output boundary and verify rejection.
    with pytest.raises(PackageIntegrityError, match="尾随"):
        bounded_gunzip(payload + b"EXTRA", 4096)


def test_gzip_output_exactly_cap_with_trailer_next_block_accepted(monkeypatch):
    """Accept output exactly at the limit when CRC/end markers fall in the next input
    chunk.
    """
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
    """Accept valid streams just before, at, and just after the chunk boundary."""
    import gzip as gzip_mod

    monkeypatch.setattr(archive_mod, "_GUNZIP_CHUNK", 16)
    for body in (b"a" * 15, b"a" * 16, b"a" * 17, b"a" * 100):
        payload = gzip_mod.compress(body)
        assert bounded_gunzip(payload, 4096) == body


def _pax_entry(name: str, records: bytes) -> tuple[str, bytes, bytes]:
    """Build a PAX extension header whose records use NN key=value newline payloads."""
    return name, records, b"x"


def _tar_header_raw(name: str, declared_size: int, typeflag: bytes) -> bytes:
    """Write only a header block; its size field can differ from physical data length."""
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
    """Build a self-consistent PAX size=NN record whose length prefix includes itself."""
    rec = f"size={value}\n".encode()
    for width in range(1, 6):
        total = width + 1 + len(rec)
        if len(str(total)) == width:
            return f"{total} ".encode() + rec
    raise AssertionError("unreachable")


def test_tar_local_pax_size_record_unsupported(monkeypatch):
    """Explicitly exclude local PAX size records, including shrinkage to zero.

    Do not emulate tarfile override semantics in the custom parser: duplicate fields,
    local chains, and numeric tolerance can diverge. Report layout-changing variants as
    unsupported before tarfile, retaining raw bytes for the caller.
    """
    monkeypatch.setattr(archive_mod, "MAX_TOTAL_DECOMPRESSED_BYTES", 1024)
    crafted = b"".join(
        [
            _tar_header_raw("PaxHeader", 9, b"x") + _pad_data(b"9 size=0\n"),
            _tar_header_raw(
                "big.bin", 4096, b"0"
            ),  # Declare 4096 bytes with zero physical data blocks.
            _tar_header_raw("PaxHidden", 3000, b"x") + b"H" * 3000 + _pad_data(b""),
            _tar_header_raw("real.tex", 2, b"0") + _pad_data(b"hi"),
            b"\0" * 1024,
        ]
    )
    with pytest.raises(UnsupportedPackageError, match="size"):
        inspect_package(crafted)
    # Acquisition reports unsupported, not damaged, and retains bytes; lifecycle tests cover this.


def test_tar_local_pax_grow_size_record_unsupported():
    """Explicitly exclude local PAX size enlargement to 2048."""
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
    """Explicitly exclude all global PAX g headers regardless of their records."""
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
    """Report unsplittable PAX payloads as unsupported without guessing tarfile tolerance."""
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
    """Reject chained x extension headers at the second header, without recursion."""
    entries = [("PaxHeader", b"11 mtime=0\n", b"x"), ("PaxNext", b"5 c=d\n", b"x")]
    payload = _make_tar_bytes(entries + [("real.tex", b"hi", b"0")])
    with pytest.raises(UnsupportedPackageError, match="连续扩展头链"):
        inspect_package(payload)


def test_tar_production_chain_depth_900_unsupported_no_recursion():
    """Reject a chain of 900 valid PAX headers under production limits before recursion."""
    entries = [("PaxHeader", b"11 mtime=0\n", b"x")] * 900
    payload = _make_tar_bytes(entries + [("real.tex", b"hi", b"0")])
    assert len(payload) < 2 * 1024 * 1024  # Keep the reproducer small.
    with pytest.raises(UnsupportedPackageError, match="连续扩展头链"):
        inspect_package(payload)


def test_tar_legitimate_short_extension_chain_accepted():
    """Accept ordinary PAX/GNU long names with one extension header per member."""
    long_name = "figs/" + "n" * 130 + ".eps"
    deep = {f"{long_name}.{i}": b"x" * 4 for i in range(5)}
    payload = make_plain_tar(deep)
    inspection = inspect_package(payload)
    assert len(inspection.members) == 5


# ---------------------------------------------------------------- R extra1: PAX allowlist


def _pax_record(key: str, value: str) -> bytes:
    """Build a PAX record with a self-consistent length and explicit key/value."""
    rec = f"{key}={value}\n".encode()
    for width in range(1, 7):
        total = width + 1 + len(rec)
        if len(str(total)) == width:
            return f"{total} ".encode() + rec
    raise AssertionError("unreachable")


def test_pax_sparse_map_field_unsupported_before_tarfile():
    """Reject a well-delimited GNU.sparse.map=garbage record before tarfile."""
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
    """Reject GNU.sparse.major/minor version fields before parsing too."""
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
    """Exclude unknown keys, including unsupported SCHILY.* namespaces, from the allowlist."""
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
    """Accept allowlisted metadata-only keys such as mtime, path, and comment."""
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
    """Never reinterpret invalid numeric metadata such as mtime=garbage as plain text.

    This runtime's tarfile tolerates malformed allowlisted values, retaining strings and
    default metadata, so the archive remains accepted as tar. Other runtimes may raise
    ValueError; classify that after preflight as observable TarMemberMetadataError, a
    subclass of integrity error. In either case, never fall back to single-file text.
    Verify observable tar recognition and validation through the real.tex manifest
    entry.
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

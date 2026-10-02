"""Bounded engineering inspection/manifests for arXiv source packages (Lane B, local C06).

Recognize gzip tar, plain tar, gzip single TeX, and plain single TeX. Bound output
before dangerous allocation: stream gzip with remaining allowances and reject
excess/multiple members/trailing bytes. Preflight all tar headers for member
count/path/type/declared size/alias collisions before a second bounded content-read
pass. Reject PDF-only/HTML, escaping/platform-variant paths, symbolic/hard links,
devices, duplicate/case-alias members, excessive counts, and declared/actual expanded
totals.

Never execute TeX, fetch external links, or parse body/tables; every manifest member is
unparsed. Callers retain original bytes content-addressably. UnsupportedPackageError
means unsupported format, distinct from PackageIntegrityError acquisition failure.
"""

from __future__ import annotations

import hashlib
import io
import tarfile
import zlib
from dataclasses import dataclass

__all__ = [
    "MAX_ARCHIVE_MEMBERS",
    "MAX_PACKAGE_BYTES",
    "MAX_TOTAL_DECOMPRESSED_BYTES",
    "PackageInspection",
    "PackageIntegrityError",
    "PackageMember",
    "UnsupportedPackageError",
    "inspect_package",
    "sniff_package_format",
]

#: Original source-package byte limit, aligned with Lane A download-response bounds.
MAX_PACKAGE_BYTES = 256 * 1024 * 1024

#: Archive member-count limit preventing member-count abuse.
MAX_ARCHIVE_MEMBERS = 2000

#: Total expanded member-byte limit preventing decompression bombs.
MAX_TOTAL_DECOMPRESSED_BYTES = 1024 * 1024 * 1024

_READ_CHUNK = 1024 * 1024
_GUNZIP_CHUNK = 512 * 1024

_GZIP_MAGIC = b"\x1f\x8b"
_PDF_MAGIC = b"%PDF-"

#: Physical extension types: PAX x/g and GNU long-name/long-link L/K.
_EXT_TYPEFLAGS = frozenset({b"x", b"g", b"L", b"K"})


class UnsupportedPackageError(Exception):
    """Material is not a recognized LaTeX source package, such as PDF-only or HTML.

    This is unsupported/unavailable material, not acquisition failure. Callers may
    retain raw bytes, but no usable source manifest is produced.
    """


class PackageIntegrityError(Exception):
    """Unsafe package or exceeded engineering limits: escaping paths, links/devices,
    duplicates, or excessive size.

    An engineering failure; the package cannot serve as usable source material.
    """


@dataclass(frozen=True)
class PackageMember:
    """Engineering description of one unparsed manifest member."""

    name: str
    size: int
    sha256: str
    media_type: str


@dataclass(frozen=True)
class PackageInspection:
    """Bounded-inspection result: format, manifest, and total expanded bytes."""

    format: str  # "tar_gzip" | "tar_plain" | "single_tex_gzip" | "single_tex"
    members: tuple[PackageMember, ...]
    total_decompressed: int


_MEDIA_TYPES = {
    ".tex": "text/x-tex",
    ".sty": "text/x-tex",
    ".cls": "text/x-tex",
    ".bib": "text/x-bibtex",
    ".bst": "text/x-bst",
    ".txt": "text/plain",
    ".md": "text/markdown",
    ".csv": "text/csv",
    ".ecsv": "text/ecsv",
    ".pdf": "application/pdf",
    ".eps": "application/postscript",
    ".ps": "application/postscript",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".json": "application/json",
}


def _media_type_for(name: str) -> str:
    lowered = name.lower()
    for suffix, media in _MEDIA_TYPES.items():
        if lowered.endswith(suffix):
            return media
    return "application/octet-stream"


def _reject_single_file_form(content: bytes) -> None:
    """Reject PDF/HTML/NUL binary contents presented as a single-file format."""
    head = content[:8192].lstrip(b"\xef\xbb\xbf\r\n\t ")
    lowered = head.lower()
    if lowered.startswith((b"<!doctype html", b"<html")):
        raise UnsupportedPackageError("材料是 HTML 页面，不是 LaTeX 源码包")
    if content.startswith(_PDF_MAGIC):
        raise UnsupportedPackageError("材料是 PDF（PDF-only 没有 LaTeX 源码包）")
    if b"\x00" in content[:8192]:
        raise UnsupportedPackageError(
            "单文件形态含二进制 NUL 字节，不是可识别的 TeX 文本"
        )


def bounded_gunzip(payload: bytes, output_cap: int) -> bytes:
    """Stream gzip with output allowance bounded before each decompression call.

    Pass remaining output_cap to zlib.Decompress.decompress max_length. At zero
    allowance, use a one-byte probe because zlib zero means unlimited. zlib stops at the
    allowance and retains unconsumed_tail, bounding allocation. At an exact output
    limit, continue one-byte probes through later CRC/end-marker chunks; reject only
    actual extra output, allowing exact-limit materials. After EOF require fully
    consumed input with no unused_data/unconsumed_tail or unsubmitted bytes; reject
    multiple members/trailing data. Input exhaustion before EOF is truncation.
    """
    view = memoryview(payload)
    total = len(view)
    decompressor = zlib.decompressobj(wbits=31)
    output = bytearray()
    pos = 0
    budget = output_cap

    while not decompressor.eof:
        if decompressor.unconsumed_tail:
            chunk: bytes = bytes(decompressor.unconsumed_tail)
        elif pos < total:
            chunk = bytes(view[pos : pos + _GUNZIP_CHUNK])
            pos += len(chunk)
        else:
            break  # Exhausted input before EOF is rejected as truncation after the loop.
        while chunk and not decompressor.eof and budget > 0:
            piece = decompressor.decompress(chunk, max_length=budget)
            if piece:
                output.extend(piece)
                budget -= len(piece)
            chunk = bytes(decompressor.unconsumed_tail)
        if decompressor.eof:
            break
        if budget <= 0:
            # Exhausted allowance may leave CRC/length trailers producing no output. Continue
            # with a one-byte probe and reject only actual extra decompressed bytes;
            # consuming all input through EOF with exact-limit output remains valid.
            while not decompressor.eof:
                nxt = chunk
                if not nxt and decompressor.unconsumed_tail:
                    nxt = bytes(decompressor.unconsumed_tail)
                if not nxt:
                    if pos < total:
                        nxt = bytes(view[pos : pos + _GUNZIP_CHUNK])
                        pos += len(nxt)
                    else:
                        break
                probe = decompressor.decompress(nxt, max_length=1)
                if probe:
                    raise PackageIntegrityError(
                        f"gzip 解压输出超过有界上限 {output_cap}"
                        "（疑似解压炸弹），已在上限处停止，未完成解压"
                    )
                chunk = bytes(decompressor.unconsumed_tail)
            break
    if not decompressor.eof:
        raise PackageIntegrityError("gzip 流截断或损坏（未到达结束标记）")
    unused = decompressor.unused_data or decompressor.unconsumed_tail
    if unused or pos < total:
        remaining = len(unused) + (total - pos)
        raise PackageIntegrityError(
            f"gzip 流之后还有 {remaining} 字节尾随数据（多成员或拼接），拒绝"
        )
    return bytes(output)


def _tar_octal_size(block: bytes) -> int:
    """Parse an octal size field from a 512-byte header; invalid fields raise integrity
    errors.
    """
    field = block[124:136].split(b"\0")[0].strip()
    if not field:
        return 0
    try:
        return int(field, 8)
    except ValueError as exc:
        raise PackageIntegrityError(f"tar 头部尺寸字段非法：{field!r}") from exc


_SUPPORTED_PAX_KEYS = frozenset(
    {
        "path",
        "linkpath",
        "mtime",
        "ctime",
        "atime",
        "comment",
        "uid",
        "gid",
        "uname",
        "gname",
        "hdrcharset",
    }
)
"""**允许清单**：不影响成员数据物理布局的纯元数据 PAX 字段。

``size``（缩放/覆盖布局）、``GNU.sparse.*``（稀疏布局，tarfile 会
按其解释数据区）及一切未承诺字段都不在清单内——按收敛边界，这些
变体在进入 tarfile **之前**报告不支持，而不是在自定义并行解析器
里仿真其语义。清单与 README 的支持声明一一对应。
"""


class TarMemberMetadataError(PackageIntegrityError):
    """Unsupported numeric/format interpretation of metadata on supported fields.

    Unlike structural non-tar input, this propagates as an observable integrity error;
    never silently fall back to plain text.
    """


def _scan_pax_records_supported(payload: bytes) -> None:
    """Boundedly validate PAX payloads against an explicit record allowlist.

    Require complete splitting by declared length/name=value/newline records and
    allowlisted names only. size, GNU.sparse.*, and every unsupported key may affect
    tarfile layout/sparse interpretation, so reject as unsupported before tarfile.
    Unsplittable payloads are likewise unsupported without guessing parser tolerance.
    """
    pos = 0
    while pos < len(payload):
        space = payload.find(b" ", pos)
        if space < 0:
            raise UnsupportedPackageError(
                "PAX 扩展负载无法按记录边界切分（不支持的归档变体）"
            )
        try:
            length = int(payload[pos:space])
        except ValueError as exc:
            raise UnsupportedPackageError(
                "PAX 扩展负载的记录长度字段非整数（不支持的归档变体）"
            ) from exc
        if length <= 0 or pos + length > len(payload):
            raise UnsupportedPackageError(
                "PAX 扩展负载的记录长度越界（不支持的归档变体）"
            )
        record = payload[space + 1 : pos + length]
        if not record.endswith(b"\n"):
            raise UnsupportedPackageError(
                "PAX 扩展负载的记录未按换行结束（不支持的归档变体）"
            )
        name = record.split(b"=", 1)[0].decode("ascii", "replace")
        if name not in _SUPPORTED_PAX_KEYS:
            raise UnsupportedPackageError(
                f"PAX 字段 {name!r} 不在支持的纯元数据允许清单内"
                "（size/GNU.sparse.* 等布局或稀疏字段明确不支持）"
            )
        pos += length


def _tar_physical_preflight(content: bytes) -> None:
    """Preflight physical layout before tarfile parses any header.

    Walk 512-byte blocks and inspect declared sizes/types using a supported extension
    subset, without emulating tarfile override semantics. Bound physical headers,
    including extensions, by the member limit. Permit at most one extension per member;
    reject chains before recursive tarfile handling. Support local PAX x and GNU
    long-name L only after declared/cumulative payload-size checks, with bounded reads;
    x records require complete allowlisted parsing and no size overrides. Exclude global
    PAX g and GNU long-link K. Rejecting every size override lets declared-size block
    advancement match tarfile offsets without divergent emulation. Limits raise
    PackageIntegrityError; unsupported variants raise UnsupportedPackageError, allowing
    callers to retain bytes and report capability boundaries.
    """
    total = len(content)
    offset = 0
    physical = 0
    cumulative_ext = 0
    chain = 0
    saw_entry = False
    while offset + 512 <= total:
        block = content[offset : offset + 512]
        if block.count(0) == 512:
            break  # Stop physical scanning at terminal zero blocks.
        physical += 1
        saw_entry = True
        if physical > MAX_ARCHIVE_MEMBERS:
            raise PackageIntegrityError(
                f"物理头部数超过工程上限 {MAX_ARCHIVE_MEMBERS}"
                f"（已扫 {physical} 个块，含扩展头；预检先于解析）"
            )
        size = _tar_octal_size(block)
        if size < 0:
            raise PackageIntegrityError("tar 头部尺寸为负")
        typeflag = block[156:157]
        if typeflag in _EXT_TYPEFLAGS:
            chain += 1
            if chain > 1:
                raise UnsupportedPackageError(
                    "连续扩展头链（多级覆盖）明确不在支持子集内，先于解析拒绝"
                )
            if typeflag in (b"g", b"K"):
                raise UnsupportedPackageError(
                    "全局 PAX 头 / GNU 长链接头明确不在支持子集内"
                    "（跨成员继承或链接变体）"
                )
            if size > MAX_TOTAL_DECOMPRESSED_BYTES:
                raise PackageIntegrityError(
                    f"扩展头声明尺寸 {size} 超过工程上限 "
                    f"{MAX_TOTAL_DECOMPRESSED_BYTES}（先于解析拒绝）"
                )
            cumulative_ext += size
            if cumulative_ext > MAX_TOTAL_DECOMPRESSED_BYTES:
                raise PackageIntegrityError(
                    f"扩展头累计字节 {cumulative_ext} 超过工程上限 "
                    f"{MAX_TOTAL_DECOMPRESSED_BYTES}（先于解析拒绝）"
                )
            if typeflag == b"x":
                payload = content[offset + 512 : offset + 512 + size]
                if len(payload) < size:
                    raise PackageIntegrityError("PAX 扩展负载被截断")
                _scan_pax_records_supported(payload)
            # GNU L payloads hold long filenames without changing subsequent member layout;
            # size limits were already checked above.
            offset += 512 + ((size + 511) // 512) * 512
            continue
        chain = 0
        offset += 512 + ((size + 511) // 512) * 512
    if not saw_entry and total > 0:
        # No nonzero headers means no recognized tar; the caller rejects its format.
        return


def _iter_tar_headers(archive: tarfile.TarFile, *, member_cap: int) -> int:
    """Iterate/count tar headers individually and stop immediately above the limit.

    Avoid getmembers(), which could allocate unlimited TarInfo objects and extension
    reads before rejection. Count each parsed header and reject promptly so work scales
    with the bound. Include directory entries in the total.
    """
    count = 0
    while True:
        try:
            info = archive.next()
        except (tarfile.TarError, EOFError) as exc:
            raise PackageIntegrityError(f"归档头部无法解析：{exc}") from exc
        except ValueError as exc:
            # Classify malformed supported metadata, such as invalid mtime, as observable/auditable
            # member metadata errors, never silent plain-text fallback.
            raise TarMemberMetadataError(
                f"成员元数据字段无法解析（支持字段上的数值/格式错误）：{exc}"
            ) from exc
        if info is None:
            return count
        count += 1
        if count > member_cap:
            raise PackageIntegrityError(
                f"成员数量超过工程上限 {member_cap}（已逐项解析 {count} 个"
                "头部后立即停止）"
            )


def _looks_like_tar(content: bytes) -> bool:
    """Recognize parseable tar, including legacy header variants.

    Rely on tarfile checksums, which random text rarely satisfies. Iterate headers
    within bounds without collecting all members. Excess count returns false here;
    subsequent full inspection supplies precise errors.
    """
    if len(content) >= 262 and content[257:262] == b"ustar":
        _tar_physical_preflight(content)
        return True
    try:
        _tar_physical_preflight(content)
        with tarfile.open(fileobj=io.BytesIO(content), mode="r:") as archive:
            count = _iter_tar_headers(archive, member_cap=MAX_ARCHIVE_MEMBERS)
    except TarMemberMetadataError:
        # Supported-field metadata errors mean tar-like parsing failed and must propagate
        # as classified observable errors, never plain-text fallback.
        raise
    except PackageIntegrityError:
        return False
    except (tarfile.TarError, EOFError):
        return False
    return count > 0


def sniff_package_format(payload: bytes) -> str:
    """Recognize package formats by magic/structure; unsupported unsafe formats raise
    UnsupportedPackageError.

    Return tar_gzip, tar_plain, single_tex_gzip, or single_tex. Bound gzip by total
    expanded bytes before distinguishing tar from arXiv single-file TeX. Bounded
    decompression also rejects trailing data/multiple members.
    """
    if payload.startswith(_PDF_MAGIC):
        raise UnsupportedPackageError("材料是 PDF（PDF-only 没有 LaTeX 源码包）")
    if payload.startswith(_GZIP_MAGIC):
        content = bounded_gunzip(payload, MAX_TOTAL_DECOMPRESSED_BYTES)
        if _looks_like_tar(content):
            return "tar_gzip"
        _reject_single_file_form(content)
        return "single_tex_gzip"
    if _looks_like_tar(payload):
        return "tar_plain"
    _reject_single_file_form(payload)
    return "single_tex"


def _check_member_name(name: str) -> None:
    """Reject escaping/platform-variant paths: absolute paths, parent components,
    backslashes, drive letters, and empty segments.
    """
    if not name or "\x00" in name:
        raise PackageIntegrityError(f"成员名为空或含 NUL：{name!r}")
    if name.startswith(("/", "~", "\\")):
        raise PackageIntegrityError(f"成员路径是绝对路径：{name!r}")
    if "\\" in name:
        raise PackageIntegrityError(f"成员路径含反斜杠（Windows 分隔符变体）：{name!r}")
    parts = name.split("/")
    if any(part in ("", ".", "..") for part in parts):
        raise PackageIntegrityError(f"成员路径含空段或上跳段：{name!r}")
    first = parts[0]
    if len(first) >= 2 and first[1] == ":" and first[0].isalpha():
        raise PackageIntegrityError(f"成员路径含盘符（Windows 变体）：{name!r}")
    if any(ch.isspace() for ch in name):
        raise PackageIntegrityError(f"成员路径含空白字符：{name!r}")


def _read_member_hash(fileobj, declared_size: int) -> tuple[str, int]:
    """Read members in declared-size chunks, accumulating actual bytes within bounds."""
    digest = hashlib.sha256()
    remaining = declared_size
    while remaining > 0:
        chunk = fileobj.read(min(_READ_CHUNK, remaining))
        if not chunk:
            break
        digest.update(chunk)
        remaining -= len(chunk)
    return digest.hexdigest(), declared_size - remaining


def _inspect_tar(content: bytes, fmt: str) -> PackageInspection:
    """Inspect tar incrementally: parse/count/check headers immediately, then read content
    boundedly.

    Never precollect getmembers/TarInfo. Stop above the count limit, then check
    paths/types/sizes/aliases; directories count but are omitted from manifests, and GNU
    sparse is explicitly rejected. Only after every header passes does a second pass
    read member bytes.
    """
    raw = io.BytesIO(content)
    _tar_physical_preflight(content)  # Physical preflight precedes any tarfile parsing.
    try:
        with tarfile.open(fileobj=raw, mode="r:") as archive:
            regular: list[tarfile.TarInfo] = []
            seen_exact: set[str] = set()
            seen_casefold: set[str] = set()
            total_declared = 0
            count = 0
            while True:
                try:
                    info = archive.next()
                except (tarfile.TarError, EOFError) as exc:
                    raise PackageIntegrityError(f"归档头部无法解析：{exc}") from exc
                except ValueError as exc:
                    raise TarMemberMetadataError(
                        f"成员元数据字段无法解析（支持字段上的数值/格式错误）：{exc}"
                    ) from exc
                if info is None:
                    break
                count += 1
                if count > MAX_ARCHIVE_MEMBERS:
                    raise PackageIntegrityError(
                        f"成员数量超过工程上限 {MAX_ARCHIVE_MEMBERS}"
                        f"（已逐项解析 {count} 个头部后立即停止）"
                    )
                name = info.name
                _check_member_name(name)
                if info.issparse():
                    raise PackageIntegrityError(
                        f"成员是 GNU sparse 文件，拒绝：{name!r}"
                    )
                if info.issym() or info.islnk():
                    raise PackageIntegrityError(f"成员是链接，拒绝：{name!r}")
                if info.isdev() or info.isfifo():
                    raise PackageIntegrityError(f"成员是设备或管道，拒绝：{name!r}")
                if info.isdir():
                    continue  # Directory entries count toward limits but not manifests.
                if not info.isfile():
                    raise PackageIntegrityError(
                        f"成员不是常规文件：{name!r}（type={info.type!r}）"
                    )
                if name in seen_exact:
                    raise PackageIntegrityError(f"成员重名：{name!r}")
                folded = name.casefold()
                if folded in seen_casefold:
                    raise PackageIntegrityError(
                        f"成员名大小写别名冲突（大小写不敏感文件系统上会覆盖）："
                        f"{name!r}"
                    )
                seen_exact.add(name)
                seen_casefold.add(folded)
                declared = int(info.size)
                if declared < 0:
                    raise PackageIntegrityError(f"成员声明尺寸非法：{name!r}")
                total_declared += declared
                if total_declared > MAX_TOTAL_DECOMPRESSED_BYTES:
                    raise PackageIntegrityError(
                        f"声明解压总量超过工程上限 {MAX_TOTAL_DECOMPRESSED_BYTES}"
                    )
                regular.append(info)
            if count == 0:
                raise PackageIntegrityError("归档中没有任何条目")
            if not regular:
                raise PackageIntegrityError("归档中没有任何常规文件成员")
            # Second pass reads contents boundedly only after every header constraint passes.
            members: list[PackageMember] = []
            total_read = 0
            for info in regular:
                fileobj = archive.extractfile(info)
                if (
                    fileobj is None
                ):  # pragma: no cover - isfile already excluded this case
                    raise PackageIntegrityError(f"成员无法读取：{info.name!r}")
                digest, actual = _read_member_hash(fileobj, int(info.size))
                total_read += actual
                if total_read > MAX_TOTAL_DECOMPRESSED_BYTES:
                    raise PackageIntegrityError(
                        f"实际解压总量超过工程上限 {MAX_TOTAL_DECOMPRESSED_BYTES}"
                    )
                if actual != int(info.size):
                    raise PackageIntegrityError(
                        f"成员实际字节数与声明不符：{info.name!r}"
                        f"（声明 {int(info.size)}，实际 {actual}）"
                    )
                members.append(
                    PackageMember(
                        name=info.name,
                        size=actual,
                        sha256=digest,
                        media_type=_media_type_for(info.name),
                    )
                )
    except tarfile.TarError as exc:
        raise PackageIntegrityError(f"归档无法解析：{exc}") from exc
    return PackageInspection(
        format=fmt, members=tuple(members), total_decompressed=total_read
    )


def inspect_package(payload: bytes) -> PackageInspection:
    """Inspect bounded source packages and build manifests without disk extraction.

    Bound original bytes first. Pass remaining allowances to zlib to limit allocation,
    handling EOF trailers and rejecting truncation/multiple members/trailing data.
    Parse/count/check tar headers individually without precollection, stopping on
    limits. Read contents boundedly only on a second pass after all headers pass.
    Members remain unparsed engineering bytes with no TeX semantics.
    """
    size = len(payload)
    if size == 0:
        raise PackageIntegrityError("源码包为空，不能当作可用材料")
    if size > MAX_PACKAGE_BYTES:
        raise PackageIntegrityError(
            f"源码包原始字节 {size} 超过工程上限 {MAX_PACKAGE_BYTES}"
        )
    fmt = sniff_package_format(payload)
    if fmt == "single_tex":
        member = PackageMember(
            name="main.tex",
            size=size,
            sha256=hashlib.sha256(payload).hexdigest(),
            media_type="text/x-tex",
        )
        return PackageInspection(format=fmt, members=(member,), total_decompressed=size)
    if fmt == "single_tex_gzip":
        content = bounded_gunzip(payload, MAX_TOTAL_DECOMPRESSED_BYTES)
        member = PackageMember(
            name="main.tex",
            size=len(content),
            sha256=hashlib.sha256(content).hexdigest(),
            media_type="text/x-tex",
        )
        return PackageInspection(
            format=fmt, members=(member,), total_decompressed=len(content)
        )
    content = (
        bounded_gunzip(payload, MAX_TOTAL_DECOMPRESSED_BYTES)
        if fmt == "tar_gzip"
        else payload
    )
    return _inspect_tar(content, fmt)

"""arXiv 源码包的工程级有界检查与清单（Lane B，C06 的本机部分）。

只做工程安全检查：识别 arXiv 源码包的官方形态——gzip 压缩 tar、
plain tar、gzip 压缩单 TeX 文件与纯单 TeX 文件——并在**任何危险分
配之前**施加输出上限：gzip 解压走流式有界解压（上限先于展开，超限
即停并拒绝多成员/尾随数据），tar 处理先做**仅头部**的第一遍检查
（成员数、路径、类型、声明尺寸与别名冲突），通过后第二遍才有界读
取成员内容。拒绝 PDF-only/HTML 形态、越界路径（绝对路径、``..``、
反斜杠/盘符等平台变体）、符号/硬链接与设备文件、重名或大小写别名
冲突成员、超大成员数与解压总量（声明预检 + 实际读取双保险）。

本模块不执行 TeX、不获取包外链接、不做任何正文或表格解析；
清单中所有成员的 ``read_state`` 都是 ``unparsed``。原始字节由调用方
（lifecycle 层）按内容寻址保留；"形态不支持"与"获取失败"是不同的
结果，分别用 :class:`UnsupportedPackageError` 与
:class:`PackageIntegrityError` 表达。
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

#: 单个源码包原始字节上限（与 Lane A 下载响应上限一致的工程护栏）。
MAX_PACKAGE_BYTES = 256 * 1024 * 1024

#: 归档成员数量上限（工程护栏，防成员数滥用）。
MAX_ARCHIVE_MEMBERS = 2000

#: 全部成员解压后总字节上限（工程护栏，防解压炸弹）。
MAX_TOTAL_DECOMPRESSED_BYTES = 1024 * 1024 * 1024

_READ_CHUNK = 1024 * 1024
_GUNZIP_CHUNK = 512 * 1024

_GZIP_MAGIC = b"\x1f\x8b"
_PDF_MAGIC = b"%PDF-"

#: 物理头部类型：PAX 扩展（x/g）与 GNU 长名/长链（L/K）。
_EXT_TYPEFLAGS = frozenset({b"x", b"g", b"L", b"K"})


class UnsupportedPackageError(Exception):
    """材料不是可识别的 LaTeX 源码包形态（如 PDF-only 或 HTML 页面）。

    这是"形态不支持/材料不可用"，不是获取失败：原始字节仍可由调用
    方保留，但不会产生可用的来源清单。
    """


class PackageIntegrityError(Exception):
    """包内容不安全或超出工程护栏（路径越界、链接/设备、重名、超限）。

    这是工程失败：不能把该包当作可用来源材料。
    """


@dataclass(frozen=True)
class PackageMember:
    """清单中一个成员的工程描述（未解析）。"""

    name: str
    size: int
    sha256: str
    media_type: str


@dataclass(frozen=True)
class PackageInspection:
    """一次有界检查的结果：包形态、成员清单与解压总量。"""

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
    """对"单文件形态"的内容做形态拒绝（PDF/HTML/二进制 NUL）。"""
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
    """流式有界 gzip 解压：解压前就限定每次调用的最大输出额度。

    与先解压再检查不同，这里把剩余额度（``output_cap - 已产出``）作为
    ``max_length`` 传给 :meth:`zlib.Decompress.decompress`（额度为 0 时
    一律用 1 字节探测——zlib 的 0 表示不限量，绝不传 0），zlib 在产出
    达到额度时立即停止并保留 ``unconsumed_tail``，因此**分配不会超
    过上限**。边界语义：

    - 输出恰等于额度而 CRC/结束标记在后续输入块时，继续以 1 字节额
      度消费尾块输入（尾块本身不产出数据），仅在探测到**实际额外输
      出**时才判超限——恰好等于上限的合法材料被接受；
    - EOF 后要求输入被完整消费（``unused_data``/``unconsumed_tail``
      为空且输入喂到尾部）：多成员与任何尾随数据一律拒绝；
    - 输入耗尽仍未到 EOF 判截断。
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
            break  # 输入耗尽仍未到 EOF：循环后按截断拒绝
        while chunk and not decompressor.eof and budget > 0:
            piece = decompressor.decompress(chunk, max_length=budget)
            if piece:
                output.extend(piece)
                budget -= len(piece)
            chunk = bytes(decompressor.unconsumed_tail)
        if decompressor.eof:
            break
        if budget <= 0:
            # 额度用尽：剩余输入里可能还有不产出数据的尾块（CRC/长
            # 度标记）。以 1 字节额度继续消费，只有真的多解出数据才
            # 算超限；消费完输入到达 EOF 则输出恰等于上限，接受。
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
    """从 512 字节头部块解析八进制尺寸字段；非法抛完整性错误。"""
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
    """支持字段上的成员元数据无法解析（库数值/格式错误的分类表达）。

    与"内容不像 tar"的结构性失败区分：这类错误如实向上传播为可观察
    的完整性错误，绝不退化为普通文本形态（无静默回退）。
    """


def _scan_pax_records_supported(payload: bytes) -> None:
    """有界校验 PAX 扩展负载属于**明确允许的记录子集**（允许清单）。

    只接受能按 ``长度 名字=值\n`` 完整切分、且每个记录名都在允许
    清单内的记录集；任何不在清单内的字段（``size``、``GNU.sparse.*``
    及一切未承诺字段）都会改变或触发 tarfile 的布局/稀疏解释，在进
    入 tarfile 之前明确报告不支持。无法完整切分的负载同样报不支持
    （不猜测 tarfile 会如何容错解释）。
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
    """物理层预检：在 tarfile 解析任何头部之前限定物理布局。

    只按 512 字节块步行头部并读尺寸/类型字段，采用**明确支持的扩
    展子集**策略（不仿真 tarfile 的覆盖语义）：

    - 物理头部总数（含扩展头与真实成员）不得超过成员上限；
    - 每个成员**至多一个**扩展头：连续扩展头链（多级覆盖）明确报
      不支持，先于 tarfile 的递归处理拒绝；
    - 局部 PAX（``x``）与 GNU 长名（``L``）支持，负载声明尺寸与
      累计字节先受解压总量上限检查，通过后才有界读取负载；``x``
      的记录集必须完整切分且不含 ``size`` 记录（见
      :func:`_scan_pax_records_supported`）；
    - 全局 PAX（``g``，覆盖跨成员继承）与 GNU 长链接（``K``）明
      确报不支持；
    - 由于一切尺寸覆盖变体都在预检层拒绝，成员数据块按头部声明尺
      寸推进，与 tarfile 的实际偏移语义一致，不存在需要仿真的分
      歧窗口。

    越限抛 :class:`PackageIntegrityError`（完整性边界）；不支持的
    特殊变体抛 :class:`UnsupportedPackageError`（能力边界，调用方
    保留原始字节并给出可观察的“不支持”结果）。
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
            break  # 结束零块：物理扫描到此为止
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
            # GNU ``L`` 负载是长文件名字符串，不影响后续成员的数据
            # 布局；上限检查已在上方完成。
            offset += 512 + ((size + 511) // 512) * 512
            continue
        chain = 0
        offset += 512 + ((size + 511) // 512) * 512
    if not saw_entry and total > 0:
        # 没有任何非零头部块：不是可识别的 tar；由调用方按形态拒绝。
        return


def _iter_tar_headers(archive: tarfile.TarFile, *, member_cap: int) -> int:
    """逐项迭代 tar 头部并计数，达到上限立即停止。

    不用会预先收集全部成员的 ``getmembers()``：恶意归档可在被拒前
    迫使它构造任意多 TarInfo（含扩展头读取）。这里每读到一个头部就
    计数并在超限时立即抛错，解析工作量与上限成正比。返回成员总数
    （含目录条目）。
    """
    count = 0
    while True:
        try:
            info = archive.next()
        except (tarfile.TarError, EOFError) as exc:
            raise PackageIntegrityError(f"归档头部无法解析：{exc}") from exc
        except ValueError as exc:
            # 支持字段上的数值/格式错误（如 mtime 非法）分类为成员元数
            # 据错误：可观察、可审计，绝不静默退化为普通文本形态。
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
    """判断内容是否为可解析的 tar（含旧式头部变体）。

    依赖 tarfile 的头部校验和验证：随机文本几乎不可能通过校验和。
    逐项有界迭代头部（不预收集全部成员）；成员数超限按否处理，由后
    续完整检查给出准确错误。
    """
    if len(content) >= 262 and content[257:262] == b"ustar":
        _tar_physical_preflight(content)
        return True
    try:
        _tar_physical_preflight(content)
        with tarfile.open(fileobj=io.BytesIO(content), mode="r:") as archive:
            count = _iter_tar_headers(archive, member_cap=MAX_ARCHIVE_MEMBERS)
    except TarMemberMetadataError:
        # 支持字段上的元数据错误：这是"像 tar 的内容解析失败"，必须
        # 以分类错误向上传播（可观察），不能退化为普通文本形态。
        raise
    except PackageIntegrityError:
        return False
    except (tarfile.TarError, EOFError):
        return False
    return count > 0


def sniff_package_format(payload: bytes) -> str:
    """按魔数与结构识别包形态；不安全形态抛 :class:`UnsupportedPackageError`。

    返回 ``"tar_gzip"``、``"tar_plain"``、``"single_tex_gzip"`` 或
    ``"single_tex"``。gzip 先做**有界**解压（上限为全部解压总量护栏），
    再判断内容是 tar 还是单 TeX——arXiv 对单文件源会返回 gzip 压缩的
    单 TeX；gzip 之后的尾随数据/多成员由有界解压一并拒绝。
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
    """拒绝越界与平台变体路径：绝对路径、``..``、反斜杠、盘符、空段。"""
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
    """有界读取一个成员：按声明尺寸分块读并累计实际读取量。"""
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
    """逐项有界的 tar 检查：头部逐个解析并立即校验，再有界读内容。

    不使用会预先收集全部成员的 ``getmembers()``：恶意归档可在被拒
    前迫使它构造任意多 TarInfo（含 PAX/GNU 扩展头读取）。这里每读
    到一个头部就先计数（超过上限立即停止），再校验路径/类型/声明
    尺寸/别名；目录条目计入总数但不进清单；GNU sparse 成员显式拒
    绝。全部头部通过后第二遍才有界读取成员字节。
    """
    raw = io.BytesIO(content)
    _tar_physical_preflight(content)  # 物理预检先于 tarfile 任何解析
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
                    continue  # 目录条目计入总数但不进入清单
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
            # 第二遍：全部头部约束通过后才有界读取成员内容。
            members: list[PackageMember] = []
            total_read = 0
            for info in regular:
                fileobj = archive.extractfile(info)
                if fileobj is None:  # pragma: no cover - isfile 已排除此情况
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
    """有界检查一个源码包并构建清单；不向磁盘解压任何成员。

    安全顺序：先护栏原始字节；gzip 解压把剩余额度作为 ``max_length``
    传给 zlib，**分配不会超过上限**（含 EOF 后排空，截断/多成员/尾
    随数据拒绝）；tar 头部逐项解析并立即计数校验（超过上限立即停，
    不预收集全部成员），全部通过后第二遍才有界读取成员字节。清单
    成员全部标记为未解析的工程字节；不做任何 TeX 语义解释。
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

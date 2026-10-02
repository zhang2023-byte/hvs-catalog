"""来源包展开：安全提取成员、文本解码与派生文件登记。

输入是获取阶段（R10）保存的来源包记录修订与原包字节哈希；复用
``inspect_package`` 的有界包检查与安全成员规则，不重新下载或混用版本。
本模块把通过检查的普通成员按内容寻址写入工作空间 ``sources/``，并登
记每个成员的字节哈希、媒体类型与读取状态；不为缺失文件造哈希或行号。

文本解码只按明确依据进行：UTF-8 直接解码；成员声明（``% !TeX
encoding``）或操作者固定选择（请求 ``member_encoding``）为 latin-1
时，按每字节一一映射生成保留行数的 UTF-8 派生文本并记录原字节、编
码与映射。未声明且 UTF-8 解码失败的成员保持未处理（不猜编码）；
带 BOM 的 UTF-16 等其他明确编码当前不支持，同样保持未处理。
"""

from __future__ import annotations

import hashlib
import io
import re
import tarfile
from dataclasses import dataclass, field, replace

from ..pipeline.acquisition.archive import (
    MAX_TOTAL_DECOMPRESSED_BYTES,
    PackageInspection,
    PackageIntegrityError,
    _media_type_for,
    bounded_gunzip,
    inspect_package,
)
from ..workspace import Workspace

__all__ = [
    "MAX_MEMBER_TEXT_BYTES",
    "ExpandedMember",
    "ExpandedPackage",
    "MaterialExpansionError",
    "expand_source_package",
]

#: 单个成员作为正文/表格输入处理的字节上限（工程护栏）。
MAX_MEMBER_TEXT_BYTES = 32 * 1024 * 1024


class MaterialExpansionError(Exception):
    """来源包展开的工程失败；原包与已提交事实保留。"""


@dataclass(frozen=True)
class ExpandedMember:
    """一个已展开成员的工程描述（原字节＋可选文本视图）。"""

    name: str
    sha256: str
    size: int
    media_type: str
    # 可用的正文文本（UTF-8 或 latin-1 原文；后者同时提供派生 UTF-8）。
    text: str | None = None
    encoding: str | None = None
    # 非 UTF-8 解码时的派生 UTF-8 文本（行数与原字节一一对应）及其哈希。
    derived_text: str | None = None
    derived_sha256: str | None = None
    # 解码不支持的明确说明；None 表示成功或按二进制保留。
    decode_note: str | None = None

    @property
    def decodable(self) -> bool:
        return self.text is not None


@dataclass
class ExpandedPackage:
    """一次展开的结果：原包哈希、包形态与成员清单。"""

    raw_sha256: str
    package_format: str
    total_decompressed: int
    members: list[ExpandedMember] = field(default_factory=list)

    def member(self, name: str) -> ExpandedMember | None:
        return next((m for m in self.members if m.name == name), None)


_TEXT_SUFFIXES = (
    ".tex",
    ".sty",
    ".cls",
    ".bib",
    ".bbl",
    ".bst",
    ".txt",
    ".md",
    ".csv",
    ".ecsv",
    ".json",
)


#: 受支持的文本编码（latin-1 每字节映射一个字符，行数与字节一一对应）。
SUPPORTED_TEXT_ENCODINGS = ("utf-8", "latin-1")

_DECLARED_ENCODING_RE = re.compile(
    rb"%\s*!TeX\s+encoding\s*=\s*([\w\-]+)", re.IGNORECASE
)


def _declared_encoding(payload: bytes) -> str | None:
    """读取成员自身的显式编码声明（``% !TeX encoding = X``）。"""
    head = payload[:4096]
    match = _DECLARED_ENCODING_RE.search(head)
    if match is None:
        return None
    try:
        return match.group(1).decode("ascii").strip().lower()
    except UnicodeDecodeError:  # pragma: no cover - 声明区不可为非 ASCII
        return None


def _decode_member(
    name: str, payload: bytes, *, fixed_encoding: str | None = None
) -> ExpandedMember:
    """按受支持方式解码一个成员；不使用 errors=replace 静默修补。

    只接受显式确认或操作者固定选择的受支持编码（utf-8／latin-1）；
    未声明且未固定、声明未支持或解码失败时保持未处理，不以 latin-1
    作为万能成功回退。
    """
    lowered = name.lower()
    digest = hashlib.sha256(payload).hexdigest()
    base = ExpandedMember(
        name=name,
        sha256=digest,
        size=len(payload),
        media_type=_media_type_for(lowered),
    )
    if not lowered.endswith(_TEXT_SUFFIXES):
        return base
    if len(payload) > MAX_MEMBER_TEXT_BYTES:
        return replace(base, decode_note="成员超过文本解码字节上限，保持未处理")
    if payload.startswith((b"\xff\xfe", b"\xfe\xff")):
        return replace(
            base, decode_note="UTF-16 BOM：该明确编码的解码当前未支持，保持未处理"
        )
    if b"\x00" in payload:
        return replace(base, decode_note="含二进制 NUL 字节，不可按文本解码")
    declared = _declared_encoding(payload)
    chosen: str | None = None
    if declared is not None:
        if declared not in SUPPORTED_TEXT_ENCODINGS:
            return replace(
                base,
                decode_note=(
                    f"成员声明的编码 {declared} 不在受支持集"
                    f"{list(SUPPORTED_TEXT_ENCODINGS)}，保持未处理"
                ),
            )
        chosen = declared
    elif fixed_encoding in SUPPORTED_TEXT_ENCODINGS:
        chosen = fixed_encoding
    if chosen == "latin-1":
        text = payload.decode("latin-1")
        derived = text.encode("utf-8")
        return replace(
            base,
            text=text,
            encoding="latin-1",
            derived_text=text,
            derived_sha256=hashlib.sha256(derived).hexdigest(),
            decode_note=(
                "latin-1 按每字节映射派生 UTF-8 文本；原字节保留在成员文件"
                + ("（成员自身声明）" if declared else "（操作者固定选择）")
            ),
        )
    try:
        text = payload.decode("utf-8-sig")
    except UnicodeDecodeError:
        return replace(
            base,
            decode_note=(
                "非 UTF-8 且未获显式声明或操作者固定选择的受支持编码，"
                "保持未处理（不静默猜 latin-1）"
            ),
        )
    return replace(base, text=text, encoding="utf-8")


def expand_source_package(
    space: Workspace, payload: bytes, *, fixed_encoding: str | None = None
) -> ExpandedPackage:
    """有界展开一个来源包；成员按内容寻址发布到工作空间 ``sources/``。

    包形态与成员安全检查完全复用获取阶段的 ``inspect_package``：越界
    路径、链接、设备、重名与超限在检查阶段已被拒绝，这里不再自行解包。
    """
    try:
        inspection: PackageInspection = inspect_package(payload)
    except PackageIntegrityError as exc:
        raise MaterialExpansionError(f"来源包未通过既有安全检查：{exc}") from exc
    members: list[ExpandedMember] = []
    for entry in inspection.members:
        member_payload = _extract_member(payload, inspection, entry.name)
        member = _decode_member(
            entry.name, member_payload, fixed_encoding=fixed_encoding
        )
        published = space.publish_source_file(
            f"materials/members/{member.sha256}.bin", member_payload
        )
        if published.sha256 != member.sha256:  # pragma: no cover - 内容寻址自洽
            raise MaterialExpansionError(f"成员内容寻址发布失败：{entry.name}")
        members.append(member)
    return ExpandedPackage(
        raw_sha256=hashlib.sha256(payload).hexdigest(),
        package_format=inspection.format,
        total_decompressed=inspection.total_decompressed,
        members=members,
    )


def _extract_member(payload: bytes, inspection: PackageInspection, name: str) -> bytes:
    """从原包字节提取一个已通过检查的成员；不做任何磁盘解包。"""
    if inspection.format == "single_tex":
        return payload
    if inspection.format == "single_tex_gzip":
        return bounded_gunzip(payload, MAX_TOTAL_DECOMPRESSED_BYTES)
    content = (
        bounded_gunzip(payload, MAX_TOTAL_DECOMPRESSED_BYTES)
        if inspection.format == "tar_gzip"
        else payload
    )
    raw = io.BytesIO(content)
    try:
        with tarfile.open(fileobj=raw, mode="r:") as archive:
            info = None
            while True:
                candidate = archive.next()
                if candidate is None:
                    break
                if candidate.isfile() and candidate.name == name:
                    info = candidate
                    break
            if info is None:  # pragma: no cover - 清单来自同一检查
                raise MaterialExpansionError(f"成员不在原包内：{name}")
            fileobj = archive.extractfile(info)
            if fileobj is None:  # pragma: no cover - isfile 已排除
                raise MaterialExpansionError(f"成员无法读取：{name}")
            data = fileobj.read(int(info.size))
    except tarfile.TarError as exc:  # pragma: no cover - 检查已通过
        raise MaterialExpansionError(f"成员提取失败：{name}：{exc}") from exc
    if len(data) != int(info.size):  # pragma: no cover - 检查已核对尺寸
        raise MaterialExpansionError(f"成员字节数与声明不符：{name}")
    return data

"""Expand source packages safely, decode text, and register derived files.

Use acquisition's saved source revision and original byte hash with bounded
inspect_package/member safety checks, without redownload/version mixing. Publish checked
regular members content-addressably under sources/ with hashes, media types, and read
states; never invent hashes/line numbers for missing files.

Decode only from explicit grounds: UTF-8 directly, or declared % !TeX encoding / fixed
member_encoding latin-1 through one-character-per-byte mapping to line-preserving
derived UTF-8 with original bytes/encoding/mapping retained. Undeclared UTF-8 failures
remain unprocessed without guessing. Unsupported explicit encodings such as BOM-marked
UTF-16 also remain unprocessed.
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

#: Byte limit for one member processed as body/table input.
MAX_MEMBER_TEXT_BYTES = 32 * 1024 * 1024


class MaterialExpansionError(Exception):
    """Engineering package-expansion failure; retain original packages and committed facts."""


@dataclass(frozen=True)
class ExpandedMember:
    """Engineering description of an expanded member: original bytes and optional text
    view.
    """

    name: str
    sha256: str
    size: int
    media_type: str
    # Usable UTF-8 or latin-1 body text; latin-1 also supplies derived UTF-8.
    text: str | None = None
    encoding: str | None = None
    # Derived UTF-8 text/hash for non-UTF-8 decoding, with lines corresponding to original bytes.
    derived_text: str | None = None
    derived_sha256: str | None = None
    # Explicit unsupported-decoding reason; None means success or binary retention.
    decode_note: str | None = None

    @property
    def decodable(self) -> bool:
        return self.text is not None


@dataclass
class ExpandedPackage:
    """Expansion result: original package hash, format, and member manifest."""

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


#: Supported encodings; latin-1 maps each byte to a character while preserving line correspondence.
SUPPORTED_TEXT_ENCODINGS = ("utf-8", "latin-1")

_DECLARED_ENCODING_RE = re.compile(
    rb"%\s*!TeX\s+encoding\s*=\s*([\w\-]+)", re.IGNORECASE
)


def _declared_encoding(payload: bytes) -> str | None:
    """Read the member's explicit % !TeX encoding = X declaration."""
    head = payload[:4096]
    match = _DECLARED_ENCODING_RE.search(head)
    if match is None:
        return None
    try:
        return match.group(1).decode("ascii").strip().lower()
    except UnicodeDecodeError:  # pragma: no cover - the declaration area must be ASCII
        return None


def _decode_member(
    name: str, payload: bytes, *, fixed_encoding: str | None = None
) -> ExpandedMember:
    """Decode members using supported methods without silent errors=replace repair.

    Accept explicitly declared or operator-fixed utf-8/latin-1 only. Missing
    declarations/choices, unsupported declarations, or failed decoding remain
    unprocessed; latin-1 is never a universal success fallback.
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
    """Expand a bounded source package and publish content-addressed members under
    sources/.

    Reuse acquisition inspect_package format/safety checks completely: escaping paths,
    links, devices, duplicates, and limits are already rejected. Do not implement
    separate unchecked extraction.
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
        if (
            published.sha256 != member.sha256
        ):  # pragma: no cover - content addressing is self-consistent
            raise MaterialExpansionError(f"成员内容寻址发布失败：{entry.name}")
        members.append(member)
    return ExpandedPackage(
        raw_sha256=hashlib.sha256(payload).hexdigest(),
        package_format=inspection.format,
        total_decompressed=inspection.total_decompressed,
        members=members,
    )


def _extract_member(payload: bytes, inspection: PackageInspection, name: str) -> bytes:
    """Extract one checked member from original package bytes without disk unpacking."""
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
            if (
                info is None
            ):  # pragma: no cover - the manifest comes from the same inspection
                raise MaterialExpansionError(f"成员不在原包内：{name}")
            fileobj = archive.extractfile(info)
            if fileobj is None:  # pragma: no cover - isfile already excluded this case
                raise MaterialExpansionError(f"成员无法读取：{name}")
            data = fileobj.read(int(info.size))
    except tarfile.TarError as exc:  # pragma: no cover - inspection already passed
        raise MaterialExpansionError(f"成员提取失败：{name}：{exc}") from exc
    if len(data) != int(
        info.size
    ):  # pragma: no cover - inspection already verified size
        raise MaterialExpansionError(f"成员字节数与声明不符：{name}")
    return data

"""Complete, bounded integer notation parsing, never substring expansion.

A lexical prefix does not prove object meaning. Callers must establish object
context from the actual source or an applicable expert decision. Unsupported
segments produce no guessed members. Adapted from stella_hvs (MIT); see
LICENSE-NOTICE.md.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

MAX_RANGE_MEMBERS = 50
_MAX_NOTATION_CHARS = 4096
_DASH = r"(?:--|[-\u2013\u2014])"
_ATOM = re.compile(r"^(?P<prefix>[A-Za-z][A-Za-z0-9 _-]*?)?(?P<number>\d{1,12})$")
_SEPARATOR = re.compile(rf"\s+(?:to|through)\s+|{_DASH}", re.IGNORECASE)
_REMAINDER = re.compile(r"\s+(and\s+others)\s*$", re.IGNORECASE)


@dataclass(frozen=True)
class RangeExpansion:
    notation: str
    status: str
    identifiers: list[str] = field(default_factory=list)
    rule: str = ""
    remainder: str | None = None
    reason: str = ""

    @property
    def expanded(self) -> bool:
        return self.status == "expanded"


def object_context(view: str, start: int, end: int) -> bool:
    """Bounded positive source context; no letter-prefix or arbitrary-unit guess."""
    before = view[max(0, start - 160) : start]
    roles = list(
        re.finditer(
            r"\b(?:stars?|objects?|candidates?|targets?|sources?)\b",
            before,
            re.IGNORECASE,
        )
    )
    if not roles:
        return False
    physical = list(
        re.finditer(
            r"\b(?:temperature|velocit(?:y|ies)|density|distance|mass|figure|table|section)\b",
            before,
            re.IGNORECASE,
        )
    )
    if physical and physical[-1].start() > roles[-1].start():
        return False
    return (
        re.match(
            r"\s*(?:K\b|km\b|mas\b|kpc\b|Mpc\b|arcsec\b|deg\b)", view[end : end + 20]
        )
        is None
    )


def expand_range_notation(
    text: str, *, max_members: int = MAX_RANGE_MEMBERS, object_context: bool = False
) -> RangeExpansion:
    """Parse every segment; no partial success after an unsupported suffix."""
    original = text.strip() if isinstance(text, str) else ""
    remainder = None

    def fail(reason, status="unsupported"):
        return RangeExpansion(original, status, remainder=remainder, reason=reason)

    if not original or len(original) > _MAX_NOTATION_CHARS:
        return fail("范围记法必须非空且不超过字符上限")
    if (
        isinstance(max_members, bool)
        or not isinstance(max_members, int)
        or max_members < 1
    ):
        return fail("max_members 必须是正整数")
    notation = original
    if (match := _REMAINDER.search(notation)) is not None:
        remainder = match[1]
        notation = notation[: match.start()].rstrip()
    segments = notation.split(",")
    if any(not segment.strip() for segment in segments):
        return fail("范围记法含空段")
    prefix, identifiers, seen, rule = None, [], set(), "stable-prefix-integer-list"
    for segment_index, segment in enumerate(segments):
        segment = segment.strip()
        # Find a separator with a valid full atom on each side. Prefix hyphens
        # (FIC-001) are not mistaken for numeric range separators.
        candidates = []
        for match in _SEPARATOR.finditer(segment):
            left, right = (
                _ATOM.fullmatch(segment[: match.start()].strip()),
                _ATOM.fullmatch(segment[match.end() :].strip()),
            )
            if left and right:
                candidates.append((left, right, match[0]))
        if len(candidates) > 1:
            return fail("范围分隔不能唯一解释")
        if candidates:
            left, right, separator = candidates[0]
            rule = (
                "word-delimited-range"
                if re.search(r"to|through", separator, re.IGNORECASE)
                else "repeated-prefix-dash-range"
                if right["prefix"]
                else rule
            )
        else:
            left = _ATOM.fullmatch(segment)
            right = None
            if left is None:
                return fail("完整范围含不支持的片段或尾注；不截断有效前缀")
        current = left["prefix"] or prefix
        if segment_index == 0:
            prefix = left["prefix"]
        if current != prefix:
            return fail("列表的对象前缀不一致")
        if right and right["prefix"] and right["prefix"] != prefix:
            return fail("范围两端的对象前缀不一致")
        start_text, end_text = (
            left["number"],
            right["number"] if right else left["number"],
        )
        start, end = int(start_text), int(end_text)
        if end < start:
            return fail("编号范围必须升序")
        width = 0
        if start_text.startswith("0") or end_text.startswith("0"):
            if len(start_text) != len(end_text):
                return fail("前导零端点宽度不一致")
            width = len(start_text)
        if end - start + 1 > max_members - len(identifiers):
            if not prefix:
                return fail("纯整数范围没有明确对象编号前缀，保持未决", "ambiguous")
            return fail(f"范围超过 {max_members} 个成员的上限")
        for number in range(start, end + 1):
            item = (prefix or "") + str(number).zfill(width)
            if item.casefold() in seen:
                return fail("范围记法重复了标识符")
            identifiers.append(item)
            seen.add(item.casefold())
    if not prefix:
        return fail("纯整数范围没有明确对象编号前缀，保持未决", "ambiguous")
    if not object_context:
        return fail("稳定字母前缀不能证明对象编号语境，保持未决", "ambiguous")
    return RangeExpansion(original, "expanded", identifiers, rule, remainder)

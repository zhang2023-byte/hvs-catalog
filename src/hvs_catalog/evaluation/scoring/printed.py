"""Approved, scale-preserving formatting comparisons with binary outcomes.

Exact strings match. Otherwise, only the supported formatting rules can establish a
match. All other expressions differ. No arithmetic, tolerances or semantic models.
"""

from __future__ import annotations

import re
from typing import Literal

Verdict = Literal["equivalent", "different"]

# Only these approved LaTeX macros participate in formatting equivalence.
_KNOWN_MACROS = frozenset(
    {
        "times",
        "mathrm",
        ",",
        ";",
        "!",
        " ",
        "quad",
        "qquad",
        "pm",
        "circ",
        "sim",
        "approx",
    }
)

_SUPERSCRIPTS = {
    "⁰": "0",
    "¹": "1",
    "²": "2",
    "³": "3",
    "⁴": "4",
    "⁵": "5",
    "⁶": "6",
    "⁷": "7",
    "⁸": "8",
    "⁹": "9",
    "⁻": "-",
    "⁺": "+",
}

_MACRO_RE = re.compile(r"\\([A-Za-z]+|[^A-Za-z])")
_SUP_RUN_RE = re.compile("[" + "".join(_SUPERSCRIPTS) + "]+")
_TIMES_SPACE_RE = re.compile(r"\s*\*\s*")
_WS_RE = re.compile(r"\s+")

# A4 sexagesimal script markers: collapse only these three single-letter markers without degree conversion.
_SEXAGESIMAL_MARKS = ("h", "m", "s")

# Full restricted numeric grammar after normalization: signed decimals with optional e/E exponents,
# approved *10^{signed integer} powers, or ordered hour-minute-second chains. Allowed characters
# alone do not prove valid syntax: incomplete exponents (8.2e+), broken powers, and unmatched
# parentheses cannot establish formatting equivalence.
_NUM_ATOM = r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)"
_NUM_EXP = r"(?:[eE][+-]?\d+)"
_NUM_POW = r"(?:\*10\^\{[+-]?\d+\})"
_NUM_SEXAGESIMAL = (
    rf"(?:{_NUM_ATOM}h\s*{_NUM_ATOM}m\s*{_NUM_ATOM}s"
    rf"|{_NUM_ATOM}h\s*{_NUM_ATOM}m"
    rf"|{_NUM_ATOM}m\s*{_NUM_ATOM}s"
    rf"|{_NUM_ATOM}h"
    rf"|{_NUM_ATOM}m"
    rf"|{_NUM_ATOM}s)"
)
_NUMERIC_FORM = re.compile(
    rf"(?:{_NUM_ATOM}(?:{_NUM_EXP}|{_NUM_POW})?|{_NUM_SEXAGESIMAL})"
)
_COLON_FORM = re.compile(r"[+-]?\d+:(\d{1,2}(?:\.\d+)?)(?::(\d{1,2}(?:\.\d+)?))?")
_APPROXIMATION = re.compile(r"^(?:about\s+|approximately\s+|[∼≈≃]\s*)")
_SPACED_INTEGER = re.compile(r"[+-]?[0-9](?: [0-9])+")
_UNIT_FACTOR = re.compile(
    r"(?P<symbol>[A-Za-z]+|°|%)(?:\^(?:\{(?P<braced>[+-]?\d+)\}|(?P<power>[+-]?\d+))|(?P<suffix>-\d+))?"
)
_UNIT_ROLES = re.compile(r"\s*(.+?)\s*(?:;\s*error\s+|±\s*)(.+?)\s*")


def _normalize_whitespace(text: str) -> str:
    return _WS_RE.sub(" ", text).strip()


def normalize_printed(text: str) -> str | None:
    """Restricted formatting normalization, returning None for unknown macros
    or unsupported expressions.

    Produce temporary comparison interpretations only; never rewrite records. Preserve
    precision, coefficient trailing zeros, exponent signs, and unit scales so C1/B1/B2
    remain distinct.
    """
    for match in _MACRO_RE.finditer(text):
        if match.group(1) not in _KNOWN_MACROS:
            return None
    work = text.replace("−", "-")
    # A leading ASCII tilde immediately qualifying a number is approximation;
    # interior TeX nonbreaking spacing remains spacing.
    work = re.sub(r"^\s*~\s*(?=[+-]?(?:\d|\.\d))", "∼", work)
    work = _SUP_RUN_RE.sub(
        lambda m: "^{" + "".join(_SUPERSCRIPTS[ch] for ch in m.group(0)) + "}",
        work,
    )
    work = _unwrap_mathrm(work)
    for spacing in ("\\,", "\\;", "\\ ", "~"):
        work = work.replace(spacing, " ")
    work = work.replace("\\!", "")
    work = work.replace("\\times", "*").replace("×", "*")
    for macro, mark in (("pm", "±"), ("circ", "°"), ("sim", "∼"), ("approx", "≈")):
        work = work.replace("\\" + macro, mark)
    work = _TIMES_SPACE_RE.sub("*", work)
    for mark in _SEXAGESIMAL_MARKS:
        work = work.replace("^{" + mark + "}", mark)
    return _normalize_whitespace(work)


def _unwrap_mathrm(text: str) -> str:
    while True:
        start = text.find("\\mathrm{")
        if start < 0:
            return text
        depth = 1
        index = start + len("\\mathrm{")
        while index < len(text) and depth:
            if text[index] == "{":
                depth += 1
            elif text[index] == "}":
                depth -= 1
            index += 1
        if depth:
            return text
        inner = text[start + len("\\mathrm{") : index - 1]
        text = text[:start] + inner + text[index:]


def is_supported_number_syntax(normalized: str) -> bool:
    """Whether normalized text fully matches restricted numeric grammar, beyond a character
    allowlist.
    """
    plain = _APPROXIMATION.sub("", normalized, count=1)
    if _NUMERIC_FORM.fullmatch(plain) is not None:
        return True
    colon = _COLON_FORM.fullmatch(plain)
    if colon is None:
        return False
    minute, second = colon.groups()
    return float(minute) < 60 and (
        second is None or ("." not in minute and float(second) < 60)
    )


def _number_form(text: str) -> str | None:
    normalized = normalize_printed(text)
    if normalized is None:
        return None
    normalized = re.sub(r"\s*:\s*", ":", normalized)
    plain = _APPROXIMATION.sub("", normalized, count=1)
    if _SPACED_INTEGER.fullmatch(plain):
        # Recover only uniformly separated digits in one complete scalar integer.
        # Never join grouped numbers, decimal/exponent fragments, or list syntax.
        normalized = normalized[: len(normalized) - len(plain)] + plain.replace(" ", "")
    if not is_supported_number_syntax(normalized):
        return None
    return _APPROXIMATION.sub("approx ", normalized, count=1)


def split_unit_roles(unit: str | None) -> tuple[str | None, str | None]:
    """Read explicitly printed value/error units without filling or converting scales."""
    if unit is None:
        return None, None
    match = _UNIT_ROLES.fullmatch(unit)
    return match.groups() if match else (unit, unit)


def _unit_factors(text: str) -> tuple | None:
    # Escaping the exponent marker does not change an explicit unit exponent.
    # Keep this recovery unit-only and require a complete signed integer power.
    text = re.sub(r"(?<=[A-Za-z°%])\\\^(?=\{[+-]?\d+\})", "^", text)
    normalized = normalize_printed(text)
    if normalized is None:
        return None
    if normalized in {"°", "^{°}", "{}^{°}"}:
        normalized = "deg"
    if normalized in {"percent", "per cent"}:
        normalized = "%"
    sides = normalized.split("/")
    if len(sides) > 2:
        return None
    factors: dict[str, int] = {}
    for side_number, side in enumerate(sides):
        side = side.strip()
        if side == "1" and side_number == 0:
            continue
        matches = list(_UNIT_FACTOR.finditer(side))
        if not matches or (side_number and len(matches) != 1):
            return None
        end = 0
        for match in matches:
            gap = side[end : match.start()]
            if (end == 0 and gap.strip()) or (
                end and (not gap or re.fullmatch(r"\s*(?:[·*]\s*)?", gap) is None)
            ):
                return None
            symbol = match["symbol"]
            symbol = "deg" if symbol == "°" else symbol
            exponent = int(match["braced"] or match["power"] or match["suffix"] or 1)
            factors[symbol] = factors.get(symbol, 0) + (
                -exponent if side_number else exponent
            )
            end = match.end()
        if side[end:].strip():
            return None
    return tuple(sorted(factors.items()))


def split_error_number(
    text: str, unit: str | None, *, symmetric: bool
) -> tuple[str, str | None]:
    """Recognize a redundant symmetric sign and explicit inline error unit only."""
    normalized = normalize_printed(text)
    if normalized is None:
        return text, unit
    if symmetric and normalized.startswith("±"):
        operand = normalized[1:].strip()
        if operand.startswith("-"):
            return text, unit
        normalized = operand
    if _number_form(normalized) is not None:
        return normalized, unit
    for match in re.finditer(r"\s+", normalized):
        number, printed_unit = normalized[: match.start()], normalized[match.end() :]
        if _number_form(number) is not None and _unit_factors(printed_unit) is not None:
            return number, printed_unit
    return text, unit


def limit_operand(text: str, limit_kind: str, *, inclusive: bool | None = None) -> str:
    """Separate a printed inequality only when its direction is already stored."""
    direction = {"upper_limit": r"(?:<=|<|≤)", "lower_limit": r"(?:>=|>|≥)"}.get(
        limit_kind
    )
    if direction is None:
        return text
    normalized = normalize_printed(text)
    if normalized is None:
        return text
    match = re.match(r"^(" + direction + r")\s*", normalized)
    if match is None or (
        inclusive is not None and inclusive != (match[1] in {"<=", "≤", ">=", "≥"})
    ):
        return text
    return normalized[match.end() :]


def percent_operand(text: str, unit: str | None) -> str:
    """Read a redundant percent mark only when the stored unit already is percent."""
    normalized = normalize_printed(text)
    if (
        unit is not None
        and compare_unit(unit, "%") == "equivalent"
        and normalized is not None
        and normalized.endswith("%")
        and _number_form(normalized[:-1].strip()) is not None
    ):
        return normalized[:-1].strip()
    return text


def printed_limit_inclusive(text: str, limit_kind: str) -> bool | None:
    """Keep an explicit printed endpoint distinction even if stored flags are absent."""
    direction = {"upper_limit": r"(?:<=|<|≤)", "lower_limit": r"(?:>=|>|≥)"}.get(
        limit_kind
    )
    normalized = normalize_printed(text)
    if direction is None or normalized is None:
        return None
    match = re.match(r"^(" + direction + r")\s*", normalized)
    if match is None or _number_form(normalized[match.end() :]) is None:
        return None
    return match[1] in {"<=", "≤", ">=", "≥"}


def compare_number(gold: str, prediction: str) -> Verdict:
    """Match exact strings or approved numeric formatting only."""
    if gold == prediction:
        return "equivalent"
    left, right = _number_form(gold), _number_form(prediction)
    return "equivalent" if left is not None and left == right else "different"


def compare_unit(gold: str, prediction: str) -> Verdict:
    """Match exact units or approved same-scale notation, without conversion."""
    if gold == prediction:
        return "equivalent"
    left = tuple(_unit_factors(unit) for unit in split_unit_roles(gold))
    right = tuple(_unit_factors(unit) for unit in split_unit_roles(prediction))
    return (
        "equivalent"
        if all(part is not None for part in (*left, *right)) and left == right
        else "different"
    )

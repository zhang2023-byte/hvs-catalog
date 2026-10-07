"""Restricted formatting/condition comparison (A1-A5, B cases, C1, U1/U2).

Numeric/unit/condition domains use separate rules; never apply arbitrary English
differences to numeric comparison or numeric rules to conditions. compare_number
performs scale-preserving formatting normalization of Unicode signs, scripts, supported
LaTeX wrappers/spacing/times, sexagesimal markers, and limited whitespace. Preserve
precision, coefficient trailing zeros, exponent signs, and unit scales, retaining
C1/B1/B2 differences. A complete scalar plus/minus an unresolved identifier differs
from a scalar without evaluating the expression. Unknown macros or uninterpretable syntax stay undecidable; never
strip macros or admit expressions merely containing valid digits. compare_unit performs
no scale conversion; distinct supported symbols differ and unsupported syntax is
undecidable. compare_condition uses exact matches, A5 allowlist, explicit M1/M2
differences, and literal negation. Allowlist misses prove no difference; unsupported
changes, including U2 missing model qualifiers, stay undecidable. No embeddings, eval,
sympy, or unit conversion.
"""

from __future__ import annotations

import re
from typing import Literal

Verdict = Literal["equivalent", "different", "undecidable"]

# Approved LaTeX macros only; any other macro such as \\mystery is undecidable.
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

# Supported unit characters: letters, script notation, hyphens, and dots.
_UNIT_ALLOWED = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789^{}-. "
)

# Full restricted numeric grammar after normalization: signed decimals with optional e/E exponents,
# approved *10^{signed integer} powers, or ordered hour-minute-second chains. Allowed characters
# alone do not prove valid syntax: incomplete exponents (8.2e+), broken powers, and unmatched
# parentheses never establish definite differences.
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
_SYMBOLIC_OFFSET = re.compile(
    rf"{_NUM_ATOM}(?:{_NUM_EXP}|{_NUM_POW})?\s*[+-]\s*[A-Za-z][A-Za-z0-9_]*"
)
_COLON_FORM = re.compile(r"[+-]?\d+:(\d{1,2}(?:\.\d+)?)(?::(\d{1,2}(?:\.\d+)?))?")
_APPROXIMATION = re.compile(r"^(?:about\s+|approximately\s+|[∼≈≃]\s*)")
_SPACED_INTEGER = re.compile(r"[+-]?[0-9](?: [0-9])+")
_UNIT_FACTOR = re.compile(
    r"(?P<symbol>[A-Za-z]+|°|%)(?:\^(?:\{(?P<braced>[+-]?\d+)\}|(?P<power>[+-]?\d+))|(?P<suffix>-\d+))?"
)
_UNIT_ROLES = re.compile(r"\s*(.+?)\s*(?:;\s*error\s+|±\s*)(.+?)\s*")

# A5 exact allowlist for model-condition formatting variants; U2 lacks a model qualifier.
_MODEL_PHRASE_MODEL = {
    "under Galactic potential M1": "M1",
    "assuming potential M1": "M1",
    "under Galactic potential M2": "M2",
    "assuming potential M2": "M2",
}
_VAGUE_MODEL_PHRASES = frozenset(
    {
        "adopting the Galactic potential model",
        "assuming the Galactic potential model",
    }
)


def _normalize_whitespace(text: str) -> str:
    return _WS_RE.sub(" ", text).strip()


def normalize_printed(text: str) -> str | None:
    """Restricted formatting normalization, returning None/undecidable for unknown macros
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


def _is_supported_number(normalized: str) -> bool:
    return is_supported_number_syntax(normalized)


def _is_supported_unit(normalized: str) -> bool:
    return bool(normalized) and all(ch in _UNIT_ALLOWED for ch in normalized)


def _compare_domain(
    gold: str, prediction: str, is_supported
) -> tuple[str | None, str | None, bool]:
    left = normalize_printed(gold)
    right = normalize_printed(prediction)
    if left is None or right is None:
        return None, None, False
    if left == right:
        return left, right, True
    return left, right, is_supported(left) and is_supported(right)


def compare_number(gold: str, prediction: str) -> Verdict:
    """Numeric-domain comparison: supported formatting equivalence or definite difference;
    otherwise undecidable.

    Even identical strings require complete supported syntax for equivalence. Incomplete
    exponents, broken powers, unmatched parentheses, or out-of-domain text never imply
    scientific differences merely through allowed characters.
    """
    left = _number_form(gold)
    right = _number_form(prediction)
    if left is None or right is None:
        # A printed unresolved offset is not one scalar. Do not substitute the
        # identifier, compute a result, or compare two symbolic expressions.
        unsupported = prediction if left is not None else gold
        normalized = normalize_printed(unsupported)
        if (
            (left is not None or right is not None)
            and normalized is not None
            and _SYMBOLIC_OFFSET.fullmatch(normalized)
        ):
            return "different"
        return "undecidable"
    return "different" if left != right else "equivalent"


def compare_unit(gold: str, prediction: str) -> Verdict:
    """Unit-domain comparison without scale conversion: distinct supported symbols differ;
    unsupported syntax is undecidable.
    """
    left, right, determined = _compare_domain(gold, prediction, _is_supported_unit)
    if left is not None and left == right:
        return "equivalent"
    left_roles, right_roles = split_unit_roles(gold), split_unit_roles(prediction)
    left_signature = tuple(_unit_factors(unit) for unit in left_roles)
    right_signature = tuple(_unit_factors(unit) for unit in right_roles)
    if all(part is not None for part in (*left_signature, *right_signature)):
        return "equivalent" if left_signature == right_signature else "different"
    return "different" if determined else "undecidable"


def compare_printed(gold: str, prediction: str) -> Verdict:
    """Compare printed strings with numeric-domain semantics; use compare_unit for units."""
    return compare_number(gold, prediction)


def compare_condition(gold: str, prediction: str) -> Verdict:
    """Condition-domain comparison using exact matches, A5 allowlist, M1/M2 differences,
    and literal negation.

    Allowlist misses cannot prove difference. Unsupported conditions stay undecidable
    for the established unknown path or compliant Agent; never default them
    correct/incorrect.
    """
    left = _normalize_whitespace(gold)
    right = _normalize_whitespace(prediction)
    if left == right:
        return "equivalent"
    if left == "not " + right or right == "not " + left:
        return "different"
    left_base = left.removeprefix("not ")
    right_base = right.removeprefix("not ")
    left_negated = left_base != left
    right_negated = right_base != right
    left_model = _MODEL_PHRASE_MODEL.get(left_base)
    right_model = _MODEL_PHRASE_MODEL.get(right_base)
    if left_model is not None and right_model is not None:
        if left_negated or right_negated:
            # Literal negation of the same model is a definite difference; negating M1 does not select M2.
            return "different" if left_model == right_model else "undecidable"
        return "equivalent" if left_model == right_model else "different"
    if (left_model is not None and right_base in _VAGUE_MODEL_PHRASES) or (
        right_model is not None and left_base in _VAGUE_MODEL_PHRASES
    ):
        return "undecidable"
    return "undecidable"

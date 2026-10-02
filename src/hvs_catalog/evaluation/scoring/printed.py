"""Restricted formatting/condition comparison (A1-A5, B cases, C1, U1/U2).

Numeric/unit/condition domains use separate rules; never apply arbitrary English
differences to numeric comparison or numeric rules to conditions. compare_number
performs scale-preserving formatting normalization of Unicode signs, scripts, supported
LaTeX wrappers/spacing/times, sexagesimal markers, and limited whitespace. Preserve
precision, coefficient trailing zeros, exponent signs, and unit scales, retaining
C1/B1/B2 differences. Unknown macros or uninterpretable syntax stay undecidable; never
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
_KNOWN_MACROS = frozenset({"times", "mathrm", ",", ";", "!", " ", "quad", "qquad"})

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
    work = _SUP_RUN_RE.sub(
        lambda m: "^{" + "".join(_SUPERSCRIPTS[ch] for ch in m.group(0)) + "}",
        work,
    )
    work = _unwrap_mathrm(work)
    for spacing in ("\\,", "\\;", "\\ ", "~"):
        work = work.replace(spacing, " ")
    work = work.replace("\\!", "")
    work = work.replace("\\times", "*").replace("×", "*")
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
    return _NUMERIC_FORM.fullmatch(normalized) is not None


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
    left = normalize_printed(gold)
    right = normalize_printed(prediction)
    if left is None or right is None:
        return "undecidable"
    left_supported = _is_supported_number(left)
    right_supported = _is_supported_number(right)
    if left == right and left_supported:
        return "equivalent"
    if not (left_supported and right_supported):
        return "undecidable"
    return "different" if left != right else "equivalent"


def compare_unit(gold: str, prediction: str) -> Verdict:
    """Unit-domain comparison without scale conversion: distinct supported symbols differ;
    unsupported syntax is undecidable.
    """
    left, right, determined = _compare_domain(gold, prediction, _is_supported_unit)
    if left is None or right is None:
        return "undecidable"
    if left == right:
        return "equivalent"
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

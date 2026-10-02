"""Shared Gold numeric-text validation preserving original expressions under legacy rules.

Never convert or fill values. Check allowed ordinary/sexagesimal strings and
probabilities within declared scales. Accept legacy Unicode signs too; do not write
interpretations back.
"""

from __future__ import annotations

import re

UNICODE_SIGN_TRANSLATION = str.maketrans({"−": "-", "﹣": "-", "－": "-", "＋": "+"})

_PLAIN_NUMBER_RE = re.compile(r"^[+-]?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?$")

COORDINATE_QUANTITY_RANGES: dict[str, tuple[float, float, str, bool]] = {
    "observed_phase_space.ra": (0.0, 360.0, "[0, 360)", False),
    "observed_phase_space.dec": (-90.0, 90.0, "[-90, 90]", True),
}


def normalize_number_text(text: str) -> str:
    return str(text).translate(UNICODE_SIGN_TRANSLATION).strip()


def parse_plain_number(text: str) -> float | None:
    """Interpret ordinary numeric text as float, or return None for unsupported forms."""
    normalized = normalize_number_text(text)
    if not _PLAIN_NUMBER_RE.fullmatch(normalized):
        return None
    return float(normalized)


def parse_sexagesimal(text: str) -> float | None:
    """Interpret sexagesimal coordinates as degrees, returning None if uninterpretable
    without changing source text.
    """
    normalized = normalize_number_text(text)
    if not normalized:
        return None
    sign = -1.0 if normalized.startswith("-") else 1.0
    if normalized[0] in "+-":
        normalized = normalized[1:]
    for marker in ("h", "H", "d", "D", "m", "M", "°", "'"):
        normalized = normalized.replace(marker, ":")
    normalized = normalized.replace('"', "").replace("s", "").replace("S", "")
    normalized = re.sub(r"\s+", ":", normalized)
    normalized = re.sub(r":+", ":", normalized).strip(":")
    parts_text = normalized.split(":")
    if len(parts_text) not in (2, 3):
        return None
    if not all(re.fullmatch(r"(?:\d+(?:\.\d*)?|\.\d+)", part) for part in parts_text):
        return None
    parts = [float(part) for part in parts_text]
    if parts[1] >= 60.0 or (len(parts) == 3 and parts[2] >= 60.0):
        return None
    magnitude = parts[0] + parts[1] / 60.0
    if len(parts) == 3:
        magnitude += parts[2] / 3600.0
    return sign * magnitude


def parse_probability(value: str, unit: str) -> float | None:
    """Interpret probabilities using fraction 0-1 or percent 0-100 scales; return None if
    uninterpretable.
    """
    number = parse_plain_number(value)
    if number is None:
        return None
    if unit == "fraction":
        return number if 0.0 <= number <= 1.0 else None
    if unit == "%":
        return number if 0.0 <= number <= 100.0 else None
    return None

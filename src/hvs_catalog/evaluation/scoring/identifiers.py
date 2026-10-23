"""Literal identifier spelling and explicitly recorded catalogue/column context.

Never infer identity from coordinates, values, row order or an external catalogue.
Bare numbers gain a namespace only from their own saved identifier evidence.
"""

import re
import unicodedata
from collections.abc import Iterable

from ...records.identifiers import normalize_catalog_identifier

_GAIA_COLUMN = re.compile(
    r"\bGaia\s+(DR[123]|EDR3)\s+(?:(?:Source\s+)?ID|source_id)\s+column\b"
)
_RELEASE = re.compile(r"\bGaia\s+(DR[123]|EDR3)\b")
_AKA = re.compile(r"(.+?)\s+\(a\.k\.a\.\s+([^()]+)\)")


def normalize_identifier(text: str) -> str:
    """Preserve case, digits and separators; canonicalize explicit label spelling."""
    normalized = " ".join(
        unicodedata.normalize("NFC", text).replace("−", "-").replace("–", "-").split()
    )
    return normalize_catalog_identifier(normalized)


def identifier_keys(identifiers: Iterable[str]) -> set[str]:
    """Read an anchored explicit a.k.a. declaration, retaining the original too."""
    keys = set()
    for identifier in identifiers:
        normalized = normalize_identifier(identifier)
        keys.add(normalized)
        alias = _AKA.fullmatch(normalized)
        if alias:
            keys.update(normalize_identifier(value) for value in alias.groups())
    return keys


def qualified_identifiers(value: str, evidence: Iterable[tuple[str, str]]) -> list[str]:
    """Keep a literal Gold identifier and its unambiguous explicit namespace.

    Column locators apply only to that identifier. Quotes must contain the exact
    integer and a single explicit Gaia release; conflicting releases do not qualify
    a bare ID. Generic row numbers, generic source_id and Gaia without a release
    supply no namespace. Leading zeros and release distinctions remain intact.
    """
    aliases = [value]
    if not re.fullmatch(r"\d+", value):
        return aliases
    releases = set()
    hvs_column = False
    for location, quote in evidence:
        releases.update(_GAIA_COLUMN.findall(location))
        hvs_column |= bool(re.search(r"\bHVS\s+column\b", location))
        if re.search(r"(?<!\d)" + re.escape(value) + r"(?!\d)", quote):
            releases.update(_RELEASE.findall(quote))
    if re.fullmatch(r"\d{15,22}", value) and len(releases) == 1:
        aliases.append(f"Gaia {next(iter(releases))} {value}")
    if hvs_column and re.fullmatch(r"\d{1,4}", value):
        aliases.append(f"HVS {value}")
    return aliases

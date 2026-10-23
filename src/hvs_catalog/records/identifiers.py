"""Format explicit catalog identifiers without inferring their identity or release."""

import re

_LABEL = r"(?i:Source\s+ID|source_id|ID|identifier|designation|name)"
_CATALOG_IDENTIFIER = re.compile(
    r"\s*(?P<catalog>Gaia|APOGEE|SDSS|LAMOST)\s+"
    r"(?:(?P<release>DR[0-9]+|EDR[0-9]+)\s+)?"
    rf"(?:{_LABEL}\s+)?"
    r"(?P<body>[0-9]+|(?:2M|J)[0-9]+(?:\.[0-9]+)?[+-][0-9]+(?:\.[0-9]+)?)\s*"
)


def normalize_catalog_identifier(value: str) -> str:
    """Remove field labels only for unambiguous, explicitly named catalog forms.

    Unknown forms and author names stay verbatim. The recognizer never identifies
    a catalog from a number or supplies a release; evidence is checked separately.
    """
    match = _CATALOG_IDENTIFIER.fullmatch(value)
    if match is None:
        return value
    catalog, release, body = match.group("catalog", "release", "body")
    if catalog == "Gaia":
        recognized = bool(re.fullmatch(r"[0-9]{15,22}", body))
    elif catalog == "APOGEE":
        recognized = body.startswith("2M")
    else:
        recognized = body.startswith("J")
    if not recognized:
        return value
    return " ".join(part for part in (catalog, release, body) if part is not None)

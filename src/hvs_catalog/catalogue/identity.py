"""Explicit identifier namespaces and conservative coordinate interpretation."""

import re
from collections import defaultdict

from astropy import units as u
from astropy.coordinates import Angle, SkyCoord

from ..records.catalog import SkyPosition
from ..records.identifiers import normalize_catalog_identifier

GAIA = re.compile(r"Gaia\s+(DR[1-3]|EDR3)\s+([0-9]{15,22})", re.IGNORECASE)
CATALOG = re.compile(
    r"(?P<catalog>HD|HIP|HR|TYC|UCAC[2-5]|SDSS(?:\s+DR\d+)?|LAMOST(?:\s+DR\d+)?|"
    r"APOGEE(?:\s+DR\d+)?|2MASS|RAVE(?:\s+DR\d+)?|USNO-[AB][12]\.0|"
    r"HE|HS|BD|CD|CPD|GSC|S5|LSPM|NLTT|LP|WD|KIC|TIC|EPIC)"
    r"\s*(?P<body>[0-9J+\-].*)",
    re.IGNORECASE,
)
BRACKETED = re.compile(r"\[[A-Za-z][A-Za-z0-9.+-]*\]\s*\S+.*")


def identifier_key(value):
    """Recognize complete namespaces, retaining component case and release."""
    value = normalize_catalog_identifier(" ".join(value.split()))
    match = GAIA.fullmatch(value)
    if match:
        return "Gaia " + match[1].upper() + " " + match[2]
    if value.casefold().startswith("gaia"):
        return None
    match = CATALOG.fullmatch(value)
    if match:
        # Normalize the namespace only; component suffix case remains significant.
        return match["catalog"].upper() + " " + match["body"]
    if BRACKETED.fullmatch(value):
        return value
    return None


def gaia_conflicts(names):
    releases = defaultdict(set)
    for name in names:
        key = identifier_key(name)
        match = GAIA.fullmatch(key) if key else None
        if match:
            releases[match[1]].add(match[2])
    return any(len(values) > 1 for values in releases.values())


def names(star):
    return [x.value for x in star.identifiers]


def keys(star):
    return {key for name in names(star) if (key := identifier_key(name))}


def _frame(values):
    """Use coordinate conditions, then their quotes; never infer an observing epoch."""
    conditions = " ".join(v.get("condition") or "" for v in values)
    gaia_source = all(
        re.search(
            r"(?:^|\bfrom\s+)(?:the\s+)?Gaia\s+(?:DR[1-3]|EDR3)\s+(?:equatorial\s+)?(?:right ascension|declination|coordinates)\b",
            v.get("condition") or "",
            re.IGNORECASE,
        )
        for v in values
    )
    epochs = {
        float(match[1])
        for v in values
        for match in re.finditer(
            r"\bepoch\s*[:=]?\s*[BJ]?(\d{4}(?:\.\d+)?)",
            v.get("condition") or "",
            re.IGNORECASE,
        )
    }
    if len(epochs) > 1:
        return None
    quotes = " ".join(e.get("quote") or "" for v in values for e in v["evidence"])
    for basis, context in (
        ("coordinate_conditions", conditions),
        ("coordinate_quotes", quotes),
    ):
        tokens = set(
            re.findall(r"\b(?:ICRS|FK[45]|[BJ]\d{4}(?:\.\d+)?)\b", context.upper())
        )
        epoch_labels = set(
            re.findall(
                r"\bepoch\s*[:=]?\s*(J\d{4}(?:\.\d+)?)\b",
                context.upper(),
                re.IGNORECASE,
            )
        )
        if "ICRS" in tokens or gaia_source:
            # ICRS has no equinox; an unqualified Julian date is not another frame.
            epoch_labels.update(t for t in tokens if t.startswith("J"))
        if (
            tokens
            - {"ICRS", "FK4", "FK5", "J2000", "J2000.0", "B1950", "B1950.0"}
            - epoch_labels
        ):
            return None
        # Other explicit equinoxes must not silently become J2000 or ICRS.
        other_equinoxes = re.findall(
            r"\bequinox\s*[:=]?\s*([BJ]\d{4}(?:\.\d+)?)", context, re.IGNORECASE
        )
        if any(
            e.upper()
            not in {
                "J2000",
                "J2000.0",
                "B1950",
                "B1950.0",
            }
            for e in other_equinoxes
        ):
            return None
        if re.search(
            r"\b(?:galactic|ecliptic|supergalactic)\s+(?:reference\s+)?(?:frame|coordinates)\b",
            context,
            re.IGNORECASE,
        ):
            return None
        if re.search(
            r"\b(?:unknown|uncertain|unspecified)\s+(?:reference\s+)?frame\b",
            context,
            re.IGNORECASE,
        ):
            return None
        old = bool(tokens & {"FK4", "B1950", "B1950.0"})
        modern = bool(tokens & {"ICRS", "FK5", "J2000", "J2000.0"})
        if old and modern or {"ICRS", "FK5"} <= tokens:
            return None
        if old:
            if not tokens & {"B1950", "B1950.0"}:
                return None
            return "fk4", "B1950", basis, False
        if "ICRS" in tokens:
            return "icrs", None, basis, False
        if tokens & {"J2000", "J2000.0"}:
            return "fk5", "J2000", basis, False
        if "FK5" in tokens:
            return None
    # A catalog name in an unrelated identifier does not establish coordinate provenance.
    if gaia_source:
        return "icrs", None, "gaia_coordinate_source", False
    return "icrs", None, "assumed_icrs", True


def _angle(value, axis):
    text, unit = value.get("value"), value.get("unit")
    if text is None or not unit:
        raise ValueError("A coordinate needs a scalar and an angular unit.")
    unit = unit.strip()
    # These labels describe central-value and error units separately.
    if unit == "deg ± mas":
        unit = "deg"
    unit = {"hms": "hourangle", "dms": "deg"}.get(unit.casefold(), unit)
    angular_unit = u.Unit(unit)
    if axis == "dec" and angular_unit == u.hourangle:
        raise ValueError("Declination cannot use hours.")
    text = text.replace("−", "-")
    # Astronomical typography may place the seconds marker before the decimal.
    text = re.sub(r"(\d+)[sS]\.([0-9]+)", r"\1.\2s", text)
    angle = float(Angle(text, unit=angular_unit).degree)
    if not (-90 <= angle <= 90 if axis == "dec" else 0 <= angle < 360):
        raise ValueError("Coordinate is outside the angular bounds.")
    return angle


def _locations(value):
    return {e["location"].split(":", 1)[-1] for e in value["evidence"]}


def coordinate(star):
    """Pair source coordinates without changing their values or propagating epochs."""
    quantities = {q.quantity: q.values for q in star.quantities}
    ras = [v.model_dump(mode="json") for v in quantities.get("ra", [])]
    decs = [v.model_dump(mode="json") for v in quantities.get("dec", [])]
    if not ras or not decs or len({v.get("source") for v in [*ras, *decs]}) > 1:
        return None
    pairs = [
        (ra, dec)
        for ra in ras
        for dec in decs
        if _locations(ra) & _locations(dec)
        or (ra.get("condition") and ra.get("condition") == dec.get("condition"))
    ]
    # Every reported value must participate; do not select a convenient subset.
    if not pairs or any(
        not any(v is r or v is d for r, d in pairs) for v in [*ras, *decs]
    ):
        return None
    info = _frame([*ras, *decs])
    if info is None:
        return None
    try:
        ra_angles = {_angle(v, "ra") for v in ras}
        dec_angles = {_angle(v, "dec") for v in decs}
        if len(ra_angles) != 1 or len(dec_angles) != 1:
            return None
        return SkyPosition(
            ra_deg=ra_angles.pop(),
            dec_deg=dec_angles.pop(),
            frame=info[0],
            equinox=info[1],
            evidence=[*ras, *decs],
        )
    except (ValueError, TypeError, u.UnitsError):
        return None


def coordinate_interpretation(position):
    """Explain query-only parsing while retaining the complete source values."""
    info = _frame(position.evidence)
    return {
        "frame": position.frame,
        "equinox": position.equinox,
        "frame_basis": info[2],
        "frame_assumed": info[3],
        "pairing": "Shared evidence location or identical nonempty condition within one source object; only one distinct pair.",
        "epoch_propagation": False,
    }


def coordinate_warnings(position):
    info = _frame(position.evidence)
    if info is not None and info[3]:
        return [
            "Coordinate reference frame was not reported; ICRS was assumed for matching."
        ]
    return []


def sky(position):
    options = {"equinox": position.equinox} if position.equinox else {}
    return SkyCoord(
        position.ra_deg * u.deg,
        position.dec_deg * u.deg,
        frame=position.frame,
        **options,
    )


def separation(left, right):
    """Transform known reference frames only; never propagate positions in time."""
    return float(sky(left).icrs.separation(sky(right).icrs).arcsec)


LOCAL_LABEL = re.compile(
    r"(?:star|object|obj\.?|source|id|no\.?|row|candidate|target)"
    r"\s*[-:#]?\s*\d+[a-z]?",
    re.IGNORECASE,
)


def display_name(stars, snapshot, catalog_id):
    """Choose a readable label without changing any identity-matching evidence."""
    if snapshot.status == "matched" and snapshot.selected is not None:
        return snapshot.selected.main_id
    votes = defaultdict(set)
    for node, star in stars:
        paper = re.sub(r"v\d+$", "", node.split("#", 1)[0])
        for name in names(star):
            label = " ".join(name.split())
            if any(character.isalpha() for character in label):
                votes[label].add(paper)
    if not votes:
        return catalog_id
    best = min(votes, key=lambda label: (len(label), -len(votes[label]), label))
    if LOCAL_LABEL.fullmatch(best):
        return f"{best} ({min(votes[best])})"
    return best

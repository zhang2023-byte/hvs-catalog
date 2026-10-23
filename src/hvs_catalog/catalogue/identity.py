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


def coordinate(star):
    """Accept one explicit pair with compatible conditions and known reference frame."""
    quantities = {q.quantity: q.values for q in star.quantities}
    ras, decs = quantities.get("ra", []), quantities.get("dec", [])
    if len(ras) != 1 or len(decs) != 1:
        return None
    ra, dec = ras[0], decs[0]
    if ra.value is None or dec.value is None or ra.condition != dec.condition:
        return None
    context = ra.condition or ""
    if re.search(r"\bICRS\b", context, re.IGNORECASE):
        frame, equinox = "icrs", None
    elif re.search(r"\b(?:J2000(?:\.0)?|FK5)\b", context, re.IGNORECASE):
        if not re.search(r"\bJ2000(?:\.0)?\b", context, re.IGNORECASE):
            return None
        frame, equinox = "fk5", "J2000"
    elif re.search(r"\bB1950(?:\.0)?\b", context, re.IGNORECASE):
        frame, equinox = "fk4", "B1950"
    else:
        return None
    if not ra.unit or not dec.unit:
        return None
    try:
        ra_angle, dec_angle = (
            Angle(ra.value, unit=u.Unit(ra.unit)),
            Angle(dec.value, unit=u.Unit(dec.unit)),
        )
        if not 0 <= ra_angle.degree < 360 or not -90 <= dec_angle.degree <= 90:
            return None
        return SkyPosition(
            ra_deg=float(ra_angle.degree),
            dec_deg=float(dec_angle.degree),
            frame=frame,
            equinox=equinox,
            evidence=[ra.model_dump(mode="json"), dec.model_dump(mode="json")],
        )
    except (ValueError, TypeError, u.UnitsError):
        return None


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


def name_rank(value):
    match = GAIA.fullmatch(value)
    return {"DR3": 1, "EDR3": 2, "DR2": 3, "DR1": 4}[match[1]] if match else 5


def display_name(stars, snapshot, catalog_id, previous=None):
    if snapshot.status == "conflict" or gaia_conflicts(
        [name for _, star in stars for name in names(star)]
    ):
        return catalog_id
    if snapshot.status == "matched" and snapshot.method == "identifier":
        return snapshot.selected.main_id
    votes = defaultdict(set)
    for node, star in stars:
        paper = node.split("#", 1)[0]
        for key in keys(star):
            votes[key].add(paper)
    if not votes:
        return catalog_id
    best = min(
        votes,
        key=lambda k: (name_rank(k), -len(votes[k]) if name_rank(k) == 5 else 0, k),
    )
    if previous in votes and name_rank(previous) <= name_rank(best):
        return previous
    return best

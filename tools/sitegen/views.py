"""Derive scientific display decisions once, before browser rendering."""

from __future__ import annotations

import re
import unicodedata
from collections import defaultdict
from decimal import Decimal
from html.parser import HTMLParser
from urllib.parse import urlencode, urlsplit

# Order, public label, standard unit, and machine-readable CSV column stem.
QUANTITIES = (
    ("galactic_rest_frame_velocity", "v_GRF", "km/s", "v_grf_km_s"),
    ("distance", "d", "kpc", "d_kpc"),
    ("galactocentric_radius", "r_GC", "kpc", "r_gc_kpc"),
    ("radial_velocity", "RV", "km/s", "rv_km_s"),
    ("unbound_probability", "P_unbound", "", "p_unbound"),
    ("bound_probability", "P_bound", "", "p_bound"),
    ("parallax", "Parallax", "mas", "parallax_mas"),
    ("proper_motion_ra", "μα*", "mas/yr", "pm_ra_mas_yr"),
    ("proper_motion_dec", "μδ", "mas/yr", "pm_dec_mas_yr"),
)
HOME_KEYS = [QUANTITIES[i][0] for i in (0, 1, 3, 4, 5)]
NUMERIC_FIELDS = (
    "value",
    "error",
    "lower_error",
    "upper_error",
    "lower_bound",
    "upper_bound",
)


def arxiv_order(source):
    """Order by submission month and sequence, never by revision/publication date."""
    bare = re.sub(r"v\d+$", "", source).rsplit("/", 1)[-1]
    match = re.fullmatch(r"(\d{2})(\d{2})\.?([0-9]+)", bare)
    if not match:
        raise ValueError(f"Invalid arXiv identifier: {source}")
    year, month, number = map(int, match.groups())
    return (1900 + year if year >= 91 else 2000 + year, month, number)


def source_view(key, source):
    author = source["authors"][0] if source.get("authors") else "Unknown author"
    # arXiv metadata supplies full names; retain common surname particles.
    parts = author.split()
    surname = author.split(",", 1)[0] if "," in author else parts[-1]
    if "," not in author:
        i = len(parts) - 2
        while i >= 0 and parts[i].lower() in {
            "de",
            "del",
            "der",
            "van",
            "von",
            "da",
            "di",
            "la",
        }:
            surname = parts[i] + " " + surname
            i -= 1
    short = surname + (" et al." if len(source.get("authors", [])) > 1 else "")
    year = arxiv_order(key)[0]
    return {
        "id": key,
        "year": year,
        "short": short,
        "label": f"{short} {year}",
        "title": source["title"],
        "url": "https://arxiv.org/abs/" + key,
    }


def tier(value):
    standard = value.get("standard")
    if value.get("attribution") != "own" or not standard:
        return -1
    if "value" in standard:
        return (
            2
            if any(k in standard for k in ("error", "lower_error", "upper_error"))
            else 1
        )
    return 0 if any(k in standard for k in ("lower_bound", "upper_bound")) else -1


def choose(values):
    candidates = [v for v in values if tier(v) >= 0]
    # max keeps the first occurrence when the scientific priority ties.
    return max(
        candidates, key=lambda v: (tier(v), arxiv_order(v["source"])), default=None
    )


def format_number(data):
    if "value" in data:
        result = str(data["value"])
        if "error" in data:
            result += f" ± {data['error']}"
        elif (
            "upper_error" in data
            and "lower_error" in data
            and Decimal(data["upper_error"]) == Decimal(data["lower_error"])
        ):
            error = min(
                (data["upper_error"], data["lower_error"]),
                key=lambda x: Decimal(x).as_tuple().exponent,
            )
            result += f" ± {error}"
        else:
            if "upper_error" in data:
                result += f" +{data['upper_error']}"
            if "lower_error" in data:
                result += f" −{data['lower_error']}"
    elif "lower_bound" in data and "upper_bound" in data:
        result = f"{data['lower_bound']}–{data['upper_bound']}"
    elif "lower_bound" in data:
        result = f"> {data['lower_bound']}"
    elif "upper_bound" in data:
        result = f"< {data['upper_bound']}"
    else:
        result = "—"
    return ("~" if data.get("approximate") else "") + result.replace("-", "−")


class QuoteText(HTMLParser):
    """Keep text and LaTeX unchanged; convert table boundaries to plain text."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []

    def handle_data(self, data):
        self.parts.append(data)

    def handle_starttag(self, tag, attrs):
        if tag in {"br", "p", "tr"}:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in {"td", "th"}:
            self.parts.append(" | ")
        elif tag in {"tr", "p"}:
            self.parts.append("\n")


def clean_quote(text):
    parser = QuoteText()
    parser.feed(text)
    lines = (
        line.strip().strip("|").strip() for line in "".join(parser.parts).splitlines()
    )
    return "\n".join(line for line in lines if line)


def evidence_view(value, pdf_url):
    parsed = urlsplit(pdf_url)
    if parsed.scheme != "https" or not parsed.netloc:
        raise ValueError("Fixed PDF links must use HTTPS")
    pages = {}

    def append(page, quote, label=""):
        if page not in pages:
            pages[page] = {
                "page": page,
                "url": f"{pdf_url}#page={page}",
                "quotes": [],
            }
        pages[page]["quotes"].append({"text": quote, "label": label})

    corrected = bool(value.get("correction"))
    for evidence in value.get("correction", {}).get("evidence", []):
        append(evidence["page"], clean_quote(evidence["quote"]), "PDF correction")
    for evidence in value.get("evidence", []):
        match = re.fullmatch(r"[TI]:p([1-9]\d*)-b\d+", evidence["location"])
        if not match:
            raise ValueError("Evidence has no physical PDF page")
        page = int(match[1])
        append(
            page,
            clean_quote(evidence["quote"])
            if evidence.get("quote")
            else "image evidence, no text quote",
            "Original extraction evidence" if corrected else "",
        )
    for page in pages.values():
        text_quotes = [
            quote
            for quote in page["quotes"]
            if quote["text"] and quote["text"] != "image evidence, no text quote"
        ]
        if text_quotes:
            page["quotes"] = text_quotes
    return list(pages.values())


def value_view(value, sources, own_count=1):
    source = source_view(value["source"], sources[value["source"]])
    data = value.get("standard", value)
    label = {"own": "", "cited": "quoted", "mismatch": "not comparable"}[
        value["attribution"]
    ]
    display = format_number(data)
    unit = data.get("unit", "")
    if unit == "1":
        unit = ""
    raw_unit = value.get("unit", "")
    if raw_unit == "1":
        raw_unit = ""
    raw = format_number(value) + (" " + raw_unit if raw_unit else "")
    displayed_value = display + (" " + unit if unit else "")
    pdf_url = sources[value["source"]]["pdf_url"]
    corrected = bool(value.get("correction"))
    return {
        "display": display,
        "table_display": display
        + (f" {unit}" if unit and not value.get("standard") else ""),
        "unit": unit,
        "raw": raw,
        "reported": ("PDF-corrected value: " if corrected else "Reported value: ") + raw
        if raw != displayed_value
        else "",
        "correction_notice": "Corrected against the fixed PDF." if corrected else "",
        "source": source,
        "tooltip": source["label"]
        + (f" · 1 of {own_count} values" if own_count > 1 else ""),
        "condition": value.get("condition", ""),
        "label": label,
        "evidence": evidence_view(value, pdf_url),
        "sort_group": 0 if "value" in data else 1,
        "sort_value": float(
            Decimal(
                data.get("value", data.get("lower_bound", data.get("upper_bound", "0")))
            )
        )
        if value.get("standard")
        else 0,
    }


def name_key(text):
    return re.sub(r"[\s−–-]+", "", text).casefold()


def aliases(star, *, key=name_key):
    """Rank names by source frequency under the requested identity comparison."""
    groups = defaultdict(list)
    for identifier in star["identifiers"]:
        groups[key(identifier["value"])].append(identifier)
    names = []
    for identity, items in groups.items():
        if identity == key(star["display_name"]):
            continue
        spellings = defaultdict(set)
        for item in items:
            spellings[item["value"]].add(item["source"])
        spelling = min(spellings, key=lambda s: (-len(spellings[s]), len(s), s))
        names.append((-len({i["source"] for i in items}), spelling))
    result = [s for _, s in sorted(names)]
    seen = {key(s) for s in [star["display_name"], *result]}
    for name in star.get("display_aliases", []) + star.get("simbad", {}).get(
        "selected", {}
    ).get("aliases", []):
        if key(name) not in seen:
            result.append(name)
            seen.add(key(name))
    return result


def gaia_id(star):
    names = [
        star["display_name"],
        *[i["value"] for i in star["identifiers"]],
        *aliases(star),
    ]
    ids = []
    for name in names:
        match = re.search(r"Gaia\s*E?DR3\s*(\d+)", name, re.IGNORECASE)
        if match and match[1] not in ids:
            ids.append(match[1])
    if len(ids) > 1:
        raise ValueError(f"Conflicting Gaia DR3 identifiers for {star['display_name']}")
    return ids[0] if ids else ""


def visible_aliases(names, gaia):
    """Filter header aliases without removing any catalog search terms."""
    result = []
    for name in names:
        if gaia and re.search(rf"(?<!\d){re.escape(gaia)}(?!\d)", name):
            continue
        if re.search(r"\bet\s+al\.", name, re.IGNORECASE):
            continue
        if re.fullmatch(r"\s*HVS\s*", name, re.IGNORECASE):
            continue
        if re.fullmatch(
            r"\s*(?:\d+|#\s*\d+|(?:id|no\.?|number|star|object|source|candidate)"
            r"[\s:#._-]*(?:[A-Z]?\d+|[A-Z]))\s*",
            name,
            re.IGNORECASE,
        ):
            continue
        result.append(name)
    return result


def coordinate_prefix(name):
    """Recognize a catalog prefix followed by a compact signed coordinate pair."""
    match = re.fullmatch(
        r"\s*(.*?)\s*J?\s*\d+(?:\.\d+)?[+−–-]\d+(?:\.\d+)?\s*",
        name,
        re.IGNORECASE,
    )
    if not match:
        return None
    return re.sub(r"\s+", "", match[1]).casefold() or None


def header_aliases(names):
    """Fill three header slots without repeating a coordinate catalog prefix."""
    shown, more, prefixes = [], [], set()
    for name in names:
        prefix = coordinate_prefix(name)
        if len(shown) == 3 or (prefix is not None and prefix in prefixes):
            more.append(name)
        else:
            shown.append(name)
            if prefix is not None:
                prefixes.add(prefix)
    return shown, more


def sexagesimal(degrees, ra=False):
    value = Decimal(str(degrees))
    sign = "" if ra else ("−" if value < 0 else "+")
    seconds = abs(value) * (240 if ra else 3600)
    seconds = seconds.quantize(Decimal("0.01") if ra else Decimal("0.1"))
    if ra:
        seconds %= 86400
    hours, remainder = divmod(seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    return (
        f"{sign}{int(hours):02}:{int(minutes):02}:{seconds:05.2f}"
        if ra
        else f"{sign}{int(hours):02}:{int(minutes):02}:{seconds:04.1f}"
    )


def coordinates(star, defaults):
    selected = star.get("simbad", {}).get("selected", {}).get("coordinates", {})
    if (
        star.get("simbad", {}).get("status") == "matched"
        and "ra" in selected
        and "dec" in selected
    ):
        ra, dec = selected["ra"]["value"], selected["dec"]["value"]
        source = "SIMBAD"
    elif (
        defaults.get("ra")
        and defaults.get("dec")
        and all("value" in defaults[k]["standard"] for k in ("ra", "dec"))
    ):
        ra, dec = (defaults[k]["standard"]["value"] for k in ("ra", "dec"))
        source = " / ".join(
            dict.fromkeys(
                source_view(
                    defaults[k]["source"], star["sources"][defaults[k]["source"]]
                )["label"]
                for k in ("ra", "dec")
            )
        )
    else:
        return None
    return {
        "ra": str(ra),
        "dec": str(dec),
        "text": f"RA {sexagesimal(ra, True)}  Dec {sexagesimal(dec)} ({source})",
    }


def linked_by_position(star, provenance):
    nodes = {
        f"{source}#{record}"
        for source, data in star["sources"].items()
        for record in data["records"]
    }
    return any(
        a.get("method") == "coordinates"
        and not a.get("rejected")
        and a.get("left") in nodes
        and a.get("right") in nodes
        and a["left"].split("#")[0] != a["right"].split("#")[0]
        for a in provenance.get("associations", [])
    )


def slugs(stars):
    bases = []
    for star in stars:
        name = (
            unicodedata.normalize("NFKD", star["display_name"])
            .encode("ascii", "ignore")
            .decode()
        )
        bases.append(re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "star")
    result = []
    for i, base in enumerate(bases):
        peers = [j for j, other in enumerate(bases) if other == base]
        result.append(base if len(peers) == 1 else f"{base}-{peers.index(i) + 1}")
    if len(set(result)) != len(result):
        raise ValueError("Display-name address collision")
    return result


def star_view(star, provenance, slug, repository):
    quantities = {q["quantity"]: q["values"] for q in star["quantities"]}
    defaults = {key: choose(values) for key, values in quantities.items()}
    latest = {}
    for key, label, unit, _ in QUANTITIES:
        value = defaults.get(key)
        if value:
            count = sum(
                v["source"] == value["source"] and v["attribution"] == "own"
                for v in quantities[key]
            )
            latest[key] = value_view(value, star["sources"], count)
    columns = [
        {"key": key, "label": label, "unit": unit}
        for key, label, unit, _ in QUANTITIES
        if key in quantities
    ]
    literature = []
    for source in sorted(star["sources"], key=arxiv_order, reverse=True):
        cells = {}
        for column in columns:
            values = [v for v in quantities[column["key"]] if v["source"] == source]
            own = [v for v in values if v["attribution"] == "own"]
            representative = choose(own)
            # Undervivable own values remain visible in Literature only.
            if representative is None and own:
                representative = own[0]
            others = [v for v in own if v is not representative]
            extra = [v for v in values if v["attribution"] != "own"]
            cells[column["key"]] = {
                "main": value_view(representative, star["sources"], len(own))
                if representative
                else None,
                "others": [value_view(v, star["sources"]) for v in others],
                "more_label": f"+{len(others)}" if others else "",
                "extra": [value_view(v, star["sources"]) for v in extra],
                "extra_label": f"+{len(extra)} quoted / not comparable"
                if extra
                else "",
            }
        literature.append(
            {
                "source": source_view(source, star["sources"][source]),
                "cells": cells,
                "has_own_values": any(
                    cell["main"] is not None for cell in cells.values()
                ),
                "has_values": any(
                    cell["main"] is not None or cell["extra"] for cell in cells.values()
                ),
            }
        )
    # Search and CSV retain their existing alias spellings and ordering.
    names = aliases(star, key=lambda s: re.sub(r"[\s-]+", "", s).casefold())
    gaia = gaia_id(star)
    header_names, more_names = header_aliases(visible_aliases(aliases(star), gaia))
    coords = coordinates(star, defaults)
    simbad = star.get("simbad", {})
    view = {
        "catalog_id": star["catalog_id"],
        "name": star["display_name"],
        "slug": slug,
        "aliases": header_names,
        "more_aliases": more_names,
        "alias_more_label": f"+{len(more_names)} more",
        "gaia": gaia,
        "gaia_line": f"Gaia DR3 {gaia}"
        if gaia
        and not re.fullmatch(
            rf"\s*Gaia\s*DR3\s*{re.escape(gaia)}\s*",
            star["display_name"],
            re.IGNORECASE,
        )
        else "",
        "coordinates": coords,
        "position_note": linked_by_position(star, provenance),
        "simbad_url": "https://simbad.cds.unistra.fr/simbad/sim-id?"
        + urlencode({"Ident": simbad["selected"]["main_id"]})
        if simbad.get("status") == "matched"
        else None,
        "simbad_position": provenance.get("simbad", {}).get("status") == "matched"
        and provenance["simbad"].get("method") == "coordinates",
        "gaia_url": "https://vizier.cds.unistra.fr/viz-bin/VizieR?-source=I/355/gaiadr3&Source="
        + gaia
        if gaia
        else None,
        "latest": latest,
        "columns": columns,
        "literature": literature,
        "literature_defaults": {
            "show_quoted": not any(p["has_own_values"] for p in literature),
            "show_empty": not any(p["has_values"] for p in literature),
        },
        "literature_identity_columns": [
            {"key": "year", "label": "Year"},
            {"key": "paper", "label": "Paper"},
        ],
        "download": f"downloads/{slug}.json",
        "issue_url": repository.rstrip("/")
        + "/issues/new?"
        + urlencode({"title": f"Catalog correction: {star['display_name']}"}),
        "data_version": star["data_version"],
    }
    search_names = [
        star["display_name"],
        gaia,
        *names,
        *[i["value"] for i in star["identifiers"]],
    ]
    row = {
        "catalog_number": int(star["catalog_id"].removeprefix("hvs-catalog-")),
        "name": star["display_name"],
        "slug": slug,
        "search": re.sub(r"\s+", "", " ".join(search_names)).lower(),
        "papers": len(star["sources"]),
        "first": min(arxiv_order(s)[0] for s in star["sources"]),
        "values": {
            k: {
                f: latest[k][f]
                for f in ("display", "tooltip", "sort_group", "sort_value")
            }
            if k in latest
            else None
            for k in HOME_KEYS
        },
    }
    csv = {
        "catalog_id": star["catalog_id"],
        "name": star["display_name"],
        "aliases": "; ".join(names),
        "gaia_dr3_id": gaia,
        "ra_deg": coords["ra"] if coords else "",
        "dec_deg": coords["dec"] if coords else "",
        "papers": row["papers"],
        "first": row["first"],
    }
    for key, _, _, stem in QUANTITIES:
        value = defaults.get(key)
        standard = value["standard"] if value else {}
        for field in NUMERIC_FIELDS:
            csv[stem if field == "value" else f"{stem}_{field}"] = standard.get(
                field, ""
            )
        csv[f"{stem}_limit"] = (
            "range"
            if "lower_bound" in standard and "upper_bound" in standard
            else ">"
            if "lower_bound" in standard
            else "<"
            if "upper_bound" in standard
            else ""
        )
        for field in ("lower_inclusive", "upper_inclusive", "approximate"):
            csv[f"{stem}_{field}"] = (
                str(standard[field]).lower() if field in standard else ""
            )
        csv[f"{stem}_source"] = value["source"] if value else ""
    return view, row, csv

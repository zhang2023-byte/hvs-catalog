"""Derive standard units without interpreting physical models or changing source values."""

import re
from decimal import Decimal, InvalidOperation, localcontext

from ..records.catalog_review import StandardValue

FIELDS = ("value", "error", "lower_error", "upper_error", "lower_bound", "upper_bound")
NUMBER = re.compile(r"^[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?$")


def _unit(text):
    text = (text or "").strip().replace("−", "-")
    for wrapper in (r"\mathrm", r"\textrm", r"\text"):
        text = text.replace(wrapper, "")
    for token in ("$", "{", "}", "^", " ", r"\,", r"\;", r"\!", r"\ "):
        text = text.replace(token, "")
    text = text.replace("<sup>", "").replace("</sup>", "")
    return text.replace("⁻¹", "-1").replace("⁻²", "-2")


def _number(text):
    text = text.strip().strip("$").strip().replace("−", "-")
    approximate = False
    for prefix in (r"\simeq", r"\approx", r"\sim", "≃", "≈", "∼", "~"):
        if text.startswith(prefix):
            text = text[len(prefix) :].strip()
            approximate = True
            break
    exponent = re.fullmatch(
        r"([+-]?(?:\d+(?:\.\d*)?|\.\d+))\s*(?:×|\\times)\s*10\s*\^\s*\{?([+-]?\d+)\}?",
        text,
    )
    if exponent:
        text = exponent[1] + "e" + exponent[2]
    if not NUMBER.fullmatch(text):
        raise ValueError("Ambiguous numerical expression.")
    return Decimal(text), approximate


def _sexagesimal(text, hours):
    text = text.strip().replace("−", "-")
    text = re.sub(r"[hHdD°mM′'sS″\"]", " ", text).replace(":", " ")
    parts = text.split()
    if len(parts) != 3 or any(not NUMBER.fullmatch(p) for p in parts):
        raise ValueError("Not an unambiguous sexagesimal angle.")
    first, minute, second = (Decimal(p) for p in parts)
    if minute < 0 or minute >= 60 or second < 0 or second >= 60:
        raise ValueError("Invalid sexagesimal component.")
    sign = -1 if parts[0].startswith("-") else 1
    return sign * (abs(first) + minute / 60 + second / 3600) * (15 if hours else 1)


def standard_value(quantity, value):
    """Return no derivation if any supplied numerical component or unit is ambiguous."""
    source = value.model_dump() if hasattr(value, "model_dump") else value
    unit = _unit(source.get("unit"))
    target, factor, sexagesimal = None, None, False
    if quantity in {"radial_velocity", "galactic_rest_frame_velocity"}:
        target = "km/s"
        factor = {
            "km/s": "1",
            "km/sec": "1",
            "kms-1": "1",
            "m/s": "0.001",
            "ms-1": "0.001",
        }.get(unit)
    elif quantity in {"distance", "galactocentric_radius"}:
        target = "kpc"
        factor = {"kpc": "1", "pc": "0.001", "Mpc": "1000"}.get(unit)
    elif quantity in {"ra", "dec"}:
        target = "deg"
        factor = {
            "deg": "1",
            "degree": "1",
            "degrees": "1",
            "°": "1",
            "deg±mas": "1",
            "hourangle": "15",
        }.get(unit)
        sexagesimal = unit in {"hms", "h:m:s", "hh:mm:ss", "dms", "d:m:s", "dd:mm:ss"}
        if sexagesimal and (
            (quantity == "ra") != (unit in {"hms", "h:m:s", "hh:mm:ss"})
        ):
            return None
    elif quantity == "parallax":
        target = "mas"
        factor = {
            "mas": "1",
            "arcsec": "1000",
            "arcsecond": "1000",
            "μas": "0.001",
            "uas": "0.001",
        }.get(unit)
    elif quantity in {"proper_motion_ra", "proper_motion_dec"}:
        target = "mas/yr"
        factor = {
            "mas/yr": "1",
            "masyr-1": "1",
            "mas/year": "1",
            "masyear-1": "1",
            "arcsec/yr": "1000",
            "arcsecyr-1": "1000",
        }.get(unit)
    elif quantity in {"bound_probability", "unbound_probability"}:
        target = "1"
        factor = {"1": "1", "dimensionless": "1", "%": "0.01", "percent": "0.01"}.get(
            unit
        )
    if not target or (factor is None and not sexagesimal):
        return None
    result = {"unit": target, "approximate": False}
    try:
        with localcontext() as context:
            context.prec = 28
            for field in FIELDS:
                text = source.get(field)
                if text is None:
                    continue
                if sexagesimal or (
                    quantity in {"ra", "dec"}
                    and unit in {"hourangle", "deg"}
                    and (
                        ":" in text
                        or len(text.split()) == 3
                        or re.search(r"\d[hHdD°]", text)
                    )
                ):
                    number = _sexagesimal(
                        text, unit in {"hms", "h:m:s", "hh:mm:ss", "hourangle"}
                    )
                    approximate = False
                else:
                    number, approximate = _number(text)
                    if unit == "deg±mas" and field in {
                        "error",
                        "lower_error",
                        "upper_error",
                    }:
                        number /= Decimal(3600000)
                    else:
                        number *= Decimal(factor)
                result[field] = format(number, "f")
                result["approximate"] |= approximate
            for field in ("lower_inclusive", "upper_inclusive"):
                if source.get(field) is not None:
                    result[field] = source[field]
    except (ValueError, InvalidOperation):
        return None
    return StandardValue.model_validate(result)

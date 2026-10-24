"""Present external science while delivering every original matching fact separately."""

import copy
import json
from datetime import datetime

from ..records.catalog_public import SimbadTrace, VersionThreeStellarRecord
from ..records.catalog_simbad import CompactSimbad, SimbadScience, SimbadSummary
from .inputs import json_bytes


def _nonempty(value):
    return value is not None and value != "" and value != [] and value != {}


def _clean(value):
    return {k: v for k, v in value.items() if _nonempty(v)}


class _Fields:
    def __init__(self, fields):
        self.fields = fields
        self.used = set()

    def take(self, key):
        self.used.add(key)
        item = self.fields.get(key, {})
        return copy.deepcopy(item) if _nonempty(item.get("value")) else {}

    def value(self, key):
        return self.take(key).get("value")

    def metadata(self, prefix):
        return _clean(
            {
                target: self.value(prefix + "_" + source)
                for source, target in (
                    ("qual", "quality"),
                    ("bibcode", "bibcode"),
                    ("wavelength", "wavelength"),
                )
            }
        )

    def remaining(self, administrative=()):
        return {
            k: copy.deepcopy(v)
            for k, v in self.fields.items()
            if k not in self.used
            and k not in administrative
            and not k.endswith("_prec")
            and _nonempty(v.get("value"))
        }


def _ellipse(fields, prefix):
    return _clean(
        {
            name: fields.take(prefix + "_err_" + column)
            for name, column in (("major", "maj"), ("minor", "min"), ("angle", "angle"))
        }
    )


def _measurement(fields, value_key, error_key=None, prefix=None):
    result = fields.take(value_key)
    if error_key and (error := fields.take(error_key)):
        result["error"] = error["value"]
        if "unit" in error and error["unit"] != result.get("unit"):
            result["error_unit"] = error["unit"]
    if prefix:
        result.update(fields.metadata(prefix))
    return result


def scientific_data(candidate):
    """Regroup returned values without rounding, unit conversion, or missing-value inference."""
    fields = _Fields(candidate["fields"])
    coordinates = _clean({"ra": fields.take("ra"), "dec": fields.take("dec")})
    if position := candidate.get("position"):
        for name in ("ra", "dec"):
            coordinates.setdefault(
                name, {"value": position[name + "_deg"], "unit": "deg"}
            )
        coordinates.update(_clean({k: position.get(k) for k in ("frame", "equinox")}))
    coordinates.update(
        _clean({"error_ellipse": _ellipse(fields, "coo"), **fields.metadata("coo")})
    )
    motion = _clean(
        {
            "ra_cosdec": fields.take("pmra"),
            "dec": fields.take("pmdec"),
            "error_ellipse": _ellipse(fields, "pm"),
            **fields.metadata("pm"),
        }
    )
    velocity = _clean(
        {
            "radial_velocity": fields.take("rvz_radvel"),
            "redshift": fields.take("rvz_redshift"),
            "reported_as": fields.value("rvz_type"),
            "error": fields.take("rvz_err"),
            "nature": fields.value("rvz_nature"),
            **fields.metadata("rvz"),
        }
    )
    photometry = []
    for band in candidate["photometry"]:
        data = _Fields(band["fields"])
        row = {
            "band": band["band"],
            **_measurement(data, "flux", "flux_err"),
            **_clean({k: data.value(k) for k in ("system", "bibcode")}),
            **_clean({"quality": data.value("qual")}),
        }
        if data.fields.get("filter", {}).get("value") == band["band"]:
            data.used.add("filter")
        row.update(_clean({"additional_fields": data.remaining({"oidref"})}))
        photometry.append(row)
    result = _clean(
        {
            "main_id": candidate["main_id"],
            "aliases": candidate["aliases"],
            "object_type": fields.value("otype"),
            "coordinates": coordinates,
            "proper_motion": motion,
            "parallax": _measurement(fields, "plx_value", "plx_err", "plx"),
            "velocity": velocity,
            "spectral_type": _measurement(fields, "sp_type", prefix="sp"),
            "photometry": photometry,
        }
    )
    for key, value in (
        ("main_id", candidate["main_id"]),
        ("otype_txt", result.get("object_type")),
    ):
        if fields.fields.get(key, {}).get("value") == value:
            fields.used.add(key)
    result.update(
        _clean({"additional_fields": fields.remaining({"nbref", "hpx", "update_date"})})
    )
    SimbadScience.model_validate(result)
    return result


def compact_simbad(trace, provenance=None):
    from .readable import _expand

    simbad = trace["simbad"]
    selected = simbad.get("selected")
    match = {}
    if selected:
        matches = _expand(simbad.get("matches", []), trace)
        if simbad.get("method") == "identifier":
            match["identifiers"] = sorted(
                {
                    item["identifier"]
                    for item in matches
                    if item.get("method") == "identifier"
                    and item.get("main_id") == selected["main_id"]
                    and "rejected_candidate" not in item
                }
            )
        else:
            coordinates = []
            for binding in simbad.get("bindings", []):
                if (
                    not binding.get("accepted", True)
                    or binding["right"] != "simbad:" + selected["main_id"]
                ):
                    continue
                details = binding["details"]
                interpretation = details.get("coordinate_interpretation", {})
                entry = {
                    "source_record": binding["left"],
                    "separation_arcsec": details["separation_arcsec"],
                    "radius_arcsec": details["threshold_arcsec"],
                }
                count = len(details.get("candidates", []))
                if count > 1:
                    entry["candidate_count"] = count
                if interpretation.get("frame_assumed"):
                    entry["assumed_frame"] = interpretation["frame"]
                if binding.get("historical"):
                    entry["historical"] = True
                if entry not in coordinates:
                    coordinates.append(entry)
            match["coordinates"] = coordinates
    elif simbad["status"] in {"conflict", "ambiguous"}:
        match["candidates"] = sorted(
            {m["main_id"] for m in simbad.get("matches", []) if m.get("main_id")}
        )
    times = [q["queried_at"] for q in simbad.get("queries", []) if q.get("queried_at")]
    result = _clean(
        {
            "status": simbad["status"],
            "method": simbad.get("method"),
            "selected": scientific_data(selected) if selected else None,
            "reason": simbad.get("reason"),
            "match": _clean(match),
            "last_queried_at": max(times, key=datetime.fromisoformat)
            if times
            else None,
            "provenance": provenance,
        }
    )
    (CompactSimbad if provenance is not None else SimbadSummary).model_validate(result)
    return result


def _association_summary(link):
    result = copy.deepcopy(link)
    result["details"] = {
        k: v
        for k, v in result["details"].items()
        if k in {"main_id", "basis", "matched_identifiers", "reason"}
    }
    return result


def _origin_refs(value):
    if isinstance(value, list):
        return set().union(*(_origin_refs(v) for v in value))
    if not isinstance(value, dict):
        return set()
    if (
        set(value) == {"$ref"}
        and isinstance(value["$ref"], str)
        and value["$ref"].startswith("#/association_origins/")
    ):
        return {
            value["$ref"]
            .removeprefix("#/association_origins/")
            .replace("~1", "/")
            .replace("~0", "~")
        }
    return set().union(*(_origin_refs(v) for v in value.values()))


def split_document(document):
    """Return a compact stellar record and its complete, self-contained SIMBAD trace."""
    from .publication import artifact

    raw = copy.deepcopy(document)
    trace = {
        "schema_version": 3,
        "data_version": raw["data_version"],
        "catalog_id": raw["catalog_id"],
        "simbad": raw["simbad"],
    }
    links = {}
    for index, link in enumerate(raw["associations"]):
        if link["method"] == "simbad":
            links[str(index)] = link
            raw["associations"][index] = _association_summary(link)
    if links:
        trace["associations"] = links
    origins = {
        key: raw["association_origins"][key] for key in sorted(_origin_refs(trace))
    }
    if origins:
        trace["association_origins"] = origins
    SimbadTrace.model_validate(trace)
    path = "provenance/simbad/" + raw["catalog_id"] + ".json"
    payload = json_bytes(trace)
    raw["schema_version"] = 3
    raw["simbad"] = compact_simbad(trace, artifact(path, payload))
    VersionThreeStellarRecord.model_validate(raw)
    return raw, path, payload


def restore_document(raw, root):
    """Validate both delivered views, then recover the version-two evidence representation."""
    from .inputs import read_bound
    from .readable import _expand

    public = VersionThreeStellarRecord.model_validate(raw)
    ref = public.simbad.provenance
    if ref.path != "provenance/simbad/" + public.catalog_id + ".json":
        raise ValueError("SIMBAD provenance must use the documented per-star path.")
    trace = json.loads(read_bound(root, ref))
    SimbadTrace.model_validate(trace)
    _expand(trace, trace)
    if (trace["catalog_id"], trace["data_version"]) != (
        public.catalog_id,
        public.data_version,
    ):
        raise ValueError("SIMBAD provenance belongs to another star or data version.")
    if raw["simbad"] != compact_simbad(trace, ref.model_dump()):
        raise ValueError("SIMBAD science or match summary differs from its provenance.")
    result = copy.deepcopy(raw)
    result["schema_version"] = 2
    result["simbad"] = trace["simbad"]
    expected = {
        str(i) for i, a in enumerate(raw["associations"]) if a["method"] == "simbad"
    }
    if set(trace.get("associations", {})) != expected:
        raise ValueError("SIMBAD association provenance is incomplete.")
    for index, link in trace.get("associations", {}).items():
        if link["method"] != "simbad" or raw["associations"][
            int(index)
        ] != _association_summary(link):
            raise ValueError("SIMBAD association summary differs from its provenance.")
        result["associations"][int(index)] = link
    expected_origins = _origin_refs(
        {k: trace.get(k, {}) for k in ("simbad", "associations")}
    )
    if set(trace.get("association_origins", {})) != expected_origins:
        raise ValueError("SIMBAD historical origin provenance is incomplete.")
    for key, origin in trace.get("association_origins", {}).items():
        if origin != raw.get("association_origins", {}).get(key):
            raise ValueError(
                "SIMBAD historical origin differs from the stellar evidence."
            )
    return result

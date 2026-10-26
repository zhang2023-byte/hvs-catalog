"""Publish complete identity evidence in one portable per-star provenance file."""

import copy
import json

from ..records.catalog_public import StellarProvenance, StellarRecord
from .inputs import json_bytes, read_bound
from .simbad_publication import _origin_refs, compact_simbad


def split_document(document, *, schema_version=5):
    """Move identity evidence without changing the version-two scientific content."""
    from .publication import artifact

    raw = copy.deepcopy(document)
    raw["schema_version"] = schema_version
    trace = {k: raw[k] for k in ("schema_version", "data_version", "catalog_id")}
    trace["associations"] = raw.pop("associations")
    decisions = raw.pop("review_decisions", [])
    if decisions:
        trace["review_decisions"] = decisions
    if "association_origins" in raw:
        trace["association_origins"] = raw.pop("association_origins")
    trace["simbad"] = raw["simbad"]
    StellarProvenance.model_validate(trace)
    path = "provenance/" + raw["catalog_id"] + "_provenance.json"
    payload = json_bytes(trace)
    raw["simbad"] = compact_simbad(trace)
    warnings = raw.pop("warnings")
    raw["provenance"] = artifact(path, payload)
    raw["warnings"] = warnings
    StellarRecord.model_validate(raw)
    return raw, path, payload


def restore_document(raw, root):
    """Validate the delivered evidence and recover the full construction record."""
    from .readable import _expand

    public = StellarRecord.model_validate(raw)
    ref = public.provenance
    if ref.path not in {
        "provenance/" + public.catalog_id + "_provenance.json",
        "provenance/" + public.catalog_id + ".json",  # Earlier version-four snapshots.
    }:
        raise ValueError("Stellar provenance must use the documented per-star path.")
    trace = json.loads(read_bound(root, ref))
    StellarProvenance.model_validate(trace)
    if (trace["catalog_id"], trace["data_version"]) != (
        public.catalog_id,
        public.data_version,
    ):
        raise ValueError("Stellar provenance belongs to another star or data version.")
    if trace["schema_version"] != public.schema_version:
        raise ValueError("Stellar provenance schema differs from the star.")
    _expand(trace, trace)
    expected_origins = _origin_refs({k: trace[k] for k in ("simbad", "associations")})
    if set(trace.get("association_origins", {})) != expected_origins:
        raise ValueError("Historical association origin provenance is incomplete.")
    if raw["simbad"] != compact_simbad(trace):
        raise ValueError("SIMBAD science or match summary differs from its provenance.")
    result = copy.deepcopy(raw)
    result["schema_version"] = 2
    result.pop("provenance")
    for field in ("associations", "association_origins", "simbad", "review_decisions"):
        if field in trace:
            result[field] = trace[field]
    return result

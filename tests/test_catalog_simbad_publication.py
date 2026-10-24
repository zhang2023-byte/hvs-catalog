"""Check scientific readability, independent evidence delivery and exact legacy recovery."""

import copy
import json
import shutil
from functools import partial

import pytest
import test_catalog
from test_catalog import GAIA_A, candidate, freeze, paper, source_star

from hvs_catalog.catalogue.inputs import json_bytes
from hvs_catalog.catalogue.publication import load_previous, verify_catalog

build = test_catalog.build
no_external_network = test_catalog.no_external_network


def rich_candidate():
    item = candidate("HD 9", aliases=[GAIA_A])
    item.fields.update(
        {
            "main_id": {"value": "HD 9"},
            "otype": {"value": "*"},
            "otype_txt": {"value": "*"},
            "ra": {"value": 10.0, "unit": "deg"},
            "dec": {"value": 0.0, "unit": "deg"},
            "coo_err_maj": {"value": 0.125, "unit": "mas"},
            "coo_err_min": {"value": 0.0625, "unit": "mas"},
            "coo_err_angle": {"value": 0, "unit": "deg"},
            "coo_bibcode": {"value": "CoordinateReference"},
            "pmra": {"value": -2.75, "unit": "mas / yr"},
            "pmdec": {"value": 0.0, "unit": "mas / yr"},
            "pm_err_maj": {"value": 0.25, "unit": "mas / yr"},
            "pm_err_min": {"value": 0.125, "unit": "mas / yr"},
            "pm_err_angle": {"value": 90, "unit": "deg"},
            "pm_qual": {"value": "A"},
            "pm_bibcode": {"value": "MotionReference"},
            "plx_value": {"value": -0.125, "unit": "mas"},
            "plx_err": {"value": 0.0625, "unit": "mas"},
            "plx_qual": {"value": "B"},
            "plx_bibcode": {"value": "ParallaxReference"},
            "rvz_err": {"value": 0.001, "unit": "m/s"},
            "rvz_type": {"value": "v"},
            "rvz_redshift": {"value": 0.00176789123456789},
            "rvz_nature": {"value": ""},
            "sp_type": {"value": "B2V"},
            "sp_bibcode": {"value": "SpectrumReference"},
            "ra_prec": {"value": 14},
            "plx_prec": {"value": 4},
            "nbref": {"value": 9},
            "hpx": {"value": 123},
            "update_date": {"value": "2026-01-01"},
            "morph_type": {"value": ""},
            "future_measurement": {"value": 0, "unit": "K"},
        }
    )
    item.photometry = [
        {
            "band": "G",
            "fields": {
                "filter": {"value": "G"},
                "flux": {"value": 18.123456789012345},
                "flux_err": {"value": 0.00234567890123456},
                "system": {"value": "V"},
                "qual": {"value": "C"},
                "bibcode": {"value": "PhotometryReference"},
                "oidref": {"value": 999},
                "flux_prec": {"value": 6},
                "unrecognized_flag": {"value": "J"},
            },
        }
    ]
    return item


def read_star(path):
    raw = json.loads((path / "stars/hvs-catalog-000001.json").read_bytes())
    trace = json.loads((path / raw["provenance"]["path"]).read_bytes())
    return raw, trace


def test_grouped_science_preserves_errors_units_references_and_unfamiliar_fields(
    build, tmp_path
):
    p = paper(1, [source_star([GAIA_A])])
    original = rich_candidate()
    folder, _ = freeze(
        tmp_path, [(p.key + "#/stars/0", p.science.stars[0])], lambda _: [original]
    )
    path, _, normalized = build([p], simbad=folder)
    raw, trace = read_star(path)
    science = raw["simbad"]["selected"]
    assert science["main_id"] == "HD 9" and science["aliases"] == [GAIA_A]
    assert science["coordinates"] == {
        "ra": {"value": 10.0, "unit": "deg"},
        "dec": {"value": 0.0, "unit": "deg"},
        "frame": "icrs",
        "bibcode": "CoordinateReference",
        "error_ellipse": {
            "major": {"value": 0.125, "unit": "mas"},
            "minor": {"value": 0.0625, "unit": "mas"},
            "angle": {"value": 0, "unit": "deg"},
        },
    }
    assert science["proper_motion"]["ra_cosdec"] == {"value": -2.75, "unit": "mas / yr"}
    assert science["proper_motion"]["dec"]["value"] == 0
    assert science["proper_motion"]["bibcode"] == "MotionReference"
    assert science["parallax"] == {
        "value": -0.125,
        "error": 0.0625,
        "unit": "mas",
        "quality": "B",
        "bibcode": "ParallaxReference",
    }
    assert science["velocity"] == {
        "radial_velocity": {"value": 530.0, "unit": "km/s"},
        "redshift": {"value": 0.00176789123456789},
        "reported_as": "v",
        "error": {"value": 0.001, "unit": "m/s"},
        "bibcode": "FictionalReference",
    }
    assert science["spectral_type"] == {"value": "B2V", "bibcode": "SpectrumReference"}
    assert science["photometry"] == [
        {
            "band": "G",
            "value": 18.123456789012345,
            "error": 0.00234567890123456,
            "system": "V",
            "quality": "C",
            "bibcode": "PhotometryReference",
            "additional_fields": {"unrecognized_flag": {"value": "J"}},
        }
    ]
    assert science["additional_fields"] == {
        "future_measurement": {"value": 0, "unit": "K"}
    }
    assert raw["simbad"]["match"] == {"identifiers": [GAIA_A]}
    assert (
        not {"queries", "matches", "bindings", "alternatives", "fields"}
        & raw["simbad"].keys()
    )
    assert trace["simbad"]["selected"] == original.model_dump(mode="json")
    assert normalized[0]["simbad"]["selected"] == original.model_dump(mode="json")
    assert trace["simbad"]["queries"][0]["response_sha256"]
    relocated = tmp_path / "relocated"
    shutil.copytree(path, relocated)
    shutil.rmtree(path)
    shutil.rmtree(folder)
    assert load_previous(relocated)[2][0].simbad.selected == original


@pytest.mark.parametrize("legacy_version", [2, 3])
def test_publication_versions_deliver_all_frozen_information(
    build, tmp_path, monkeypatch, legacy_version
):
    from hvs_catalog.catalogue import readable

    p = paper(1, [source_star(["Star 1"], 10, condition="Table 1 coordinates")])
    folder, _ = freeze(
        tmp_path,
        [(p.key + "#/stars/0", p.science.stars[0])],
        lambda _: [candidate("HD 9", ra=10.0001), candidate("HD 10", ra=10.0002)],
    )
    with monkeypatch.context() as patch:
        patch.setattr(
            readable,
            "export_files",
            partial(readable.export_files, schema_version=legacy_version),
        )
        old, _, before = build([p], simbad=folder)
    original = json.loads((old / "stars/hvs-catalog-000001.json").read_bytes())
    assert original["schema_version"] == legacy_version
    if legacy_version == 3:
        from hvs_catalog.catalogue.simbad_publication import restore_document

        original = restore_document(original, old)
    new, _, after = build([p], "v4", simbad=folder)
    raw, trace = read_star(new)
    assert trace["simbad"] == original["simbad"]
    for field in (
        "sources",
        "identifiers",
        "quantities",
        "contribution_types",
        "warnings",
    ):
        assert raw[field] == original[field]
    assert trace["associations"] == original["associations"]
    assert "associations" not in raw
    assert before[0]["simbad"] == after[0]["simbad"]
    summary = raw["simbad"]["match"]["coordinates"][0]
    assert summary["source_record"] == p.key + "#/stars/0"
    assert summary["candidate_count"] == 2 and summary["assumed_frame"] == "icrs"
    assert summary["separation_arcsec"] == pytest.approx(0.36)
    assert summary["radius_arcsec"] == 5
    newer, _, _ = build([p], "v4-again", simbad=folder)
    assert read_star(newer) == (raw, trace)


@pytest.mark.parametrize(
    "change",
    [
        "value",
        "error",
        "reference",
        "summary",
        "missing",
        "hash",
        "identity",
        "version",
        "path",
        "extra",
        "trace_value",
    ],
)
def test_delivered_provenance_and_science_cannot_silently_diverge(
    build, tmp_path, change
):
    p = paper(1, [source_star([GAIA_A])])
    folder, _ = freeze(
        tmp_path,
        [(p.key + "#/stars/0", p.science.stars[0])],
        lambda _: [rich_candidate()],
    )
    path, _, _ = build([p], simbad=folder)
    raw, trace = read_star(path)
    trace_path = path / raw["provenance"]["path"]
    if change in {"value", "error", "reference"}:
        raw["simbad"]["selected"]["parallax"][
            "bibcode" if change == "reference" else change
        ] = "Wrong" if change == "reference" else 999
    elif change == "summary":
        raw["simbad"]["match"]["identifiers"] = ["HD 999"]
    elif change == "identity":
        trace["catalog_id"] = "hvs-catalog-000002"
    elif change == "version":
        trace["data_version"] = "another-version"
    elif change == "trace_value":
        trace["simbad"]["selected"]["fields"]["plx_value"]["value"] = 999
    elif change == "path":
        raw["provenance"]["path"] = "papers/index.json"
    elif change == "extra":
        (path / "provenance/unreferenced.json").write_bytes(json_bytes(trace))
    if change not in {"missing", "hash", "path"}:
        test_catalog.replace_provenance(path, raw, trace)
    else:
        if change == "missing":
            trace_path.unlink()
        elif change == "hash":
            trace_path.write_bytes(b"{}\n")
        test_catalog.replace_star(path, raw)
    with pytest.raises((ValueError, FileNotFoundError)):
        verify_catalog(path)


def test_conflict_summary_does_not_present_an_unselected_candidate_as_science(
    build, tmp_path
):
    p = paper(1, [source_star(["HD 1", "HIP 2"])])
    folder, _ = freeze(
        tmp_path,
        [(p.key + "#/stars/0", p.science.stars[0])],
        lambda q: [candidate(q.identifier)],
    )
    path, _, _ = build([p], simbad=folder)
    raw, trace = read_star(path)
    assert raw["simbad"]["status"] == "conflict"
    assert "selected" not in raw["simbad"]
    assert raw["simbad"]["match"] == {"candidates": ["HD 1", "HIP 2"]}
    assert len(trace["simbad"]["alternatives"]) == 2
    assert (
        raw["simbad"]["reason"] == "Reliable identifiers resolve to different objects."
    )


def test_measurement_error_units_are_not_assumed_or_converted():
    from hvs_catalog.catalogue.simbad_publication import scientific_data

    raw = rich_candidate().model_dump(mode="json")
    changed = copy.deepcopy(raw)
    changed["fields"]["plx_err"]["unit"] = "arcsec"
    assert scientific_data(changed)["parallax"]["error_unit"] == "arcsec"
    assert scientific_data(changed)["parallax"]["error"] == 0.0625
    assert "error_unit" not in scientific_data(raw)["parallax"]

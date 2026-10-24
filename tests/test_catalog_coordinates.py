"""Coordinate parsing and frozen enrichment preserve paper values and stable identities."""

import copy

import pytest
import test_catalog
from test_catalog import candidate, freeze, paper, source_star

from hvs_catalog.catalogue.identity import coordinate, coordinate_interpretation, sky
from hvs_catalog.catalogue.simbad import FrozenSimbad, queries_for, resolve_simbad

build = test_catalog.build


def coordinates(
    ra="10",
    dec="20",
    ra_unit="deg",
    dec_unit="deg",
    ra_condition="Right ascension",
    dec_condition="Declination",
):
    obj = source_star(["Star 1"], ra, dec)
    obj["quantities"][1]["values"][0].update(unit=ra_unit, condition=ra_condition)
    obj["quantities"][2]["values"][0].update(unit=dec_unit, condition=dec_condition)
    return obj


@pytest.mark.parametrize(
    "ra_condition,dec_condition,frame,equinox,basis,assumed",
    [
        ("Right ascension", "Declination", "icrs", None, "assumed_icrs", True),
        ("ICRS RA", "Declination", "icrs", None, "coordinate_conditions", False),
        ("RA", "J2000 Dec", "fk5", "J2000", "coordinate_conditions", False),
        (
            "J2000 / ICRS RA",
            "Declination",
            "icrs",
            None,
            "coordinate_conditions",
            False,
        ),
        ("B1950 RA", "FK4 B1950 Dec", "fk4", "B1950", "coordinate_conditions", False),
        (
            "Gaia DR2 right ascension",
            "Gaia DR2 declination",
            "icrs",
            None,
            "gaia_coordinate_source",
            False,
        ),
        (
            "RA at epoch J2016.0",
            "Dec at epoch J2016.0",
            "icrs",
            None,
            "assumed_icrs",
            True,
        ),
    ],
)
def test_coordinate_frame_basis(
    ra_condition, dec_condition, frame, equinox, basis, assumed
):
    source = paper(
        1, [coordinates(ra_condition=ra_condition, dec_condition=dec_condition)]
    ).science.stars[0]
    original = source.model_dump(mode="json")
    position = coordinate(source)
    assert (position.ra_deg, position.dec_deg) == (10, 20)
    assert (position.frame, position.equinox) == (frame, equinox)
    info = coordinate_interpretation(position)
    assert info["frame_basis"] == basis and info["frame_assumed"] is assumed
    assert info["epoch_propagation"] is False
    assert source.model_dump(mode="json") == original


@pytest.mark.parametrize(
    "left,right",
    [
        ("ICRS RA", "B1950 Dec"),
        ("FK4 RA", "Dec"),
        ("FK5 RA", "Dec"),
        ("RA equinox J1975", "Dec equinox J1975"),
        ("ICRS RA", "FK5 J2000 Dec"),
        ("Unknown frame", "Unknown frame"),
        ("RA B1900", "Dec B1900"),
        ("RA J1975", "Dec J1975"),
    ],
)
def test_incompatible_or_unsupported_frames_do_not_default(left, right):
    obj = paper(1, [coordinates(ra_condition=left, dec_condition=right)]).science.stars[
        0
    ]
    assert coordinate(obj) is None


@pytest.mark.parametrize(
    "ra,dec,ru,du,expected",
    [
        (
            "01h02m03.4s",
            "−20d30m40s",
            "hms",
            "dms",
            (15.514166666666666, -20.511111111111113),
        ),
        (
            "01:02:03.4",
            "-20:30:40",
            "hourangle",
            "deg",
            (15.514166666666666, -20.511111111111113),
        ),
        (
            "01h02m03s.4",
            "-20°30′40″",
            "hourangle",
            "deg",
            (15.514166666666666, -20.511111111111113),
        ),
        ("10", "20", "deg ± mas", "deg ± mas", (10, 20)),
    ],
)
def test_coordinate_typography_changes_query_only(ra, dec, ru, du, expected):
    obj = paper(1, [coordinates(ra, dec, ru, du)]).science.stars[0]
    original = obj.model_dump(mode="json")
    pos = coordinate(obj)
    assert (pos.ra_deg, pos.dec_deg) == pytest.approx(expected)
    assert pos.evidence == [
        v.model_dump(mode="json") for q in obj.quantities[1:] for v in q.values
    ]
    assert obj.model_dump(mode="json") == original


@pytest.mark.parametrize(
    "ra,dec,ru,du",
    [
        ("360", "0", "deg", "deg"),
        ("-1", "0", "deg", "deg"),
        ("10", "91", "deg", "deg"),
        ("nan", "0", "deg", "deg"),
        ("10", "20", None, "deg"),
        ("10", "20", "deg", "hourangle"),
        ("10 ± 2", "20", "deg", "deg"),
    ],
)
def test_invalid_coordinates_are_not_queried(ra, dec, ru, du):
    raw = coordinates(ra, dec, ru, du)
    if ru is None:
        del raw["quantities"][1]["values"][0]["unit"]
    obj = paper(1, [raw]).science.stars[0]
    assert queries_for([("source", obj)]) == []


def test_pairing_uses_shared_evidence_and_deduplicates_identical_pairs():
    obj = coordinates()
    for quantity in obj["quantities"][1:]:
        extra = copy.deepcopy(quantity["values"][0])
        extra["evidence"][0]["location"] = "I:p2-b0"
        del extra["evidence"][0]["quote"]
        quantity["values"].append(extra)
    source = paper(1, [obj]).science.stars[0]
    pos = coordinate(source)
    assert pos is not None and len(pos.evidence) == 4
    assert len(queries_for([("source", source)])) == 1
    source.quantities[1].values[-1].value = "11"
    assert coordinate(source) is None
    source.quantities[1].values.pop()
    source.quantities[2].values.pop()
    source.quantities[2].values[0].evidence[0].location = "T:p9-b0"
    assert coordinate(source) is None
    source.quantities.pop()
    assert coordinate(source) is None


def test_coordinate_quotes_supply_frame_but_unrelated_ids_do_not():
    obj = coordinates()
    obj["quantities"][1]["values"][0]["evidence"][0]["quote"] = (
        "RA (B1950), Dec (B1950)"
    )
    pos = coordinate(paper(1, [obj]).science.stars[0])
    assert pos.frame == "fk4"
    assert coordinate_interpretation(pos)["frame_basis"] == "coordinate_quotes"
    obj = coordinates()
    obj["identifiers"][0]["value"] = "Gaia DR3 1234567890123456789"
    pos = coordinate(paper(1, [obj]).science.stars[0])
    assert coordinate_interpretation(pos)["frame_assumed"]
    obj = coordinates(
        ra_condition="RA for VIRAC2 (no Gaia DR3 counterpart)",
        dec_condition="Dec for VIRAC2 (no Gaia DR3 counterpart)",
    )
    pos = coordinate(paper(1, [obj]).science.stars[0])
    assert coordinate_interpretation(pos)["frame_assumed"]


def test_different_reported_coordinate_epochs_are_not_paired():
    obj = coordinates(
        ra_condition="ICRS RA at epoch J2000", dec_condition="ICRS Dec at epoch J2016"
    )
    assert coordinate(paper(1, [obj]).science.stars[0]) is None


def test_gaia_coordinate_date_is_not_an_equinox():
    obj = coordinates(
        ra_condition="Gaia DR2 right ascension, J2015.5",
        dec_condition="Gaia DR2 declination, J2015.5",
    )
    pos = coordinate(paper(1, [obj]).science.stars[0])
    assert pos.frame == "icrs" and pos.equinox is None
    assert coordinate_interpretation(pos)["frame_basis"] == "gaia_coordinate_source"
    assert (pos.ra_deg, pos.dec_deg) == (10, 20)


def test_old_reference_frame_transforms_without_epoch_propagation():
    obj = coordinates("0", "0", ra_condition="B1950", dec_condition="B1950")
    position = coordinate(paper(1, [obj]).science.stars[0])
    transformed = sky(position).icrs
    assert transformed.ra.degree == pytest.approx(0.6406846, abs=1e-6)
    assert transformed.dec.degree == pytest.approx(0.2784066, abs=1e-6)


def test_mixed_reference_frames_match_one_direction(build):
    old_frame = paper(
        1, [coordinates("0", "0", ra_condition="B1950", dec_condition="B1950")]
    )
    modern = paper(2, [source_star(["Star 2"], 0.6406846, 0.2784066)])
    _, _, stars = build([old_frame, modern])
    assert len(stars) == 1
    edge = next(e for e in stars[0]["associations"] if e["method"] == "coordinates")
    assert edge["details"]["separation_arcsec"] < 0.01
    assert not edge["details"]["epoch_propagation"]


@pytest.mark.parametrize("matched", [True, False])
def test_assumed_frame_persists_for_matches_and_empty_responses(tmp_path, matched):
    source = paper(1, [coordinates("10", "0")]).science.stars[0]
    entries = [("2601.00001v1#/stars/0", source)]
    folder, _ = freeze(
        tmp_path,
        entries,
        lambda q: (
            [candidate("HD 9", ra=10.0001), candidate("HD 10", ra=10.0002)]
            if matched
            else []
        ),
    )
    snapshot = resolve_simbad(entries, FrozenSimbad(folder))
    assert snapshot.status == ("matched" if matched else "not_found")
    assert any("ICRS was assumed" in w for w in snapshot.warnings)
    if matched:
        assert snapshot.selected.main_id == "HD 9"
        assert any("Multiple SIMBAD" in w for w in snapshot.warnings)
        assert any("coordinates only" in w for w in snapshot.warnings)
    query = snapshot.queries[0]
    assert query["coordinate_inputs"][0]["node"] == entries[0][0]
    assert query["coordinate_inputs"][0]["interpretation"]["frame_assumed"]
    # Query provenance never changes the service request or its frozen cache key.
    assert query["query"]["position"]["evidence"] == []
    explicit = paper(2, [source_star(["Star 2"], 10)]).science.stars[0]
    assert queries_for(entries) == queries_for([("other", explicit)])


def test_frozen_coordinate_enrichment_preserves_source_science(build, tmp_path):
    p = paper(1, [coordinates("10", "0")])
    _, manifest, before = build([p])
    folder, _ = freeze(
        tmp_path,
        [(p.key + "#/stars/0", p.science.stars[0])],
        lambda q: [candidate("HD 9", ra=10.0001)],
    )
    _, newer, after = build([p], "enriched", simbad=folder)
    assert newer.next_id == manifest.next_id
    for field in (
        "catalog_id",
        "sources",
        "identifiers",
        "contribution_types",
        "quantities",
    ):
        assert after[0][field] == before[0][field]
    assert after[0]["simbad"]["method"] == "coordinates"
    assert before[0]["display_name"] == "Star 1 (2601.00001)"
    assert after[0]["display_name"] == "HD 9"
    assert any("ICRS was assumed" in w for w in after[0]["warnings"])
    edge = next(e for e in after[0]["associations"] if e["right"] == "simbad:HD 9")
    assert edge["details"]["coordinate_interpretation"]["frame_assumed"]

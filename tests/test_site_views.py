"""Scientific website choices use fabricated values, not the released catalog."""

import copy

import pytest
from sitegen.views import (
    aliases,
    arxiv_order,
    choose,
    clean_quote,
    coordinate_prefix,
    coordinates,
    evidence_view,
    format_number,
    gaia_id,
    linked_by_position,
    name_key,
    sexagesimal,
    slugs,
    source_view,
    star_view,
    value_view,
)


def value(
    number="10", source="2001.00001v1", attribution="own", standard=True, **fields
):
    raw = {
        "value": number,
        "unit": "pc",
        "source": source,
        "attribution": attribution,
        "condition": "Synthetic model A",
        "evidence": [
            {"location": "T:p12-b0", "quote": "<tr><td>star</td><td>10</td></tr>"}
        ],
        **fields,
    }
    if standard:
        raw["standard"] = {
            "value": number,
            "unit": "kpc",
            "approximate": False,
            **fields,
        }
    return raw


def source(author="A. Example"):
    return {
        "title": "A fabricated paper",
        "authors": [author, "B. Other"],
        "pdf_url": "https://arxiv.org/pdf/2001.00001v1",
        "records": ["/stars/0"],
    }


def star():
    return {
        "catalog_id": "hvs-catalog-000010",
        "display_name": "Example",
        "data_version": "synthetic",
        "identifiers": [],
        "sources": {"2001.00001v1": source()},
        "quantities": [{"quantity": "distance", "values": [value()]}],
        "simbad": {"status": "not_found"},
    }


@pytest.mark.parametrize(
    "bad",
    [
        value("99", attribution="cited"),
        value("99", attribution="mismatch"),
        value("99", standard=False),
    ],
)
def test_default_never_falls_back_to_unqualified_values(bad):
    assert choose([bad]) is None
    good = value("5")
    assert choose([bad, good]) is good


def test_error_tier_precedes_recency_then_month_number_and_first_occurrence():
    old = value("917", source="1503.01650v1", error="7")
    recent = value("750", source="2304.02969v1")
    assert choose([recent, old]) is old
    same = value("918", source="1503.01650v1", error="2")
    assert choose([old, same]) is old
    newer = value("919", source="1503.01651v1", lower_error="1", upper_error="3")
    assert choose([old, newer]) is newer
    revised = value("920", source="1502.99999v9", error="1")
    assert choose([newer, revised]) is newer
    zero_error = value("0", source="2401.00001v1", error="0")
    assert choose([old, zero_error]) is zero_error


def test_distance_modulus_is_literature_only_and_pc_conversion_is_reused():
    modulus = value("14.68", standard=False, error="0.07")
    modulus["unit"] = "mag"
    distance = value("8884", error="11")
    distance["standard"].update(value="8.884", error="0.011")
    assert choose([modulus, distance]) is distance
    sample = star()
    sample["quantities"][0]["values"] = [modulus, distance]
    view, row, csv = star_view(sample, {}, "example", "https://github.com/a/b")
    assert row["values"]["distance"]["display"] == "8.884 ± 0.011"
    assert view["latest"]["distance"]["display"] == "8.884 ± 0.011"
    assert csv["d_kpc"] == "8.884" and csv["d_kpc_error"] == "0.011"
    cell = view["literature"][0]["cells"]["distance"]
    assert cell["main"]["display"] == "8.884 ± 0.011"
    assert cell["more_label"] == "+1"
    assert cell["others"][0]["unit"] == "mag"
    assert (
        view["latest"]["distance"]["tooltip"] == "Example et al. 2020 · 1 of 2 values"
    )
    sample["quantities"][0]["values"] = [modulus]
    view, _, _ = star_view(sample, {}, "example", "https://github.com/a/b")
    assert "distance" not in view["latest"]
    assert (
        view["literature"][0]["cells"]["distance"]["main"]["table_display"]
        == "14.68 ± 0.07 mag"
    )


def test_literature_keeps_all_own_values_and_labels_quoted_and_mismatch():
    sample = star()
    sample["sources"]["2101.00001v1"] = source("B. Empty")
    sample["quantities"][0]["values"] = [
        value("4"),
        value("3", error="1"),
        value("2", error="1"),
        value("8", attribution="cited"),
        value("9", attribution="mismatch"),
    ]
    view, _, _ = star_view(sample, {}, "example", "https://github.com/a/b")
    assert len(view["literature"]) == 2
    assert view["literature"][0]["cells"]["distance"]["main"] is None
    assert not view["literature"][0]["has_own_values"]
    assert not view["literature"][0]["has_values"]
    assert view["literature"][1]["has_own_values"]
    assert view["literature"][1]["has_values"]
    cell = view["literature"][1]["cells"]["distance"]
    assert cell["main"]["display"] == "3 ± 1"
    assert cell["more_label"] == "+2"
    assert [v["display"] for v in cell["others"]] == ["4", "2 ± 1"]
    assert [v["label"] for v in cell["extra"]] == ["quoted", "not comparable"]


@pytest.mark.parametrize("attribution", ["cited", "mismatch"])
def test_literature_visibility_tracks_own_and_quoted_values(attribution):
    sample = star()
    sample["quantities"][0]["values"] = [value("0", attribution=attribution)]
    view, _, _ = star_view(sample, {}, "example", "https://github.com/a/b")
    assert not view["literature"][0]["has_own_values"]
    assert view["literature"][0]["has_values"]
    sample["quantities"][0]["values"] = [value("0", standard=False)]
    view, _, _ = star_view(sample, {}, "example", "https://github.com/a/b")
    assert view["literature"][0]["has_own_values"]
    assert view["literature"][0]["has_values"]


def test_public_catalog_number_is_numeric_but_page_keeps_full_identifier():
    sample = star()
    sample["catalog_id"] = "hvs-catalog-000356"
    view, row, _ = star_view(sample, {}, "example", "https://github.com/a/b")
    assert row["catalog_number"] == 356
    assert view["catalog_id"] == "hvs-catalog-000356"


def test_bound_probability_does_not_imply_unbound_probability():
    sample = star()
    sample["quantities"] = [{"quantity": "bound_probability", "values": [value("0.1")]}]
    view, row, csv = star_view(sample, {}, "example", "https://github.com/a/b")
    assert "unbound_probability" not in view["latest"]
    assert row["values"]["unbound_probability"] is None
    assert csv["p_unbound"] == ""
    assert csv["p_bound"] == "0.1"


@pytest.mark.parametrize(
    "data,expected",
    [
        ({"value": "10.00", "error": "0.10"}, "10.00 ± 0.10"),
        ({"value": "1", "lower_error": "0.2", "upper_error": "0.3"}, "1 +0.3 −0.2"),
        ({"lower_bound": "2", "upper_bound": "5"}, "2–5"),
        ({"lower_bound": "2"}, "> 2"),
        ({"upper_bound": "5"}, "< 5"),
        ({"value": "5", "approximate": True}, "~5"),
        ({"value": "-2.00", "error": "0.1"}, "−2.00 ± 0.1"),
        ({"lower_bound": "-5", "upper_bound": "-2"}, "−5–−2"),
        ({"upper_bound": "-3"}, "< −3"),
        ({"value": "-1e-3", "error": "2e-4"}, "−1e−3 ± 2e−4"),
        ({"value": "10", "lower_error": "0.2", "upper_error": "0.20"}, "10 ± 0.20"),
        ({"value": "10", "lower_error": "0.00", "upper_error": "0"}, "10 ± 0.00"),
    ],
)
def test_display_preserves_precision_and_separate_markers(data, expected):
    assert format_number(data) == expected


def test_point_precedes_limits_and_csv_is_numeric():
    bound = value()
    bound["standard"] = {
        "lower_bound": "20",
        "upper_bound": "40",
        "unit": "kpc",
        "approximate": True,
        "lower_inclusive": False,
    }
    assert choose([bound, value("3")])["value"] == "3"
    sample = star()
    sample["quantities"][0]["values"] = [bound]
    _, row, csv = star_view(sample, {}, "example", "https://github.com/a/b")
    assert row["values"]["distance"]["sort_group"] == 1
    assert csv["d_kpc"] == ""
    assert csv["d_kpc_lower_bound"] == "20"
    assert csv["d_kpc_upper_bound"] == "40"
    assert csv["d_kpc_limit"] == "range"
    assert csv["d_kpc_approximate"] == "true"
    assert csv["d_kpc_lower_inclusive"] == "false"


def test_arxiv_year_and_source_name_use_submission_not_version():
    assert arxiv_order("astro-ph/9901002v9") == (1999, 1, 2)
    assert arxiv_order("astro-ph/0701002v1") == (2007, 1, 2)
    assert arxiv_order("0704.0002v1") == (2007, 4, 2)
    assert (
        source_view("1907.11725v2", source("Sergey E. Koposov"))["label"]
        == "Koposov et al. 2019"
    )
    assert (
        source_view("1907.11725v2", source("P. van der Meer"))["short"]
        == "van der Meer et al."
    )
    assert source_view("1907.11725v2", source("Li, Q."))["short"] == "Li et al."


def test_quote_cleanup_preserves_latex_and_plain_comparisons():
    assert (
        clean_quote(
            r"<table><tr><td>A</td><td>$x_{1}$</td></tr><tr><td>B</td><td>&lt; 2</td></tr></table>"
        )
        == "A | $x_{1}$\nB | < 2"
    )
    assert clean_quote("v < 2 and <sup>−1</sup>") == "v < 2 and −1"
    assert clean_quote("<img src=x onerror=alert(1)><b>safe</b>") == "safe"


def test_page_links_image_evidence_and_corrections():
    sample = value()
    with pytest.raises(ValueError, match="HTTPS"):
        evidence_view(sample, "javascript:alert(1)")
    sample["evidence"].append({"location": "I:p3-b18"})
    assert evidence_view(sample, "https://example.com/fixed.pdf") == [
        {
            "page": 12,
            "url": "https://example.com/fixed.pdf#page=12",
            "quotes": [{"text": "star | 10", "label": ""}],
        },
        {
            "page": 3,
            "url": "https://example.com/fixed.pdf#page=3",
            "quotes": [{"text": "image evidence, no text quote", "label": ""}],
        },
    ]
    sample["correction"] = {
        "decision": "hidden-internal-decision",
        "evidence": [{"page": 8, "quote": "<b>Corrected</b>"}],
    }
    view = value_view(sample, {sample["source"]: source()})
    assert view["correction_notice"] == "Corrected against the fixed PDF."
    assert view["evidence"][0] == {
        "page": 8,
        "url": "https://arxiv.org/pdf/2001.00001v1#page=8",
        "quotes": [{"text": "Corrected", "label": "PDF correction"}],
    }
    assert "hidden-internal-decision" not in str(view)
    sample["evidence"] = [{"location": "T:p0-b0"}]
    with pytest.raises(ValueError):
        evidence_view(sample, "https://example.com/fixed.pdf")


def test_alias_frequency_gaia_edr3_and_position_provenance():
    sample = star()
    sample["sources"]["2101.00001v1"] = source()
    sample["identifiers"] = [
        {"value": v, "source": s}
        for v, s in [
            ("Frequent", "2001.00001v1"),
            ("Frequent", "2101.00001v1"),
            ("Rare", "2001.00001v1"),
            ("Gaia EDR3 1234567890123456789", "2001.00001v1"),
        ]
    ]
    sample["simbad"] = {
        "status": "matched",
        "method": "coordinates",
        "selected": {"main_id": "External", "aliases": ["External"]},
    }
    assert aliases(sample)[0] == "Frequent"
    assert aliases(sample)[-1] == "External"
    assert gaia_id(sample) == "1234567890123456789"
    trace = {
        "associations": [
            {
                "method": "coordinates",
                "left": "2001.00001v1#/stars/0",
                "right": "2101.00001v1#/stars/0",
            }
        ]
    }
    assert linked_by_position(sample, trace)
    assert not linked_by_position(sample, {"associations": []})
    trace["associations"][0]["right"] = "2201.00001v1#/stars/0"
    assert not linked_by_position(sample, trace)
    sample["warnings"] = ["linked by coordinates"]
    assert not linked_by_position(sample, {})
    view, _, _ = star_view(sample, {}, "example", "https://github.com/a/b")
    assert not view["simbad_position"]
    view, _, _ = star_view(
        sample,
        {"simbad": {"status": "matched", "method": "coordinates"}},
        "example",
        "https://github.com/a/b",
    )
    assert view["simbad_position"]


def test_coordinates_prefer_simbad_then_defaults_and_can_be_absent():
    sample = star()
    assert coordinates(sample, {}) is None
    defaults = {"ra": value("136.937458"), "dec": value("2.751917")}
    result = coordinates(sample, defaults)
    assert result["text"] == "RA 09:07:44.99  Dec +02:45:06.9 (Example et al. 2020)"
    sample["simbad"] = {
        "status": "matched",
        "selected": {"coordinates": {"ra": {"value": 0}, "dec": {"value": -0.5}}},
    }
    assert (
        coordinates(sample, defaults)["text"]
        == "RA 00:00:00.00  Dec −00:30:00.0 (SIMBAD)"
    )
    assert sexagesimal("359.9999999", True) == "00:00:00.00"


def test_addresses_are_named_and_duplicate_suffixes_do_not_expose_internal_ids():
    a = star()
    b = copy.deepcopy(a)
    b["catalog_id"] = "hidden-id"
    assert slugs([a, b, {"display_name": "US 708"}]) == [
        "example-1",
        "example-2",
        "us-708",
    ]


@pytest.mark.parametrize(
    "quantities,expected",
    [
        ([{"quantity": "distance", "values": [value()]}], (False, False)),
        ([{"quantity": "distance", "values": [value(standard=False)]}], (False, False)),
        (
            [{"quantity": "distance", "values": [value(attribution="cited")]}],
            (True, False),
        ),
        (
            [{"quantity": "distance", "values": [value(attribution="mismatch")]}],
            (True, False),
        ),
        ([], (True, True)),
    ],
)
def test_literature_defaults_ignore_coordinates_and_reveal_available_papers(
    quantities, expected
):
    sample = star()
    sample["quantities"] = quantities + [
        {"quantity": "ra", "values": [value("15")]},
        {"quantity": "dec", "values": [value("-0.5")]},
    ]
    view, _, _ = star_view(sample, {}, "example", "https://github.com/a/b")
    assert (
        view["coordinates"]["text"]
        == "RA 01:00:00.00  Dec −00:30:00.0 (Example et al. 2020)"
    )
    assert not {"ra", "dec"} & {c["key"] for c in view["columns"]}
    assert not {"ra", "dec"} & view["literature"][0]["cells"].keys()
    assert view["literature_defaults"] == dict(
        zip(("show_quoted", "show_empty"), expected)
    )
    assert view["literature_identity_columns"] == [
        {"key": "year", "label": "Year"},
        {"key": "paper", "label": "Paper"},
    ]


def test_filtered_header_aliases_remain_searchable_and_in_csv():
    sample = star()
    hidden = [
        "Gaia DR2 1234567890123456789",
        "Gaia EDR3 1234567890123456789",
        "Gaia DR3 1234567890123456789",
        "1234567890123456789",
        "7",
        "#12",
        "ID 1",
        "star 2",
        "No. 3",
        "object C",
        "source #4",
        "candidate A5",
        "Smith et al. (2020) star",
        "HVS",
    ]
    kept = ["HVS18", "S1", "S5-HVS1", "Star Alpha", "Gaia DR2 9876543210987654321"]
    sample["identifiers"] = [
        {"value": name, "source": "2001.00001v1"} for name in hidden + kept
    ]
    sample["display_aliases"] = ["ID 99"]
    sample["simbad"] = {"selected": {"aliases": ["star 88"]}}
    view, row, csv = star_view(sample, {}, "example", "https://github.com/a/b")
    assert set(view["aliases"] + view["more_aliases"]) == set(kept)
    assert view["alias_more_label"] == "+2 more"
    for name in hidden + kept + ["ID 99", "star 88"]:
        assert "".join(name.lower().split()) in row["search"]
        assert name in csv["aliases"]
    assert view["gaia_line"] == "Gaia DR3 1234567890123456789"
    sample["display_name"] = "Gaia DR3 1234567890123456789"
    view, _, _ = star_view(sample, {}, "example", "https://github.com/a/b")
    assert view["gaia_line"] == ""
    assert view["gaia"] == "1234567890123456789"
    assert view["gaia_url"].endswith("Source=1234567890123456789")


def test_csv_preserves_ascii_and_asymmetric_fields_with_catalog_id_first():
    sample = star()
    sample["quantities"][0]["values"] = [
        value("-1.20", lower_error="0.30", upper_error="0.3")
    ]
    view, _, csv = star_view(sample, {}, "example", "https://github.com/a/b")
    assert next(iter(csv)) == "catalog_id"
    assert csv["catalog_id"] == sample["catalog_id"]
    assert view["latest"]["distance"]["display"] == "−1.20 ± 0.30"
    assert (
        csv["d_kpc"],
        csv["d_kpc_lower_error"],
        csv["d_kpc_upper_error"],
        csv["d_kpc_error"],
    ) == ("-1.20", "0.30", "0.3", "")


def test_evidence_groups_corrections_and_extraction_by_page_and_hides_repeated_value():
    sample = value("-4", lower_error="2.0", upper_error="2")
    sample["unit"] = "kpc"
    sample["evidence"] = [
        {"location": "T:p12-b0", "quote": "first -4"},
        {"location": "T:p12-b1", "quote": "second"},
        {"location": "I:p12-b2"},
        {"location": "T:p13-b0", "quote": "next page"},
    ]
    sample["correction"] = {"evidence": [{"page": 12, "quote": "corrected"}]}
    view = value_view(sample, {sample["source"]: source()})
    assert not view["reported"]
    assert view["raw"] == "−4 ± 2.0 kpc"
    assert [p["page"] for p in view["evidence"]] == [12, 13]
    assert [q["text"] for q in view["evidence"][0]["quotes"]] == [
        "corrected",
        "first -4",
        "second",
    ]
    assert view["evidence"][0]["quotes"][0]["label"] == "PDF correction"
    assert view["evidence"][0]["quotes"][1]["label"] == "Original extraction evidence"
    sample.pop("correction")
    sample["unit"] = "pc"
    sample["value"] = "-4000"
    view = value_view(sample, {sample["source"]: source()})
    assert view["reported"] == "Reported value: −4000 ± 2.0 pc"


def test_unicode_hyphens_deduplicate_header_without_changing_exported_aliases():
    sample = star()
    sample["display_name"] = "J0927−6335"
    spellings = ["J0927-6335", "J0927–6335", "J0927−6335"]
    assert {name_key(name) for name in spellings} == {"j09276335"}
    sample["identifiers"] = [
        {"value": name, "source": "2001.00001v1"} for name in spellings
    ]
    sample["display_aliases"] = ["Kept", "J0927–6335"]
    sample["simbad"] = {"selected": {"aliases": ["J0927-6335"]}}
    view, row, csv = star_view(sample, {}, "example", "https://github.com/a/b")
    assert view["name"] == "J0927−6335"
    assert view["aliases"] == ["Kept"]
    assert not view["more_aliases"]
    assert csv["aliases"] == "J0927-6335; J0927–6335; Kept"
    for name in spellings:
        assert name.lower() in row["search"]


def test_equivalent_hyphen_spellings_keep_frequency_ranking():
    sample = star()
    sample["identifiers"] = [
        {"value": name, "source": paper}
        for name, paper in [
            ("Other-1", "2001.00001v1"),
            ("Other−1", "2001.00001v1"),
            ("Other−1", "2101.00001v1"),
            ("Other–1", "2101.00001v1"),
        ]
    ]
    assert aliases(sample) == ["Other−1"]


@pytest.mark.parametrize(
    "name,prefix",
    [
        ("SDSS J090744.99+024506.9", "sdss"),
        ("SDSSJ090744.99+024506.9", "sdss"),
        ("sdss 090744.99−024506.9", "sdss"),
        ("SDSS J090744.99–024506.9", "sdss"),
        ("2MASS J09074499-0245069", "2mass"),
        ("HE 0437-5439", "he"),
        ("HVS2", None),
        ("HVS3", None),
        ("Gaia DR3 1234567890123456789", None),
        ("J0927−6335", None),
    ],
)
def test_coordinate_alias_prefixes_require_signed_coordinates(name, prefix):
    assert coordinate_prefix(name) == prefix


def test_coordinate_alias_variants_move_to_more_without_affecting_numbers_or_csv():
    sample = star()
    ordered = [
        "SDSS J090745.0+024507",
        "SDSSJ090744.99+024506.9",
        "SDSS 090744.99+024506.8",
        "HVS2",
        "HVS3",
        "2MASS J09074499+0245069",
    ]
    sample["display_aliases"] = ordered.copy()
    view, row, csv = star_view(sample, {}, "example", "https://github.com/a/b")
    assert view["aliases"] == [ordered[0], "HVS2", "HVS3"]
    assert view["more_aliases"] == [ordered[1], ordered[2], ordered[5]]
    assert view["alias_more_label"] == "+3 more"
    assert csv["aliases"] == "; ".join(ordered)
    assert sample["display_aliases"] == ordered
    for name in ordered:
        assert "".join(name.lower().split()) in row["search"]
    sample["display_aliases"] = ordered[:3]
    view, _, _ = star_view(sample, {}, "example", "https://github.com/a/b")
    assert view["aliases"] == [ordered[0]]
    assert view["more_aliases"] == ordered[1:3]
    assert view["alias_more_label"] == "+2 more"


def test_image_placeholders_are_removed_only_on_pages_with_text():
    sample = value()
    sample["evidence"] = [
        {"location": "I:p2-b0"},
        {"location": "I:p3-b0"},
        {"location": "T:p2-b1", "quote": "<b>Text on page two</b>"},
        {"location": "T:p4-b0", "quote": "Text on page four"},
        {"location": "I:p4-b1"},
        {"location": "I:p5-b0"},
    ]
    sample["correction"] = {"evidence": [{"page": 5, "quote": "Correction text"}]}
    pages = evidence_view(sample, "https://example.com/fixed.pdf")
    assert {p["page"]: [q["text"] for q in p["quotes"]] for p in pages} == {
        2: ["Text on page two"],
        3: ["image evidence, no text quote"],
        4: ["Text on page four"],
        5: ["Correction text"],
    }
    assert pages[0]["quotes"][0]["label"] == "PDF correction"

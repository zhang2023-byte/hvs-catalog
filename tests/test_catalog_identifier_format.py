"""Catalog formatting must not supply identity or alter identifier bodies."""

import pytest

from hvs_catalog.records.identifiers import normalize_catalog_identifier


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Gaia source ID 0123456789012345678", "Gaia 0123456789012345678"),
        (
            " Gaia\tDR2  designation 0123456789012345678 ",
            "Gaia DR2 0123456789012345678",
        ),
        ("APOGEE DR17 ID 2M01234567+1234567", "APOGEE DR17 2M01234567+1234567"),
        ("SDSS DR10 name J012345.00-123456.0", "SDSS DR10 J012345.00-123456.0"),
        ("LAMOST designation J012345.00+123456.0", "LAMOST J012345.00+123456.0"),
    ],
)
def test_only_explicit_catalog_components_are_formatted(raw, expected):
    assert normalize_catalog_identifier(raw) == expected
    assert normalize_catalog_identifier(expected) == expected


@pytest.mark.parametrize(
    "value",
    [
        "0123456789012345678",
        "ID 0123456789012345678",
        "Gaia ID 123",
        "Gaia DR2 ID ABC",
        "gaia ID 0123456789012345678",
        "Gaia-DR2-0123456789012345678",
        "LAMOST-HVS5",
        "LAMOST HVS-5",
        "ID Star",
        "Star name",
        "APOGEE ID variable",
        "Other Survey ID 0123456789012345678",
        "SDSS ID j012345.00+123456.0",
        "SDSS ID J012345.00+123456.0-A",
        "J012345.00−123456.0",
    ],
)
def test_unknown_forms_and_author_names_remain_verbatim(value):
    assert normalize_catalog_identifier(value) == value

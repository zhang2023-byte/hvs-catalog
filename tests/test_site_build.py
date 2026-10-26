"""Portable, reproducible website output from a fabricated catalog."""

import csv
import json
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest
from build_site import build
from test_site_views import source, star, value


def synthetic_catalog(root):
    (root / "stars").mkdir(parents=True)
    (root / "papers").mkdir()
    (root / "provenance").mkdir()
    entries = []
    samples = [star() for _ in range(4)]
    for i, sample in enumerate(samples):
        sample["display_name"] = [
            "Example",
            "Only bound",
            "Missing",
            "Fast <img src=x onerror=alert(1)>",
        ][i]
        sample["catalog_id"] = f"hvs-catalog-{[10, 2, 100, 1][i]:06d}"
        sample["provenance"] = {"path": f"provenance/{i}.json"}
        sample["identifiers"] = (
            [{"value": "Gaia EDR3 1234567890123456789", "source": "2001.00001v1"}]
            if i == 0
            else []
        )
        values = [
            value("30", error="2"),
            value("40", source="2101.00001v1"),
            value("10", attribution="cited"),
            value("50"),
        ]
        if i == 1:
            values[i]["standard"] = {
                "lower_bound": "100",
                "unit": "km/s",
                "approximate": False,
            }
        if i == 0:
            sample["sources"]["2101.00001v1"] = source("B. Newer")
            sample["sources"]["2201.00001v1"] = source("C. Quoted")
            sample["identifiers"].extend(
                {"value": f"Alias {letter}", "source": "2001.00001v1"}
                for letter in "ABCD"
            )
            sample["quantities"][0]["values"].append(
                value("7", source="2201.00001v1", attribution="cited")
            )
            sample["quantities"] += [
                {
                    "quantity": "radial_velocity",
                    "values": [value("9", attribution="mismatch")],
                }
            ]
        if i == 1:
            sample["sources"]["2101.00001v1"] = source("B. Newer")
        if i == 2:
            sample["quantities"][0]["values"][0]["attribution"] = "cited"
        sample["quantities"] += [
            {"quantity": "galactic_rest_frame_velocity", "values": [values[i]]}
        ]
        if i == 0:
            sample["quantities"][-1]["values"].extend(
                [value("20"), value("80", attribution="cited")]
            )
            sample["quantities"][-1]["values"][0]["evidence"].append(
                {"location": "T:p12-b3", "quote": "second passage on the same page"}
            )
        for measurement in sample["quantities"][-1]["values"]:
            measurement["unit"] = "km/s"
            measurement["standard"]["unit"] = "km/s"
        (root / f"stars/{i}.json").write_text(json.dumps(sample))
        (root / f"provenance/{i}.json").write_text(json.dumps({"associations": []}))
        entries.append(
            {
                "display_name": sample["display_name"],
                "file": {"path": f"stars/{i}.json"},
            }
        )
    (root / "stars/index.json").write_text(json.dumps({"stars": entries}))
    (root / "papers/index.json").write_text(
        json.dumps(
            {
                "papers": {
                    "2001.00001v1": {},
                    "2101.00001v1": {},
                    "2201.00001v1": {},
                    "2301.00001v1": {},
                }
            }
        )
    )
    (root / "manifest.json").write_text(
        json.dumps(
            {
                "data_version": "synthetic",
                "stars": {"path": "stars/index.json"},
                "papers": {"path": "papers/index.json"},
            }
        )
    )
    return root


def snapshot(root):
    return {
        p.relative_to(root).as_posix(): p.read_bytes()
        for p in root.rglob("*")
        if p.is_file()
    }


def test_build_is_reproducible_read_only_and_downloads_are_original(tmp_path):
    catalog = synthetic_catalog(tmp_path / "catalog")
    before = snapshot(catalog)
    first, second = tmp_path / "first", tmp_path / "second"
    result = build(catalog, first, "https://github.com/a/b", ["Example"])
    build(catalog, second, "https://github.com/a/b", ["Example"])
    assert snapshot(first) == snapshot(second)
    assert snapshot(catalog) == before
    assert result["contributing_papers"] == 3
    assert result["paper_count"] == 4
    assert result["star_count"] == 4
    assert list((first / "views").glob("*.json")) == [first / "views/example.json"]
    assert (first / "downloads/example.json").read_bytes() == before["stars/0.json"]
    with zipfile.ZipFile(first / "downloads/catalog.zip") as archive:
        assert {
            p.removeprefix("catalog/"): archive.read(p) for p in archive.namelist()
        } == before
        assert all(i.date_time == (1980, 1, 1, 0, 0, 0) for i in archive.infolist())
    with (first / "downloads/stars.csv").open() as stream:
        rows = list(csv.DictReader(stream))
    assert rows[0]["v_grf_km_s"] == "30"
    assert next(iter(rows[0])) == "catalog_id"
    assert rows[0]["catalog_id"] == "hvs-catalog-000010"
    assert rows[0]["v_grf_km_s_error"] == "2"
    assert rows[2]["v_grf_km_s"] == ""
    assert rows[0]["gaia_dr3_id"] == "1234567890123456789"
    assert result["rows"][0]["catalog_number"] == 10
    assert (
        json.loads((first / "views/example.json").read_text())["catalog_id"]
        == "hvs-catalog-000010"
    )
    assert "T:p" not in (first / "views/example.json").read_text()
    about = json.loads((first / "about.json").read_text())
    scope = about["sections"][0]["paragraphs"][1]
    assert scope.startswith("Data version synthetic lists 4 objects.")
    assert "Of 4 papers read, 3 report at least one qualifying star." in scope
    downloads = next(s for s in about["sections"] if s["title"] == "Downloads")
    assert 'dtype={"gaia_dr3_id": "string"}' in downloads["code"]
    assert downloads["links"][-1]["url"] == "https://github.com/a/b#using-the-catalog"
    assert about["sections"][-1]["links"][0]["url"] == (
        "https://github.com/a/b/blob/HEAD/catalog-review/KNOWN_ISSUES.md"
    )


def test_build_cli_and_rejection_do_not_overwrite_inputs(tmp_path):
    catalog = synthetic_catalog(tmp_path / "catalog")
    output = tmp_path / "site"
    command = [
        sys.executable,
        str(Path(__file__).resolve().parents[1] / "tools/build_site.py"),
        "--catalog",
        str(catalog),
        "--output",
        str(output),
        "--repository",
        "https://github.com/a/b",
    ]
    result = subprocess.run(command, capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["detail_pages"] == 4
    index = json.loads((output / "index.json").read_text())
    assert not index["preview"]
    assert all(row["available"] for row in index["rows"])
    for row, entry in zip(
        index["rows"], json.loads((catalog / "stars/index.json").read_text())["stars"]
    ):
        view = json.loads((output / "views" / f"{row['slug']}.json").read_text())
        assert view["name"] == row["name"]
        assert (output / view["download"]).read_bytes() == (
            catalog / entry["file"]["path"]
        ).read_bytes()
    with pytest.raises(ValueError, match="already exists"):
        build(catalog, output, "https://github.com/a/b")
    with pytest.raises(ValueError, match="separate"):
        build(catalog, catalog / "site", "https://github.com/a/b")
    with pytest.raises(ValueError, match="unknown"):
        build(catalog, tmp_path / "new", "https://github.com/a/b", ["Absent"])
    with pytest.raises(ValueError, match="HTTPS"):
        build(catalog, tmp_path / "new", "javascript:alert(1)")

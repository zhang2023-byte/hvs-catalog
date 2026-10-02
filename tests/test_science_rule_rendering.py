"""Check English rule generation, stale-output detection, and source protection."""

import re
from pathlib import Path

from render_science_rules import SOURCE, main, read_source, render


def arguments(tmp_path: Path, source: Path = SOURCE) -> list[str]:
    return [
        "--source",
        str(source),
        "--output",
        str(tmp_path / "extractor.md"),
    ]


def test_extractor_content_excludes_maintenance_metadata():
    data = read_source(SOURCE)
    english = render(data)
    assert not re.search(r"[\u3400-\u9fff]", english)
    assert "High-velocity star literature extraction rules" in english
    assert data["review_note"] not in english
    assert data["legacy"]["revision"] not in english
    for section in ("rules", "examples"):
        for row in data[section]:
            assert row["id"] in english


def test_check_detects_stale_output_without_writing(tmp_path):
    args = arguments(tmp_path)
    assert main(args) == 0
    assert main(args + ["--check"]) == 0
    stale = tmp_path / "extractor.md"
    stale.write_text("old content", encoding="utf-8")
    before = {p.name: p.read_bytes() for p in tmp_path.iterdir()}
    assert main(args + ["--check"]) == 1
    assert before == {p.name: p.read_bytes() for p in tmp_path.iterdir()}
    assert main(args) == 0
    assert main(args + ["--check"]) == 0


def test_invalid_source_does_not_overwrite_output(tmp_path):
    source = tmp_path / "invalid.toml"
    original = SOURCE.read_text(encoding="utf-8")
    line = next(line for line in original.splitlines() if line.startswith("title ="))
    source.write_text(original.replace(line + "\n", "", 1), encoding="utf-8")
    (tmp_path / "extractor.md").write_text("keep", encoding="utf-8")
    assert main(arguments(tmp_path, source)) == 2
    assert (tmp_path / "extractor.md").read_text() == "keep"


def test_output_cannot_overwrite_source(tmp_path):
    source = tmp_path / "rules.toml"
    source.write_bytes(SOURCE.read_bytes())
    args = arguments(tmp_path, source)
    args[-1] = str(source)
    assert main(args) == 2
    assert source.read_bytes() == SOURCE.read_bytes()
    assert not (tmp_path / "extractor.md").exists()

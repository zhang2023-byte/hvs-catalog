"""Generate a portable static site from an approved catalog, without modifying it."""

from __future__ import annotations

import argparse
import csv
import json
import shutil
import zipfile
from pathlib import Path
from urllib.parse import urlparse

from sitegen.about import about_view
from sitegen.views import HOME_KEYS, QUANTITIES, slugs, star_view


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path, data):
    path.write_text(
        json.dumps(data, ensure_ascii=False, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )


def build(catalog, output, repository, preview_names=None):
    catalog, output = Path(catalog).resolve(), Path(output).resolve()
    if output == catalog or output in catalog.parents or catalog in output.parents:
        raise ValueError("Output must be separate from the read-only catalog")
    parsed = urlparse(repository)
    if parsed.scheme != "https" or not parsed.netloc or not parsed.path.strip("/"):
        raise ValueError("Repository must be an HTTPS repository URL")
    if output.exists():
        raise ValueError("Output already exists; choose a new output directory")
    manifest = read_json(catalog / "manifest.json")
    entries = read_json(catalog / manifest["stars"]["path"])["stars"]
    papers = read_json(catalog / manifest["papers"]["path"])["papers"]
    stars = [read_json(catalog / e["file"]["path"]) for e in entries]
    if preview_names and set(preview_names) - {s["display_name"] for s in stars}:
        raise ValueError("Preview includes an unknown display name")
    output.mkdir(parents=True)
    (output / "views").mkdir()
    (output / "downloads").mkdir()
    rows, csv_rows, previews = [], [], []
    for star, slug, entry in zip(stars, slugs(stars), entries, strict=True):
        provenance = read_json(catalog / star["provenance"]["path"])
        view, row, csv_row = star_view(star, provenance, slug, repository)
        selected = preview_names is None or star["display_name"] in preview_names
        row["available"] = selected
        if selected:
            write_json(output / "views" / f"{slug}.json", view)
            shutil.copyfile(catalog / entry["file"]["path"], output / view["download"])
            previews.append({"name": row["name"], "slug": slug})
        rows.append(row)
        csv_rows.append(csv_row)
    columns = [
        {"key": k, "label": label, "unit": unit}
        for k, label, unit, _ in QUANTITIES
        if k in HOME_KEYS
    ]
    index = {
        "data_version": manifest["data_version"],
        "paper_count": len(papers),
        "contributing_papers": len({p for s in stars for p in s["sources"]}),
        "star_count": len(stars),
        "columns": columns,
        "rows": rows,
        "preview": previews if preview_names else [],
        "repository": repository,
    }
    write_json(output / "index.json", index)
    write_json(output / "about.json", about_view(index))
    with (output / "downloads/stars.csv").open(
        "w", newline="", encoding="utf-8"
    ) as stream:
        writer = csv.DictWriter(
            stream, fieldnames=list(csv_rows[0]) if csv_rows else ["name"]
        )
        writer.writeheader()
        writer.writerows(csv_rows)
    with zipfile.ZipFile(
        output / "downloads/catalog.zip", "w", compression=zipfile.ZIP_DEFLATED
    ) as archive:
        for path in sorted(catalog.rglob("*")):
            if path.is_file():
                info = zipfile.ZipInfo(
                    "catalog/" + path.relative_to(catalog).as_posix(),
                    (1980, 1, 1, 0, 0, 0),
                )
                info.compress_type = zipfile.ZIP_DEFLATED
                info.external_attr = 0o100644 << 16
                archive.writestr(info, path.read_bytes())
    for asset in (Path(__file__).parent / "sitegen/assets").iterdir():
        shutil.copyfile(asset, output / asset.name)
    return index


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalog", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--repository", required=True)
    parser.add_argument(
        "--preview-star",
        action="append",
        help="Generate only these detail pages; repeat per display name",
    )
    args = parser.parse_args()
    index = build(args.catalog, args.output, args.repository, args.preview_star)
    print(
        json.dumps(
            {
                "data_version": index["data_version"],
                "stars": index["star_count"],
                "papers": index["contributing_papers"],
                "detail_pages": len(index["preview"]) or index["star_count"],
            }
        )
    )


if __name__ == "__main__":
    main()

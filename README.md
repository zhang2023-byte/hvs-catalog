# hvs-catalog

A catalog of published measurements of hypervelocity star candidates, extracted from the
literature with the quoted text and PDF page behind every value.

This repository holds the catalog, the review inputs it is built from, the extraction and
build software, and the generator for the static website. The website lets you search the
catalog, read each star's values by paper and open the source page; its About page explains
what the data are and how to read them. Visit the
[website](https://zhang2023-byte.github.io/hvs-catalog/); see
[Maintenance](docs/maintenance.md#website) to build it locally.

The current data version is `2026-10-25-release-review`: 666 objects from 163 of 413 papers
read. Exact counts are in [manifest.json](catalog/manifest.json); known problems are in
[KNOWN_ISSUES](catalog-review/KNOWN_ISSUES.md).

## Repository

| Path | Contents |
|---|---|
| [catalog/](catalog/manifest.json) | The released catalog: one JSON file per star, paper index and provenance. |
| [catalog-review/](catalog-review) | Approved review inputs (identity, attribution, corrections) and known issues. |
| [src/hvs_catalog/](src/hvs_catalog) | Literature acquisition, PDF parsing, extraction, scoring and catalog build. |
| [tools/](tools) | Website generator, contract renderers and distribution checks. |
| [contracts/generated/](contracts/generated/README.md) | Generated formats, schemas, prompts and examples. |
| [docs/](docs) | Maintenance rules, pipeline usage and the science rules. |

## Using the catalog

The website offers `catalog.zip`, which unpacks to the same `catalog/` directory, and
`stars.csv` with one row per star. The CSV is described on the website's About page. The
JSON keeps every reported value with its source and evidence; it stores no website defaults.

Read one star and its radial velocities with the Python standard library:

```python
import json
from pathlib import Path

root = Path("catalog")  # Repository catalog or the extracted ZIP directory.
manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
entries = json.loads((root / manifest["stars"]["path"]).read_text(encoding="utf-8"))[
    "stars"
]
entry = next(item for item in entries if item["display_name"] == "US 708")
star = json.loads((root / entry["file"]["path"]).read_text(encoding="utf-8"))
print(manifest["data_version"], star["display_name"])
for quantity in star["quantities"]:
    if quantity["quantity"] == "radial_velocity":
        for value in quantity["values"]:
            print(value["source"], value["attribution"], value.get("standard"))
```

| Field | Meaning |
|---|---|
| `sources` | Paper metadata keyed by the exact versioned arXiv identifier. |
| `quantities[].values` | Reported measurements, conditions and evidence. |
| `attribution` | `own`: measured or derived by this paper, or a direct survey measurement; `cited`: another paper's result; `mismatch`: not this quantity's definition. |
| `standard` | Optional standard-unit derivation; decimal strings keep the reported precision. |
| `evidence`, `correction` | Original extraction evidence and, when present, the reviewed fixed-PDF correction. |

All fields are defined in the [catalog format](contracts/generated/catalog-format.md).

## Installation

Requires Python 3.11 or later and [uv](https://docs.astral.sh/uv/). Install the locked
development environment:

```bash
uv sync --frozen --python 3.12
uv run python -m hvs_catalog --help
```

Recreate the environment after moving the repository; its launch scripts contain absolute
paths. To install the program on its own, build a wheel and run it outside the repository
without `PYTHONPATH`:

```bash
uv build
uv venv /path/to/env --python 3.12
uv pip install --python /path/to/env/bin/python dist/hvs_catalog-*.whl
/path/to/env/bin/hvs-catalog --help
```

The wheel contains only the program; the catalog is distributed separately.

## Documentation

- [Maintenance](docs/maintenance.md): roles, data versions, website release, verification and Git.
- [Pipeline](docs/pipeline.md): literature acquisition, parsing, extraction, benchmarks and scoring.
- [Science rules](docs/science-rules.md): the approved extraction rules, generated from source.
- [Generated contracts](contracts/generated/README.md): record formats, schemas and prompts.
- [AGENTS](AGENTS.md): entry point for coding agents.

Catalog citation and license are pending.

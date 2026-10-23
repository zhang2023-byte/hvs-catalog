"""Generate the current scorer's interface contracts and fictional examples."""

import json
from pathlib import Path

from .bundle import ScoringBundle
from .generate import example_annotation
from .saved import SavedScoringInput
from .scoring import GoldReference, ScoringReport


def build_scoring_outputs():
    models = {
        "reference": GoldReference,
        "manifest": SavedScoringInput,
        "inputs": ScoringBundle,
        "report": ScoringReport,
    }
    outputs = {
        f"schemas/{name}.json": json.dumps(
            model.model_json_schema(), ensure_ascii=False, indent=2
        )
        + "\n"
        for name, model in models.items()
    }
    outputs["examples/annotation.json"] = (
        example_annotation().model_dump_json(indent=2) + "\n"
    )
    outputs["examples/manifest.json"] = (
        json.dumps(
            {
                "format": "hvs.scoring-manifest",
                "version": 1,
                "scope": "full",
                "papers": [
                    {
                        "arxiv_id": "2601.99999",
                        "reference": "annotation.json",
                        "workspace": "synthetic-workspace",
                        "execution_id": "synthetic-execution",
                    }
                ],
            },
            indent=2,
        )
        + "\n"
    )
    outputs[
        "README.md"
    ] = """<!-- Generated from hvs_catalog.evaluation.generate_scoring. -->
# Scoring contract

The scorer reads reviewed references and saved extraction results. It does not call a model,
edit a reference answer, or resume extraction. Gold authoring and scientific reannotation
remain a separate workflow. All examples here are fictional.

## Inputs

- [Reference schema](schemas/reference.json): the same single-file Gold annotation used
  in `data/<paper>/annotation.json`, with `metadata`, `stars` and `notes`.
  Its metadata binds the paper version, science rules and one original PDF hash.
  Evidence uses only one-based PDF `page` and visible `location`. No reference export
  or separate approval file is required. Only human-confirmed complete answers belong
  in formal Gold; the scorer cannot infer approval from arbitrary JSON.
- [Manifest schema](schemas/manifest.json): fix the complete paper list before comparison.
  Paths are relative to the manifest. Each paper references its annotation.json directly. Provide both
  `workspace` and `execution_id`, or omit both to count an absent prediction.
  Full scoring defaults to all eleven current quantities. A restricted quantity list
  must be fixed in the manifest. Roster scoring uses `scope: "roster"` and `quantities: []`.
  It produces no L2 score. This version requires complete reference coverage for the
  declared paper/quantity scope; it has no per-object or condition-based exclusions.
- [Frozen input schema](schemas/inputs.json): contains the plan, reference answers,
  retained predictions, exact provenance and content digest. Replay needs neither the
  source workspace nor the Gold repository. It checks program, scheme and rule versions.
- [Report schema](schemas/report.json): counts, rates, identifiers, contribution types,
  per-quantity metrics and record-level pairings. JSON paths refer to each annotation and `content` in each prediction. Original conditions, evidence and notes
  remain in the frozen inputs.

## Commands

```sh
hvs-catalog score run --manifest /path/to/manifest.json --output /path/to/new-report
hvs-catalog score replay --inputs /path/to/new-report/scoring-inputs.json
```

`run` creates `scoring-inputs.json`, `scoring-report.json`, `papers.csv`, `quantities.csv`
and `differences.csv`. `replay --output` optionally writes a new report directory.
Existing output directories and files are not overwritten. The
[example manifest](examples/manifest.json) and [example reference](examples/annotation.json)
contain placeholder execution and material references; they are not a runnable experiment.

## Rules

L0 reports delivered/planned, valid/delivered, valid/planned and completion counts.
For full scoring, delivery means a committed scientific file. For roster scoring,
delivery means a valid saved roster, including a valid empty roster. A validated partial
result can have a valid format. Completion does not certify scientific correctness.
Without a final science file, retained stage snapshots still contribute to L1/L2.
An existing published scientific generation is evaluated at its recorded stage revisions.
No raw response is repaired, merged or rescued by the scorer. Corrupt archives, wrong
bindings and incompatible versions stop scoring explicitly.

L1 matches each star through paper-local identifiers and explicit aliases. Ambiguous
identity is unmatched; no coordinates, value similarity, row order or external lookup
resolve it. Duplicate stars count as extras. Identifier omissions/extras and contribution
type accuracy are reported on matched stars separately from L1.

L2 counts maximum complete one-to-one numerical matches within the same paper, star and
quantity. It compares all reported numerical fields and units. A reference value can
be consumed once, including across duplicate star entries. Every in-scope retained
prediction counts in P; every in-scope reference value counts in G. Missing and extra
stars therefore affect numerical recall and precision. Precision=C/P, recall=C/G,
F1=2C/(P+G). Batch rates use summed counts. Zero denominators are null, not perfect scores.
Correct complete empty rosters are reported separately.

Exact strings match. Otherwise only approved limited formatting can establish a match;
all other expressions are mismatches. There is no undecidable or provisional outcome.
Preserve printed precision, trailing zeros, approximation, error form, unit scale, bound
direction and endpoint inclusion. Do not calculate, convert scales or add tolerances.
Existing approved literal numerical qualifiers in `condition` retain their numerical
effect. Other condition wording, statistical meaning, reference-frame interpretation,
evidence support and notes are not semantically scored. Evidence location and exact
text validity are checked against the fixed extraction material, not against Gold's
choice of evidence. Check Gold PDF bytes and page bounds with `gold check` before
publication. Scoring checks the prediction's PDF hash against annotation metadata;
it does not reopen the source PDF. Frozen inputs v4 preserve the complete annotation,
its exact source-file hash and observed Git commit/status when available, plus the
prediction archive and program bindings. Old reference wrappers and older frozen-input
versions are rejected; use their recorded program to replay them. Numerical equality
does not certify overall scientific correctness.
"""
    return outputs


def write_scoring_outputs(output):
    outputs = build_scoring_outputs()
    (Path(output) / "examples/reference.json").unlink(missing_ok=True)
    for name, content in outputs.items():
        path = Path(output) / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    return outputs


def check_scoring_outputs(output):
    output = Path(output)
    expected = build_scoring_outputs()
    problems = [
        f"Outdated scoring contract: {name}"
        for name, content in expected.items()
        if not (output / name).is_file()
        or (output / name).read_text(encoding="utf-8") != content
    ]
    problems.extend(
        f"Unexpected scoring contract: {p.relative_to(output)}"
        for p in output.rglob("*")
        if p.is_file() and p.relative_to(output).as_posix() not in expected
    )
    return problems

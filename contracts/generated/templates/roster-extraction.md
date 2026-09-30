<!-- Generated -->
# Roster extraction filling format (direct method)

Fill this format for the roster extraction step of the `direct` method. All scientific content is in English; object names, values and excerpts keep the paper's original language.

## Provided by the application

Fixed paper and source context (paper key, source package files with identifiers, known material gaps with their identifiers, existing open questions with their identifiers), the roster method, and all record identifiers, numbering, revisions and execution metadata.

## Filled by the extractor

| Filled by the extractor | Provided or assigned by the application |
|---|---|
| Roster material coverage: state, checked file identifiers, known gap identifiers | File identifiers, gap identifiers, question identifiers, record references |
| Entries: printed identifiers with inline evidence, disposition (`included` / `excluded` / `pending`), inclusion or exclusion evidence | Roster entry numbering (`item_id`), contribution numbering |
| For included entries: contribution summary, contribution type (`candidates_found` / `follow_up`), the authors' overall boundness assessment (one of 5 states or `null`), conditional assessments, each with inline evidence | Question records for pending items and undetermined assessments |
| Unresolved printed ranges: original range expression, evidence, already identified members, reason | Group identifiers; no member is invented or expanded |

## Required behavior

- `included` entries require inclusion evidence and full contribution fields; `excluded` entries require exclusion evidence; `pending` entries require a submitted question. Excluded and pending entries never carry contribution fields.
- An assessment with status `null` must submit a question or reference a known material gap; it must not be filled as `no_overall_conclusion`.
- `no_overall_conclusion` is a reading of the paper; the other four states require positive evidence inline.
- A printed range that does not list its members stays an unresolved group; do not expand it and do not report an empty roster as "no objects".
- A known material gap that names an inclusion or identity judgment keeps the roster coverage partial with that gap referenced; only quantity-scoped known gaps can leave roster coverage complete, and an unknown-scope gap always keeps it partial. A complete roster claim never contradicts an open gap.
- Every identifier must appear in exactly one entry; if two printed names may denote the same object, submit a question instead of merging silently.

## Common instructions

> Return only the scientific fields requested for this step. Write new summaries, questions and precedent notes in English; preserve the paper's printed identifiers, values, units, conditions and excerpts in their original form. Use only the supplied paper materials and fixed references. Provide every required list explicitly; an empty list does not establish that the paper reported nothing. Keep material gaps, unresolved scientific questions and the authors' assessment distinct. Supply the evidence required by this step; never invent missing content or silently resolve conflicting results. Use application-supplied identifiers where provided. The application creates persistent record identifiers, references and execution metadata; it does not supply scientific judgments or expert decisions.

The complete field structure is defined by `schemas/submission.roster.json`.

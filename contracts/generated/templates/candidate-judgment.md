<!-- Generated -->
# Candidate judgment filling format (candidate_review method)

Fill this format for the per-candidate judgment step of the `candidate_review` method. The application supplies the fixed candidate list with identifiers; every candidate must receive exactly one judgment. Do not add or remove candidates.

## Provided by the application

The candidate list (item identifiers with printed identifiers and their evidence), fixed paper and source context, known gaps and open questions with identifiers, and all numbering, references and execution metadata.

## Filled by the extractor

| Filled by the extractor | Provided or assigned by the application |
|---|---|
| Per candidate: disposition (`included` / `excluded` / `pending`), judgment evidence | Candidate numbering; judgments must cover exactly the given candidates |
| For included candidates: contribution summary, contribution type, the authors' overall assessment (one of 5 states or `null`), conditional assessments, each with inline evidence | Contribution numbering and question records |
| Questions for pending or undecidable candidates | Question identifiers and references |

## Required behavior

- Missing, extra or duplicated judgments are rejected; an incomplete judgment batch never masquerades as a completed roster.
- `included` requires inclusion evidence and contribution fields; `excluded` requires evidence; `pending` requires a question.
- Groups whose members are not fully listed remain unfinished; the application does not invent members.

## Common instructions

> Return only the scientific fields requested for this step. Write new summaries, questions and precedent notes in English; preserve the paper's printed identifiers, values, units, conditions and excerpts in their original form. Use only the supplied paper materials and fixed references. Provide every required list explicitly; an empty list does not establish that the paper reported nothing. Keep material gaps, unresolved scientific questions and the authors' assessment distinct. Supply the evidence required by this step; never invent missing content or silently resolve conflicting results. Use application-supplied identifiers where provided. The application creates persistent record identifiers, references and execution metadata; it does not supply scientific judgments or expert decisions.

The complete field structure is defined by `schemas/submission.candidate.json`.

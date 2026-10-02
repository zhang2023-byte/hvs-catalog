<!-- Generated -->
# Candidate discovery filling format (candidate_review method)

Return coverage, a provisional candidates list with printed identifiers and inline source evidence, unresolved_groups and identity questions. Reuse the shared source, coverage, question and group fields. The application assigns candidate identifiers after discovery. Do not fill eligibility decisions or contribution judgments in this inventory.

## Required behavior

- Preserve every identifiable potential candidate and its paper evidence; discovery is not a final inclusion decision.
- Preserve original identifiers and unresolved ranges. Do not invent missing members or silently merge ambiguous identities.
- Report missing or unreadable material in coverage and keep affected parts incomplete.
- Return every required list, including empty lists. An empty inventory alone does not establish that the paper contains no eligible objects.

## Common instructions

> Return only the scientific fields requested for this step. Write new summaries, questions and precedent notes in English; preserve the paper's printed identifiers, values, units, conditions and excerpts in their original form. Use only the supplied paper materials and fixed references. Provide every required list explicitly; an empty list does not establish that the paper reported nothing. Keep material gaps, unresolved scientific questions and the authors' assessment distinct. Supply the evidence required by this step; never invent missing content or silently resolve conflicting results. Use application-supplied identifiers where provided. The application creates persistent record identifiers, references and execution metadata; it does not supply scientific judgments or expert decisions.

The complete field structure is defined by `schemas/submission.discovery.json`.

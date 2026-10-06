<!-- Generated -->
# Quantity extraction filling format

Fill this format for the quantity extraction step, for each application-supplied object of the paper.

## Provided by the application

Fixed paper and source context, the object identifiers of the current roster, the requested quantity scope, known material gaps with identifiers and affected targets, existing open questions with identifiers, approved precedent snapshots, and all record identifiers, value numbering, revisions, call states and usage.

## Filled by the extractor

| Filled by the extractor | Provided or assigned by the application |
|---|---|
| For the fixed object: a flat measurements list, each with its quantity and values in their printed form (value, errors, unit, condition, frame, source), citations using supplied location identifiers and verbatim quotes | Value, conflict, question, evidence and record identifiers |
| Numeric conflicts: both alternatives in full with evidence and the unresolved reason | A `numeric_conflict` question bound to the object and quantity |
| Coverage declaration: common state, plus quantity and state for each exception. Do not fill technical file, gap or question IDs. | Fixed supplied file binding, material gaps and inline scientific question references; expansion to all 18 requested quantities |
| Other scientific questions inline: affected quantity, category, content, evidence | Question records; the application never answers them |
| Precedent use: the given precedent identifier and a short applicability note | Precedent snapshot references and version checks |

## Requested quantity scope

The requested quantities are exactly the approved 18: `observed_phase_space.ra`, `observed_phase_space.dec`, `observed_phase_space.distance`, `observed_phase_space.parallax`, `observed_phase_space.proper_motion_ra`, `observed_phase_space.proper_motion_dec`, `observed_phase_space.radial_velocity`, `derived_kinematics.galactocentric_x`, `derived_kinematics.galactocentric_y`, `derived_kinematics.galactocentric_z`, `derived_kinematics.galactocentric_radius`, `derived_kinematics.galactocentric_vx`, `derived_kinematics.galactocentric_vy`, `derived_kinematics.galactocentric_vz`, `derived_kinematics.tangential_velocity`, `derived_kinematics.galactic_rest_frame_velocity`, `bound_assessment.bound_probability`, `bound_assessment.unbound_probability`.

## Required behavior

- Exactly one application-supplied object is processed per call. Provide top-level `measurements`, `quantity_conflicts`, and `questions` lists even when empty, plus a `coverage` declaration object. Each measurements entry contains `quantity` and one complete numeric expression directly (e.g. value, error, condition, source, evidence). Repeat entries for distinct results. Do not submit objects, object_id, quantity groups, or nested values lists; the application binds and groups them. An empty list never means the paper reported nothing.
- Omit a measurement entirely when there is no result; never create an empty numeric expression.
- A one-sided lower or upper limit uses value for its printed operand; range_lower and range_upper describe a two-sided range. Preserve the explicit inequality and its printed number.
- Provide optional numeric fields (interval endpoints, inclusiveness, interval kind, coverage, quantiles, error components, frame, epoch, coordinate format, representation, purpose, source note, author usage) exactly when the paper reports them.
- The coverage declaration must be explicit. Declare the common state for all requested quantities and give a complete replacement entry for every exception, e.g. "all checked in the main text; radial velocity exceptional, the supplement is not yet processed". Declaring all quantities not checked is also a valid explicit declaration. Use {common: {state: ...}, exceptions: [{quantity: ..., state: ...}]}. The scope is all requested quantities. The application assigns checked files from the supplied PDF whenever the state indicates checking, and binds known material gaps and inline questions using their affected quantities. Do not supply scope, checked_file_ids, gap_ids or issue_ids in coverage. Declarations contradicting known material gaps are rejected, and gaps with unknown impact scope block complete declarations everywhere.
- Cite evidence with location and quote only for each measurement and nested numerical structure. Text quotes must match the fixed parser text. Literal numeric containment is not checked; this does not certify that the cited passage supports the filled number. The application assigns component names and evidence references; do not submit parts, file identifiers, locators or context flags. Include condition and source support in the cited passages. Preserve math notation and signed strings exactly as printed; do not rewrite them into decimal approximations.
- Conflicting printings of one result are reported as a conflict with at least two distinct complete alternatives; the alternatives are not simultaneously reported as confirmed values. Other confirmed results of the paper continue unaffected when the conflict affects neither inclusion nor identity.
- All measurements, conflicts and scientific questions belong to the single supplied target. The application assigns its identifier; do not repeat object_id in the submission or questions.

## Common instructions

> Return only the scientific fields requested for this step. Write new summaries, questions and precedent notes in English; preserve the paper's printed identifiers, values, units, conditions and excerpts in their original form. Use only the supplied paper materials and fixed references. Provide every required list explicitly; an empty list does not establish that the paper reported nothing. Keep material gaps, unresolved scientific questions and the authors' assessment distinct. Supply the evidence required by this step; never invent missing content or silently resolve conflicting results. Use application-supplied identifiers where provided. The application creates persistent record identifiers, references and execution metadata; it does not supply scientific judgments or expert decisions.

The complete field structure is defined by `schemas/submission.quantity.json`.

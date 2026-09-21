<!-- Generated -->
# Quantity extraction filling format

Fill this format for the quantity extraction step, for each application-supplied object of the paper.

## Provided by the application

Fixed paper and source context, the object identifiers of the current roster, the requested quantity scope, known material gaps with identifiers and affected targets, existing open questions with identifiers, approved precedent snapshots, and all record identifiers, value numbering, revisions, call states and costs.

## Filled by the extractor

| Filled by the extractor | Provided or assigned by the application |
|---|---|
| Per object: reported quantity groups with values in their printed form (value, errors, unit, condition, frame, source), inline evidence grouped by source location | Value, conflict, question, evidence and record identifiers |
| Numeric conflicts: both alternatives in full with evidence and the unresolved reason | A `numeric_conflict` question bound to the object and quantity |
| Coverage declaration: `scope: all_requested`, common state, checked files, known gap and question references, plus a complete replacement for each exceptional quantity | Expansion to one completion entry for each of the 18 requested quantities |
| Other scientific questions inline: affected object and quantity, category, content, evidence | Question records; the application never answers them |
| Precedent use: the given precedent identifier and a short applicability note | Precedent snapshot references and version checks |

## Requested quantity scope

The requested quantities are exactly the approved 18: `observed_phase_space.ra`, `observed_phase_space.dec`, `observed_phase_space.distance`, `observed_phase_space.parallax`, `observed_phase_space.proper_motion_ra`, `observed_phase_space.proper_motion_dec`, `observed_phase_space.radial_velocity`, `derived_kinematics.galactocentric_x`, `derived_kinematics.galactocentric_y`, `derived_kinematics.galactocentric_z`, `derived_kinematics.galactocentric_radius`, `derived_kinematics.galactocentric_vx`, `derived_kinematics.galactocentric_vy`, `derived_kinematics.galactocentric_vz`, `derived_kinematics.tangential_velocity`, `derived_kinematics.galactic_rest_frame_velocity`, `bound_assessment.bound_probability`, `bound_assessment.unbound_probability`.

## Required behavior

- Required lists (`objects`, per object `quantities`, `quantity_conflicts`, `coverage`, `questions`) must be present even when empty; an empty list never means the paper reported nothing.
- Omit a numeric group entirely when there is no result; never create a group with an empty `values` list.
- Provide optional numeric fields (interval endpoints, inclusiveness, interval kind, coverage, quantiles, error components, frame, epoch, coordinate format, representation, purpose, source note, author usage) exactly when the paper reports them.
- The coverage declaration must be explicit. Declare the common state for all requested quantities and give a complete replacement entry for every exception, e.g. "all checked in the main text; radial velocity exceptional, the supplement is not yet processed". Declaring all quantities not checked is also a valid explicit declaration. Declarations contradicting known material gaps are rejected, and gaps with unknown impact scope block complete declarations everywhere.
- Cite every filled numeric component (value, each error side, both endpoints, coverage, quantiles) with its printed fragment; group components that share one source location into a single inline evidence item and mark it as context evidence when it also supports the condition or source.
- Conflicting printings of one result are reported as a conflict with at least two distinct complete alternatives; the alternatives are not simultaneously reported as confirmed values. Other confirmed results of the paper continue unaffected when the conflict affects neither inclusion nor identity.
- Use only application-supplied object identifiers; unknown or missing objects are rejected.

## Common instructions

> Return the scientific fields requested for this step. Preserve the paper's printed identifiers, values, units, conditions and evidence. Use only the supplied paper and source references. Provide every required list explicitly; an empty list does not establish that the paper reported nothing. For each object, explicitly declare common coverage for all requested quantities and provide a complete replacement entry for each exception. Missing coverage is invalid. Keep material gaps, unresolved scientific questions and the authors' assessment distinct. Include optional numeric fields when applicable; never omit a reported component or invent a missing one. Give evidence inline, grouping numeric components supported by the same source location. Report conflicting printings together with their full values, evidence and the reason they remain unresolved. Give other questions inline with their affected targets. Use application-supplied object identifiers. The application creates record identifiers, references and execution metadata; it does not supply scientific judgments or expert decisions.

The complete field structure is defined by `schemas/submission.quantity.json`.

<!-- Generated -->
# High-velocity star literature extraction rules

## Main rules in order

Follow the sequence below. Consult the explanations and examples when needed.

1. **Use only the paper materials provided for this task (S01)**: Extract only from the paper materials provided for this task. Preserve the objects studied, the authors' claims, the reported values, their applicable conditions, and their sources as stated in the paper.

2. **Identify the objects and the work done in this paper (S02)**: Determine which object each record in the paper refers to. Record its paper-supported names and catalog or survey identifiers, including their explicitly supplied qualifiers and sources as specified below. Briefly describe in free text what this paper does with the object, with supporting evidence from the paper.

3. **Check the inclusion evidence and contribution type (S03)**: Include two kinds of research: Galactic unbound candidates found by this paper's selection or analysis and still accepted as candidates by its authors at the end of the study; and observations, analyses, or other substantive follow-up of previously identified unbound candidates. Both require evidence in this paper. Determine the contribution type from why the authors selected the object for study.

4. **Record the authors' overall boundness assessment (S04)**: Use one of the five states below to record the authors' overall assessment of the object's boundness. Record a clear conclusion or preference directly. Use no_overall_conclusion when the paper gives no overall assessment. If the authors' meaning is ambiguous, apply S07. Retain the original conclusions under each set of conditions.

5. **Collect all reported values within the quantity scope (S05)**: For each eligible object, extract all values reported or adopted by this paper for the 18 quantities below, with their conditions. Include cited historical values and values obtained in this paper using different models, changing input data within the same model, or correcting measurements. Preserve the original units, precision, uncertainties, and numerical representations. Do not derive additional values or choose a best value.

6. **Check object assignments, duplicates, and conflicting values (S06)**: Check which object and physical quantity each value describes and under which conditions it applies. Preserve direct evidence from the paper. Merge repeated occurrences of the same result and retain different valid results separately. Isolate unexplained conflicting values and clear source errors as diagnostics rather than confirmed measurements.

7. **Interpret ambiguities and report completeness (S07)**: Return every identifiable eligible object and valid reported value. Resolve interpretation ambiguities using the supplied paper and these rules, briefly recording the evidence for the chosen interpretation. Distinguish source-error and numerical-conflict diagnostics from missing materials, technical failures and genuinely unreported quantities.

## Explanations to consult as needed

### S01 Use only the paper materials provided for this task

The materials for this task consist of a fixed PDF version of one paper and that paper's supplementary files explicitly listed and supplied for this task. They include the main text, appendices, tables, captions, notes, and supplied supplementary data. A cited paper, a different version, or an external resource not included in the supplied materials is not evidence for this extraction. LaTeX source files are not extraction evidence, including when they have been downloaded for archival use.

Parsed text, Markdown, and reconstructed tables are reading aids derived from the supplied originals, not additional scientific sources. Read table values together with the original captions, column headings, notes, and surrounding text. If a reading aid differs from the corresponding original PDF or supplied supplementary file, check the original and report the discrepancy; preserve what the original actually states. Do not treat a parsing error as an author result or as a contradiction within the paper. Retain evidence that can be located in the original PDF or supplementary file.

Apply only the formatting operations specified in these rules. Do not add information that the supplied materials do not report. Keep evidence excerpts as printed and cite separate locations separately. Do not present an identifier assembled from multiple locations as one continuous quotation from the paper.

If material is missing, unreadable, or cannot be reliably interpreted because of a parsing failure, state the affected material and content. Continue extracting what can be established; leave any part that depends on unavailable evidence incomplete. An external link does not establish that its contents have been supplied or processed.

### S02 Identify the objects and the work done in this paper

For each catalog or survey identifier, record the catalog or survey name, the release when established, and the unchanged identifier body, in that order. Separate these components with one space. Omit unknown components and do not repeat components already present.

Establish qualifiers from a directly applicable heading, caption, note, or explicit statement in the paper. A statement can apply to one identifier or to a defined group of catalog entries. For a group statement, the paper must explicitly identify the catalog and release from which the entries were selected, and the corresponding identifier column or list must be unambiguous. Do not transfer qualifiers between catalogs in a combined or cross-matched sample. A release stated only as the source of measurements, including a cross-match used only to obtain measurements, does not establish the release of the identifier.

Apply this format whether the qualifiers are printed with the identifier body or supplied elsewhere. Omit ID, Source ID, source_id, identifier, designation, and name when they serve only as field labels. Do not remove these strings when they are part of the identifier body or an object name. Preserve the identifier body, including every digit, leading zeros, letter case, and actual separators. Preserve author-assigned object names as printed.

Keep established qualifiers when other qualifiers are unknown. Do not invent missing qualifiers or discard the object because they are missing. Digit length, nearby text, the paper's topic, and external knowledge do not establish a qualifier. Retain separate original evidence for the identifier body and any qualifiers supplied elsewhere. Keep evidence excerpts as printed.

After establishing that occurrences refer to the same object, record bare and qualified occurrences of the same catalog, release, and identifier body as one qualified identifier, retaining the evidence from every occurrence. Do not merge identifiers from different catalogs or releases merely because their bodies match. Preserve other object names as printed: record an identical name once and retain every location where it appears; retain distinct names or spellings separately. Group different names under one object only when this paper's materials support the correspondence. Do not assume two names identify the same object merely because they become identical after removing spaces or hyphens, or ignoring case. Even identical strings require checking for different objects with the same name or conflicting object information. Handle unclear or conflicting correspondences according to S07. Do not add external aliases or qualifiers absent from the applicable paper evidence.

Observations, measurements, data processing, reanalysis, modeling, or actual use of an object's data in an analysis can constitute a research contribution by this paper; a new numerical value is not required. A background mention, an unchanged retelling of earlier results, a table or figure displaying cited results, or only an initial data-quality screen is insufficient by itself. If the authors identify a measurement error through analysis and revise their interpretation of the object accordingly, record that research work.

For each contribution, briefly describe in free text the work this paper does with the object and provide evidence from the paper. The summary describes the actual observation, analysis, or reassessment; record the contribution type separately under S03. The summary does not require an additional explanation of why the extractor included the object.

A paper may use a numbered range such as 'stars 1–5' instead of naming every member. When the numbering and object context identify the members, interpret the range and submit a separate object entry for every member, citing the original range expression and its source. For example, 'HVS4–6' identifies HVS4, HVS5, and HVS6. Preserve the prefix and numbering supported by the paper; do not invent names or members not specified by the range. If the members cannot yet be fully listed, state which range remains unresolved. Missing or unreadable material leaves the affected part incomplete; ambiguous wording is handled according to S07. An unresolved membership list does not mean there are no objects.

A name containing digits or hyphens is not by itself a numbered group. The application does not expand object ranges or generate their members. Distinguish a numbered group from an individual name and from a physical numerical interval using the paper context. Do not invent unnamed members or omit explicitly identified members because another part of the group remains unprocessed.

### S03 Check the inclusion evidence and contribution type

Candidates found (candidates_found): the object was found through this paper's selection or analysis, and the authors ultimately still regard it as a Galactic unbound candidate. An independently selected object belongs here even if it was discovered before. Do not include it through this route if the authors' subsequent analysis in this paper explicitly concludes that it is bound, or withdraws or rejects its interpretation as an unbound candidate. A warning that a measurement may be problematic does not necessarily withdraw candidate status.

Follow-up (follow_up): the authors selected and studied the object because it had previously been considered an unbound candidate. This background must be explicit in the current paper. Retain the contribution whether the current paper supports, rejects, or does not reassess its boundness. New observations or reprocessed data do not turn this into candidates_found. Conversely, follow-up spectroscopy of a newly selected object within the same discovery study does not turn it into follow_up of a historical candidate.

The authors must supply the basis for unbound candidacy. This may be an explicit statement that the object is unbound from or escaping the Galaxy; an accepted possibility that it is unbound under specified conditions; or an explicit criterion for being Galactic unbound together with evidence that the object meets it. Labels such as HVS, runaway, high velocity, a proposed origin, or an ejection mechanism are insufficient by themselves. The extractor must not choose a threshold or gravitational potential model, or calculate a probability, to establish eligibility.

Escape from a star cluster or another galaxy does not establish escape from the Milky Way. An event at a future point on an orbit does not by itself establish Galactic boundness at the present or specified reference state.

Assign a group conclusion to individual objects only when it explicitly applies to every member and the members can be identified. If the authors say only some members meet the condition without identifying them, do not assign the conclusion to every member. A table title or membership in a catalog is not sufficient by itself. Do not apply the authors' definition of one class of objects to a different class without support.

### S04 Record the authors' overall boundness assessment

When the authors explicitly summarize results obtained under different conditions, record the conclusion or preference expressed in that summary. Do not count physical models favoring bound or unbound outcomes and use the majority as the authors' overall view. Do not select a probability or gravitational potential model to infer that view. An explicit statement that an object may be or is likely unbound can be recorded as possibly_unbound; discussion of multiple models is not required.

No overall conclusion includes both no discussion of boundness and discussion without an overall conclusion or preference. Record it directly when the paper clearly supports that reading. If the authors' meaning is ambiguous, apply S07. The extractor's own uncertainty is not evidence that the authors give no overall conclusion. Processing status is not a sixth author state. Missing material or a parsing failure must not be recorded as no_overall_conclusion either.

An object explicitly concluded to be bound in this paper is not included as candidates_found. An object meeting the follow_up conditions may have the state bound. Neither possibly_bound nor no_overall_conclusion is sufficient grounds for inclusion by itself; S03 must still be satisfied.

A statement that the object was previously an unbound candidate describes an earlier assessment. Use that earlier conclusion as this paper's overall assessment only if the authors explicitly adopt it here.

### S05 Collect all reported values within the quantity scope

Check the object's relevant columns in all readable tables, as well as results in the main text and appendices. Retain this paper's results, recomputed values, historical values adopted or compared, and results under different models, priors, epochs, or representations. Describe important results outside the 18 quantities in the object's contribution summary. A value mentioned in the background does not by itself qualify an object for the extraction list.

Identify a quantity from its physical meaning, coordinate frame, and epoch or state, not its units alone. For observed quantities, preserve the observation or catalog epoch reported or adopted by the authors. If the authors have propagated a position or other quantity to another epoch, retain the result they give. For Galactocentric positions and velocities, record values at the present state, the initial state of an orbit integration, or the authors' specified reference state. Do not place values at other orbital times or specific events in these fields. See the quantity definitions below.

Preserve units and values as printed. Do not convert distance and parallax, calculate a speed magnitude, derive one probability from another, or calculate uncertainties. Do not average or round values. Preserve fractions, percentages, logarithms, and distance moduli as reported, identifying the representation without converting it into another value.

Retain the reported conditions separately for each quantity. Do not assemble values obtained under different conditions into a single parameter set yourself. If the authors write only the word 'zero', do not turn it into a reported numerical value of 0. If they report only simulation counts, do not convert those counts into probabilities.

Preserve symmetric errors, asymmetric errors, and separately named error components as reported. For an interval without a central value, preserve its endpoints and description without adding a central value. Distinguish ordinary numerical ranges, allowed prior ranges, confidence intervals, credible intervals, and intervals with no stated statistical interpretation according to the authors' wording. Record coverage and quantiles only when reported; do not derive them. Preserve upper limits, lower limits, and prior bounds used to analyze the object with their stated purpose; do not recast them as new measurements.

Use this paper's description to identify whether a value is a result of this paper, an earlier work, or an unclear source. Cited or directly adopted old values remain earlier results. Values recomputed in this paper from old data may be recorded as this paper's results. Preserve the authors' statements about adopting, replacing, or rejecting values without selecting a recommended value.

### S06 Check object assignments, duplicates, and conflicting values

Assign a value given for a group to individual objects only when it explicitly applies to every member and the members can be identified. Such values may be shared research results, numerical limits, or parameters adopted in the analysis. A sample maximum, minimum, typical value, or distribution range is not automatically a value for each object.

Passing a selection or quality check does not make the selection threshold a measurement or upper or lower limit for the object. Record a result only if the authors separately report it for the object or explicitly for every member of the group.

For every recorded number, including uncertainty values and interval endpoints, retain its location in the original PDF or supplied supplementary file and the corresponding excerpt. PDF evidence must identify the source document and page; table evidence must also identify the relevant row and column context. A location in parsed Markdown alone is insufficient. Units, frames, and conditions may be supported by other relevant passages, headings, or notes. Check the object's row in a table; do not take values from a neighboring row. Multiple records may reference the same evidence passage without copying it repeatedly.

The same result may appear more than once, for example in both the text and a table. Merge duplicates after confirming that their source, method, conditions, epoch, and numerical representation are the same. When different expressions are confirmed to be the authors' rounded or approximate restatements of the same result, retain the most complete and precise expression actually printed; do not calculate a replacement. Retain scientific results with different sources, methods, conditions, epochs, or representations separately.

If the values or errors for the same object, result and conditions genuinely differ between locations, first check for an explicit author correction or a difference in conditions. If the discrepancy remains unexplained, retain both complete original expressions, their evidence and the reason in a numerical-conflict diagnostic. Do not submit either alternative as a confirmed measurement. Do not ignore a small discrepancy, automatically prefer the text or table, average the values, or relabel them as results from different models. A plausible interpretation does not erase an unexplained printed discrepancy or make either value an author-corrected result.

Identify a clear author error only when the original paper, an explicit definition, or a direct logical contradiction supports that diagnosis. An unusual or physically surprising value alone is insufficient. Preserve the erroneous printed fragments, original evidence and reason in an author-error diagnostic; do not guess a corrected value. Negative parallaxes, results under different conditions, and errors introduced by the parser are not automatically author errors. Stop only the affected content and its dependencies. Continue other established objects and quantities, and retain valid results under other conditions separately.

### S07 Interpret ambiguities and report completeness

Check all readable materials for eligible objects and values. Do not sample, impose an object-count limit, or select only representative objects. If some content is available only in external or unreadable materials, identify what remains unprocessed. Do not record it as unreported by the paper or as grounds for scientific exclusion.

For objects or groups that could reasonably be mistaken for eligible entries but fail the inclusion conditions on inspection, record the exclusion and supporting evidence. There is no need to list every ordinary background object or reference.

Direct application of the rules requires neither per-result rule identifiers nor an additional justification.

When author meaning, object correspondence, inclusion or quantity assignment has more than one plausible interpretation, choose the interpretation best supported by the supplied paper and these rules. Briefly record the chosen interpretation and its original evidence as a model judgment, then continue the affected extraction. Interpretation uncertainty alone is not a reason to skip eligible objects or reported values. This does not permit inventing evidence or missing members, changing inclusion criteria or quantity definitions, deriving unreported quantities, or selecting one unexplained conflicting number as a confirmed value.

For a source error or numerical conflict under S06, retain the diagnostic and leave the affected content incomplete, without waiting for another decision. Continue independent content. Missing materials and parsing failures remain material gaps, not author errors. Record a quantity as unreported only after its relevant materials have been checked and no reported value or affecting gap, error or conflict remains. A finished run is not a claim of complete or scientifically correct extraction.

## Authors' overall assessment

| Name | Meaning |
|---|---|
| No overall conclusion | The paper gives no overall assessment of the object: either boundness is not discussed, or it is discussed without an overall conclusion or preference. |
| Unbound | The authors explicitly give an overall conclusion that the object is unbound from or escaping the Galaxy. |
| Possibly unbound | The authors describe the object overall as possibly or likely unbound, or explicitly favor an unbound interpretation after discussing different cases. |
| Possibly bound | The authors overall favor a Galactic bound interpretation while still considering an unbound interpretation possible. |
| Bound | The authors explicitly give an overall bound conclusion. Such an object is not included as candidates_found; an object eligible as follow_up of a historical candidate may have this state. |

## Quantity scope and definitions

| Name | Meaning |
|---|---|
| Right ascension | Right ascension reported or adopted by the authors. Preserve the original angular or hour-angle representation and its epoch. |
| Declination | Declination reported or adopted by the authors. Preserve its sign, original representation, and epoch. |
| Heliocentric distance | Heliocentric distance given by the authors. If expressed as a logarithm of distance or a distance modulus, preserve the original value and identify the representation without converting it. |
| Parallax | An object's measured, inferred, or predicted parallax, or a parallax value actually adopted as an assumption or prior in its analysis. Preserve zero and negative values and record the method and purpose. Exclude calibration offsets and general selection thresholds from this field. |
| Proper motion in right ascension | The right-ascension component of proper motion in equatorial coordinates. Preserve the authors' μ_α or μ_α* (including cos δ) convention without conversion. |
| Proper motion in declination | The declination component of proper motion in equatorial coordinates. Preserve the epoch and the authors' convention. |
| Heliocentric or barycentric radial velocity | Line-of-sight velocity reported in the heliocentric or barycentric frame. Do not enter local-standard-of-rest (LSR) or Galactic-standard-of-rest (GSR) line-of-sight velocities here. |
| Galactocentric x | Galactocentric Cartesian position x at the reference state. Preserve the authors' axis directions and frame description. |
| Galactocentric y | Galactocentric Cartesian position y at the reference state. Preserve the authors' axis directions and frame description. |
| Galactocentric z | Galactocentric Cartesian position z at the reference state. Preserve the authors' axis directions and frame description. |
| Three-dimensional Galactocentric distance | The three-dimensional distance r from the object to the Galactic center at the reference state. Do not enter cylindrical radius R here. If the type of distance is unclear, apply S07. |
| Galactocentric vx | Galactocentric Cartesian velocity component vx at the reference state. Preserve the frame convention. |
| Galactocentric vy | Galactocentric Cartesian velocity component vy at the reference state. Preserve the frame convention. |
| Galactocentric vz | Galactocentric Cartesian velocity component vz at the reference state. Preserve the frame convention. |
| Heliocentric tangential speed | The magnitude of the velocity in the plane of the sky relative to the Sun, at the present or reference state. Do not enter Galactocentric tangential or cylindrical velocities here. |
| Three-dimensional Galactic rest-frame speed | The total three-dimensional speed in a Galactic or Galactocentric rest frame at the present or reference state. Exclude velocity components, LSR/GSR line-of-sight velocities, escape speeds, and speeds at other orbital times. |
| Galactic bound probability | A numerical Galactic bound probability reported by the authors. Preserve its fraction or percentage representation. Do not calculate it as one minus the unbound probability. |
| Galactic unbound probability | A numerical Galactic unbound or escape probability reported by the authors. Do not substitute an origin probability, classification confidence, or simulation count. |

## Synthetic examples

These fictional examples illustrate the rules. They are neither evidence for the current paper.

### E01

**Input:** This paper independently selects A and explicitly concludes that A is escaping the Galaxy.

**Expected:** Include A as candidates_found with overall state unbound.

See rules: S03, S04.

### E02

**Input:** This paper selects A and uses only one model. The authors' overall assessment is that A is likely unbound from the Galaxy.

**Expected:** With qualifying evidence in the paper, include A as candidates_found with overall state possibly_unbound. A second model is not required.

See rules: S03, S04.

### E03

**Input:** This paper selects A. The authors explicitly favor a bound interpretation but still regard another reasonable scenario as supporting A as a Galactic unbound candidate.

**Expected:** Include A as candidates_found with overall state possibly_bound. Retain each scenario's original results separately.

See rules: S03, S04.

### E04

**Input:** This paper initially selects A, later establishes that the initial measurement was wrong, and ultimately concludes that A is bound and withdraws its unbound-candidate classification. A was not selected for this study because it was already a historical unbound candidate.

**Expected:** Do not include A as candidates_found. Record its exclusion and supporting evidence; do not relabel it as follow_up.

See rules: S03, S04.

### E05

**Input:** This paper explicitly reobserves A because earlier work identified it as a Galactic unbound candidate, and ultimately establishes that A is bound.

**Expected:** Include A as follow_up with overall state bound. Preserve this paper's negative reassessment.

See rules: S02, S03, S04.

### E06

**Input:** This paper performs spectroscopic analysis of an explicitly identified historical unbound candidate A without discussing its Galactic boundness.

**Expected:** Include A as follow_up with overall state no_overall_conclusion. Retain its spectroscopic study even though boundness is not reassessed.

See rules: S03, S04.

### E07

**Input:** This paper selects A. The authors accept two reasonable models giving bound and unbound results respectively and explicitly state that the available material does not support an overall assessment.

**Expected:** Retain the candidate accepted by the authors and the results under each set of conditions. Record no_overall_conclusion. The meaning is clear; the existence of two models alone does not create an interpretation ambiguity.

See rules: S03, S04.

### E08

**Input:** The authors' summaries in different passages appear inconsistent. The extractor cannot tell whether A is still considered an unbound candidate or what the authors' overall preference is.

**Expected:** Choose the interpretation of A's inclusion and overall state best supported by the paper and rules. Briefly retain the chosen interpretation and its original evidence, and continue extraction. Do not automatically exclude A or enter no_overall_conclusion merely because the extractor is uncertain.

See rules: S04, S07.

### E09

**Input:** The materials are complete. A appears only in a table labeled HVS; the paper does not define that label as Galactic unbound and provides no other qualifying evidence for A.

**Expected:** Do not include A on the strength of the HVS label. Exclusion can be automatic when the rule application is clear.

See rules: S03.

### E10

**Input:** A was discovered before. This paper does not preselect A because of its previous candidate status; it independently finds A again from the original sample and retains its unbound-candidate classification.

**Expected:** Include A as candidates_found. Whether the name or discovery is new does not determine the contribution type.

See rules: S02, S03.

### E11

**Input:** After selecting A, this paper obtains follow-up spectroscopy within the same discovery study and confirms its interpretation as an unbound candidate.

**Expected:** A remains candidates_found. The spectroscopy is part of the same discovery study; the phrase follow-up spectroscopy alone does not make it follow_up of a historical candidate.

See rules: S02, S03.

### E12

**Input:** The authors explicitly state that every identifiable object in Table 2 meets their Galactic unbound criterion. Elsewhere they say only that some objects in Table 3 are unbound without identifying them.

**Expected:** The shared statement for Table 2 can support each member's unbound candidacy. The unbound members of Table 3 remain unidentified; do not assign an unbound conclusion to every member.

See rules: S03, S06.

### E13

**Input:** A and B can be identified in the supplied PDF. The text also points to a complete object list on an external website, which is absent from the supplied materials.

**Expected:** Process the objects that can be established in the supplied PDF. Record that the external list remains unprocessed and retain the passage referencing it. Do not mark the paper fully processed or treat unprocessed external objects as ineligible.

See rules: S01, S07.

### E14

**Input:** For the same measurement, conditions, and source, the text prints a velocity of 100 ± 5 and a table prints 100 ± 8, with no correction or explanation of different conditions.

**Expected:** Preserve both full original expressions, their evidence and the unexplained numerical conflict as a diagnostic. Do not include either alternative as a confirmed measurement; mark the affected quantity incomplete. Do not average, select one, or label them as results from different models. Continue independent valid results.

See rules: S05, S06.

### E15

**Input:** The paper selects objects using a probability threshold below 1%. A separate passage independently reports that the recomputed bound probability for every object in Table 4 is below 1%.

**Expected:** Passing selection does not make its threshold an object's probability upper limit. The authors separately report computed results for Table 4, so each identifiable member may have a bound-probability upper limit below 1%, with evidence.

See rules: S05, S06.

### E16

**Input:** The authors report only a credible interval of [1.2, 2.0] kpc for A's distance, with no central value or coverage.

**Expected:** Preserve the endpoints, original unit, and credible-interval interpretation. Do not calculate a central value, uncertainty, or coverage.

See rules: S05, S06.

### E17

**Input:** The authors report a negative parallax for A and a distance modulus elsewhere, as well as a GSR line-of-sight velocity.

**Expected:** Preserve the negative parallax. Retain the distance modulus and its representation without converting it to distance. Do not enter the GSR line-of-sight velocity as heliocentric radial velocity or total three-dimensional speed.

See rules: S05.

### E18

**Input:** One velocity measurement of A has an unresolved numerical conflict in this paper. Other values are confirmed, and the conflict affects neither A's eligibility nor its identity.

**Expected:** Keep the conflicting measurement only in a diagnostic and leave its affected coverage incomplete. Allow other confirmed results to proceed. Do not discard all results from the paper or mark either conflicting value as confirmed.

See rules: S06, S07.

### E19

**Input:** The authors explicitly state that the printed distance for A is a typographical error and supply no replacement. A's identity, eligibility and other reported measurements remain established.

**Expected:** Record the printed distance, the author's correction statement and the reason in an author-error diagnostic. Do not include the erroneous distance as a confirmed measurement or invent a replacement. Leave that content incomplete and continue A's independent valid quantities.

See rules: S07.

### E20

**Input:** A supplied PDF table clearly prints a parallax of −0.2 ± 0.1 mas for A. The parsed table loses the minus sign and displays +0.2 ± 0.1 mas.

**Expected:** Use −0.2 ± 0.1 mas with the PDF page and row evidence, and report the parsing discrepancy. This is not a numerical conflict in the paper. If the original cannot be checked reliably, report the affected content as incomplete instead of guessing the sign.

See rules: S01, S06.

### E21

**Input:** The PDF identifies one object as S5-HVS1. Another passage identifies the numbered objects 'HVS4–6 and others'.

**Expected:** Treat S5-HVS1 as one printed name, not a numbered group. Submit separate entries for HVS4, HVS5, and HVS6 with the original range as evidence, applying the ordinary inclusion rules. Preserve the unnamed remainder as incomplete; do not invent names for 'others'.

See rules: S02, S07.

### E22

**Input:** The PDF explicitly identifies eligible objects A and B and refers to an online supplementary table containing the remaining names. The table is not supplied.

**Expected:** Process A and B, preserve the reference to the missing supplement, and report the remaining membership as incomplete. Do not invent the absent names or claim complete coverage.

See rules: S01, S07.

### E23

**Input:** The paper reports a Galactocentric height z = 0.3 kpc at its stated reference epoch and z = 5 kpc at a later orbital event.

**Expected:** Enter only the reference-state value in galactocentric_z. Do not substitute the later event value. Important out-of-scope orbital results may be described in the contribution summary.

See rules: S05.

### E24

**Input:** The paper reports a heliocentric distance and the distance between the Sun and the Galactic center, but gives no star-to-Galactic-center distance. The extractor could calculate one using other reported geometry.

**Expected:** Do not derive galactocentric_radius. If all relevant materials have been processed and no value is reported or adopted, record it as unreported. Complete automatic extraction does not require filling every quantity with a number.

See rules: S05, S07.

### E25

**Input:** Two object names differ only in spacing. Complete supplied materials explicitly discuss them as different objects.

**Expected:** Keep the objects separate and retain both printed names. Normalization and interpretation choices cannot override explicit object identity evidence.

See rules: S02, S07.

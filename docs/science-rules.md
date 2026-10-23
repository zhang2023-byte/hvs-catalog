<!-- Generated -->
# High-velocity star literature extraction rules

## 1. Use the supplied material faithfully

Use only the supplied text and images. Extract what the authors report. Do not add unsupported information. Do not calculate or convert values. Support each extracted item with evidence from the supplied material.

## 2. Select eligible stars

Include all identifiable stars that this paper studies and that meet either contribution category below.

- **`candidates_found`**: This paper’s selection or analysis identifies the star as a Galactic unbound candidate. The authors retain this candidacy in their final assessment.
- **`follow_up`**: This paper studies the star because previous studies identified it as a Galactic unbound candidate. Include the study regardless of its current boundness conclusion.

Galactic unbound means unbound from the Milky Way in the star’s present-day state. Candidacy requires an explicit author statement, an accepted conditional possibility, or an explicit unbound criterion that the paper establishes as satisfied. Labels, proposed origins, and ejection mechanisms alone do not establish candidacy. Apply group claims only when they explicitly cover every identified member.

Record all star identifiers supported by the paper, including names. Group star identifiers under one star only when the paper supports their correspondence. Expand star identifier ranges when their members are unambiguous from the supplied material.

For Gaia star identifiers, write `Gaia`, the data release, and the numeric source ID in this order. Separate the parts with one space. If the data release is unknown, omit it. Example: `Gaia DR3 5912901375001820288`.

## 3. Record reported quantities

For each eligible star, extract all reported or adopted values for the quantities below. Include values from previous studies that this paper reports. For positions and velocities, use the star’s present-day state.

The table lists common units. Use the listed spelling for these units. You may retain other explicitly reported units.

| Quantity | Definition | Common units |
|---|---|---|
| `ra` | Equatorial right ascension. | `deg`, `rad`, `hourangle` |
| `dec` | Equatorial declination. | `deg`, `rad` |
| `distance` | Heliocentric distance of the star. | `pc`, `kpc`, `mag` for distance modulus |
| `parallax` | Annual parallax of the star. | `mas`, `arcsec`, `μas` |
| `proper_motion_ra`, `proper_motion_dec` | Equatorial proper-motion components. | `mas/yr`, `arcsec/yr` |
| `radial_velocity` | Heliocentric or barycentric line-of-sight velocity. Exclude velocities relative to the local or Galactic standard of rest. | `km/s`, `m/s` |
| `galactocentric_radius` | Three-dimensional distance to the Galactic center, excluding cylindrical radius. | `pc`, `kpc` |
| `galactic_rest_frame_velocity` | The star’s total three-dimensional speed in the Galactic rest frame. | `km/s`, `m/s` |
| `bound_probability`, `unbound_probability` | Probability that the star is bound to or unbound from the Milky Way, respectively. | dimensionless, `%` |

`condition` describes the reported scientific context needed to interpret a value. Record each distinct result once, with its applicable condition. Do not choose a best value.

Assign group values only when the paper explicitly applies them to every identified member. Selection thresholds and sample statistics are not individual results.

## 4. Resolve ambiguities and record necessary notes

Identify printing errors or logical contradictions only from evidence in the paper. Omit only the affected content. Ambiguity means the material supports multiple interpretations. Follow the paper’s explicit definitions. Otherwise, use the established interpretation in the relevant scientific field. Briefly describe consequential issues, evidence, and decisions in `notes`.

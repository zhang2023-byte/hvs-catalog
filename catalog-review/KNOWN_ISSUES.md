# Known issues

Data version `2026-10-25-release-review`. Issues found after this release are recorded here
and fixed together in the next data version through the normal review cycle
(edit review inputs → rebuild from scratch → compare). The catalog is not rebuilt for single items.
The build reads only the three JSON files in this directory; this note is not a build input.

## Data

| Object | Paper | Issue | Planned action |
|---|---|---|---|
| Gaia DR2 5932173855446728064 (S1) | 2108.02796v1 | RV −56.5 ± 5.3 km/s (`/stars/0/quantities/0/values/1`) is the Boubert et al. (2019) value quoted on p. 3–4, but is marked own. Default values are unaffected. | Add a cited exception in papers.json. |
| Hivel3, Hivel4, Hivel7, Hivel8, Hivel17 | 2011.10206v1 | Table 1 coordinates are labelled “Gaia DR2/J2015.5”, but the table notes do not state their source. Hivel8 is 0.05″ from the SIMBAD J2000 position; Hivel3/4/7/17 differ by about ten years of motion, inconsistent with J2015.5. | Recheck the fixed PDF; correct only if the PDF supports it. |
| KIC 7881304; HVS18; HVS23 | see identity.json | Three identity decisions remain unresolved (`R21-simbad-140`, `R21-separate-141`, `R21-separate-144`); the objects are kept separate. | Resolve when new evidence is available. |
| Gaia DR3 1184128157160434560 (HVS-8) | 2608.05814v1 | Fixed PDF p. 11, Table 2 reports R_GC = 3.1 kpc, but this object's catalog quantities contain no galactocentric_radius. Found during website evidence-page checks; the website preserves the omission. | Review the missing quantity against the fixed PDF and include it in the next data version. |
| US 708 | 1804.10179v3 | The own bound_probability upper limit 0.5 has neither a unit nor a standard derivation. Under the approved website rule, P_bound is therefore absent from Latest, the index and CSV; Literature retains the original <0.5 limit. | Review derivation of unitless probabilities for the next data version; do not infer a standard value in the website generator. |

## Display rules

- A value that the paper lists but explicitly does not adopt can still become the default.
  Example: 2108.02796v1 lists the Gaia DR2 RVS velocity of S1 (−614.3 km/s) and states that it
  was removed in EDR3; under the current definitions it is own and is the default RV.
  Distinguishing such values needs a new attribution label, a science-rule change applied to all
  papers, for example also to the DR2 comparison parallaxes in 2402.10714.

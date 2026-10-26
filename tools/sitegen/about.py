"""Public explanations derived from the same catalog summary as the homepage."""

# Literature search and SIMBAD snapshot for the current data version. The manifest
# does not record them, so update both when a new data version is released.
SEARCH_TERMS = (
    "hypervelocity star",
    "hyper-velocity star",
    "high velocity star",
    "unbound star",
)
SEARCH_PERIOD = ("2005-01-01", "2026-10-23")
SIMBAD_DATE = "2026-10-25"

CSV_EXAMPLE = """import pandas as pd

stars = pd.read_csv("stars.csv", dtype={"gaia_dr3_id": "string"})
fast = stars[stars["v_grf_km_s"] > 1000]
print(fast[["name", "v_grf_km_s", "v_grf_km_s_source"]])"""


def about_view(index):
    repository = index["repository"].rstrip("/")
    terms = ", ".join(f"“{term}”" for term in SEARCH_TERMS[:-1])
    terms += f" or “{SEARCH_TERMS[-1]}”"
    return {
        "title": "About HVS Catalog",
        "sections": [
            {
                "title": "What this catalog is",
                "paragraphs": [
                    (
                        "HVS Catalog collects published measurements of hypervelocity "
                        "star candidates: stars that a paper proposes may be unbound "
                        "from the Milky Way, and stars that later papers study because "
                        "of such a proposal. Being listed does not mean a star is "
                        "unbound; later results that find a star bound are kept."
                    ),
                    (
                        f"Data version {index['data_version']} lists "
                        f"{index['star_count']} objects. Papers were found by an arXiv "
                        f"search for {terms}, first submitted from {SEARCH_PERIOD[0]} "
                        f"to {SEARCH_PERIOD[1]}. Of {index['paper_count']} papers read, "
                        f"{index['contributing_papers']} report at least one qualifying "
                        "star. The search is not guaranteed to be complete."
                    ),
                ],
            },
            {
                "title": "How it was built",
                "paragraphs": [
                    (
                        "A language model read a fixed PDF version of each paper and "
                        "extracted star names and measurements, keeping the quoted text "
                        "and page for each value. Entries from different papers were "
                        "joined when they share an identifier, resolve to the same "
                        "SIMBAD object, or report positions within 5″. Objects were "
                        f"matched to SIMBAD on {SIMBAD_DATE}."
                    ),
                    (
                        "Identity merges, whether each value was measured by the paper "
                        "itself, and transcription errors were reviewed against the "
                        "papers. Individual values have not all been checked by a "
                        "person; check the original paper before use."
                    ),
                ],
            },
            {
                "title": "Reading the catalog",
                "paragraphs": [
                    (
                        "Latest values are a reproducible default, not a best estimate. "
                        "For each quantity, only values a paper measured or derived "
                        "itself are used. Values with errors come first, then values "
                        "without errors, then bounds; within that tier the most recent "
                        "paper by arXiv identifier is chosen. If that paper reports "
                        "several values, the first is shown; hover a value to see its "
                        "source and how many it has. P_unbound and P_bound are shown "
                        "as reported; neither is derived from the other."
                    ),
                    (
                        "Literature lists the papers that studied the star, newest "
                        "first, with one representative value per quantity chosen the "
                        "same way; +N opens that paper's other values and their "
                        "conditions. Values taken from other papers (quoted) and values "
                        "that do not match the quantity's definition (not comparable), "
                        "such as minimum or ejection speeds, are hidden unless the star "
                        "has nothing else to show; the checkboxes show them. Values "
                        "that cannot be converted to standard units, such as distance "
                        "moduli, appear only in Literature."
                    ),
                    (
                        "Click any value to see its condition, the quoted text and a "
                        "link to its page in the fixed PDF. Corrected values show the "
                        "correction and its page. “Linked by position only” means at "
                        "least one paper was joined to this star by coordinates rather "
                        "than a shared identifier; “matched by position” means the same "
                        "for SIMBAD."
                    ),
                    (
                        "In the table, Papers counts all papers linked to an object and "
                        "First is the earliest year among them. All years come from "
                        "arXiv identifiers, not journal publication dates."
                    ),
                ],
            },
            {
                "title": "Units and notation",
                "paragraphs": [
                    (
                        "v_GRF is the Galactic rest-frame speed and RV the radial "
                        "velocity, in km/s. d is the heliocentric distance and r_GC the "
                        "Galactocentric radius, in kpc. Parallax is in mas; proper "
                        "motions μα* (including cos δ) and μδ are in mas/yr. "
                        "Probabilities are fractions from 0 to 1."
                    ),
                    (
                        "± e is a symmetric error and +u −l an asymmetric one. > and < "
                        "are bounds, a–b is a range, ~ marks an approximate value and "
                        "— means no value. Reported precision is kept. Star-page "
                        "coordinates come from SIMBAD when matched, otherwise from the "
                        "papers, and are shown in sexagesimal form."
                    ),
                ],
            },
            {
                "title": "Downloads",
                "paragraphs": [
                    (
                        "catalog.zip contains the complete catalog for this version: "
                        "one JSON file per star with every reported value, its source "
                        "and evidence. stars.csv has one row per star with the same "
                        "defaults as the website. Each star page also offers that "
                        "star's JSON."
                    ),
                    (
                        "In the CSV, units are in the column names (v_grf_km_s, d_kpc, "
                        "…) and coordinates are in degrees. Values, errors and bounds "
                        "are plain numbers; approximation and bound types are in "
                        "separate flag columns, and *_source gives the arXiv "
                        "identifier. Read gaia_dr3_id as text to keep every digit:"
                    ),
                ],
                "code": CSV_EXAMPLE,
                "links": [
                    {
                        "label": "catalog.zip",
                        "url": "downloads/catalog.zip",
                        "download": True,
                    },
                    {
                        "label": "stars.csv",
                        "url": "downloads/stars.csv",
                        "download": True,
                    },
                    {
                        "label": "Reading the JSON",
                        "url": repository + "#using-the-catalog",
                    },
                ],
            },
            {
                "title": "Citing",
                "paragraphs": [
                    "Catalog citation and license: pending.",
                    (
                        "Please cite the original papers for the values you use; each "
                        "value links to its paper. Catalog IDs such as "
                        "hvs-catalog-000001 identify objects within one data version "
                        "and may change in later versions, so refer to stars by name "
                        "and data version."
                    ),
                ],
            },
            {
                "title": "Corrections",
                "paragraphs": [
                    (
                        "Known issues are kept for the next data version. To report an "
                        "error, open an issue with the star, paper, value and PDF page; "
                        "the link on each star page fills in the star name."
                    ),
                ],
                "links": [
                    {
                        "label": "Known issues",
                        "url": repository + "/blob/HEAD/catalog-review/KNOWN_ISSUES.md",
                    },
                    {"label": "Report an error", "url": repository + "/issues/new"},
                ],
            },
        ],
    }

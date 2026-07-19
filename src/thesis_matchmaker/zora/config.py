"""
Central configuration for the ZORA harvester.

Every hardcoded value the pipeline depends on lives here, not buried in
logic files. If UZH changes a field name or the scope UUID, this is the
only file that needs to change.
"""
import os

# --- ZORA scope -------------------------------------------------------
# Set to a community UUID to restrict to a single faculty, or None to
# harvest all of ZORA (~238K items across every UZH faculty).
DEFAULT_SCOPE_UUID: str | None = None

# --- Department resolution ---------------------------------------------
# Departments are resolved dynamically per item by parsing the
# owningCollection name (see normalize._get_department). No hardcoded
# mapping needed — this covers all 291 departments across every UZH faculty.

# --- API endpoint -------------------------------------------------------
DEFAULT_API_ENDPOINT = "https://www.zora.uzh.ch/server/api"

# --- Dublin Core field names ---------------------------------------------
# These are the DSpace defaults. UZH's DSpace-CRIS install *may* extend or
# rename some of these (especially author-identifier fields). Before trusting
# this list, run `python -m scripts.inspect_fields` against a handful of real
# WWF records and diff the printed field names against what's below.
FIELD_TITLE = "dc.title"
FIELD_AUTHOR = "uzh.contributor.author"  # UZH custom — NOT dc.contributor.author
FIELD_ABSTRACT = "dc.description.abstract"
FIELD_DATE_ISSUED = "dc.date.issued"
FIELD_DATE_ACCESSIONED = "dc.date.accessioned"
FIELD_TYPE = "dc.type"
FIELD_DOI = "dc.identifier.doi"
FIELD_URI = "dc.identifier.uri"

# Keywords / subject fields — UZH doesn't use plain dc.subject. Instead:
# - dc.subject.ddc: Dewey Decimal classification, e.g. "330 Economics"
# - uzh.scopus.subjects: Scopus subject areas, e.g. "Economics and Econometrics"
# Both are useful for topic matching. We merge all available into one list.
FIELD_SUBJECT_DDC = "dc.subject.ddc"
FIELD_SCOPUS_SUBJECTS = "uzh.scopus.subjects"
FIELD_SUBJECT = "dc.subject"  # kept as fallback — may appear on some items
FIELD_LANGUAGE = "dc.language.iso"

# Candidate fields for author ORCID — UZH uses cris.virtual.orcid with full
# URL format ("https://orcid.org/0000-..."), not a bare ID. The harvester
# tries each candidate in order and takes the first hit, stripping any URL
# prefix to store a bare ORCID.
FIELD_ORCID_CANDIDATES = [
    "cris.virtual.orcid",       # confirmed present on real WWF records
    "person.identifier.orcid",  # kept as fallback
    "dc.contributor.orcid",
    "dc.identifier.orcid",
]

# --- Paths -------------------------------------------------------------
DATA_DIR = os.environ.get("ZORA_DATA_DIR", "data")
PUBLICATIONS_PATH = os.path.join(DATA_DIR, "publications.jsonl")
STATE_PATH = os.path.join(DATA_DIR, "state.json")
RAW_DIR = os.path.join(DATA_DIR, "raw")

# --- Safety thresholds ---------------------------------------------------
# If a harvest run returns dramatically fewer publications than the previous
# run recorded, something is probably wrong upstream (auth failure returning
# an empty-but-200 response, scope UUID typo, API outage) rather than the
# faculty genuinely losing most of its publications overnight. Abort instead
# of committing a destructive update.
MIN_RETENTION_RATIO = 0.5  # new total must be >= 50% of previous total

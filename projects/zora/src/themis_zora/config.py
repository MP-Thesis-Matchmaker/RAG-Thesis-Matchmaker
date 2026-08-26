"""
Central configuration for the ZORA harvester.

Every hardcoded value the pipeline depends on lives here, not buried in
logic files. If UZH changes a field name or the scope UUID, this is the
only file that needs to change.
"""

import os
from pathlib import Path

# --- ZORA scope -------------------------------------------------------
# Set to a community UUID to restrict to a single faculty, or None to
# harvest all of ZORA (~238K items across every UZH faculty).
DEFAULT_SCOPE_UUID: str | None = None

# Root of the UZH community tree: the org structure (faculties, institutes)
# lives in communities below this node. Walked by zora_client.iter_org_tree
# for the org_unit mirror; ZORA's OrgUnit entity type is empty upstream.
UZH_ROOT_COMMUNITY_UUID = "323725a5-950d-4b89-8765-1b955e305664"

# --- Department resolution ---------------------------------------------
# Departments are resolved dynamically per item by parsing the
# owningCollection name (see normalize._get_department). No hardcoded
# mapping needed — this covers all 291 departments across every UZH faculty.
# Each org unit's publications live in a collection named with this prefix;
# normalize strips it for `publication.department` and uses it to pick the
# publications collection out of a community's collection list.
PUBLICATIONS_COLLECTION_PREFIX = "Publications of "

# --- API endpoint -------------------------------------------------------
DEFAULT_API_ENDPOINT = "https://www.zora.uzh.ch/server/api"

# --- Auth ---------------------------------------------------------------
# The ZORA personal API token comes from one of two environment variables:
#   ZORA_UZH_API_KEY_FILE  path to a file containing the token
#   ZORA_UZH_API_KEY       the token itself
# The file wins when both are set. In the cluster the token arrives as a
# mounted Secret, so a file is the deployed truth, whereas an inline value
# is usually a stale export in someone's shell.
#
# We resolve the token here and assign it to DSpaceClient.api_token (see
# zora_client.get_client). The vendored client has its own lookup —
# PERSONAL_API_TOKEN_FILE, then .dspace-personal-api-token.secret in the
# working and home directories — but our assignment overrides it, so those
# are not part of the contract and are not documented anywhere else.
ENV_API_KEY_FILE = "ZORA_UZH_API_KEY_FILE"
ENV_API_KEY = "ZORA_UZH_API_KEY"


def resolve_api_token() -> str:
    """
    Return the ZORA personal API token, read from the environment on every
    call (not at import time, so a test can set the variables itself).

    @raise RuntimeError: if neither variable is set, or if ENV_API_KEY_FILE
                          points at a file that cannot be read or is empty.
                          A broken path fails loudly rather than falling back
                          to the inline token: authenticating with a different
                          credential than the one asked for hides the mistake.
    """
    path = os.environ.get(ENV_API_KEY_FILE, "").strip()
    if path:
        try:
            # .strip() because writing a token with echo or an editor leaves
            # a trailing newline, which the API rejects as part of the header.
            token = Path(path).read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise RuntimeError(f"{ENV_API_KEY_FILE}={path} could not be read: {exc}") from exc
        if not token:
            raise RuntimeError(f"{ENV_API_KEY_FILE}={path} is empty.")
        return token

    token = os.environ.get(ENV_API_KEY, "").strip()
    if token:
        return token

    raise RuntimeError(
        f"No ZORA API token configured. Set {ENV_API_KEY_FILE} to a file containing "
        f"the token, or {ENV_API_KEY} to the token itself. {ENV_API_KEY_FILE} takes "
        f"precedence if both are set."
    )


# --- Dublin Core field names ---------------------------------------------
# These are the DSpace defaults. UZH's DSpace-CRIS install *may* extend or
# rename some of these (especially author-identifier fields). Before trusting
# this list, run `python projects/zora/scripts/zora_inspect_fields.py` against a handful of real
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

# Person entity fields (dspace.entity.type:Person items — the CRIS researcher
# profiles that cris-typed author authorities resolve to). Probed live
# 2026-08-24: these plus dc.title / dc.identifier.uri are all the substance a
# Person item carries; there is no affiliation, department or email upstream.
FIELD_PERSON_FAMILY = "person.familyName"
FIELD_PERSON_GIVEN = "person.givenName"
FIELD_PERSON_ORCID = "person.identifier.orcid"

# Community (org unit) field: UZH's own numeric org-unit id, independent of
# the DSpace uuid.
FIELD_ORG_SUBJECT_ID = "dc.zora.subjectid"

# Candidate fields for author ORCID — UZH uses cris.virtual.orcid with full
# URL format ("https://orcid.org/0000-..."), not a bare ID. The harvester
# tries each candidate in order and takes the first hit, stripping any URL
# prefix to store a bare ORCID.
FIELD_ORCID_CANDIDATES = [
    "cris.virtual.orcid",  # confirmed present on real WWF records
    "person.identifier.orcid",  # kept as fallback
    "dc.contributor.orcid",
    "dc.identifier.orcid",
]

# --- Paths -------------------------------------------------------------
# Harvest output and the watermark live in Postgres now (see zora/store.py); the
# only thing still written to disk is the raw-response cache, which keeps
# ingestion reproducible without re-hitting ZORA.
DATA_DIR = os.environ.get("ZORA_DATA_DIR", "data")
RAW_DIR = os.path.join(DATA_DIR, "raw")

# Gone with output_schema.py (2026-08-24): PUBLICATIONS_PATH pointed at
# data/publications.jsonl for a standalone validator of pre-Postgres harvests.
# Nothing wrote that file, git stopped tracking it, and the validator was its
# only reader. `indexing/sources.py::JsonlSourceReader` still validates JSONL --
# that is data/samples, and unrelated.

# --- Safety thresholds ---------------------------------------------------
# If a harvest run returns dramatically fewer publications than the previous
# run recorded, something is probably wrong upstream (auth failure returning
# an empty-but-200 response, scope UUID typo, API outage) rather than the
# faculty genuinely losing most of its publications overnight. Abort instead
# of committing a destructive update.
MIN_RETENTION_RATIO = 0.5  # new total must be >= 50% of previous total

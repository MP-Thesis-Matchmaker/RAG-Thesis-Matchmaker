"""The DSpace metadata field names ZORA records are read through.

A vocabulary, not configuration. These are the keys UZH's DSpace-CRIS install
puts in an item's metadata map; changing one does not tune the harvester, it
points it at a different field. Nothing reads them from the environment and
nothing should, which is why they are module constants here rather than settings
in `config.py`.

They are separate from `config.py` because that file is now a pydantic settings
model, and fifteen names that are neither settable nor validated would bury the
handful of knobs that are.

Before trusting this list, run `python projects/zora/scripts/zora_inspect_fields.py`
against a handful of real records and diff the printed field names against what
is below. UZH's install extends the DSpace defaults, especially around author
identifiers.
"""

from __future__ import annotations

FIELD_TITLE = "dc.title"
FIELD_AUTHOR = "uzh.contributor.author"  # UZH custom -- NOT dc.contributor.author
FIELD_ABSTRACT = "dc.description.abstract"
FIELD_DATE_ISSUED = "dc.date.issued"
FIELD_DATE_ACCESSIONED = "dc.date.accessioned"
FIELD_TYPE = "dc.type"
FIELD_DOI = "dc.identifier.doi"
FIELD_URI = "dc.identifier.uri"

# Keywords / subject fields -- UZH doesn't use plain dc.subject. Instead:
# - dc.subject.ddc: Dewey Decimal classification, e.g. "330 Economics"
# - uzh.scopus.subjects: Scopus subject areas, e.g. "Economics and Econometrics"
# Both are useful for topic matching. We merge all available into one list.
FIELD_SUBJECT_DDC = "dc.subject.ddc"
FIELD_SCOPUS_SUBJECTS = "uzh.scopus.subjects"
FIELD_SUBJECT = "dc.subject"  # kept as fallback -- may appear on some items
FIELD_LANGUAGE = "dc.language.iso"

# Person entity fields (dspace.entity.type:Person items -- the CRIS researcher
# profiles that cris-typed author authorities resolve to). Probed live
# 2026-08-24: these plus dc.title / dc.identifier.uri are all the substance a
# Person item carries; there is no affiliation, department or email upstream.
FIELD_PERSON_FAMILY = "person.familyName"
FIELD_PERSON_GIVEN = "person.givenName"
FIELD_PERSON_ORCID = "person.identifier.orcid"

# Community (org unit) field: UZH's own numeric org-unit id, independent of
# the DSpace uuid.
FIELD_ORG_SUBJECT_ID = "dc.zora.subjectid"

# Candidate fields for author ORCID -- UZH uses cris.virtual.orcid with full
# URL format ("https://orcid.org/0000-..."), not a bare ID. The harvester
# tries each candidate in order and takes the first hit, stripping any URL
# prefix to store a bare ORCID.
FIELD_ORCID_CANDIDATES = [
    "cris.virtual.orcid",  # confirmed present on real WWF records
    "person.identifier.orcid",  # kept as fallback
    "dc.contributor.orcid",
    "dc.identifier.orcid",
]

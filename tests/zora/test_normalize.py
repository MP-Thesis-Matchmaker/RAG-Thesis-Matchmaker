import pytest

from thesis_matchmaker.zora import config
from thesis_matchmaker.zora.normalize import normalize_item, normalize_org_unit, normalize_person

from .fake_dso import FakeDSO


def test_normalize_single_author_with_orcid():
    dso = FakeDSO(
        handle="20.500.14742/1001",
        uuid="uuid-1",
        fields={
            config.FIELD_TITLE: ["Trade Policy and Growth"],
            config.FIELD_AUTHOR: ["Doe, Jane"],
            config.FIELD_ABSTRACT: ["This paper examines..."],
            config.FIELD_DATE_ISSUED: ["2025-03-01"],
            config.FIELD_TYPE: ["Journal Article"],
            "cris.virtual.orcid": ["https://orcid.org/0000-0002-1111-2222"],
        },
    )

    record = normalize_item(dso)

    assert record["title"] == "Trade Policy and Growth"
    assert record["authors"] == ["Doe, Jane"]
    assert record["author_orcid"] == "0000-0002-1111-2222"  # URL prefix stripped
    assert record["year"] == 2025
    assert record["handle"] == "20.500.14742/1001"


def test_normalize_missing_fields_do_not_crash():
    dso = FakeDSO(handle="h", uuid="u", fields={config.FIELD_TITLE: ["Only a title"]})

    record = normalize_item(dso)

    assert record["title"] == "Only a title"
    assert record["authors"] == []
    assert record["author_orcid"] is None
    assert record["abstract"] is None
    assert record["year"] is None
    assert record["keywords"] == []
    assert record["department"] is None
    assert record["language"] is None
    assert record["uzh_authors"] == []
    assert record["author_authority_map"] == {}


def test_normalize_year_extracted_from_full_date():
    dso = FakeDSO(
        handle="h",
        uuid="u",
        fields={config.FIELD_DATE_ISSUED: ["2024-11-15T00:00:00Z"]},
    )

    record = normalize_item(dso)

    assert record["year"] == 2024


def test_orcid_url_prefix_stripped():
    """cris.virtual.orcid stores full URLs — we strip to bare ID."""
    dso = FakeDSO(
        handle="h",
        uuid="u",
        fields={"cris.virtual.orcid": ["https://orcid.org/0000-0003-3333-4444"]},
    )

    record = normalize_item(dso)

    assert record["author_orcid"] == "0000-0003-3333-4444"


def test_orcid_bare_id_preserved():
    """If a future field stores a bare ORCID (no URL), it's kept as-is."""
    dso = FakeDSO(
        handle="h",
        uuid="u",
        fields={"person.identifier.orcid": ["0000-0001-2222-3333"]},
    )

    record = normalize_item(dso)

    assert record["author_orcid"] == "0000-0001-2222-3333"


def test_keywords_merged_from_ddc_and_scopus():
    """Keywords come from dc.subject.ddc + uzh.scopus.subjects, merged."""
    dso = FakeDSO(
        handle="h",
        uuid="u",
        fields={
            config.FIELD_SUBJECT_DDC: ["330 Economics"],
            config.FIELD_SCOPUS_SUBJECTS: ["Economics and Econometrics"],
        },
    )

    record = normalize_item(dso)

    assert record["keywords"] == ["330 Economics", "Economics and Econometrics"]


def test_keywords_deduped_across_fields():
    """If the same value appears in multiple fields, it's kept once."""
    dso = FakeDSO(
        handle="h",
        uuid="u",
        fields={
            config.FIELD_SUBJECT_DDC: ["330 Economics"],
            config.FIELD_SUBJECT: ["330 Economics"],  # duplicate
        },
    )

    record = normalize_item(dso)

    assert record["keywords"] == ["330 Economics"]


def test_keywords_empty_when_no_subject_fields():
    dso = FakeDSO(
        handle="h",
        uuid="u",
        fields={config.FIELD_TITLE: ["A paper without keywords"]},
    )

    record = normalize_item(dso)

    assert record["keywords"] == []


def test_author_field_uses_uzh_namespace():
    """Confirm we read from uzh.contributor.author, not dc.contributor.author."""
    dso = FakeDSO(
        handle="h",
        uuid="u",
        fields={
            "uzh.contributor.author": ["Schmutzler, Armin"],
            "dc.contributor.author": ["WRONG — should not be read"],
        },
    )

    record = normalize_item(dso)

    assert record["authors"] == ["Schmutzler, Armin"]


# --- New tests for department, uzh_authors, author_authority_map, language ---


def test_department_extracted_from_embedded_collection():
    """Department is resolved from the embedded owningCollection UUID."""
    dso = FakeDSO(
        handle="h",
        uuid="u",
        fields={config.FIELD_TITLE: ["A paper"]},
        embedded={
            "owningCollection": {
                "uuid": "f61a17ca-109f-481a-bbc3-3f410fa6ef57",
                "name": "Publications of Department of Informatics",
            }
        },
    )

    record = normalize_item(dso)

    assert record["department"] == "Department of Informatics"


def test_department_none_when_no_embedded_collection():
    """Department is None when no owningCollection is embedded."""
    dso = FakeDSO(handle="h", uuid="u", fields={config.FIELD_TITLE: ["A paper"]})

    record = normalize_item(dso)

    assert record["department"] is None


def test_department_resolved_by_parsing_collection_name_if_not_mapped():
    """Department is parsed from owningCollection name when the UUID is not in WWF mapping."""
    dso = FakeDSO(
        handle="h",
        uuid="u",
        fields={config.FIELD_TITLE: ["A paper"]},
        embedded={
            "owningCollection": {
                "uuid": "unknown-uuid-not-in-mapping",
                "name": "Institute of Psychology",
            }
        },
    )

    record = normalize_item(dso)

    assert record["department"] == "Institute of Psychology"


def test_department_resolved_by_parsing_collection_name_strips_prefix():
    """Department name extraction strips 'Publications of ' prefix."""
    dso = FakeDSO(
        handle="h",
        uuid="u",
        fields={config.FIELD_TITLE: ["A paper"]},
        embedded={
            "owningCollection": {
                "uuid": "unknown-uuid-not-in-mapping",
                "name": "Publications of Institute of Computational Linguistics",
            }
        },
    )

    record = normalize_item(dso)

    assert record["department"] == "Institute of Computational Linguistics"


def test_department_extracted_from_mapped_collections():
    """Department is resolved from mappedCollections if owningCollection is missing."""
    dso = FakeDSO(
        handle="h",
        uuid="u",
        fields={config.FIELD_TITLE: ["A paper"]},
        embedded={
            "mappedCollections": {
                "_embedded": {
                    "mappedCollections": [
                        {
                            "uuid": "f61a17ca-109f-481a-bbc3-3f410fa6ef57",
                            "name": "Publications of Department of Informatics",
                        }
                    ]
                }
            }
        },
    )

    record = normalize_item(dso)

    assert record["department"] == "Department of Informatics"


def test_uzh_authors_admits_only_cris_authorities():
    """uzh_authors holds authors with a CRIS Person UUID, not merely any authority.

    The three authors are the three cases that exist upstream: no authority at
    all, a CRIS Person UUID, and an ORCID placeholder. Only the middle one is a
    registered UZH researcher.
    """
    dso = FakeDSO(
        handle="h",
        uuid="u",
        fields={
            config.FIELD_AUTHOR: ["External, Alice", "Schmutzler, Armin", "Foreign, Bob"],
        },
        authorities={
            config.FIELD_AUTHOR: [
                None,
                "f45b3ec1-cf2a-43ae-85d4-528afff07a40",
                "will be referenced::ORCID::0000-0002-1825-0097",
            ],
        },
    )

    record = normalize_item(dso)

    assert record["authors"] == ["External, Alice", "Schmutzler, Armin", "Foreign, Bob"]
    assert record["uzh_authors"] == ["Schmutzler, Armin"]


def test_uzh_authors_empty_when_no_authorities():
    """uzh_authors is empty when no author has an authority key."""
    dso = FakeDSO(
        handle="h",
        uuid="u",
        fields={config.FIELD_AUTHOR: ["Doe, Jane", "Smith, John"]},
    )

    record = normalize_item(dso)

    assert record["uzh_authors"] == []


def test_author_authority_map_includes_all_authors():
    """author_authority_map maps every author, with None for external ones."""
    dso = FakeDSO(
        handle="h",
        uuid="u",
        fields={
            config.FIELD_AUTHOR: ["External, Alice", "Schmutzler, Armin"],
        },
        authorities={
            config.FIELD_AUTHOR: [None, "f45b3ec1-cf2a-43ae-85d4-528afff07a40"],
        },
    )

    record = normalize_item(dso)

    assert record["author_authority_map"] == {
        "External, Alice": None,
        "Schmutzler, Armin": {"type": "cris", "id": "f45b3ec1-cf2a-43ae-85d4-528afff07a40"},
    }


def test_language_extracted():
    """Language is read from dc.language.iso."""
    dso = FakeDSO(
        handle="h",
        uuid="u",
        fields={config.FIELD_LANGUAGE: ["eng"]},
    )

    record = normalize_item(dso)

    assert record["language"] == "eng"


def test_language_none_when_missing():
    """Language is None when dc.language.iso is not present."""
    dso = FakeDSO(handle="h", uuid="u", fields={config.FIELD_TITLE: ["A paper"]})

    record = normalize_item(dso)

    assert record["language"] is None


def test_author_authority_map_types_orcid_placeholder():
    """The 'will be referenced::ORCID::' marker becomes an orcid-typed entry."""
    dso = FakeDSO(
        handle="h",
        uuid="u",
        fields={
            config.FIELD_AUTHOR: ["External, Alice", "Theile, Gudrun"],
        },
        authorities={
            config.FIELD_AUTHOR: [None, "will be referenced::ORCID::0000-0002-9454-3617"],
        },
    )

    record = normalize_item(dso)

    assert record["author_authority_map"] == {
        "External, Alice": None,
        "Theile, Gudrun": {"type": "orcid", "id": "0000-0002-9454-3617"},
    }
    # Present in the map (full provenance) but NOT eligible: the marker says this
    # item is not linked to a local Person, so the affiliation is unknown.
    assert record["uzh_authors"] == []


def test_a_marked_authority_stays_orcid_however_malformed_its_payload():
    """The marker outranks the id's shape — the half of the rule that never moved.

    Upstream ORCIDs are frequently broken. If a malformed payload could demote a
    marked entry to `cris`, those authors would become phantom UZH researchers,
    which is exactly the failure the marker exists to prevent.
    """
    dso = FakeDSO(
        handle="h",
        uuid="u",
        fields={config.FIELD_AUTHOR: ["Broken, Bea"]},
        authorities={config.FIELD_AUTHOR: ["will be referenced::ORCID::not-an-orcid-at-all"]},
    )

    record = normalize_item(dso)

    assert record["author_authority_map"] == {
        "Broken, Bea": {"type": "orcid", "id": "not-an-orcid-at-all"},
    }


def test_an_unmarked_but_well_formed_orcid_is_typed_orcid():
    """Shape decides when there is no marker, because `cris` is the wrong default there.

    One real record does this (`20.500.14742/59205`): upstream omitted the marker,
    so the old fall-through filed a bare ORCID as a CRIS Person id — a supervisor
    candidate that joins to nothing in `person` and still counted as eligible.
    """
    dso = FakeDSO(
        handle="h",
        uuid="u",
        fields={config.FIELD_AUTHOR: ["Phantom, Phil"]},
        authorities={config.FIELD_AUTHOR: ["0000-0002-7695-501X"]},  # no marker
    )

    record = normalize_item(dso)

    assert record["author_authority_map"] == {
        "Phantom, Phil": {"type": "orcid", "id": "0000-0002-7695-501X"},
    }
    assert record["uzh_authors"] == []


def test_an_unmarked_orcid_is_canonicalised_before_the_shape_test():
    """A lowercase check digit is still an ORCID; testing the raw value would miss it."""
    dso = FakeDSO(
        handle="h",
        uuid="u",
        fields={config.FIELD_AUTHOR: ["Lower, Lena"]},
        authorities={config.FIELD_AUTHOR: ["0000-0001-5644-045x"]},  # no marker, lowercase x
    )

    record = normalize_item(dso)

    assert record["author_authority_map"] == {
        "Lower, Lena": {"type": "orcid", "id": "0000-0001-5644-045X"},
    }


def test_a_cris_uuid_survives_the_shape_test_byte_for_byte():
    """The regression the throwaway-variable design exists to prevent.

    `_normalize_orcid` uppercases. A CRIS id is a lowercase-hex UUID and the
    `person.uuid` join is exact, so normalising one in place would silently break
    every author-to-researcher link in the corpus. The normalised value has to be
    used as a *test* and then discarded.
    """
    uuid = "f45b3ec1-cf2a-43ae-85d4-528afff07a40"
    dso = FakeDSO(
        handle="h",
        uuid="u",
        fields={config.FIELD_AUTHOR: ["Real, Researcher"]},
        authorities={config.FIELD_AUTHOR: [uuid]},
    )

    record = normalize_item(dso)

    assert record["author_authority_map"] == {"Real, Researcher": {"type": "cris", "id": uuid}}
    assert record["uzh_authors"] == ["Real, Researcher"]


# --- owning_collection_uuid ---


def test_owning_collection_uuid_extracted():
    dso = FakeDSO(
        handle="h",
        uuid="u",
        fields={config.FIELD_TITLE: ["A paper"]},
        embedded={
            "owningCollection": {
                "uuid": "f61a17ca-109f-481a-bbc3-3f410fa6ef57",
                "name": "Publications of Department of Informatics",
            }
        },
    )

    record = normalize_item(dso)

    assert record["owning_collection_uuid"] == "f61a17ca-109f-481a-bbc3-3f410fa6ef57"
    assert record["department"] == "Department of Informatics"


def test_owning_collection_uuid_from_mapped_fallback():
    """Name and uuid come from the same mapped collection when owningCollection is absent."""
    dso = FakeDSO(
        handle="h",
        uuid="u",
        fields={config.FIELD_TITLE: ["A paper"]},
        embedded={
            "mappedCollections": {
                "_embedded": {
                    "mappedCollections": [
                        {
                            "uuid": "aaaa17ca-109f-481a-bbc3-3f410fa6ef57",
                            "name": "Publications of Institute of Psychology",
                        }
                    ]
                }
            }
        },
    )

    record = normalize_item(dso)

    assert record["owning_collection_uuid"] == "aaaa17ca-109f-481a-bbc3-3f410fa6ef57"
    assert record["department"] == "Institute of Psychology"


def test_owning_collection_uuid_none_when_no_collections():
    dso = FakeDSO(handle="h", uuid="u", fields={config.FIELD_TITLE: ["A paper"]})

    record = normalize_item(dso)

    assert record["owning_collection_uuid"] is None


# --- normalize_person ---


def test_normalize_person_extracts_all_fields():
    dso = FakeDSO(
        handle="20.500.14742/239047",
        uuid="00d53153-03a6-4fd3-a581-de9a75a0015a",
        fields={
            config.FIELD_TITLE: ["Runge, Jan-Niklas"],
            config.FIELD_PERSON_FAMILY: ["Runge"],
            config.FIELD_PERSON_GIVEN: ["Jan-Niklas"],
            config.FIELD_PERSON_ORCID: ["0000-0002-0450-9897"],
            config.FIELD_URI: ["https://www.zora.uzh.ch/handle/20.500.14742/239047"],
            config.FIELD_DATE_ACCESSIONED: ["2025-12-08T16:28:41Z"],
        },
    )

    record = normalize_person(dso)

    assert record == {
        "uuid": "00d53153-03a6-4fd3-a581-de9a75a0015a",
        "display_name": "Runge, Jan-Niklas",
        "family_name": "Runge",
        "given_name": "Jan-Niklas",
        "orcid": "0000-0002-0450-9897",
        "handle": "20.500.14742/239047",
        "url": "https://www.zora.uzh.ch/handle/20.500.14742/239047",
        "accessioned": "2025-12-08T16:28:41Z",
    }


def test_normalize_person_strips_orcid_url_prefix():
    dso = FakeDSO(
        handle="h",
        uuid="u",
        fields={config.FIELD_PERSON_ORCID: ["https://orcid.org/0000-0002-0450-9897"]},
    )

    record = normalize_person(dso)

    assert record["orcid"] == "0000-0002-0450-9897"


def test_normalize_person_missing_fields_do_not_crash():
    dso = FakeDSO(handle="h", uuid="u", fields={})

    record = normalize_person(dso)

    assert record["uuid"] == "u"
    assert record["display_name"] is None
    assert record["orcid"] is None


# --- normalize_org_unit ---


def _community(uuid: str, name: str, subject_id: str | None = None) -> dict:
    metadata = {}
    if subject_id:
        metadata[config.FIELD_ORG_SUBJECT_ID] = [{"value": subject_id}]
    return {"uuid": uuid, "name": name, "handle": f"20.500.14742/{uuid[:2]}", "metadata": metadata}


def test_normalize_org_unit_picks_publications_collection():
    community = _community("c-1", "03 Faculty of Economics", subject_id="10232")
    collections = [
        {"uuid": "x-1", "name": "Some other collection"},
        {"uuid": "x-2", "name": "Publications of Faculty of Economics"},
    ]

    record = normalize_org_unit(community, "root-uuid", 1, "c-1", collections)

    assert record == {
        "uuid": "c-1",
        "name": "03 Faculty of Economics",
        "parent_uuid": "root-uuid",
        "faculty_uuid": "c-1",
        "depth": 1,
        "handle": "20.500.14742/c-",
        "subject_id": "10232",
        "collection_uuid": "x-2",
        "collection_name": "Publications of Faculty of Economics",
    }


def test_normalize_org_unit_without_publications_collection():
    community = _community("c-2", "Some grouping node")

    record = normalize_org_unit(community, "root-uuid", 1, "c-2", [])

    assert record["collection_uuid"] is None
    assert record["collection_name"] is None
    assert record["subject_id"] is None


def test_normalize_org_unit_warns_and_keeps_first_on_multiple_collections(caplog):
    community = _community("c-3", "Institute of Ambiguity")
    collections = [
        {"uuid": "x-1", "name": "Publications of Institute of Ambiguity"},
        {"uuid": "x-2", "name": "Publications of Institute of Ambiguity (old)"},
    ]

    with caplog.at_level("WARNING"):
        record = normalize_org_unit(community, "f-uuid", 2, "f-uuid", collections)

    assert record["collection_uuid"] == "x-1"
    assert "Publications of" in caplog.text or "collections" in caplog.text


# ---------------------------------------------------------------------------
# ORCID normalization
#
# Every raw value below is real -- taken from the 2026-08-25 corpus, where 20 of
# 157,800 orcid-typed authority ids were corrupt in one of four ways. The old
# placeholder test used a *bare* ORCID in the marker, which is precisely why none
# of this was caught.
# ---------------------------------------------------------------------------


def _authority_map(authority: str) -> dict:
    dso = FakeDSO(
        handle="h",
        uuid="u",
        fields={config.FIELD_AUTHOR: ["Preisig, Martina Vanessa"]},
        authorities={config.FIELD_AUTHOR: [authority]},
    )
    return normalize_item(dso)["author_authority_map"]["Preisig, Martina Vanessa"]


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        # The bug that started this: the marker was stripped, the URL was not.
        ("https://orcid.org/0009-0005-4380-7204", "0009-0005-4380-7204"),
        ("http://orcid.org/0000-0001-5968-592X", "0000-0001-5968-592X"),
        # Lowercase check digit -- ORCID canonicalises to uppercase X.
        ("0000-0001-5644-045x", "0000-0001-5644-045X"),
        ("0000-0003-3065-530x", "0000-0003-3065-530X"),
        # Trailing punctuation. The 4 is real data (checksum-valid); only the dot goes.
        ("0000-0002-3148-0954.", "0000-0002-3148-0954"),
        # Already canonical -- must survive untouched.
        ("0000-0002-1825-0097", "0000-0002-1825-0097"),
    ],
)
def test_orcid_authority_is_canonicalised(raw: str, expected: str) -> None:
    assert _authority_map(f"will be referenced::ORCID::{raw}") == {
        "type": "orcid",
        "id": expected,
    }


@pytest.mark.parametrize("raw", ["0000-0002-8070-773", "0000-0002-8632-166", "0000-0003-1927-993"])
def test_a_stripped_check_digit_is_restored(raw: str) -> None:
    """All three real truncated ids compute to X, matching a manual ORCID lookup."""
    assert _authority_map(f"will be referenced::ORCID::{raw}") == {
        "type": "orcid",
        "id": f"{raw}X",
    }


def test_a_truncated_id_whose_checksum_is_not_x_is_left_alone() -> None:
    """The guard, and the reason this repair is safe to ship.

    A 3-character final group has two possible causes -- a stripped X, or a dropped
    leading zero -- and the checksum cannot always tell them apart (both
    `0000-0002-8070-773X` and `0000-0002-8070-0773` are valid). Appending the
    computed digit is only justified when it is X, the one case the stripped-X
    explanation covers. Anything else stays visibly broken rather than being
    completed into a wrong ORCID attributed to a named researcher.
    """
    # 0000-0002-1825-009 -> checksum 7, so the stripped-X story does not hold.
    assert _authority_map("will be referenced::ORCID::0000-0002-1825-009") == {
        "type": "orcid",
        "id": "0000-0002-1825-009",
    }


def test_an_unrecognisable_orcid_is_preserved_not_blanked() -> None:
    """Bad data has to stay visible; a silently emptied field is unrecoverable."""
    assert _authority_map("will be referenced::ORCID::garbage") == {
        "type": "orcid",
        "id": "garbage",
    }


@pytest.mark.parametrize(
    "raw",
    [
        "https://orcid.org/0009-0005-4380-7204",
        "http://orcid.org/0009-0005-4380-7204",
        # Malformed payload behind the prefix: still unambiguously an ORCID, and
        # still repaired. The URL is the declaration; the shape is not consulted.
        "https://orcid.org/0000-0002-8070-773",
    ],
)
def test_an_unmarked_orcid_url_is_still_typed_orcid(raw: str) -> None:
    """Defensive branch: an orcid.org URL declares itself, marker or no marker.

    Every URL observed so far carried the marker too, so this changes no existing
    row. It exists because the alternative failure is silent -- an unmarked URL
    typed `cris` is a phantom UZH researcher that resolves to nobody in `person`
    and still counts toward eligibility.
    """
    result = _authority_map(raw)

    assert result["type"] == "orcid"
    assert not result["id"].startswith("http")


def test_a_cris_authority_is_never_orcid_normalised() -> None:
    """The one constraint that would corrupt the corpus if it were wrong.

    CRIS ids are lowercase-hex UUIDs joining to `person.uuid`. The ORCID pipeline
    uppercases, so running it here would break every join in the entity mirror.
    """
    uuid = "d5df134e-965f-4125-94d2-1cc5483553cf"

    assert _authority_map(uuid) == {"type": "cris", "id": uuid}


def test_person_orcid_uses_the_same_normalisation() -> None:
    """One definition across all three ORCID paths, so they cannot drift apart."""
    dso = FakeDSO(
        handle="h",
        uuid="u",
        fields={
            config.FIELD_TITLE: ["Preisig, Martina Vanessa"],
            config.FIELD_PERSON_ORCID: ["https://orcid.org/0000-0001-5644-045x"],
        },
    )

    assert normalize_person(dso)["orcid"] == "0000-0001-5644-045X"

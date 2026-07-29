from thesis_matchmaker.zora import config
from thesis_matchmaker.zora.normalize import normalize_item

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


def test_uzh_authors_filters_by_authority_key():
    """uzh_authors includes only authors with a non-null authority key."""
    dso = FakeDSO(
        handle="h",
        uuid="u",
        fields={
            config.FIELD_AUTHOR: ["External, Alice", "Schmutzler, Armin", "Other, Bob"],
        },
        authorities={
            config.FIELD_AUTHOR: [None, "f45b3ec1-cf2a-43ae-85d4-528afff07a40", None],
        },
    )

    record = normalize_item(dso)

    assert record["authors"] == ["External, Alice", "Schmutzler, Armin", "Other, Bob"]
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
        "Schmutzler, Armin": "f45b3ec1-cf2a-43ae-85d4-528afff07a40",
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


def test_author_authority_map_strips_orcid_placeholder():
    """author_authority_map strips the 'will be referenced::ORCID::' prefix."""
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
        "Theile, Gudrun": "0000-0002-9454-3617",
    }
    assert record["uzh_authors"] == ["Theile, Gudrun"]

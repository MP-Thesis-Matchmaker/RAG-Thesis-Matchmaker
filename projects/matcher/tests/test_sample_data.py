"""The checked-in sample corpus, held to the contracts it claims to satisfy.

Unlike every other test in this directory, these read the *real* files under
`data/samples` rather than writing fixtures into `tmp_path`. That is the point:
those files are what a fresh clone indexes and what CI's offline job runs the
whole pipeline against, and nothing else was checking them.

Nothing here needs a database or a network.

Why it exists: between 2026-07-19 and 2026-08-26 the samples silently stopped
parsing. `ZoraPublication` gained typed authorities, the data kept bare strings,
and `JsonlSourceReader` counts invalid lines instead of failing -- so the offline
corpus quietly became 20 documents rather than 50, and no test, no CI job and no
error message said so. Regenerate with:

    python projects/matcher/scripts/export_samples.py
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from themis_matcher.indexing.sources import (
    PUBLICATIONS_FILE,
    THESES_FILE,
    JsonlSourceReader,
)
from themis_shared.contracts import PostingStatus, ThesisPosting, ZoraPublication

# Resolved from this file rather than the working directory: pytest fixes rootdir
# at the repository root, but a test that silently reads nothing when run from
# elsewhere is the same class of failure this module exists to catch.
SAMPLES = Path(__file__).resolve().parents[3] / "data" / "samples"

EXPECTED_PUBLICATIONS = 30
EXPECTED_POSTINGS = 20


@pytest.fixture(scope="module")
def publications() -> list[ZoraPublication]:
    return list(JsonlSourceReader(SAMPLES).publications())


@pytest.fixture(scope="module")
def postings() -> list[ThesisPosting]:
    return list(JsonlSourceReader(SAMPLES).postings())


def test_every_line_parses() -> None:
    """The regression. A skipped line is invisible at runtime, so assert on the count."""
    reader = JsonlSourceReader(SAMPLES)
    parsed_publications = list(reader.publications())
    parsed_postings = list(reader.postings())

    assert reader.invalid_records == 0, (
        f"{reader.invalid_records} sample line(s) no longer parse against the contracts. "
        "Regenerate: python projects/matcher/scripts/export_samples.py"
    )
    assert len(parsed_publications) == EXPECTED_PUBLICATIONS
    assert len(parsed_postings) == EXPECTED_POSTINGS


def test_neither_kind_is_empty(
    publications: list[ZoraPublication], postings: list[ThesisPosting]
) -> None:
    """The specific shape of the 2026-08 failure: one kind silently reaching zero.

    Separate from the count assertion above on purpose. If someone deliberately
    changes the sample size, that test is meant to be updated; this one never is.
    """
    assert publications, "no publications in the sample corpus"
    assert postings, "no postings in the sample corpus"


def test_no_email_address_is_committed() -> None:
    """The privacy guarantee, enforced rather than promised.

    Real postings carry supervisor emails -- 336 distinct addresses across the
    corpus -- and some departments write a contact into the description too. The
    export strips both. This is the assertion that keeps that true, because a
    leaked address cannot be withdrawn by a later commit.
    """
    pattern = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
    for name in (PUBLICATIONS_FILE, THESES_FILE):
        found = pattern.findall((SAMPLES / name).read_text(encoding="utf-8"))
        assert not found, f"{name} contains {len(found)} email address(es): {found[:3]}"


def test_publications_cover_the_awkward_paths(publications: list[ZoraPublication]) -> None:
    """Coverage the selection promises. A corpus that is all happy-path hides bugs.

    The UZH-author case is the load-bearing one: without a publication that has
    none, `MATCHER_RETRIEVAL_REQUIRE_UZH_AUTHOR` cannot be exercised offline, because
    every record passes the filter whichever way it is set.
    """
    authority_types = {
        authority.type
        for publication in publications
        for authority in publication.author_authority_map.values()
        if authority is not None
    }

    assert any(not p.uzh_authors for p in publications), "no publication without a UZH author"
    assert any(p.uzh_authors for p in publications), "no publication with a UZH author"
    assert "orcid" in authority_types, "no ORCID authority -- the CRIS/ORCID split is untested"
    assert "cris" in authority_types, "no CRIS authority"
    assert any(p.abstract for p in publications), "no publication with an abstract to embed"


def test_postings_cover_the_awkward_paths(postings: list[ThesisPosting]) -> None:
    """Same, for postings.

    The unavailable posting is the load-bearing one here, for the same reason:
    it is what makes `MATCHER_RETRIEVAL_REQUIRE_AVAILABLE_POSTING` mean anything offline.
    """
    unavailable = {PostingStatus.assigned, PostingStatus.private}

    assert any(p.status in unavailable for p in postings), "every posting is available"
    assert any(p.status not in unavailable for p in postings), "no available posting"
    assert any(not p.supervisors for p in postings), "every posting names a supervisor"
    assert any(len(p.degree_levels) > 1 for p in postings), "no posting takes two degree levels"
    assert any(p.description for p in postings), "no posting with a description to embed"

"""Where the indexer reads its records from.

Two implementations behind one protocol, following the repository idiom: the
Postgres reader is what production uses now that ingestion writes rows, and the
JSONL reader stays because `data/samples` is checked-in fixture data and CI has to
run with no database.

Read-only, both of them (invariant 1). Writes to `publication` belong to
`zora/store.py`; writes to `posting` belong to `scraper/store.py`.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from pathlib import Path
from typing import Protocol

from pydantic import BaseModel, ValidationError

from thesis_matchmaker import db
from thesis_matchmaker.contracts import Supervisor, ThesisPosting, ZoraPublication

logger = logging.getLogger(__name__)

PUBLICATIONS_FILE = "publications.jsonl"
THESES_FILE = "theses.jsonl"

# Indexing takes NO position on UZH authorship, deliberately.
#
# It used to: `WHERE cardinality(uzh_authors) > 0` kept ineligible publications out
# of the index entirely, on the reasoning that retrieval hardcoded
# `has_uzh_author: True` and so could never return them -- 123,012 of 214,685 rows
# embedded to produce vectors no query could reach.
#
# That reasoning held only while eligibility was a constant. It is now
# `retrieval_require_uzh_author`, defaulting to off, with
# `retrieval_ranking_strategy` demoting the ineligible rather than excluding them.
# A filter here would make that setting unflippable: turning it off would silently
# return nothing extra until someone re-embedded the whole corpus, which is hours of
# work triggered by an environment variable. Paying ~2.3x the embedding cost once
# buys the ability to change the rule without paying it again.
#
# The cost is real and worth naming: 214,756 publications instead of 91,734, roughly
# 500 MB of additional vectors. It is also what makes the pending uzh_authors
# eligibility decision (CRIS-vs-ORCID authorities) a configuration change rather
# than another re-index.

# Postings the scraper wrote. `status` IS filtered here, and the contrast with the
# publications above is the point: availability is not eligibility. A topic already
# assigned to a student is not a recommendation under any query, from any user, under
# any setting -- there is no configuration in which returning it would be correct, so
# it stays a query rather than a knob. NULL status is kept: 12 of 713 scraped topics
# say nothing about availability, and "the page did not say" is not "taken".
_SELECT_POSTINGS = """
SELECT id, title, description, supervisors, faculty, department, degree_levels,
       status, keywords, language, url, listed_on, source_id, scraped_at
FROM posting
WHERE status IS NULL OR status NOT IN ('assigned', 'private')
ORDER BY id
"""

_SELECT_PUBLICATIONS = """
SELECT id, title, abstract, authors, uzh_authors, author_authority_map, year,
       keywords, department, owning_collection_uuid, language, publication_type,
       doi, url, accessioned
FROM publication
ORDER BY id
"""


class SourceReader(Protocol):
    """What the indexer needs from a source of records."""

    @property
    def label(self) -> str:
        """Human-readable origin, recorded in the index manifest."""
        ...

    @property
    def invalid_records(self) -> int:
        """Records that could not be parsed. Populated while reading."""
        ...

    def publications(self) -> Iterator[ZoraPublication]:
        """Every harvested publication."""
        ...

    def postings(self) -> Iterator[ThesisPosting]:
        """Every open thesis posting."""
        ...


class JsonlSourceReader:
    """Reads the JSONL files that ingestion used to write, one record per line.

    Malformed lines are counted and skipped rather than fatal: one bad record in
    a 22,000-line file should not cost the whole index build.
    """

    def __init__(self, directory: Path | str) -> None:
        self.directory = Path(directory)
        self._invalid = 0

    @property
    def label(self) -> str:
        return str(self.directory)

    @property
    def invalid_records(self) -> int:
        return self._invalid

    def publications(self) -> Iterator[ZoraPublication]:
        yield from self._read(PUBLICATIONS_FILE, ZoraPublication)

    def postings(self) -> Iterator[ThesisPosting]:
        yield from self._read(THESES_FILE, ThesisPosting)

    def _read(self, filename: str, model: type[BaseModel]) -> Iterator:
        path = self.directory / filename
        if not path.exists():
            logger.warning("source file missing, skipping: %s", path)
            return
        with path.open(encoding="utf-8") as handle:
            for line_no, line in enumerate(handle, start=1):
                if not line.strip():
                    continue
                try:
                    yield model.model_validate_json(line)
                except ValidationError as exc:
                    self._invalid += 1
                    logger.warning("skipping invalid line %s:%d: %s", path, line_no, exc)


class PostgresSourceReader:
    """Reads harvested publications from the `publication` table.

    Yields only publications with at least one registered UZH author -- see
    `_SELECT_PUBLICATIONS`. No parse step and so no invalid records: rows were
    validated against `ZoraPublication` on the way in.
    """

    def __init__(self, dsn: str) -> None:
        self.dsn = dsn

    @property
    def label(self) -> str:
        return "postgres"

    @property
    def invalid_records(self) -> int:
        return 0

    def publications(self) -> Iterator[ZoraPublication]:
        with db.connection(self.dsn) as conn:
            for row in conn.execute(_SELECT_PUBLICATIONS):
                yield ZoraPublication(
                    id=row[0],
                    # No `or ""` on title: the column is nullable and so is the
                    # contract field. Substituting an empty string here used to
                    # satisfy a required field by inventing a value, which put
                    # publications with a blank title into the index.
                    title=row[1],
                    abstract=row[2],
                    authors=list(row[3] or []),
                    uzh_authors=list(row[4] or []),
                    author_authority_map=row[5] or {},
                    year=row[6],
                    keywords=list(row[7] or []),
                    department=row[8],
                    owning_collection_uuid=row[9],
                    language=row[10],
                    publication_type=row[11],
                    doi=row[12],
                    url=row[13],
                    accessioned=row[14],
                )

    def postings(self) -> Iterator[ThesisPosting]:
        with db.connection(self.dsn) as conn:
            for row in conn.execute(_SELECT_POSTINGS):
                yield ThesisPosting(
                    id=row[0],
                    # Nullable column, nullable field -- see the note on
                    # publications above.
                    title=row[1],
                    description=row[2],
                    # jsonb comes back already decoded, so these are dicts.
                    supervisors=[Supervisor.model_validate(s) for s in (row[3] or [])],
                    faculty=row[4],
                    department=row[5],
                    degree_levels=list(row[6] or []),
                    status=row[7],
                    keywords=list(row[8] or []),
                    language=row[9],
                    url=row[10],
                    listed_on=row[11],
                    source_id=row[12],
                    scraped_at=row[13],
                )

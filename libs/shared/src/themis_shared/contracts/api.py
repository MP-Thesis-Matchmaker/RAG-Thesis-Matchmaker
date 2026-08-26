"""The shapes that cross the matcher's HTTP boundary.

These live in `themis-shared` rather than in either member because both ends
need them and neither may import the other: `themis-gateway` depends on
`themis-shared` alone, and the whole point of putting HTTP between it and
`themis-matcher` is that the dependency edge no longer exists. A shared contract
is what keeps both sides typed without reintroducing it.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, Field


class IndexRunState(StrEnum):
    """Where one index run got to."""

    running = "running"
    succeeded = "succeeded"
    failed = "failed"


class IndexRunKind(StrEnum):
    """What an index run covered.

    The two record values match `document.source_type`, so a run's kind is also
    the filter its diff and its orphan sweep are scoped by.
    """

    publication = "publication"
    thesis_posting = "thesis_posting"
    all = "all"


class IndexRun(BaseModel):
    """One index run: what it covered, how far it got, and what it wrote.

    `index_manifest` describes the index that is in place; this describes the
    attempts to build it. A manifest row only appears once a run has finished, so
    without this a run that died mid-way is invisible while having already
    written documents.
    """

    id: int
    kind: IndexRunKind
    state: IndexRunState
    source: str = Field(description="SourceReader.label: 'postgres', or a JSONL directory.")
    embedded: int = 0
    skipped: int = 0
    deleted: int = 0
    truncated: int = 0
    invalid_lines: int = 0
    error: str | None = None
    started_at: datetime
    heartbeat_at: datetime = Field(
        description="Bumped at every committed chunk; how a dead run is told from a slow one."
    )
    finished_at: datetime | None = None

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

from themis_shared.contracts.retrieval import SupervisorMatch


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


class MatchRequest(BaseModel):
    """A student's interests, in their own words."""

    query: str = Field(min_length=1, description="Free text; the matcher parses it.")
    top_k: int = Field(default=5, ge=1, le=50)


class MatchResponse(BaseModel):
    """Ranked matches, no prose.

    Already ordered, and **not necessarily by score** -- see `SupervisorMatch`.
    Do not re-sort and assume the order survives.
    """

    matches: list[SupervisorMatch]


class RecommendResponse(BaseModel):
    """A written recommendation, grounded in the same matches."""

    answer: str


class IndexStatus(BaseModel):
    """What is in the index right now, from `index_manifest`."""

    embedding_model: str
    embedding_dim: int
    document_count: int
    sources: str | None = None
    max_seq_length: int | None = None
    truncated_docs: int = 0


class IndexRunAccepted(BaseModel):
    """A trigger was accepted. The work happens after the response.

    Indexing is measured in minutes at steady state and days from cold, so a
    trigger can only ever hand back a receipt. Poll `/v1/index/runs/{run_id}`.
    """

    run_id: int
    kind: IndexRunKind


class ApiError(BaseModel):
    """A refusal the caller can branch on.

    `code` is the machine-readable half: the gateway maps `index_not_built` back
    onto its own exception type rather than matching on prose.
    """

    code: str
    message: str
    run_id: int | None = Field(
        default=None, description="Set on index_run_in_progress: the run that holds the slot."
    )

"""Contracts for the retrieval boundary: the query going in and matches coming out.

These sit between the orchestration layer and the retrieval and ranking
component. The orchestration and LLM steps only ever touch these shapes.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, model_validator

from themis_shared.contracts.sources import DegreeLevel


class ParsedQuery(BaseModel):
    """A student's free-text query after it has been structured.

    This is what the retriever receives. In the LibreChat setup the agent fills
    these fields from the conversation; locally the parse step produces it.
    """

    topics: list[str] = Field(description="Research topics or interests pulled from the query.")
    keywords: list[str] = Field(default_factory=list)
    degree_level: DegreeLevel | None = None
    department: str | None = Field(
        default=None, description="Department, only if the student named one."
    )
    raw_query: str | None = Field(default=None, description="Original text, kept for reference.")


class Evidence(BaseModel):
    """One piece of support behind a recommendation, used for citations.

    Points back to a real publication or posting so the answer stays grounded
    instead of inventing a supervisor.
    """

    source_type: Literal["publication", "thesis_posting"]
    source_id: str = Field(description="Id of the ZoraPublication or ThesisPosting.")
    title: str
    url: str | None = None
    year: int | None = None


class SupervisorMatch(BaseModel):
    """One ranked recommendation from the retrieval and ranking layer.

    This is what the orchestration and LLM steps consume. A returned list is
    already ranked, best first -- but **not necessarily by score**: under the
    default `uzh_first` strategy, `has_uzh_affiliation` outranks similarity, so a
    lower-scored UZH supervisor precedes a higher-scored external researcher.
    Within each tier the key is the *margin* -- the best of `source_scores`
    minus that source's own threshold -- not `score`, because the two sources
    are on different scales. Do not re-sort on `score` and assume the order is
    preserved.
    """

    supervisor: str = Field(description="Name of the recommended supervisor.")
    department: str | None = None
    score: float = Field(
        description=(
            "Cosine similarity in [-1, 1], higher is a better match. Inherited "
            "unchanged from ScoredHit.score -- it is the maximum over this person's "
            "retrieved documents, and a maximum over [-1, 1] is a [-1, 1] value. Not "
            "a probability and not a percentage: it can be negative."
        )
    )
    source_scores: dict[Literal["publication", "thesis_posting"], float] = Field(
        description=(
            "This person's best score per kind of document, one entry for each kind "
            "that retrieved them. The two sources are not on a common scale -- 695 "
            "short postings against 214,756 abstracts, so an arbitrary query lands "
            "closer to *something* among the publications purely from sampling "
            "density -- which means a threshold has to be chosen per source, and a "
            "person clears it if **either** source does. Keeping only the winning "
            "source's score would let a 0.56 publication, just under its own bar, hide "
            "a 0.50 posting comfortably over its. Measured bands are in "
            "docs/score-calibration.md. Required rather than defaulted on purpose: a "
            "default would silently mis-threshold the source it guessed wrong."
        )
    )
    has_uzh_affiliation: bool = Field(
        default=True,
        description=(
            "Whether this person is a registered UZH researcher -- a UZH author on "
            "some retrieved publication, or the named supervisor of a UZH thesis "
            "posting. False means an external co-author surfaced only because "
            "MATCHER_RETRIEVAL_REQUIRE_UZH_AUTHOR is off: relevant work, but nobody a "
            "student here can actually be supervised by, so callers should say so "
            "rather than presenting them as a supervisor. Defaults True because "
            "every producer predating the setting emitted UZH-only matches."
        ),
    )
    matched_topics: list[str] = Field(
        default_factory=list,
        description=(
            "Topics this person matched on. Not computed yet: the retriever leaves it "
            "empty (2026-09-28) rather than copying the query."
        ),
    )
    publication_count: int = Field(
        default=0, description="Supporting publications, one of the ranking signals."
    )
    posting_count: int = Field(
        default=0,
        description=(
            "Thesis postings retrieved for this person by this query. Says nothing "
            "about whether they accept students: the posting query is unthresholded, "
            "so 0 means none of theirs reached the top-k, not that none exist."
        ),
    )
    evidence: list[Evidence] = Field(
        default_factory=list, description="Publications and postings behind the match."
    )

    @model_validator(mode="after")
    def _score_is_the_best_source_score(self) -> SupervisorMatch:
        # `score` is kept, not derived, because every consumer orders on it. The
        # price is two fields that could disagree, so disagreement is refused here
        # rather than discovered as a mis-thresholded candidate downstream.
        if not self.source_scores:
            raise ValueError("source_scores must name at least one source")
        if self.score != max(self.source_scores.values()):
            raise ValueError("score must equal the best of source_scores")
        # Evidence is optional, but when present it is the hits the scores came
        # from, so the two must name the same sources. A posting-backed person with
        # only a publication score would be thresholded on the wrong scale -- or,
        # as the canned FakeRetriever data did, never on the posting one at all.
        evidenced = {item.source_type for item in self.evidence}
        if evidenced and evidenced != set(self.source_scores):
            raise ValueError("source_scores must name exactly the sources in evidence")
        return self

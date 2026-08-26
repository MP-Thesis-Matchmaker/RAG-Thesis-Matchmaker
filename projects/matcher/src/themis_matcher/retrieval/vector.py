"""The real retriever: semantic search over the vector index.

Implements the Retriever protocol the pipeline already depends on, so it
drops in where FakeRetriever sits today. Scoring is vector similarity grouped
per person, then ordered by the configured strategy; richer ranking signals
live in the ranking component later.

UZH affiliation is handled in two places here, and they are not the same
question. `require_uzh_author` decides whether an unaffiliated researcher can be
returned **at all**; `ranking_strategy` decides where they sit when they can.
The default is permissive-but-demoted: reachable, always below any UZH match.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Literal

from themis_matcher.indexing.embedder import Embedder
from themis_matcher.indexing.store import ScoredHit, VectorStore
from themis_shared.contracts import Evidence, ParsedQuery, SupervisorMatch

# How much wider than top_k to search when `require_uzh_author` is on.
#
# pgvector applies metadata filters AFTER the HNSW scan (see the partial-index
# comment in schema.sql), and the two partial indexes key on source_type only. Over
# a full index roughly 43% of publications carry a UZH author, so an unwidened
# filtered query returns about that fraction of top_k and silently comes back short.
# 4x covers the measured ratio with margin. It is a mitigation, not a fix: the fix
# is a partial index matching this predicate, which needs a schema change.
#
# Not applied when the filter is off, where nothing is discarded after the scan.
_FILTERED_OVERFETCH = 4


def _query_text(query: ParsedQuery) -> str:
    """Compose the string that gets embedded for the search."""
    parts = query.topics + query.keywords
    if not parts and query.raw_query:
        parts = [query.raw_query]
    return "; ".join(parts)


class VectorRetriever:
    """Retriever over an Embedder + VectorStore pair built by the indexer."""

    def __init__(
        self,
        embedder: Embedder,
        store: VectorStore,
        *,
        require_uzh_author: bool = False,
        require_available_posting: bool = True,
        ranking_strategy: str = "uzh_first",
    ) -> None:
        self.embedder = embedder
        self.store = store
        self.require_uzh_author = require_uzh_author
        self.require_available_posting = require_available_posting
        self.ranking_strategy = ranking_strategy

    def retrieve(self, query: ParsedQuery, top_k: int = 5) -> list[SupervisorMatch]:
        vector = self.embedder.embed_query(_query_text(query))

        # Postings and publications are filtered differently: degree_level only
        # exists on postings, so one combined query would wrongly drop every
        # publication whenever the student names a level.
        shared: dict[str, str] = {}
        if query.department:
            shared["department"] = query.department
        posting_filters: dict[str, str | bool] = {"source_type": "thesis_posting", **shared}
        if query.degree_level:
            # One boolean per level, not an equality test on the level itself. A
            # posting can be open to several -- 121 of 247 scraped topics read
            # "Bachelor, Master" -- and neither store can filter a list-valued field:
            # jsonb containment will not match a scalar inside an array, and the
            # in-memory store compares with `==`. See posting_to_document.
            posting_filters[f"degree_{query.degree_level.value}"] = True
        if self.require_available_posting:
            # Availability as a hard cut, the posting-side twin of has_uzh_author
            # below: a topic already assigned to a student cannot be recommended to
            # another one. Indexing takes no position -- assigned and private postings
            # are embedded like any other and this filter is what keeps them out, so
            # the rule can be turned off without re-embedding anything.
            #
            # No overfetch to match _FILTERED_OVERFETCH: that number exists because the
            # UZH predicate discards well over half of what the scan returns, while
            # this one discards 17 of 695 postings. Widening top_k here would inflate
            # posting_count per person for a recall problem two orders of magnitude
            # smaller.
            posting_filters["is_available"] = True
        publication_filters: dict[str, str | bool] = {"source_type": "publication", **shared}
        if self.require_uzh_author:
            # Eligibility as a hard cut: an unaffiliated researcher cannot supervise
            # a UZH thesis, so they should not occupy a slot at all.
            publication_filters["has_uzh_author"] = True

        hits = self.store.query(vector, top_k=top_k, filters=posting_filters)
        hits += self.store.query(
            vector,
            top_k=top_k * _FILTERED_OVERFETCH if self.require_uzh_author else top_k,
            filters=publication_filters,
        )

        return self._rank(self._group_by_person(hits, query))[:top_k]

    @staticmethod
    def _source_type(hit: ScoredHit) -> Literal["publication", "thesis_posting"]:
        return "publication" if hit.metadata["source_type"] == "publication" else "thesis_posting"

    @staticmethod
    def _names(hit: ScoredHit, key: str) -> list[str]:
        people = hit.metadata.get(key) or []
        return [str(name) for name in people] if isinstance(people, list) else []

    @staticmethod
    def _persons(hit: ScoredHit) -> list[str]:
        """Whom a hit counts towards: named supervisors, UZH authors, or plain authors.

        Postings fan out the same way publications do. Before the scraper landed this
        read a single `supervisor` string, which was fine against 20 fixtures that all
        named exactly one person and wrong against real pages: co-supervision is
        normal, and a posting naming nobody credits nobody and so disappears from
        every result. That last case is 63 of 247 scraped topics, which is why
        `has_supervisor` exists as a filterable companion rather than this being
        treated as an edge case.

        Publications fall back from `uzh_authors` to `authors`. Without that fallback
        the whole permissive default is inert: a publication with no UZH author
        credits nobody, so it is grouped into nothing and vanishes after being
        embedded and retrieved. 156,300 of the corpus's 161,212 unaffiliated
        publications name authors and become reachable this way; the remaining 4,912
        name nobody at all and stay unreachable, because there is no one to credit.

        Those totals grew on 2026-08-25, when `uzh_authors` narrowed to CRIS-backed
        authors only: unaffiliated went from 123,022 to 161,212. So this fallback
        carries more weight than it used to, and it changes who gets credited on the
        records that moved. Where a publication's only authorities were ORCIDs,
        `uzh_authors` is now empty, so *every* author is credited rather than only
        the ORCID-holders. That is intended -- they are all equally of unknown
        affiliation and all land in the demoted tier -- but the credited set on those
        records is genuinely wider than before, not merely re-ranked.
        """
        if hit.metadata["source_type"] == "thesis_posting":
            return VectorRetriever._names(hit, "supervisors")
        return VectorRetriever._names(hit, "uzh_authors") or VectorRetriever._names(hit, "authors")

    @staticmethod
    def _is_uzh_credit(hit: ScoredHit) -> bool:
        """Whether this hit credits its people *as UZH researchers*.

        Reuses the `has_uzh_author` flag the indexer already writes rather than
        re-deriving it. A posting always counts: it is a UZH thesis advertisement, so
        whoever it names supervises here by construction.
        """
        if hit.metadata["source_type"] == "thesis_posting":
            return True
        return bool(hit.metadata.get("has_uzh_author"))

    def _rank(self, matches: list[SupervisorMatch]) -> list[SupervisorMatch]:
        """Order grouped matches by the configured strategy.

        `uzh_first` is two-level: affiliation, then similarity within each level. It
        is not a score adjustment -- no weight or boost can guarantee an ordering,
        and a UZH supervisor whose work matches slightly less well is still the
        better answer to "who can supervise my thesis here".

        Inert under `require_uzh_author`, where every surviving match is affiliated
        and the first sort key is constant.
        """
        if self.ranking_strategy == "score":
            return sorted(matches, key=lambda m: m.score, reverse=True)
        return sorted(matches, key=lambda m: (m.has_uzh_affiliation, m.score), reverse=True)

    @staticmethod
    def _group_by_person(hits: list[ScoredHit], query: ParsedQuery) -> list[SupervisorMatch]:
        by_person: dict[str, list[ScoredHit]] = defaultdict(list)
        uzh_person: dict[str, bool] = defaultdict(bool)
        for hit in hits:
            # A publication with several UZH co-authors credits each of them.
            uzh_credit = VectorRetriever._is_uzh_credit(hit)
            for person in VectorRetriever._persons(hit):
                by_person[person].append(hit)
                # Affiliation is a property of the person, not of one hit: being a
                # UZH author on any single hit establishes it, even if their other
                # hits are external collaborations.
                uzh_person[person] |= uzh_credit

        matches = []
        for person, person_hits in by_person.items():
            publications = [h for h in person_hits if h.metadata["source_type"] == "publication"]
            postings = [h for h in person_hits if h.metadata["source_type"] == "thesis_posting"]
            departments = [h.metadata.get("department") for h in person_hits]
            matches.append(
                SupervisorMatch(
                    supervisor=person,
                    department=next((str(d) for d in departments if d), None),
                    score=max(h.score for h in person_hits),
                    has_uzh_affiliation=uzh_person[person],
                    matched_topics=query.topics,
                    publication_count=len(publications),
                    posting_count=len(postings),
                    evidence=[
                        Evidence(
                            source_type=VectorRetriever._source_type(h),
                            source_id=h.id,
                            title=h.text.splitlines()[0] if h.text else h.id,
                            url=str(h.metadata["url"]) if h.metadata.get("url") else None,
                            year=int(h.metadata["year"]) if h.metadata.get("year") else None,
                        )
                        for h in person_hits
                    ],
                )
            )
        return matches

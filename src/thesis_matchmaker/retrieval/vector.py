"""The real retriever: semantic search over the vector index.

Implements the Retriever protocol the pipeline already depends on, so it
drops in where FakeRetriever sits today. Scoring here is plain vector
similarity grouped per person; richer ranking signals live in the ranking
component later.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Literal

from thesis_matchmaker.contracts import Evidence, ParsedQuery, SupervisorMatch
from thesis_matchmaker.indexing.embedder import Embedder
from thesis_matchmaker.indexing.store import ScoredHit, VectorStore


def _query_text(query: ParsedQuery) -> str:
    """Compose the string that gets embedded for the search."""
    parts = query.topics + query.keywords
    if not parts and query.raw_query:
        parts = [query.raw_query]
    return "; ".join(parts)


class VectorRetriever:
    """Retriever over an Embedder + VectorStore pair built by the indexer."""

    def __init__(self, embedder: Embedder, store: VectorStore) -> None:
        self.embedder = embedder
        self.store = store

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
        # Only publications with at least one registered UZH researcher are
        # supervisor-eligible; external-only author lists are pre-filtered out.
        publication_filters: dict[str, str | bool] = {
            "source_type": "publication",
            "has_uzh_author": True,
            **shared,
        }

        hits = self.store.query(vector, top_k=top_k, filters=posting_filters)
        hits += self.store.query(vector, top_k=top_k, filters=publication_filters)

        return self._group_by_person(hits, query)[:top_k]

    @staticmethod
    def _source_type(hit: ScoredHit) -> Literal["publication", "thesis_posting"]:
        return "publication" if hit.metadata["source_type"] == "publication" else "thesis_posting"

    @staticmethod
    def _persons(hit: ScoredHit) -> list[str]:
        """Whom a hit counts towards: every named supervisor, or every UZH author.

        Postings fan out the same way publications do. Before the scraper landed this
        read a single `supervisor` string, which was fine against 20 fixtures that all
        named exactly one person and wrong against real pages: co-supervision is
        normal, and a posting naming nobody credits nobody and so disappears from
        every result. That last case is 63 of 247 scraped topics, which is why
        `has_supervisor` exists as a filterable companion rather than this being
        treated as an edge case.
        """
        key = "supervisors" if hit.metadata["source_type"] == "thesis_posting" else "uzh_authors"
        people = hit.metadata.get(key) or []
        return [str(name) for name in people] if isinstance(people, list) else []

    @staticmethod
    def _group_by_person(hits: list[ScoredHit], query: ParsedQuery) -> list[SupervisorMatch]:
        by_person: dict[str, list[ScoredHit]] = defaultdict(list)
        for hit in hits:
            # A publication with several UZH co-authors credits each of them.
            for person in VectorRetriever._persons(hit):
                by_person[person].append(hit)

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
        matches.sort(key=lambda m: m.score, reverse=True)
        return matches

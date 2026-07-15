"""The real retriever: semantic search over the vector index.

Implements the Retriever protocol the pipeline already depends on, so it
drops in where FakeRetriever sits today. Scoring here is plain vector
similarity grouped per person; richer ranking signals live in the ranking
component later.
"""

from __future__ import annotations

import json
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


class ChromaRetriever:
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
            posting_filters["degree_level"] = query.degree_level.value
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
        """Whom a hit counts towards: the posting's supervisor, or every UZH author."""
        if hit.metadata["source_type"] == "thesis_posting":
            supervisor = hit.metadata.get("supervisor")
            return [str(supervisor)] if supervisor else []
        # uzh_authors is a JSON-encoded list (Chroma metadata is scalar-only).
        return json.loads(str(hit.metadata.get("uzh_authors", "[]")))

    @staticmethod
    def _group_by_person(hits: list[ScoredHit], query: ParsedQuery) -> list[SupervisorMatch]:
        by_person: dict[str, list[ScoredHit]] = defaultdict(list)
        for hit in hits:
            # A publication with several UZH co-authors credits each of them.
            for person in ChromaRetriever._persons(hit):
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
                    has_open_position=bool(postings),
                    evidence=[
                        Evidence(
                            source_type=ChromaRetriever._source_type(h),
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

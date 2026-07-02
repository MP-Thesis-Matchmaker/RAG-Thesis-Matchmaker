"""A fake retriever with canned results.

Lets the orchestration, LLM, and CLI work start now, before the real index and
ranker exist. The retrieval and ranking component replaces this later.
"""

from __future__ import annotations

from thesis_matchmaker.contracts import Evidence, ParsedQuery, SupervisorMatch

_CANNED: list[SupervisorMatch] = [
    SupervisorMatch(
        supervisor="Prof. A. Müller",
        department="Department of Computational Linguistics",
        score=0.91,
        matched_topics=["retrieval-augmented generation", "nlp"],
        publication_count=12,
        has_open_position=True,
        evidence=[
            Evidence(
                source_type="publication",
                source_id="zora:12345",
                title="Retrieval-Augmented Generation for Low-Resource QA",
                url="https://www.zora.uzh.ch/id/eprint/12345",
                year=2024,
            ),
            Evidence(
                source_type="thesis_posting",
                source_id="posting:cl-2026-07",
                title="MSc thesis: grounding LLM answers with retrieval",
                url="https://www.cl.uzh.ch/theses/rag-grounding",
            ),
        ],
    ),
    SupervisorMatch(
        supervisor="Dr. B. Rossi",
        department="Department of Informatics",
        score=0.78,
        matched_topics=["information retrieval"],
        publication_count=7,
        has_open_position=False,
        evidence=[
            Evidence(
                source_type="publication",
                source_id="zora:22890",
                title="Dense Retrieval Benchmarks for German Text",
                url="https://www.zora.uzh.ch/id/eprint/22890",
                year=2023,
            ),
        ],
    ),
    SupervisorMatch(
        supervisor="Prof. C. Schmid",
        department="Department of Informatics",
        score=0.64,
        matched_topics=["misinformation detection"],
        publication_count=5,
        has_open_position=True,
        evidence=[
            Evidence(
                source_type="publication",
                source_id="zora:30011",
                title="Detecting Misinformation with Weak Supervision",
                url="https://www.zora.uzh.ch/id/eprint/30011",
                year=2025,
            ),
        ],
    ),
]


class FakeRetriever:
    """Returns fixed recommendations and ignores the query."""

    def retrieve(self, query: ParsedQuery, top_k: int = 5) -> list[SupervisorMatch]:
        return _CANNED[:top_k]

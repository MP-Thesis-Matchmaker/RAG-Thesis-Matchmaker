"""A deterministic, offline query extractor.

No API calls. This is the default so the pipeline, CLI, and tests run with zero
credentials, and the fallback when no LLM is configured. The real extractor
lives in openai_compat.py. Intentionally simple: detect the degree level and
split the rest into rough topics. It is a stand-in, not the final parser.
"""

from __future__ import annotations

import re

from thesis_matchmaker.contracts import DegreeLevel, ParsedQuery

_DEGREE_KEYWORDS: list[tuple[DegreeLevel, tuple[str, ...]]] = [
    (DegreeLevel.phd, ("phd", "ph.d", "doctoral", "doctorate")),
    (DegreeLevel.master, ("master", "msc", "m.sc", "graduate")),
    (DegreeLevel.bachelor, ("bachelor", "bsc", "b.sc", "undergraduate")),
]

_FILLER = (
    "i am interested in",
    "i'm interested in",
    "i am looking for",
    "i would like to",
    "i want to",
    "do a",
    "work on",
    "master's thesis",
    "masters thesis",
    "master thesis",
    "bachelor thesis",
    "phd thesis",
    "a thesis",
    "thesis",
    "in the area of",
    "related to",
    "research on",
    "a project on",
    "project on",
)

_SPLIT = re.compile(r"\s+and\s+|,|;|/")


def _detect_degree(text: str) -> DegreeLevel | None:
    for level, keywords in _DEGREE_KEYWORDS:
        if any(k in text for k in keywords):
            return level
    return None


def _topics(text: str) -> list[str]:
    for phrase in _FILLER:
        text = text.replace(phrase, " ")
    parts = [p.strip(" .") for p in _SPLIT.split(text)]
    return [p for p in parts if len(p) > 2]


class RuleBasedExtractor:
    """Extracts a ParsedQuery with simple heuristics, no LLM."""

    def extract(self, raw_query: str) -> ParsedQuery:
        lowered = raw_query.lower()
        topics = _topics(lowered) or [raw_query.strip()]
        return ParsedQuery(
            topics=topics,
            keywords=[],
            degree_level=_detect_degree(lowered),
            department=None,
            raw_query=raw_query,
        )

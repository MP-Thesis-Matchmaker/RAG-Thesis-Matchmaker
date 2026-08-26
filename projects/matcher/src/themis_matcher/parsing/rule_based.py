"""A deterministic, offline query extractor.

No API calls. This is the default so the pipeline, CLI, and tests run with zero
credentials, and the fallback when no LLM is configured. The real extractor
lives in openai_compat.py. Intentionally simple: detect the degree level and
split the rest into rough topics. It is a stand-in, not the final parser.
"""

from __future__ import annotations

import re

from themis_shared.contracts import DegreeLevel, ParsedQuery

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

# One alternation, longest phrase first so "master's thesis" wins over "thesis"
# at the same position, and case-insensitive so the query's own casing survives
# into the topics ("NLP", not "nlp"). Replacing rather than str.replace also
# means a phrase cannot be eaten out of the middle of a longer word.
_FILLER_RE = re.compile(
    "|".join(re.escape(phrase) for phrase in sorted(_FILLER, key=len, reverse=True)),
    re.IGNORECASE,
)

# "/" is deliberately not a separator: it joins a compound far more often than it
# divides two topics, and splitting on it turned "AI/ML in healthcare" into
# "something about ai" + "ml in healthcare".
_SPLIT = re.compile(r"\s+and\s+|,|;", re.IGNORECASE)
_WHITESPACE = re.compile(r"\s+")

# Trimmed from both ends of every candidate topic. This, not the filler list, is
# what makes the parser robust: cutting a phrase out of a sentence always leaves
# grammatical glue behind ("doing a ... on"), and no filler list ever covers
# every preamble a student might type. Interior glue stays, because "NLP on RAG"
# and "internet of things" are single topics. The degree words are here because
# _detect_degree reads them from the raw text separately, so dropping them from
# the topic list costs nothing.
_GLUE = frozenset(
    {
        "a",
        "am",
        "an",
        "any",
        "anything",
        "are",
        "about",
        "area",
        "bachelor",
        "bsc",
        "do",
        "doctoral",
        "doctorate",
        "doing",
        "done",
        "for",
        "i",
        "i'd",
        "i'm",
        "i've",
        "in",
        "interested",
        "is",
        "like",
        "looking",
        "master",
        "master's",
        "masters",
        "msc",
        "my",
        "of",
        "on",
        "phd",
        "project",
        "projects",
        "related",
        "research",
        "some",
        "something",
        "the",
        "thesis",
        "to",
        "want",
        "wants",
        "work",
        "working",
        "would",
    }
)


def _detect_degree(text: str) -> DegreeLevel | None:
    for level, keywords in _DEGREE_KEYWORDS:
        if any(k in text for k in keywords):
            return level
    return None


def _trim_glue(phrase: str) -> str:
    words = phrase.split()
    while words and words[0].lower().strip(".") in _GLUE:
        words.pop(0)
    while words and words[-1].lower().strip(".") in _GLUE:
        words.pop()
    return " ".join(words)


def _topics(text: str) -> list[str]:
    text = _WHITESPACE.sub(" ", _FILLER_RE.sub(" ", text))
    parts = (_trim_glue(part.strip(" .")) for part in _SPLIT.split(text))
    # Longer than one character, not two: the old threshold silently swallowed
    # exactly the topics a student is most likely to type as an acronym -- AI,
    # ML, IR, HCI.
    return [part for part in parts if len(part) > 1]


class RuleBasedExtractor:
    """Extracts a ParsedQuery with simple heuristics, no LLM."""

    def extract(self, raw_query: str) -> ParsedQuery:
        topics = _topics(raw_query)
        if not topics:
            # Nothing survived: a one-word query, or pure preamble. The raw text
            # is a better topic than none, but an empty string is not a topic.
            stripped = raw_query.strip()
            topics = [stripped] if stripped else []
        return ParsedQuery(
            topics=topics,
            keywords=[],
            degree_level=_detect_degree(raw_query.lower()),
            department=None,
            raw_query=raw_query,
        )

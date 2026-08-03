"""Retrieval metrics, plus the abstention counts for no-match queries.

Standard IR metrics computed over the ranked supervisor list. Relevance is
binary here: a person is either in the annotated set or not.
"""

from __future__ import annotations

import math
import re

_TITLES = re.compile(r"\b(prof|professor|dr|phd|dipl|em)\b\.?", flags=re.IGNORECASE)
_NOISE = re.compile(r"[^\w\s]", flags=re.UNICODE)


def normalize_name(name: str) -> str:
    """Compare names loosely enough to survive formatting differences.

    ZORA writes "Surname, Firstname" while a posting may say "Prof. Firstname
    Surname". Lowercasing, dropping titles and punctuation, and sorting the
    remaining parts makes those two forms comparable.
    """
    cleaned = _NOISE.sub(" ", _TITLES.sub(" ", name.lower()))
    return " ".join(sorted(cleaned.split()))


def _hits(ranked: list[str], relevant: set[str]) -> list[bool]:
    return [normalize_name(name) in relevant for name in ranked]


def recall_at_k(ranked: list[str], relevant: list[str], k: int) -> float:
    """Share of the annotated supervisors that appear in the top k."""
    if not relevant:
        return 0.0
    wanted = {normalize_name(name) for name in relevant}
    found = {name for name in wanted if name in {normalize_name(r) for r in ranked[:k]}}
    return len(found) / len(wanted)


def reciprocal_rank(ranked: list[str], relevant: list[str], k: int) -> float:
    """1/rank of the first correct supervisor, 0 if none in the top k."""
    wanted = {normalize_name(name) for name in relevant}
    for position, hit in enumerate(_hits(ranked[:k], wanted), start=1):
        if hit:
            return 1.0 / position
    return 0.0


def ndcg_at_k(ranked: list[str], relevant: list[str], k: int) -> float:
    """Normalised discounted cumulative gain with binary relevance."""
    if not relevant:
        return 0.0
    wanted = {normalize_name(name) for name in relevant}
    gain = sum(
        1.0 / math.log2(position + 1)
        for position, hit in enumerate(_hits(ranked[:k], wanted), start=1)
        if hit
    )
    ideal = sum(1.0 / math.log2(position + 1) for position in range(1, min(len(wanted), k) + 1))
    return gain / ideal if ideal else 0.0

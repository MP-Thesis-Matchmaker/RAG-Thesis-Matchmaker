"""Run the pipeline over the ground-truth set and report how it did.

Answerable and no-match queries are scored differently and reported separately:
ranking metrics only make sense for the former, abstention only for the latter.
Averaging them together would hide which half is failing.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from thesis_matchmaker.evaluation.dataset import GroundTruthQuery
from thesis_matchmaker.evaluation.metrics import ndcg_at_k, recall_at_k, reciprocal_rank
from thesis_matchmaker.pipeline import Pipeline


@dataclass
class QueryResult:
    """What the system returned for one query, and how it scored."""

    query: GroundTruthQuery
    predicted: list[str]
    abstained: bool
    recall: float = 0.0
    rr: float = 0.0
    ndcg: float = 0.0

    @property
    def correct(self) -> bool:
        """For no-match queries only: did we do the right thing?"""
        return self.abstained if self.query.no_match else self.recall > 0


@dataclass
class Report:
    """Aggregated results, split by query type."""

    top_k: int
    min_score: float
    results: list[QueryResult] = field(default_factory=list)

    @property
    def answerable(self) -> list[QueryResult]:
        return [r for r in self.results if not r.query.no_match]

    @property
    def no_match(self) -> list[QueryResult]:
        return [r for r in self.results if r.query.no_match]

    def _mean(self, values: list[float]) -> float:
        return sum(values) / len(values) if values else 0.0

    def summary(self) -> dict[str, float | int]:
        answerable, negatives = self.answerable, self.no_match
        return {
            "queries": len(self.results),
            "answerable": len(answerable),
            "no_match": len(negatives),
            f"recall@{self.top_k}": self._mean([r.recall for r in answerable]),
            f"mrr@{self.top_k}": self._mean([r.rr for r in answerable]),
            f"ndcg@{self.top_k}": self._mean([r.ndcg for r in answerable]),
            # How often we correctly said "nobody fits".
            "abstention_rate": self._mean([float(r.abstained) for r in negatives]),
            # How often we wrongly stayed silent on an answerable query.
            "false_abstention_rate": self._mean([float(r.abstained) for r in answerable]),
        }

    def format(self) -> str:
        lines = [f"queries: {len(self.results)} (top_k={self.top_k}, min_score={self.min_score})"]
        for key, value in self.summary().items():
            lines.append(
                f"  {key}: {value:.3f}" if isinstance(value, float) else f"  {key}: {value}"
            )
        failures = [r for r in self.results if not r.correct]
        if failures:
            lines.append(f"\nmissed ({len(failures)}):")
            for result in failures[:20]:
                expected = (
                    "no match"
                    if result.query.no_match
                    else ", ".join(result.query.relevant_supervisors)
                )
                got = ", ".join(result.predicted[:3]) or "nothing"
                lines.append(
                    f"  {result.query.id} [{result.query.difficulty}] {result.query.query}"
                )
                lines.append(f"      expected: {expected}")
                lines.append(f"      got:      {got}")
        return "\n".join(lines)


def evaluate(
    queries: list[GroundTruthQuery],
    pipeline: Pipeline,
    top_k: int = 5,
    min_score: float = 0.0,
) -> Report:
    """Run every query and score the ranked supervisors that come back.

    Evaluation deliberately reads the structured matches rather than the written
    answer, so the numbers reflect retrieval and ranking rather than the wording
    the LLM happened to choose. `min_score` mirrors the synthesis threshold: a
    query counts as abstained when nothing clears it.
    """
    report = Report(top_k=top_k, min_score=min_score)
    for query in queries:
        matches = pipeline.run(query.query, top_k=top_k)
        kept = [m for m in matches if m.score >= min_score]
        predicted = [m.supervisor for m in kept]
        result = QueryResult(query=query, predicted=predicted, abstained=not kept)
        if not query.no_match:
            result.recall = recall_at_k(predicted, query.relevant_supervisors, top_k)
            result.rr = reciprocal_rank(predicted, query.relevant_supervisors, top_k)
            result.ndcg = ndcg_at_k(predicted, query.relevant_supervisors, top_k)
        report.results.append(result)
    return report

"""Evaluation: the shared ground-truth set and the metrics we score against it."""

from thesis_matchmaker.evaluation.dataset import (
    DEFAULT_DATASET,
    GroundTruthQuery,
    load_dataset,
)
from thesis_matchmaker.evaluation.runner import QueryResult, Report, evaluate

__all__ = [
    "DEFAULT_DATASET",
    "GroundTruthQuery",
    "QueryResult",
    "Report",
    "evaluate",
    "load_dataset",
]

"""Data contracts shared across the whole system.

Every boundary between components is described here as a pydantic model, so the
data-retrieval, scraping, retrieval, and orchestration parts can be built in
parallel against a fixed shape.
"""

from thesis_matchmaker.contracts.retrieval import Evidence, ParsedQuery, SupervisorMatch
from thesis_matchmaker.contracts.sources import (
    ApplicationProcess,
    DegreeLevel,
    PostingStatus,
    ResearcherProfile,
    Supervisor,
    ThesisPosting,
    ZoraRecord,
)

__all__ = [
    "ApplicationProcess",
    "DegreeLevel",
    "Evidence",
    "ParsedQuery",
    "PostingStatus",
    "ResearcherProfile",
    "Supervisor",
    "SupervisorMatch",
    "ThesisPosting",
    "ZoraRecord",
]

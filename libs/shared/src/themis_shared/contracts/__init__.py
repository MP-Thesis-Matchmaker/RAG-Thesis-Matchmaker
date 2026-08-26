"""Data contracts shared across the whole system.

Every boundary between components is described here as a pydantic model, so the
data-retrieval, scraping, retrieval, and orchestration parts can be built in
parallel against a fixed shape.
"""

from thesis_matchmaker.contracts.retrieval import Evidence, ParsedQuery, SupervisorMatch
from thesis_matchmaker.contracts.sources import (
    ApplicationProcess,
    AuthorAuthority,
    DegreeLevel,
    PostingStatus,
    ResearcherProfile,
    Supervisor,
    ThesisPosting,
    ZoraOrgUnit,
    ZoraPerson,
    ZoraPublication,
)

__all__ = [
    "ApplicationProcess",
    "AuthorAuthority",
    "DegreeLevel",
    "Evidence",
    "ParsedQuery",
    "PostingStatus",
    "ResearcherProfile",
    "Supervisor",
    "SupervisorMatch",
    "ThesisPosting",
    "ZoraOrgUnit",
    "ZoraPerson",
    "ZoraPublication",
]

"""Data contracts shared across the whole system.

Every boundary between components is described here as a pydantic model, so the
data-retrieval, scraping, retrieval, and orchestration parts can be built in
parallel against a fixed shape.
"""

from themis_shared.contracts.api import (
    ApiError,
    IndexRun,
    IndexRunAccepted,
    IndexRunKind,
    IndexRunState,
    IndexStatus,
    MatchRequest,
    MatchResponse,
    RecommendResponse,
)
from themis_shared.contracts.retrieval import Evidence, ParsedQuery, SupervisorMatch
from themis_shared.contracts.sources import (
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
    "ApiError",
    "ApplicationProcess",
    "AuthorAuthority",
    "DegreeLevel",
    "Evidence",
    "IndexRun",
    "IndexRunAccepted",
    "IndexRunKind",
    "IndexRunState",
    "IndexStatus",
    "MatchRequest",
    "MatchResponse",
    "ParsedQuery",
    "PostingStatus",
    "RecommendResponse",
    "ResearcherProfile",
    "Supervisor",
    "SupervisorMatch",
    "ThesisPosting",
    "ZoraOrgUnit",
    "ZoraPerson",
    "ZoraPublication",
]

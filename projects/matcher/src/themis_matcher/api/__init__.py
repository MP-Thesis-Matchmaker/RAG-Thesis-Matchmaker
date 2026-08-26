"""The matcher's HTTP front door.

`app.py` holds routes and status codes; `service.py` holds the process-wide
components they call. Nothing here contains matching logic -- that is
`pipeline/`, `retrieval/` and `indexing/`, exactly as it is for the CLI.
"""

from themis_matcher.api.app import MatcherApiError, create_app
from themis_matcher.api.service import MatcherService, build_service

__all__ = ["MatcherApiError", "MatcherService", "build_service", "create_app"]

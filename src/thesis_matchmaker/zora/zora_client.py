"""
Thin wrapper around dspace_rest_client.DSpaceClient, scoped to the
Faculty of Economics community.

Auth: DSpaceClient reads a personal access token automatically at
construction time, in this order:
  1. the file path in the PERSONAL_API_TOKEN_FILE env var
  2. .dspace-personal-api-token.secret in the current working directory
  3. .dspace-personal-api-token.secret in the user's home directory
No manual header wiring is needed on our side — just make sure one of
those three is set before this module constructs the client.
"""
import logging
import os

from dspace_rest_client.client import DSpaceClient
from requests.adapters import HTTPAdapter
from urllib3.util import Retry

from . import config

logger = logging.getLogger(__name__)


class TimeoutHTTPAdapter(HTTPAdapter):
    """An HTTPAdapter that injects a default timeout to all requests if not specified."""
    def __init__(self, *args, timeout=None, **kwargs):
        self.timeout = timeout
        super().__init__(*args, **kwargs)

    def send(self, request, **kwargs):
        kwargs.setdefault("timeout", self.timeout)
        return super().send(request, **kwargs)


def get_client() -> DSpaceClient:
    """Construct and authenticate a DSpaceClient against the ZORA API."""
    endpoint = os.environ.get("DSPACE_API_ENDPOINT", config.DEFAULT_API_ENDPOINT)
    client = DSpaceClient(api_endpoint=endpoint)

    # Configure request timeout (10s connect, 60s read) and automatic retries
    retries = Retry(
        total=3,
        backoff_factor=2,  # sleeps 2s, 4s, 8s between retries
        status_forcelist=[500, 502, 503, 504],
        raise_on_status=False,
    )
    adapter = TimeoutHTTPAdapter(timeout=(10.0, 60.0), max_retries=retries)
    client.session.mount("http://", adapter)
    client.session.mount("https://", adapter)

    if client.api_token is None:
        raise RuntimeError(
            "No personal API token found. Set PERSONAL_API_TOKEN_FILE to a "
            "file containing the token, or place it at "
            "./.dspace-personal-api-token.secret"
        )

    authenticated = client.authenticate()
    if not authenticated:
        raise RuntimeError(
            "ZORA authentication failed with the provided token. "
            "Token may be expired or invalid — check the ZORA profile page."
        )

    logger.info("Authenticated against %s", endpoint)
    return client


def iter_items(
    client: DSpaceClient,
    scope: str | None = config.DEFAULT_SCOPE_UUID,
    since: str | None = None,
):
    """
    Yield every item, optionally scoped to a single community.

    @param scope: community UUID to restrict to, or None for all of ZORA.
    @param since: optional ISO date string. If given, only items accessioned
                   on or after this date are returned (incremental mode).
                   If None, every item in scope is returned (full mode).
    """
    query = "dspace.entity.type:Publication"
    if since is not None:
        # Matches the range-query pattern on the date-time typed field dc.date.accessioned_dt.
        # Solr requires a fully formatted date-time string (e.g. YYYY-MM-DDTHH:MM:SSZ).
        formatted_since = since
        if len(since) == 10:  # YYYY-MM-DD
            formatted_since = f"{since}T00:00:00Z"
        query = (
            "dspace.entity.type:Publication AND "
            f"dc.date.accessioned_dt:[{formatted_since} TO *]"
        )

    yield from client.search_objects_iter(
        scope=scope,
        dso_type="item",
        query=query,
        sort="dc.date.accessioned,asc",
        embeds=["owningCollection", "mappedCollections"],
    )

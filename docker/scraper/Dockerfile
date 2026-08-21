# The posting scraper: fetches UZH departmental pages, extracts topics, supervisor
# profiles and application procedures from cached HTML, and writes them to Postgres.
# A batch job -- it runs, writes, and exits -- so in the cluster it is a CronJob.
#
# Single-stage, like docker/zora/Dockerfile and unlike docker/indexer/. The
# [scraping] closure is requests / beautifulsoup4 / PyYAML / pypdf / openai: pure
# Python, no torch, so the artefact is small enough that copying only /app/.venv
# into a fresh stage would buy little. Revisit if the render extra is ever installed
# here -- a browser is a different weight class.
FROM python:3.12-slim

# Pinned for the same reason as everywhere else: uv is the tool that reads the lock.
COPY --from=ghcr.io/astral-sh/uv:0.11.17 /uv /uvx /bin/

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_NO_CACHE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    PATH="/app/.venv/bin:$PATH"

WORKDIR /app

COPY pyproject.toml uv.lock README.md ./
COPY src/thesis_matchmaker/ ./src/thesis_matchmaker/
# --extra scraping is the whole point of this image. --no-default-groups still drops
# the dev group, so no pytest and no ruff. The render extra is NOT installed: it
# downloads a chromium build, fetch.py imports playwright lazily, and a scheduled
# run that hits a JS-only page degrades to the static fetch and flags the source
# rather than failing.
RUN uv sync --locked --no-default-groups --no-editable --extra scraping

# The scraper's data tree, and it has to be COPYied rather than left to a mount:
# `uv sync --no-editable` builds the project into a wheel, so nothing outside
# src/thesis_matchmaker/ survives on its own. registry/ names the 103 sources and
# specs/ holds the per-source spec.yaml that spec_engine reads AT RUNTIME -- without
# them the image starts and then finds nothing to scrape.
#
# zora/schema/zora_publication.schema.json is the cautionary precedent: it is not
# declared as package data and so does not reach any image. That file is only a
# documentation mirror; these are load-bearing.
COPY data/scraper/registry/ ./data/scraper/registry/
COPY data/scraper/specs/ ./data/scraper/specs/

# cache/ and var/ are written at runtime. In the cluster they want a PVC so a
# scheduled run does not re-fetch every page it already has -- the same open
# question docs/deployment.md raises about the ZORA raw cache. emptyDir works and
# throws the cache away.
RUN mkdir -p data/scraper/cache data/scraper/var data/scraper/output

# No USER directive, on the same reasoning as docker/zora/Dockerfile: a short-lived
# batch job whose data directory may be a bind mount, where a baked-in UID matches
# the host only by luck. `docker run --user "$(id -u):$(id -g)"` is what keeps
# locally written files owned by the invoking user.

# Default: extract every verified source and write the results. `fetch` is the other
# half and is a separate invocation on purpose -- it is the one that talks to
# uzh.ch, and it is polite (sequential, delayed, honestly identified), so it should
# never be an accident of running the image with no arguments.
ENTRYPOINT ["python", "-m", "thesis_matchmaker.scraper.main"]
CMD ["run", "--resume"]

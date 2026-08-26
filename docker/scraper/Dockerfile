# The posting scraper: fetches UZH departmental pages, extracts topics, supervisor
# profiles and application procedures from cached HTML, and writes them to Postgres.
# A batch job -- it runs, writes, and exits -- so in the cluster it is a CronJob.
#
# Single-stage, like docker/zora/Dockerfile and unlike docker/indexer/. The
# [scraping] closure is requests / beautifulsoup4 / PyYAML / pypdf / openai: pure
# Python, no torch. The render extra IS installed here, browser included -- that
# was a deliberate revisit (2026-08-22): ibw--1 and ivw--4 serve their listings
# JS-only and iff--3's profile follows render client-side, so without a browser
# those sources are permanently FETCH_FAILED however healthy the run. That is
# the "absolut notwendigen Komponenten" test the container Compliance Policy
# asks for: the browser is not a convenience, it is the only way to read three
# of the 103 sources. Headless shell keeps the cost at roughly half of full
# chromium; measured after the change the image is 1.64 GB uncompressed (docker
# images), most of it the --with-deps OS libraries -- still comfortably under
# the 10 GB Harbor project quota, which stores compressed layers besides.
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

# TODO(workspace-split): these COPY lines and the --package flag were updated
# mechanically when the distribution became a uv workspace. A proper per-member
# build context, and a shared builder stage so five images do not each resolve the
# same lock, are the follow-up -- along with the k8s manifests.
COPY pyproject.toml uv.lock ./
COPY libs/shared/ ./libs/shared/
COPY projects/scraper/ ./projects/scraper/
# --extra scraping is the whole point of this image; --extra render is what lets it
# read the three JS-only sources (see the header). --no-default-groups still drops
# the dev group, so no pytest and no ruff.
RUN uv sync --locked --no-default-groups --no-editable \
    --package themis-scraper --extra scraping --extra render

# The browser is baked into the image, not fetched at runtime -- a CronJob must not
# download 100 MB from Microsoft's CDN on every scheduled run, and the cluster may
# not allow it anyway. Three deliberate choices:
#   PLAYWRIGHT_BROWSERS_PATH pins the install to a fixed, world-readable path
#     instead of the invoking user's home -- this image has no USER directive and
#     the runtime UID is whatever `--user` or the pod securityContext says, so a
#     browser cached under /root would be unreadable exactly when it is needed.
#   --with-deps installs the OS libraries chromium needs on python:3.12-slim
#     (libnss3, libatk, fonts, ...); without it the binary exists but will not launch.
#   --only-shell installs the headless shell rather than full chromium -- roughly
#     half the size, and the scraper never opens a headed window.
# Runtime note (k8s/README.md): chromium wants a real /dev/shm; give the pod an
# emptyDir with medium: Memory or it can crash on heavy pages.
ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright
RUN playwright install --with-deps --only-shell chromium

# The scraper's data tree, and it has to be COPYied rather than left to a mount:
# `uv sync --no-editable` builds the project into a wheel, so nothing outside
# projects/scraper/ survives on its own. registry/ names the 103 sources and
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
ENTRYPOINT ["python", "-m", "themis_scraper.main"]
CMD ["run", "--resume"]

FROM python:3.12-slim

# uv, pinned to the version the team runs. This image must install exactly what
# uv.lock resolves, so the tool that reads the lock is itself a dependency worth
# pinning. Installing from version ranges instead would mean the artefact
# deployed to the cluster is built from a fresh resolution every time.
COPY --from=ghcr.io/astral-sh/uv:0.11.17 /uv /uvx /bin/

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_NO_CACHE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    PATH="/app/.venv/bin:$PATH"

WORKDIR /app

# uv.lock is what makes this build reproducible. pyproject.toml, README.md and
# the source are also needed because --no-editable builds the project into a
# wheel rather than linking /app/src -- which is why schema.sql has to be
# declared as package data to survive the trip.
# uv validates the ENTIRE workspace against uv.lock, so every member's manifest
# must be present even though only one member is installed here -- without them uv
# sees a smaller workspace than the lock describes and --locked fails with
# "the lockfile needs to be updated". Manifests only, no source: they change far
# less often than code, so this stays a warm layer.
COPY pyproject.toml uv.lock ./
COPY libs/shared/pyproject.toml       ./libs/shared/
COPY projects/gateway/pyproject.toml  ./projects/gateway/
COPY projects/matcher/pyproject.toml  ./projects/matcher/
COPY projects/scraper/pyproject.toml  ./projects/scraper/
COPY projects/zora/pyproject.toml     ./projects/zora/

# Source for what actually gets installed.
COPY libs/shared/ ./libs/shared/
COPY projects/zora/ ./projects/zora/
# --no-default-groups drops the dev group: no pytest, no ruff in a batch image.
RUN uv sync --locked --no-default-groups --no-editable --package themis-zora

# data/ is expected to be bind-mounted at runtime, but it is now ONLY the
# raw-response cache (data/raw/). Harvest output and the incremental watermark
# go to Postgres via zora/store.py, so nothing is written back into the repo
# checkout. This placeholder just means the image doesn't error out if someone
# runs it without a mount for a quick smoke test.
RUN mkdir -p data/raw

# No USER directive here on purpose: this image is only ever run as a
# short-lived batch job (`docker run --rm ...`), never as a long-lived
# service, so the meaningful security boundary is the container sandbox
# itself, not the in-container UID. What DOES matter is that files written
# to the bind-mounted data/ directory come out owned by the invoking host
# user rather than root — that's handled at `docker run` time with
# `--user "$(id -u):$(id -g)"` (see docs/zora-harvester.md), which also makes
# local runs on any dev machine behave identically without sudo cleanup.
# Baking a fixed UID into the image would only work by coincidence on
# whichever host happens to share it.

# One Dockerfile per deployable role — that is what the docker/<role>/ layout is
# for, and this one is the ZORA harvester: a batch job that runs, writes, exits.
# The remaining roles get their own directories rather than extra entrypoints
# here, because they differ in what has to be installed, not just in how they are
# invoked: projects/matcher/ and projects/gateway/ need the [embeddings] and [mcp]
# extras this image deliberately leaves out, and the posting scraper arrives with
# a dependency set of its own. See the Images section of docs/deployment.md.
#
# docker-compose.yml builds this image for init-db too, overriding the entrypoint.
# That is not the misnomer it looks like: init-db is not a separate role, it is
# this same distribution applying its own schema, and it needs no extras.
#
# ENTRYPOINT is the console script, CMD is its default arguments -- the same
# shape as the other three images. Two reasons beyond consistency:
#
#   Kubernetes maps `command:` onto ENTRYPOINT and `args:` onto CMD, so a chart
#   changing the schedule sets `args: ["--mode", "full"]` and touches nothing
#   else. With `python -m themis_zora.harvest` as the entrypoint, anyone who
#   needed to set `command` would have to restate that incantation exactly, and
#   a typo there is a crash-loop that reads like a Python fault rather than a
#   configuration one.
#
#   `themis-zora-harvest` is the declared interface ([project.scripts]);
#   `themis_zora.harvest` is an internal module path. Pinning a deployment
#   artefact to the latter means renaming the module breaks the image, which is
#   exactly the coupling the boundaries CI job exists to prevent elsewhere.
#
# `python -m themis_zora.harvest` still works and the README still documents it;
# it is simply not what the container runs.
#
# Default: one-shot incremental harvest. A scheduled run is this same invocation
# with a --mode flag, issued by a Kubernetes CronJob — see k8s/.
ENTRYPOINT ["themis-zora-harvest"]
CMD ["--mode", "incremental"]

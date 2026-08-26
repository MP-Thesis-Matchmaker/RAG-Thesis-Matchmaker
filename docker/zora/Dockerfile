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
# TODO(workspace-split): these COPY lines and the --package flag were updated
# mechanically when the distribution became a uv workspace. A proper per-member
# build context, and a shared builder stage so five images do not each resolve the
# same lock, are the follow-up -- along with the k8s manifests.
COPY pyproject.toml uv.lock ./
COPY libs/shared/ ./libs/shared/
COPY projects/zora/ ./projects/zora/
COPY scripts/ ./scripts/
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
# invoked: docker/indexer/ and docker/serving/ need the [embeddings] and [mcp]
# extras this image deliberately leaves out, and the posting scraper arrives with
# a dependency set of its own. See the Images section of docs/deployment.md.
#
# docker-compose.yml builds this image for init-db too, overriding the entrypoint.
# That is not the misnomer it looks like: init-db is not a separate role, it is
# this same distribution applying its own schema, and it needs no extras.
#
# Default: one-shot incremental harvest. A scheduled run is this same invocation
# with a --mode flag, issued by a Kubernetes CronJob — see k8s/.
ENTRYPOINT ["python", "-m", "themis_zora.harvest"]
CMD ["--mode", "incremental"]

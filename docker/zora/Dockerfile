FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Install dependencies. We need pyproject.toml, README.md, and the source code
# present because pip will build the package metadata using setuptools.
COPY pyproject.toml README.md ./
COPY src/thesis_matchmaker/ ./src/thesis_matchmaker/
COPY scripts/ ./scripts/
RUN pip install .

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

# NAME IS NOW A MISNOMER: docker-compose.yml builds this same image for the
# init-db and harvester services, and the MCP deployment will use it too, so it
# is the application image rather than the ZORA harvester's. It wants moving to
# docker/Dockerfile — deferred to the change that deletes scheduler.py.
#
# Default: one-shot incremental harvest. A scheduled run is this same
# invocation with a --mode flag, issued by a Kubernetes CronJob; do NOT
# override the command to run zora.scheduler, which is deprecated.
ENTRYPOINT ["python", "-m", "thesis_matchmaker.zora.harvest"]
CMD ["--mode", "incremental"]

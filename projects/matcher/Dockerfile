# The matcher, in both its roles. `serve` is the HTTP API the gateway and the two
# ingestion jobs talk to -- long-lived, a Deployment plus an in-cluster Service.
# `index` is the batch build -- it runs, writes and exits.
#
# One image, because both roles need the same 2.27 GB model and the same closure;
# the role is chosen by overriding the CMD (compose's `command`, the chart's
# `shellCommand`). The default is `index --source db`, so an unqualified run of
# this image still does what it always did.
#
# The batch role has not become redundant: a cold full index is measured in days
# under the namespace CPU quota, and that is not something to start over HTTP and
# hope the pod survives. Steady-state incremental runs are what the triggers are
# for.
#
# Multi-stage, unlike projects/zora/Dockerfile. On a 426 MB harvester image the
# difference is cosmetic; here the [embeddings] closure is 42 packages and torch
# alone is a few hundred MB, so leaving uv, the build context and a build cache
# in the artefact is real weight. The container Compliance Policy asks images to
# carry "nur die absolut notwendigen Komponenten", and the Harbor project quota
# is 10 GB by default for ALL our images together -- see docs/deployment.md.
FROM python:3.12-slim AS builder

# Pinned for the same reason as everywhere else: uv is the tool that reads the
# lock, so it is a dependency worth pinning.
COPY --from=ghcr.io/astral-sh/uv:0.11.17 /uv /uvx /bin/

ENV UV_NO_CACHE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_COMPILE_BYTECODE=1

WORKDIR /app

# --no-editable builds the project into a wheel instead of linking /app/src,
# which is why schema.sql has to be declared as package data to survive the
# trip, and why the runtime stage below needs nothing but the venv.
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
COPY projects/matcher/ ./projects/matcher/
# --extra embeddings is the whole point of this image: SentenceTransformerEmbedder
# instead of HashEmbedder. --no-default-groups still drops the dev group, so no
# pytest and no ruff. The lock resolves a CPU-only torch on linux (see the
# [tool.uv.sources] comment in pyproject.toml); nothing here asks for that, it is
# a property of uv.lock, which is exactly where such a decision belongs.
RUN uv sync --locked --no-default-groups --no-editable \
    --package themis-matcher --extra embeddings

FROM python:3.12-slim AS runtime

# HF_HOME is where sentence-transformers caches BAAI/bge-m3 -- 2.27 GB, and the
# repository publishes no safetensors, so it is a single pytorch_model.bin. It is
# deliberately NOT baked into the image: a dependency rebuild would re-push
# weights that did not change, against a 10 GB registry quota. In the cluster
# this path is an RWX PVC on the idnas21zb.uzh.ch storage class, shared with the
# serving image, so the download happens once; locally it is a compose volume.
#
# API_HOST must be 0.0.0.0, not config.py's 127.0.0.1 default: a server bound to
# loopback inside a container is unreachable from its own Service. Same reasoning
# as MCP_HOST in projects/gateway/Dockerfile -- a property of running in a
# container rather than a deployment choice, so it belongs in the image.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    HF_HOME=/var/cache/huggingface \
    API_HOST=0.0.0.0 \
    API_PORT=8100 \
    PATH="/app/.venv/bin:$PATH"

WORKDIR /app

COPY --from=builder /app/.venv /app/.venv

# A real UID, unlike projects/zora/Dockerfile. That image argues against a USER
# directive and is right to: it is a local batch job writing to a bind mount,
# where a baked-in UID only matches the host by luck. This one never bind-mounts
# -- it writes to Postgres and to a PVC -- so the argument does not carry over.
# require-run-as-nonroot is Audit rather than Enforce in the ZI cluster, so this
# is not admission-blocking; it is just correct. The chart's
# securityContext.runAsUser should be set to match, and note that an NFS-backed
# PVC may need an fsGroup before this UID can write to HF_HOME.
RUN useradd --system --uid 10001 --create-home --shell /usr/sbin/nologin app \
    && mkdir -p "$HF_HOME" \
    && chown -R app:app "$HF_HOME"

USER 10001

# Documentation only -- EXPOSE publishes nothing by itself. Reachable in the
# cluster through its Service and nothing else: this image must NOT get an
# HTTPRoute. Its index triggers start work measured in hours and it answers
# unauthenticated, so the namespace boundary is the only thing guarding it.
EXPOSE 8100

# The subcommand is in CMD, not ENTRYPOINT, so the serve role is a `command`
# override rather than an entrypoint override: `themis-matcher serve`. The
# default stays `index --source db` -- the harvested publication table, not the
# 50 checked-in samples -- so nothing that ran this image before changes.
ENTRYPOINT ["themis-matcher"]
CMD ["index", "--source", "db"]

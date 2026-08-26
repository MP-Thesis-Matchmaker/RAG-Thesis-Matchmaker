# The serving adapter: the MCP tool server the UZH AI Buddy agent points at.
# Long-lived and read-only -- a Deployment plus a Service, not a job.
#
# It needs one extra: [mcp], to serve the tools. It used to need [embeddings] as
# well, because it embedded the incoming query in-process -- which meant this
# always-on pod carried its own 2.27 GB copy of bge-m3 and peaked around 3.6 GiB,
# against a namespace quota of 4 GiB in total. It now asks the matcher over HTTP,
# so the model, the database and the LLM all live there and this image is the
# thin front door it was supposed to be. See the Images section of
# docs/deployment.md.
FROM python:3.12-slim AS builder

COPY --from=ghcr.io/astral-sh/uv:0.11.17 /uv /uvx /bin/

ENV UV_NO_CACHE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_COMPILE_BYTECODE=1

WORKDIR /app

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
COPY projects/gateway/ ./projects/gateway/
RUN uv sync --locked --no-default-groups --no-editable \
    --package themis-gateway \
    --extra mcp

FROM python:3.12-slim AS runtime

# GATEWAY_MCP_HOST must be 0.0.0.0, not themis_gateway/config.py's 127.0.0.1
# default: a server bound to loopback inside a container is unreachable from its
# own Service. This is the one setting that is a property of running in a
# container rather than a deployment choice, so it belongs in the image and not
# in a ConfigMap.
#
# No HF_HOME any more: this image holds no model and reads no weights cache. The
# RWX requirement on the storage class was driven by this pod sharing that cache
# with the indexer, so with the matcher owning the model on both sides of its own
# process, that requirement is now the matcher's alone.
#
# MATCHER_BASE_URL has no default here on purpose. It is a deployment fact -- the
# in-cluster Service address -- not a property of the image, so it belongs in the
# chart values, and an unset one produces a clear refusal rather than a hopeful
# connection to localhost.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    GATEWAY_MCP_HOST=0.0.0.0 \
    GATEWAY_MCP_PORT=8000 \
    PATH="/app/.venv/bin:$PATH"

WORKDIR /app

COPY --from=builder /app/.venv /app/.venv

RUN useradd --system --uid 10001 --create-home --shell /usr/sbin/nologin app

USER 10001

# Documentation only -- EXPOSE publishes nothing by itself. The tools are served
# at http://<host>:<port>/mcp over streamable HTTP; --stdio exists for a local
# MCP inspector and is not how this is deployed.
EXPOSE 8000

ENTRYPOINT ["themis-gateway-mcp"]

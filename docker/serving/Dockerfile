# The serving adapter: the MCP tool server the UZH AI Buddy agent points at.
# Long-lived and read-only -- a Deployment plus a Service, not a job.
#
# It needs BOTH extras, and that is not an oversight to trim later: [mcp] serves
# the tools, and [embeddings] embeds the incoming query. A query has to be
# embedded with the same model as the corpus, so a serving image without the
# model could answer nothing. Splitting it from the indexer therefore buys
# lifecycle separation rather than size -- one is a batch job that writes and
# exits, the other is always on -- which is reason enough to version and roll
# them independently. See the Images section of docs/deployment.md.
FROM python:3.12-slim AS builder

COPY --from=ghcr.io/astral-sh/uv:0.11.17 /uv /uvx /bin/

ENV UV_NO_CACHE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_COMPILE_BYTECODE=1

WORKDIR /app

# TODO(workspace-split): these COPY lines and the --package flag were updated
# mechanically when the distribution became a uv workspace. A proper per-member
# build context, and a shared builder stage so five images do not each resolve the
# same lock, are the follow-up -- along with the k8s manifests.
COPY pyproject.toml uv.lock ./
COPY libs/shared/ ./libs/shared/
COPY projects/matcher/ ./projects/matcher/
COPY projects/gateway/ ./projects/gateway/
RUN uv sync --locked --no-default-groups --no-editable \
    --package themis-gateway \
    --extra embeddings \
    --extra mcp

FROM python:3.12-slim AS runtime

# MCP_HOST must be 0.0.0.0, not config.py's 127.0.0.1 default: a server bound to
# loopback inside a container is unreachable from its own Service. This is the
# one setting that is a property of running in a container rather than a
# deployment choice, so it belongs in the image and not in a ConfigMap.
#
# HF_HOME is the same cache as the indexer's, and in the cluster the same RWX
# PVC. That sharing is the reason the storage class has to support RWX: an
# always-on serving pod and a scheduled indexer pod both read these weights, and
# re-downloading 2.27 GB on every rollout would make restarts minutes long.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    HF_HOME=/var/cache/huggingface \
    MCP_HOST=0.0.0.0 \
    MCP_PORT=8000 \
    PATH="/app/.venv/bin:$PATH"

WORKDIR /app

COPY --from=builder /app/.venv /app/.venv

RUN useradd --system --uid 10001 --create-home --shell /usr/sbin/nologin app \
    && mkdir -p "$HF_HOME" \
    && chown -R app:app "$HF_HOME"

USER 10001

# Documentation only -- EXPOSE publishes nothing by itself. The tools are served
# at http://<host>:<port>/mcp over streamable HTTP; --stdio exists for a local
# MCP inspector and is not how this is deployed.
EXPOSE 8000

ENTRYPOINT ["themis-gateway-mcp"]

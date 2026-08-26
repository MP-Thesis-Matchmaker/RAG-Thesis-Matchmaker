# The indexer: reads the harvested publication table, embeds each record with the
# real model, and maintains the pgvector index. A batch job -- it runs, writes,
# and exits -- so in the cluster it is a CronJob fired after each harvest.
#
# Multi-stage, unlike docker/zora/Dockerfile. On a 426 MB harvester image the
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
# TODO(workspace-split): these COPY lines and the --package flag were updated
# mechanically when the distribution became a uv workspace. A proper per-member
# build context, and a shared builder stage so five images do not each resolve the
# same lock, are the follow-up -- along with the k8s manifests.
COPY pyproject.toml uv.lock ./
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
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    HF_HOME=/var/cache/huggingface \
    PATH="/app/.venv/bin:$PATH"

WORKDIR /app

COPY --from=builder /app/.venv /app/.venv

# A real UID, unlike docker/zora/Dockerfile. That image argues against a USER
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

# --source db is what a deployed indexer does: read the harvested publication
# table, not the 50 checked-in samples. Override the whole entrypoint (compose
# and the chart's shellCommand both can) to index a directory instead.
ENTRYPOINT ["themis-matcher", "index"]
CMD ["--source", "db"]

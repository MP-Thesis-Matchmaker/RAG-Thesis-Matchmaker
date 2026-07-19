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

# data/ is expected to be bind-mounted at runtime (it's where
# publications.jsonl and state.json live, and where the container writes
# its output back onto the host / repo checkout). This placeholder just means
# the image doesn't error out if someone runs it without a mount for a quick
# smoke test.
RUN mkdir -p data/raw

# No USER directive here on purpose: this image is only ever run as a
# short-lived batch job (`docker run --rm ...`), never as a long-lived
# service, so the meaningful security boundary is the container sandbox
# itself, not the in-container UID. What DOES matter is that files written
# to the bind-mounted data/ directory come out owned by the invoking host
# user rather than root — that's handled at `docker run` time with
# `--user "$(id -u):$(id -g)"` (see .github/workflows and README), which
# also makes local runs on any dev machine behave identically without sudo
# cleanup. Baking a fixed UID into the image would only work by coincidence
# on whichever host happens to share it.

# Default: one-shot harvest (safest for CI / manual use).
# For continuous operation, override the command to:
#   python -m thesis_matchmaker.zora.scheduler
ENTRYPOINT ["python", "-m", "thesis_matchmaker.zora.harvest"]
CMD ["--mode", "incremental"]

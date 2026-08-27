#!/usr/bin/env bash
#
# Run what CI runs, before pushing.
#
# Why this exists. CI installs *less* than a development machine does: `offline`
# and `pgvector` sync `--all-packages` with no extras at all, while the local
# .venv has accumulated `embeddings`, `mcp` and `render`. So a green local
# `pytest` is not evidence about those jobs -- it is evidence about a strictly
# larger environment. That gap has shipped a red build at least twice: an `mcp`
# resolution that removed FastMCP, and a conftest that killed the whole session
# when `bs4` was absent while the one job that installs `bs4` stayed green.
#
# Two modes:
#
#   scripts/check.sh          Fast. Lint, format and tests in the current .venv.
#                             Catches the common failures (formatting, a broken
#                             test) in ~1 minute. Does NOT catch the environment
#                             gap described above.
#
#   scripts/check.sh --ci     Slow, ~5-10 minutes warm. Rehearses every job in
#                             .github/workflows/ci.yml in its own throwaway
#                             environment. This is the one to run before pushing
#                             a branch you intend to merge.
#
# The .venv is never touched in --ci mode. That matters: a uv workspace has a
# single environment, so `uv sync --package themis-scraper` *replaces* .venv
# rather than building a second one -- which would silently uninstall torch and
# cost a multi-GB re-download. UV_PROJECT_ENVIRONMENT redirects each rehearsal
# into a scratch directory instead, and that is what makes this affordable
# enough to actually run.
#
# Python is pinned to 3.11 to match the workflow, not left to whatever `uv`
# would otherwise pick -- a 3.13 rehearsal is a different test.

set -uo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.." || exit 1

PYTHON_VERSION=3.11
ENV_ROOT="${TMPDIR:-/tmp}/themis-ci-envs"

FAILED=()
PASSED=()

step() { printf '\n\033[1m── %s\033[0m\n' "$1"; }

# Run one command, record the outcome, and keep going. Every job reports rather
# than the first failure hiding the rest -- the same reason the boundaries matrix
# sets fail-fast: false.
run() {
    local label="$1"
    shift
    if "$@"; then
        PASSED+=("$label")
    else
        FAILED+=("$label")
        printf '\033[31mFAILED: %s\033[0m\n' "$label"
    fi
}

# Sync into a named scratch environment and leave UV_PROJECT_ENVIRONMENT pointing
# at it for the caller's subsequent `uv run --no-sync` invocations.
use_env() {
    local name="$1"
    shift
    export UV_PROJECT_ENVIRONMENT="$ENV_ROOT/$name"
    uv sync --locked --python "$PYTHON_VERSION" "$@" >/dev/null || return 1
}

fast_checks() {
    step "ruff check"
    run "lint" uv run --no-sync ruff check .

    step "ruff format --check"
    run "format" uv run --no-sync ruff format --check .

    step "pytest"
    run "tests" uv run --no-sync pytest
}

# --- fast mode ---------------------------------------------------------------

if [[ "${1:-}" != "--ci" ]]; then
    printf 'Fast checks against the current .venv.\n'
    printf 'Before pushing a branch to merge, run: scripts/check.sh --ci\n'
    fast_checks
else

# --- full rehearsal ----------------------------------------------------------

    printf 'Rehearsing every CI job. Environments live in %s\n' "$ENV_ROOT"
    printf 'The repository .venv is not touched.\n'

    # offline: --all-packages, no extras. Lint and format live here because this
    # is the job that runs them in CI.
    step "job: offline (all packages, no extras)"
    if use_env offline --all-packages; then
        run "offline/lint" uv run --no-sync ruff check .
        run "offline/format" uv run --no-sync ruff format --check .
        run "offline/tests" uv run --no-sync pytest
    else
        FAILED+=("offline/install")
    fi

    # pgvector: needs a real Postgres. Skipped rather than failed when none is
    # configured, so this script stays runnable on a laptop with no server.
    #
    # THEMIS_TEST_DATABASE_URL, not DATABASE_URL: these fixtures TRUNCATE between
    # tests, and DATABASE_URL is the variable most likely to be pointing at the
    # 215,451-document production index in whatever shell you are sitting in.
    # Requiring a separate name, and refusing anything not ending in _test, means
    # the destructive suite cannot be aimed at production by inheritance.
    step "job: pgvector"
    TEST_DSN="${THEMIS_TEST_DATABASE_URL:-}"
    if [[ -z "$TEST_DSN" ]]; then
        printf 'skipped: set THEMIS_TEST_DATABASE_URL to rehearse this job\n'
    elif [[ "${TEST_DSN##*/}" != *_test ]]; then
        printf '\033[31mrefusing: THEMIS_TEST_DATABASE_URL does not name a database ending in _test\033[0m\n'
        FAILED+=("pgvector/unsafe-dsn")
    elif use_env pgvector --all-packages; then
        export DATABASE_URL="$TEST_DSN"
        run "pgvector/schema" uv run --no-sync themis-init-db
        run "pgvector/tests" uv run --no-sync pytest
        unset DATABASE_URL
    else
        FAILED+=("pgvector/install")
    fi

    # boundaries: each member installed alone, so a cross-member import fails
    # with ModuleNotFoundError before any test runs.
    #
    # The import target is the third field rather than being derived as
    # themis_$member, because the scraper's package __init__ imports nothing --
    # the bare package import would pass while its console script was unusable,
    # which is exactly the bug this leg was added to catch.
    for leg in "shared:themis-shared:themis_shared:libs/shared/tests" \
               "matcher:themis-matcher:themis_matcher:projects/matcher/tests" \
               "gateway:themis-gateway:themis_gateway:projects/gateway/tests" \
               "zora:themis-zora:themis_zora:projects/zora/tests" \
               "scraper:themis-scraper:themis_scraper.main:projects/scraper/tests"; do
        IFS=: read -r member package import_target tests <<<"$leg"
        step "job: boundaries / $member"
        if use_env "boundaries-$member" --package "$package"; then
            run "boundaries/$member/import" \
                uv run --no-sync python -c "import $import_target"
            run "boundaries/$member/tests" uv run --no-sync pytest "$tests"
        else
            FAILED+=("boundaries/$member/install")
        fi
    done

    # wheels: schema.sql is package data, and an editable install resolves it
    # through the source tree whether or not it is declared. Only a built wheel
    # proves the container image will find it.
    step "job: wheels"
    unset UV_PROJECT_ENVIRONMENT
    if uv build --all-packages >/dev/null 2>&1; then
        run "wheels/package-data" bash -c \
            "python -m zipfile -l dist/themis_shared-*-py3-none-any.whl | grep -q 'themis_shared/schema.sql'"
        run "wheels/resolvable" bash -c \
            "uv run --isolated --no-project --with dist/themis_shared-*-py3-none-any.whl \
             python -c \"
from importlib import resources
sql = resources.files('themis_shared').joinpath('schema.sql').read_text(encoding='utf-8')
assert 'create table' in sql.lower(), 'schema.sql is present but not readable as DDL'
\""
    else
        FAILED+=("wheels/build")
    fi
fi

# --- report ------------------------------------------------------------------

printf '\n\033[1m── summary\033[0m\n'
for name in "${PASSED[@]:-}"; do
    [[ -n "$name" ]] && printf '  \033[32mok\033[0m    %s\n' "$name"
done
for name in "${FAILED[@]:-}"; do
    [[ -n "$name" ]] && printf '  \033[31mFAIL\033[0m  %s\n' "$name"
done

if ((${#FAILED[@]})); then
    printf '\n\033[31m%d check(s) failed.\033[0m\n' "${#FAILED[@]}"
    exit 1
fi
printf '\n\033[32mAll checks passed.\033[0m\n'

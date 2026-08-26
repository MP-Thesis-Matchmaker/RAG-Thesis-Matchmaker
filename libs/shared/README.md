# themis-shared

What the other four distributions agree on. This is the only library in the
workspace: the four under `projects/` are deployables, and none of them may
import another, so anything more than one of them needs lives here.

| Module | Role |
|---|---|
| [`contracts/`](src/themis_shared/contracts/README.md) | Every data model, harvester output shapes included. Imports nothing of ours — it is the base of the dependency graph |
| `config.py` | `Settings`, loaded from the environment and an optional `.env` |
| `db.py` | Postgres connection pooling, and the pgvector literal helper |
| `schema.py` | Applies `schema.sql` idempotently, fingerprint-guarded |
| `schema.sql` | All ten tables, in one file with one fingerprint |
| `initdb.py` | The `themis-init-db` command |

## Install and run

```bash
uv sync --package themis-shared
uv run themis-init-db            # idempotent; --reset drops every table first
```

## Two things worth knowing

**`schema.sql` is package data.** `schema.py` resolves it with
`importlib.resources` against the package name, so `[tool.setuptools.package-data]`
in `pyproject.toml` is load-bearing. Drop that entry and every test still passes —
an editable install resolves through the source tree — while a built wheel fails at
runtime, first inside the cluster's init-db Job. CI asserts against the wheel for
exactly this reason.

**The fingerprint ignores comments.** `schema.py` normalises the DDL before
hashing, so editing a comment does not demand a `--reset`. That is deliberate: a
tool that prices correct documentation at "drop the database" gets incorrect
documentation.

## Why init-db lives here

The cluster applies the schema from a Job, and that Job should run from an image
whose closure is `pydantic` plus `psycopg`. Were the command part of
`themis-matcher`, the Job would carry `httpx`, `sentence-transformers` and `torch`
to run one `CREATE TABLE`.

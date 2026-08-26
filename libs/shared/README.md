# themis-shared

What the other four distributions agree on. This is the only library in the
workspace: the four under `projects/` are deployables, and none of them may
import another, so anything more than one of them needs lives here.

| Module | Role |
|---|---|
| [`contracts/`](src/themis_shared/contracts/README.md) | Every data model, harvester output shapes included. Imports nothing of ours — it is the base of the dependency graph |
| `config.py` | `Settings`, loaded from the environment and an optional `.env` |
| `db.py` | Postgres connection pooling, and the pgvector literal helper |
| `schema.py` | Applies `schema.sql` idempotently, fingerprint-guarded. Also creates the eleventh table, `schema_version` |
| `schema.sql` | Ten of the eleven tables, in one file with one fingerprint |
| `initdb.py` | The `themis-init-db` command |

## Install and run

```bash
uv sync --package themis-shared
uv run themis-init-db            # idempotent; --reset drops every table first
```

## Four things worth knowing

**`schema_version` is not in `schema.sql`, on purpose.** It is the one table
`schema.py` creates itself, because it is the thing that *describes* `schema.sql` —
it holds the fingerprint of the file, so it cannot be inside the file it fingerprints.
A live database therefore has eleven tables where `schema.sql` declares ten.
`schema.py:existing_tables` excludes it by name for the same reason: it is
bookkeeping, not part of the managed schema.

**`schema.sql` is package data.** `schema.py` resolves it with
`importlib.resources` against the package name, so `[tool.setuptools.package-data]`
in `pyproject.toml` is load-bearing. Drop that entry and every test still passes —
an editable install resolves through the source tree — while a built wheel fails at
runtime, first inside the cluster's init-db Job. CI asserts against the wheel for
exactly this reason.

**Adding a table costs a migration, not a `--reset`.** `apply()` runs `schema.sql`
whole or refuses: none of its `CREATE TABLE`s carry `IF NOT EXISTS`, and any
fingerprint drift raises `SchemaChangedError`. The only route it offers is
`--reset`, which drops the 214,756 embedded publication documents with it. The
forward alternative is a dated file under [`docs/migrations/`](../../docs/migrations/):
apply the delta, then stamp `schema_version`. Run one on every database, including
each developer's -- and never stamp a fingerprint the tables do not match, or
`require_current()` starts passing while the next query dies on a missing relation.

**The fingerprint ignores comments.** `schema.py` normalises the DDL before
hashing, so editing a comment does not demand a `--reset`. That is deliberate: a
tool that prices correct documentation at "drop the database" gets incorrect
documentation.

## Why init-db lives here

The cluster applies the schema from a Job, and that Job should run from an image
whose closure is `pydantic` plus `psycopg`. Were the command part of
`themis-matcher`, the Job would carry `httpx`, `sentence-transformers` and `torch`
to run one `CREATE TABLE`.

# docker/ — local development only

**Nothing here is deployed.** One file lives in this directory:

| File | What it does |
|---|---|
| `postgres/init/10-test-database.sql` | Creates the `matchmaker_test` database on first initialisation of an empty Postgres volume |

It is mounted into the stock `pgvector/pgvector:pg16` image by
[`docker-compose.yml`](../docker-compose.yml) at
`/docker-entrypoint-initdb.d`, which Postgres runs once, only when its data
volume is empty. There is no Dockerfile here and no image is built from this
directory.

## Why it is not in a project

The four deployable images moved next to the projects they build —
`projects/matcher/Dockerfile`, `projects/gateway/Dockerfile`,
`projects/zora/Dockerfile`, `projects/scraper/Dockerfile`. This file did not
follow them, because it belongs to no single project: it provisions the database
that *every* member's tests run against. It is also not Python and is consumed by
compose rather than by any distribution, so putting it inside `libs/shared/`
would file workspace infrastructure as though it were library source.

## Why the test database exists at all

The Postgres fixtures in the root [`conftest.py`](../conftest.py) TRUNCATE
between tests, so they refuse to run against a database whose name does not end
in `_test`. That guard is what stops `pytest` in a shell with a development
`DATABASE_URL` exported from wiping a multi-hour ZORA harvest. This file is the
other half of that arrangement — it makes a correctly named database exist
locally, so the guard is something you satisfy rather than something you
override.

CI does the same thing differently: its `pgvector` service container sets
`POSTGRES_DB: matchmaker_test` directly, so it never needs this script.

```bash
docker compose up -d postgres     # creates matchmaker and matchmaker_test

# point the suite at the test database, never at matchmaker
DATABASE_URL=postgresql://matchmaker:matchmaker@localhost:5432/matchmaker_test \
  uv run pytest
```

If you have an existing volume from before this file was added, the script will
not run — Postgres only executes `docker-entrypoint-initdb.d` on an empty data
directory. Create the database by hand, or recreate the volume.

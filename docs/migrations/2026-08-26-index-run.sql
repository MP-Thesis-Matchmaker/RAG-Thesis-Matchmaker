-- Add the index_run table.  3d4f0475bf80 -> 032028c3e280
--
-- Why this file exists at all: schema.sql is applied whole or not at all. Its
-- nine (now ten) CREATE TABLE statements carry no IF NOT EXISTS, and
-- schema.apply() refuses any database whose recorded fingerprint differs from
-- the file's. The only path it offers is `themis-init-db --reset`, which drops
-- every table -- including 214,756 publication documents that cost days of CPU
-- to embed. This migration is the forward alternative: apply the delta, then
-- stamp the fingerprint so the guard agrees again.
--
-- Run it once per database, including each developer's:
--
--     psql "$DATABASE_URL" -f docs/migrations/2026-08-26-index-run.sql
--     themis-init-db          # must then report no change
--
-- A scratch database with nothing worth keeping does not need this -- a plain
-- `themis-init-db --reset` gets there faster.
--
-- Everything below is one transaction, and it refuses to run against a database
-- that is not on 3d4f0475bf80. Stamping a fingerprint the tables do not match
-- would be worse than leaving the database alone: require_current() would start
-- passing, and the failure would move to whichever query first touched a
-- relation that was never created.

BEGIN;

DO $$
DECLARE
    current_fingerprint text;
BEGIN
    SELECT fingerprint INTO current_fingerprint FROM schema_version WHERE id = 1;
    IF current_fingerprint IS DISTINCT FROM '3d4f0475bf80' THEN
        RAISE EXCEPTION
            'this migration expects schema 3d4f0475bf80, but this database is on %. '
            'Nothing has been changed.', coalesce(current_fingerprint, '(none)');
    END IF;
END
$$;

CREATE TABLE index_run (
    id            bigserial PRIMARY KEY,
    kind          text        NOT NULL,
    state         text        NOT NULL CHECK (state IN ('running', 'succeeded', 'failed')),
    source        text        NOT NULL,
    embedded      int         NOT NULL DEFAULT 0,
    skipped       int         NOT NULL DEFAULT 0,
    deleted       int         NOT NULL DEFAULT 0,
    truncated     int         NOT NULL DEFAULT 0,
    invalid_lines int         NOT NULL DEFAULT 0,
    error         text,
    started_at    timestamptz NOT NULL DEFAULT now(),
    heartbeat_at  timestamptz NOT NULL DEFAULT now(),
    finished_at   timestamptz
);

CREATE UNIQUE INDEX index_run_single_active ON index_run ((true)) WHERE state = 'running';

CREATE INDEX index_run_started_at ON index_run (started_at DESC);

UPDATE schema_version SET fingerprint = '032028c3e280', applied_at = now() WHERE id = 1;

COMMIT;

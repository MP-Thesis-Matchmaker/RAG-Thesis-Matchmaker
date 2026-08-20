-- A separate database for the test suite.
--
-- The pgvector fixtures are destructive between tests, so they refuse to run
-- against a database whose name does not end in _test. Without this, running
-- pytest with a development DATABASE_URL exported would clear it.
--
-- This script only runs when Postgres initialises an empty data volume. On an
-- existing volume, create the database by hand:
--   docker compose exec postgres createdb -U matchmaker matchmaker_test
CREATE DATABASE matchmaker_test OWNER matchmaker;

"""Tests for applying schema.sql, and for refusing to apply it destructively."""

from __future__ import annotations

import pytest

from thesis_matchmaker import db, schema


def test_fingerprint_is_stable_and_sensitive() -> None:
    assert schema.fingerprint("create table a ()") == schema.fingerprint("create table a ()")
    assert schema.fingerprint("create table a ()") != schema.fingerprint("create table b ()")


def test_shipped_schema_is_readable_as_package_data() -> None:
    """schema.sql has to survive `pip install` for the init-db Job to work."""
    sql_text = schema.schema_sql()
    assert "CREATE TABLE document" in sql_text
    assert "CREATE TABLE publication" in sql_text


def test_apply_is_idempotent(dsn: str) -> None:
    first = schema.apply(dsn)
    assert first.fingerprint == schema.fingerprint()
    second = schema.apply(dsn)
    assert second.applied is False
    assert second.fingerprint == first.fingerprint


def test_changed_schema_is_refused_rather_than_silently_skipped(dsn: str) -> None:
    schema.apply(dsn)
    with db.connection(dsn) as conn:
        conn.execute("UPDATE schema_version SET fingerprint = 'deadbeef' WHERE id = 1")
    with pytest.raises(schema.SchemaChangedError, match="--reset"):
        schema.apply(dsn)
    # --reset is the documented way out, and it works.
    assert schema.apply(dsn, reset=True).applied is True


def test_unmanaged_tables_are_refused_with_a_useful_message(dsn: str) -> None:
    """A hand-applied schema has no fingerprint; do not crash on DuplicateTable."""
    schema.apply(dsn, reset=True)
    with db.connection(dsn) as conn:
        conn.execute("DROP TABLE schema_version")
    with pytest.raises(schema.SchemaChangedError, match="no recorded schema fingerprint"):
        schema.apply(dsn)
    schema.apply(dsn, reset=True)


def test_reset_reports_what_it_dropped(dsn: str) -> None:
    schema.apply(dsn)
    result = schema.apply(dsn, reset=True)
    assert "document" in result.dropped
    assert "publication" in result.dropped
    assert result.applied is True


def test_require_current_passes_on_an_up_to_date_database(dsn: str) -> None:
    schema.apply(dsn)

    assert schema.require_current(dsn) is None


def test_require_current_refuses_a_stale_database(dsn: str) -> None:
    """The failure a harvest hit after fetching 2,018 records, caught in one round-trip.

    A plain RuntimeError, not SchemaChangedError: the callers that need this have a
    one-line handler for operator conditions, and cli.py maps SchemaChangedError
    onto its own init-db-specific SystemExit.
    """
    schema.apply(dsn)
    with db.connection(dsn) as conn:
        conn.execute("UPDATE schema_version SET fingerprint = 'deadbeef' WHERE id = 1")

    with pytest.raises(RuntimeError, match="init-db --reset") as exc:
        schema.require_current(dsn)

    # Both fingerprints named, so the message says what to compare, not just that
    # something differs.
    assert "deadbeef" in str(exc.value)
    assert schema.fingerprint() in str(exc.value)
    assert not isinstance(exc.value, schema.SchemaChangedError)

    schema.apply(dsn, reset=True)


def test_require_current_on_a_virgin_database_says_init_db_not_reset(dsn: str) -> None:
    """Nothing to reset when nothing was ever applied -- --reset would be misleading advice."""
    schema.drop_all_tables(dsn)

    with pytest.raises(RuntimeError, match="no schema applied") as exc:
        schema.require_current(dsn)

    assert "--reset" not in str(exc.value)

    schema.apply(dsn, reset=True)

"""Tests for applying schema.sql, and for refusing to apply it destructively."""

from __future__ import annotations

import pytest

from thesis_matchmaker import db, schema


def test_fingerprint_is_stable_and_sensitive() -> None:
    assert schema.fingerprint("create table a ()") == schema.fingerprint("create table a ()")
    assert schema.fingerprint("create table a ()") != schema.fingerprint("create table b ()")


def test_fingerprint_ignores_comments_and_formatting() -> None:
    """Documentation is not schema, so editing it must not demand a reset.

    Before this, correcting a comment raised SchemaChangedError on the next
    init-db -- which is why one wrong comment sat in schema.sql deliberately
    unfixed. A tool that prices correct documentation that high gets incorrect
    documentation.
    """
    plain = "create table a (b int)"

    assert schema.fingerprint(plain) == schema.fingerprint("create table a (b int) -- a note")
    assert schema.fingerprint(plain) == schema.fingerprint("-- leading\ncreate table a (b int)")
    assert schema.fingerprint(plain) == schema.fingerprint("create   table\n\n  a (b int)\n")
    assert schema.fingerprint(plain) == schema.fingerprint("create /* mid */ table a (b int)")
    # Still sensitive to the thing it exists to detect.
    assert schema.fingerprint(plain) != schema.fingerprint("create table a (b text)")


def test_an_apostrophe_inside_a_comment_does_not_open_a_string() -> None:
    """The trap this normaliser has to survive.

    schema.sql is full of possessives inside comments -- "the scraper's topic_id",
    "the record's own seed". Anything that finds string literals before it finds
    comments reads those apostrophes as quote delimiters and mis-parses the rest of
    the file. `grep -o "'[^']*'"` on the real schema returns
    `'s topic_id: sha1 over the source url and the record'`, which is not a literal
    at all. Comments have to win first, left to right.
    """
    with_possessive = "create table a (b int) -- don't reorder this\ncreate table c (d int)"
    without = "create table a (b int)\ncreate table c (d int)"

    assert schema.fingerprint(with_possessive) == schema.fingerprint(without)


def test_a_double_dash_inside_a_string_literal_is_not_a_comment() -> None:
    """The mirror-image failure: stripping too much, silently changing the DDL."""
    assert schema.fingerprint("create table a (b text default '--x')") != schema.fingerprint(
        "create table a (b text default '')"
    )
    # An escaped quote must not end the literal early and expose its tail.
    assert schema.fingerprint("create table a (b text default 'it''s -- fine')") == (
        schema.fingerprint("create table a (b text default 'it''s -- fine')")
    )


def test_the_real_schema_still_fingerprints_and_keeps_its_ddl() -> None:
    """Guards a normaliser that eats too much: the DDL has to survive stripping."""
    sql_text = schema.schema_sql()

    assert schema.fingerprint(sql_text) == schema.fingerprint(sql_text)
    normalized = schema._normalize_sql(sql_text)
    for statement in ("CREATE TABLE document", "CREATE TABLE publication", "vector(1024)"):
        assert statement in normalized
    assert "--" not in normalized


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

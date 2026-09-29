"""Tests for the raw-dump cache, focused on routing a dump back to its step.

`write_raw_dump` puts the kind in the filename precisely so a replay can tell
`<ts>_persons.jsonl` from `<ts>_full.jsonl`. Nothing in a dump's *contents* says
which it is -- both are plain JSONL objects -- so the name is the only signal,
and these pin what happens when it does and does not carry one.
"""

from __future__ import annotations

import pytest

from themis_zora import raw_dump


@pytest.fixture()
def raw_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("ZORA_DATA_DIR", str(tmp_path))
    return tmp_path / "raw"


@pytest.mark.parametrize("kind", raw_dump.KINDS)
def test_a_written_dump_can_always_be_routed_back(raw_dir, kind):
    """The round trip is the actual contract: whatever write produces, dump_kind reads."""
    path = raw_dump.write_raw_dump([{"uuid": "x"}], kind)

    assert raw_dump.dump_kind(path) == kind


def test_kinds_cover_both_publication_modes_and_both_mirrors():
    """A new dump kind that forgets to join KINDS would be silently unroutable."""
    assert set(raw_dump.KINDS) == {"full", "incremental", "persons", "orgunits"}


@pytest.mark.parametrize(
    "name",
    [
        "copy.jsonl",  # hand-copied out of data/raw/
        "20260825T091305Z.jsonl",  # timestamp but no kind
        "persons.jsonl",  # kind, but not as a _suffix
        "20260825T091305Z_people.jsonl",  # plausible, not a kind we write
    ],
)
def test_a_name_without_a_kind_is_refused_rather_than_guessed(name):
    """Guessing would defer the failure to the validator, with a worse message."""
    with pytest.raises(RuntimeError, match="--dump-kind"):
        raw_dump.dump_kind(name)


def test_the_kind_is_read_from_the_basename_not_the_path(tmp_path):
    """A dump living under a directory called `persons/` is still a publication dump."""
    path = tmp_path / "persons" / "20260821T151956Z_full.jsonl"

    assert raw_dump.dump_kind(str(path)) == "full"


def test_a_dump_only_gets_its_final_name_once_the_block_exits_cleanly(raw_dir):
    with raw_dump.open_raw_dump("full") as dump:
        dump.write({"uuid": "x"})
        assert [p.name for p in raw_dir.iterdir()] == [
            dump.path.rsplit("/", 1)[-1] + raw_dump.PARTIAL_SUFFIX
        ]
    assert [p.name for p in raw_dir.iterdir()] == [dump.path.rsplit("/", 1)[-1]]


def test_a_crash_leaves_only_a_partial_dump(raw_dir):
    """A truncated dump under a routable name would replay as a whole snapshot."""
    with pytest.raises(RuntimeError, match="boom"):
        with raw_dump.open_raw_dump("full") as dump:
            dump.write({"uuid": "x"})
            raise RuntimeError("boom")

    (leftover,) = raw_dir.iterdir()
    assert leftover.name.endswith("_full.jsonl.partial")
    with pytest.raises(RuntimeError, match="incomplete dump"):
        raw_dump.dump_kind(str(leftover))


def test_dumps_are_off_unless_switched_on(monkeypatch):
    monkeypatch.delenv("ZORA_WRITE_RAW_DUMP", raising=False)
    assert raw_dump.enabled() is False
    monkeypatch.setenv("ZORA_WRITE_RAW_DUMP", "true")
    assert raw_dump.enabled() is True

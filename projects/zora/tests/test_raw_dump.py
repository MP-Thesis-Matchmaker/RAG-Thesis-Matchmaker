"""Tests for the raw-dump cache, focused on routing a dump back to its step.

`write_raw_dump` puts the kind in the filename precisely so a replay can tell
`<ts>_persons.jsonl` from `<ts>_full.jsonl`. Nothing in a dump's *contents* says
which it is -- both are plain JSONL objects -- so the name is the only signal,
and these pin what happens when it does and does not carry one.
"""

from __future__ import annotations

import pytest

from themis_zora import config, raw_dump


@pytest.fixture()
def raw_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "RAW_DIR", str(tmp_path / "raw"))
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

"""The init-db subcommand must keep delegating to themis_shared.

`themis-init-db` and `themis-matcher init-db` are two spellings of one command.
The matcher keeps the subcommand for people; the cluster's Job uses the entry
point shared ships, whose image needs neither torch nor httpx. This is the test
that stops the two from drifting.

It lived in shared's suite before the split, which was the wrong side of the
boundary: what it asserts is the matcher's dispatch, not shared's behaviour.
"""

from __future__ import annotations

import pytest

from themis_matcher import cli
from themis_shared import initdb

def test_the_cli_subcommand_and_the_entry_point_do_the_same_work(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`thesis-matchmaker init-db` must not drift from `thesis-matchmaker-init-db`."""
    from themis_matcher import cli

    calls: list[bool] = []
    monkeypatch.setattr(dbcli, "run", lambda settings, *, reset: calls.append(reset))
    monkeypatch.setattr(cli.db, "close_pools", lambda: None)

    cli.main(["init-db", "--reset"])

    assert calls == [True]

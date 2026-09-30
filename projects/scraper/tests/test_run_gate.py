"""`run` must not report success when it was never able to run anything.

Verification used to live only in `data/scraper/var/state.json`, which is gitignored,
so a fresh clone -- and, more to the point, a cluster pod with an empty volume -- saw
0 verified sources no matter how many specs were committed. That path first exited 0
(a CronJob reporting Success forever while `posting` stayed empty), then exited 1,
which was honest but still left a fresh deployment unable to run at all without
someone copying a state file into its volume.

Now the committed contracts (`specs/<id>/expected.json`) are the record of what was
onboarded, so a fresh volume starts verified. These tests pin down both halves: a
fresh clone *can* run, and a data root with no contracts still fails loudly rather
than looking like a healthy no-op -- which "--resume says everything is done" is.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from unittest import mock

from themis_scraper import registry
from themis_scraper.main import main

_REAL_DATA_ROOT = Path("data/scraper")


def _clean_env(**overrides):
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith("SCRAPER_") and k != "OPENAI_API_KEY"
    }
    env.update(overrides)
    return mock.patch.dict(os.environ, env, clear=True)


def _data_root(tmp_path: Path, *, specs: bool) -> Path:
    """A data root shaped like a checkout: the registry, optionally the specs, no var/.

    Symlinked rather than copied -- the specs tree is 11 MB and these tests only
    read it.
    """
    root = tmp_path / "scraper"
    root.mkdir()
    for name in ("registry", "specs") if specs else ("registry",):
        (root / name).symlink_to((_REAL_DATA_ROOT / name).resolve())
    return root


def test_a_fresh_volume_starts_from_the_committed_contracts(tmp_path):
    root = _data_root(tmp_path, specs=True)
    assert not (root / "var" / "state.json").exists()

    with _clean_env(SCRAPER_DATA_ROOT=str(root)):
        state = registry.load_state()
        sources = list(registry.iter_sources())
        with_contract = [s for s in sources if registry.committed_contract(s.source_id)]

    assert with_contract, "the repository ships contracts; this test needs some"
    for src in with_contract:
        entry = state["sources"][src.source_id]
        assert entry["onboarding"] == registry.ONBOARD_VERIFIED, src.source_id
        assert entry["page_type"], src.source_id


def test_no_contracts_and_no_state_exits_non_zero(tmp_path, capsys):
    root = _data_root(tmp_path, specs=False)

    with _clean_env(SCRAPER_DATA_ROOT=str(root)):
        code = main(["run"])

    out = capsys.readouterr().out
    assert code == 1, "a run that could not run anything must not look like success"
    assert "none of the" in out
    # The message has to name where verification comes from, because that is the fix.
    assert "expected.json" in out


def test_everything_already_done_under_resume_still_exits_zero(tmp_path, capsys):
    """The legitimate empty run. It must keep its exit code 0.

    Guards the fix against over-reach: distinguishing the two cases is the whole
    point, so a change that made every empty run loud would be just as wrong.
    """
    root = _data_root(tmp_path, specs=True)
    (root / "var").mkdir()

    with _clean_env(SCRAPER_DATA_ROOT=str(root)):
        sources = list(registry.iter_sources())
        sid = next(s.source_id for s in sources if registry.committed_contract(s.source_id))
        state = {
            "version": registry.STATE_VERSION,
            "sources": {sid: {"onboarding": registry.ONBOARD_VERIFIED, "run": registry.RUN_DONE}},
        }
        (root / "var" / "state.json").write_text(json.dumps(state), encoding="utf-8")

        code = main(["run", "--only", sid, "--resume"])

    out = capsys.readouterr().out
    assert code == 0, "resume with nothing pending is a healthy no-op"
    assert "nothing to run." in out
    assert "none of the" not in out

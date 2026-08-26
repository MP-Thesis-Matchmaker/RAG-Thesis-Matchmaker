"""`run` must not report success when it was never able to run anything.

Verification lives only in `data/scraper/var/state.json`, which is gitignored. So a
fresh clone -- and, more to the point, a cluster pod with an empty volume -- sees 0
verified sources no matter how many specs are committed. That path used to print
"nothing to run." and exit 0, which is indistinguishable from a healthy no-op: a
CronJob would have reported Success forever while `posting` stayed empty.

The distinction these tests pin down is between "nothing was verified" (a
misconfiguration, loud) and "--resume says everything is already done" (normal, quiet).
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from unittest import mock

from thesis_matchmaker.scraper import registry
from thesis_matchmaker.scraper.main import main

_REAL_DATA_ROOT = Path("data/scraper")


def _clean_env(**overrides):
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith("SCRAPER_") and k != "OPENAI_API_KEY"
    }
    env.update(overrides)
    return mock.patch.dict(os.environ, env, clear=True)


def _fresh_clone(tmp_path: Path) -> Path:
    """A data root shaped like a fresh checkout: specs and registry, no var/.

    Symlinked rather than copied -- the specs tree is 11 MB and these tests only
    read it.
    """
    root = tmp_path / "scraper"
    root.mkdir()
    for name in ("registry", "specs"):
        (root / name).symlink_to((_REAL_DATA_ROOT / name).resolve())
    return root


def test_a_deployment_with_no_state_file_exits_non_zero(tmp_path, capsys):
    root = _fresh_clone(tmp_path)
    assert not (root / "var" / "state.json").exists()

    with _clean_env(SCRAPER_DATA_ROOT=str(root)):
        code = main(["run"])

    out = capsys.readouterr().out
    assert code == 1, "a run that could not run anything must not look like success"
    assert "none of the" in out
    # The message has to name the file, because the fix is to restore or rebuild it.
    assert "state.json" in out


def test_everything_already_done_under_resume_still_exits_zero(tmp_path, capsys):
    """The legitimate empty run. It must keep its exit code 0.

    Guards the fix against over-reach: distinguishing the two cases is the whole
    point, so a change that made every empty run loud would be just as wrong.
    """
    root = _fresh_clone(tmp_path)
    (root / "var").mkdir()

    with _clean_env(SCRAPER_DATA_ROOT=str(root)):
        sources = list(registry.iter_sources())
        sid = sources[0].source_id
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

"""Shared helper for the contract-replay tests (`test_contracts.py`).

The spec engine is deterministic: a frozen `snapshot.html` (or `snapshot.json`)
plus a `spec.yaml` must always yield the same records, with no network. This
module replays that offline extraction and compares it to a committed golden
baseline (`tests/golden_specs.json`).

Why a golden baseline and not the contracts' own `expected.json`? `expected.json`
is frozen once during onboarding and additionally carries *enrichment* (followed
profiles, PDF-parsed supervisors) that is not reproducible offline — and some
specs/snapshots were edited after their `expected.json` was written. The golden
file captures exactly what the current engine extracts offline from each
snapshot, so the test guards against future *regressions* in the engine and
specs. Regenerate it deliberately (after an intended change) with:

    python tests/regen_golden.py

Only the deterministic, offline-reproducible part of a contract is replayed:
topics/people specs (including `grouped_people`, `sectioned_people`, and `json`
sources). Process pages (LLM summaries) and link/PDF enrichment need the network
and are out of scope for this offline replay.
"""

from __future__ import annotations

import contextlib
import json
import os
from pathlib import Path

import yaml

from thesis_matchmaker.scraper import cache, registry, spec_engine
from thesis_matchmaker.scraper.config import get_settings

CONTRACTS = get_settings().specs_dir
GOLDEN_PATH = Path(__file__).resolve().parent / "golden_specs.json"

# Fields that legitimately differ between runs and must be ignored on compare.
VOLATILE = ("scraped_at",)


def _strip(rec: dict) -> dict:
    return {k: v for k, v in rec.items() if k not in VOLATILE}


def _has_record(spec: dict) -> bool:
    st = spec.get("source_type")
    if st in ("grouped_people", "sectioned_people", "json"):
        return True
    return "record" in spec and "fields" in spec.get("record", {})


def replayable_ids() -> list[str]:
    """Every contract whose primary extraction can be replayed offline from a
    snapshot: a topics/people spec with a matching snapshot file on disk."""
    ids: list[str] = []
    for name in sorted(os.listdir(CONTRACTS)):
        cdir = CONTRACTS / name
        spec_file = cdir / "spec.yaml"
        if not spec_file.exists():
            continue  # process/none sources have no spec
        spec = yaml.safe_load(spec_file.read_text(encoding="utf-8"))
        if not spec or spec.get("page_type") == "process" or not _has_record(spec):
            continue
        snap = "snapshot.json" if spec.get("source_type") == "json" else "snapshot.html"
        if (cdir / snap).exists():
            ids.append(name)
    return ids


def _base_url(source_id: str, spec: dict) -> str:
    """The URL the engine used when the contract was verified: the cached meta
    url, falling back to the registry url (or the spec's api_url for JSON)."""
    url = ""
    try:
        url = cache.read_meta(source_id).get("url", "") or ""
    except Exception:  # noqa: BLE001 — no/parse-broken meta is fine
        url = ""
    if not url:
        try:
            url = registry.get_source(source_id).url
        except Exception:  # noqa: BLE001 — orphaned id not in current registry
            url = ""
    if spec.get("source_type") == "json":
        url = spec.get("api_url", url)
    return url


@contextlib.contextmanager
def _snapshot_cache(source_id: str, spec: dict):
    """Point the cache reads at the frozen snapshot for the duration of a replay,
    so `spec_engine.extract` runs entirely offline against known-fixed bytes."""
    cdir = CONTRACTS / source_id
    url = _base_url(source_id, spec)  # read the real meta BEFORE patching it
    html = ""
    if (cdir / "snapshot.html").exists():
        html = (cdir / "snapshot.html").read_text(encoding="utf-8")
    jtext = "null"
    if (cdir / "snapshot.json").exists():
        jtext = (cdir / "snapshot.json").read_text(encoding="utf-8")

    saved = {k: getattr(cache, k) for k in ("read_page", "read_meta", "read_json", "is_cached")}
    cache.read_page = lambda _sid: html
    cache.read_meta = lambda _sid: {"url": url}
    cache.read_json = lambda _sid: json.loads(jtext)
    cache.is_cached = lambda _sid: True
    try:
        yield url
    finally:
        for k, v in saved.items():
            setattr(cache, k, v)


def replay(source_id: str, spec: dict | None = None) -> list[dict]:
    """Deterministic, offline extraction of a contract's primary records."""
    if spec is None:
        spec = yaml.safe_load((CONTRACTS / source_id / "spec.yaml").read_text(encoding="utf-8"))
    with _snapshot_cache(source_id, spec):
        records = spec_engine.extract(source_id, spec)
    return [_strip(r) for r in records]


def load_golden() -> dict:
    if GOLDEN_PATH.exists():
        return json.loads(GOLDEN_PATH.read_text(encoding="utf-8"))
    return {}


def regenerate() -> dict:
    """Rebuild the golden baseline from the current snapshots + specs."""
    out = {sid: replay(sid) for sid in replayable_ids()}
    GOLDEN_PATH.write_text(
        json.dumps(out, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return out

"""Registry + state.

The *registry* is the immutable, human-authored source of truth
(`registry/scraping_sources.json`): 37 units across 7 faculties, 103 thesis
sources (106 URLs — a few sources bundle two). The *state* (`var/state.json`) is
the mutable per-source bookkeeping — onboarding status and per-run progress —
that makes pause/resume possible. Both live here so every other stage has a
single place to ask "what sources exist and where are they in their lifecycle?".

Onboarding is not state-only. An approved onboarding freezes a contract,
`specs/<id>/expected.json`, which is committed and shipped in the image, and
`load_state` treats it as the record that the source is verified. Before that,
verification lived only in the gitignored state file, so a pod with a fresh volume
saw every source unverified and `run` had nothing to do. The file is re-frozen
on every re-onboarding, so it is as current as the onboarding itself. The state
keeps what only a run can know: progress, fetch results, and quarantine.

Both file locations come from `config.Settings` (`registry_path`, `state_path`), so
pointing `SCRAPER_DATA_ROOT` at another tree moves them together.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

from .config import get_settings

# Onboarding lifecycle (does a human trust this source's extraction?).
ONBOARD_UNVERIFIED = "unverified"
ONBOARD_VERIFIED = "verified"
ONBOARD_QUARANTINED = "quarantined"

# Per-run progress (where is this source in the current run?).
RUN_PENDING = "pending"
RUN_FETCHED = "fetched"
RUN_EXTRACTED = "extracted"
RUN_DONE = "done"
RUN_FAILED = "failed"

STATE_VERSION = 1


@dataclass(frozen=True)
class Source:
    """One thesis source URL, denormalized with its unit's context."""

    source_id: str
    url: str
    notes: str
    # Denormalized unit context (handy everywhere downstream):
    unit_id: str
    faculty_code: str
    faculty: str
    unit: str
    classification: str
    urls: tuple = ()  # all URLs when a source lists several (url is urls[0])

    @property
    def cache_dir(self) -> Path:
        return get_settings().cache_dir / self.source_id


# --- Registry (read-only) ---------------------------------------------------


def load_registry() -> dict:
    with get_settings().registry_path.open(encoding="utf-8") as fh:
        return json.load(fh)


def iter_sources() -> Iterator[Source]:
    """Flatten the unit tree into denormalized Source records, in file order."""
    data = load_registry()
    for unit in data["units"]:
        for src in unit["thesis_sources"]:
            # A source usually has one "url"; some list several under "urls"
            # (or, in a few registry rows, as a list under "url").
            raw = src.get("url")
            listed = raw if isinstance(raw, list) else None
            urls = tuple(src.get("urls") or listed or ([raw] if raw else []))
            yield Source(
                source_id=src["source_id"],
                url=(raw if isinstance(raw, str) else (urls[0] if urls else "")),
                notes=src.get("notes", ""),
                urls=urls,
                unit_id=unit["unit_id"],
                faculty_code=unit["faculty_code"],
                faculty=unit["faculty"],
                unit=unit["unit"],
                classification=unit["classification"],
            )


def all_sources() -> list[Source]:
    return list(iter_sources())


def sources_by_id() -> dict[str, Source]:
    return {s.source_id: s for s in iter_sources()}


def get_source(source_id: str) -> Source:
    src = sources_by_id().get(source_id)
    if src is None:
        raise KeyError(f"unknown source_id: {source_id!r}")
    return src


# --- State (mutable, on disk) ----------------------------------------------


def _fresh_state() -> dict:
    return {"version": STATE_VERSION, "sources": {}}


def committed_contract(source_id: str) -> dict | None:
    """The onboarding contract committed for a source, or None if it has none.

    Only the two fields the state needs: `page_type` and `verified_content_sha1`
    (the page hash the contract was approved on, which changes whenever the source
    is re-onboarded). `expected.json` is written by `main._freeze_contract` on an
    approved onboarding and never otherwise, so its presence means "verified".
    """
    path = get_settings().specs_dir / source_id / "expected.json"
    if not path.exists():
        return None
    with path.open(encoding="utf-8") as fh:
        data = json.load(fh)
    if not data.get("page_type"):
        return None
    return {
        "page_type": data["page_type"],
        "verified_content_sha1": data.get("verified_content_sha1"),
    }


def _reconcile_with_contract(entry: dict, contract: dict | None) -> None:
    """Fold the committed contract into one state entry. Mutates `entry`.

    - unverified + contract → verified, with the contract's page_type. The case a
      fresh volume is in, and the one that made `run` do nothing in the cluster.
    - quarantined stays quarantined, because that is a run's verdict on the live
      page, which a committed file knows nothing about -- unless the contract was
      re-frozen since the quarantine (`quarantined_contract` recorded at the time no
      longer matches): the source was re-onboarded and is verified again. Without
      that, a quarantine in the cluster would be permanent, since re-onboarding
      happens on a laptop and never touches the cluster's state file.
    - page_type comes from the contract whenever there is one; `run` otherwise
      falls back to "process", which reads a topics page with the wrong extractor.
    """
    if contract is None:
        return
    entry["page_type"] = contract["page_type"]
    onboarding = entry.get("onboarding", ONBOARD_UNVERIFIED)
    if onboarding == ONBOARD_UNVERIFIED:
        entry["onboarding"] = ONBOARD_VERIFIED
    elif onboarding == ONBOARD_QUARANTINED and "quarantined_contract" in entry:
        # Absent on quarantines recorded before this field existed: those stay put,
        # since there is nothing to tell a re-onboarding apart from the original.
        if entry["quarantined_contract"] != contract_id(contract):
            entry["onboarding"] = ONBOARD_VERIFIED
            del entry["quarantined_contract"]


def contract_id(contract: dict | None) -> str:
    """What identifies one freezing of a contract, for `quarantined_contract`."""
    return (contract or {}).get("verified_content_sha1") or ""


def load_state() -> dict:
    """Load state.json, creating a blank one if absent, ensure every registry
    source has an entry, and fold in the committed contracts.

    A source without a contract starts unverified/pending; one with a contract is
    verified (see `_reconcile_with_contract` for quarantine). So a fresh volume --
    a new clone, a pod with an empty PVC -- starts from what was onboarded, not
    from nothing.
    """
    path = get_settings().state_path
    if path.exists():
        with path.open(encoding="utf-8") as fh:
            state = json.load(fh)
    else:
        state = _fresh_state()

    state.setdefault("version", STATE_VERSION)
    entries = state.setdefault("sources", {})
    for src in iter_sources():
        entry = entries.setdefault(
            src.source_id,
            {"onboarding": ONBOARD_UNVERIFIED, "run": RUN_PENDING},
        )
        _reconcile_with_contract(entry, committed_contract(src.source_id))
    return state


def save_state(state: dict) -> None:
    """Atomically persist state (write to temp, then replace)."""
    path = get_settings().state_path
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        json.dump(state, fh, indent=2, ensure_ascii=False)
        fh.write("\n")
    tmp.replace(path)


def source_state(state: dict, source_id: str) -> dict:
    return state["sources"].setdefault(
        source_id, {"onboarding": ONBOARD_UNVERIFIED, "run": RUN_PENDING}
    )


def last_fetch_failed(entry: dict) -> bool:
    """Whether a source's most recent fetch failed.

    Both fetch paths in `main` record a failure as `last_fetch` without a
    `content_sha1` and a success with one, so its absence is the signal. A source
    that was never fetched has no `last_fetch` at all and does not count.
    """
    last = entry.get("last_fetch")
    return bool(last) and "content_sha1" not in last


def update_source_state(state: dict, source_id: str, **fields) -> dict:
    entry = source_state(state, source_id)
    entry.update(fields)
    return entry

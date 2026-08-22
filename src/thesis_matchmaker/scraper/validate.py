"""Break detection — classify each source's result every run.

Statuses (plan §4):
  OK            — extracted, schema-valid, content unchanged since verification
  PAGE_CHANGED  — schema-valid but the page hash differs from the verified hash
                  (data is still updated, but the source is flagged for review)
  NEEDS_REVIEW  — schema-valid, but a record's title is implausible and no better
                  candidate was found on the page (see title_check). Stored and
                  flagged; the template is what needs fixing
  LLM_FALLBACK  — the deterministic template matched nothing, but an LLM rescue
                  extraction produced schema-valid records (stored, but flagged
                  for review — the template likely needs fixing)
  FETCH_FAILED  — no usable cached page / last fetch errored
  EXTRACT_FAILED— template/LLM produced nothing usable
  SCHEMA_INVALID— required fields missing or malformed (emails/links)

Schema checks are global (no per-source config): they encode what a valid
record of each type must look like, nothing page-specific.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

OK = "ok"
PAGE_CHANGED = "page_changed"
NEEDS_REVIEW = "needs_review"
LLM_FALLBACK = "llm_fallback"
FETCH_FAILED = "fetch_failed"
EXTRACT_FAILED = "extract_failed"
SCHEMA_INVALID = "schema_invalid"

# LLM_FALLBACK is flagged (needs review) yet writable (the recovered data is
# still stored) — the same "store but alert" contract as PAGE_CHANGED.
FLAGGED = {PAGE_CHANGED, NEEDS_REVIEW, LLM_FALLBACK, FETCH_FAILED, EXTRACT_FAILED, SCHEMA_INVALID}

# Statuses that keep a source verified and in the run rotation. OK is obvious;
# PAGE_CHANGED too — its data is good and stored, the flag is only "review this
# change", so the source keeps being scraped. Every other flagged status
# quarantines (a hard failure, or an LLM rescue that means the template is broken
# and should be fixed) — those are excluded from future runs until re-onboarded.
# NEEDS_REVIEW joins them: the data is good enough to store and the rest of the
# page extracts fine, so quarantining the whole source over one questionable
# title would stop refreshing every good record on it.
KEEPS_VERIFIED = {OK, PAGE_CHANGED, NEEDS_REVIEW}


def quarantines(status: str) -> bool:
    """Whether a result status should quarantine the source — i.e. drop it from
    future runs until a human re-onboards it."""
    return status not in KEEPS_VERIFIED


_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_URL_RE = re.compile(r"^https?://[^\s]+$", re.I)


@dataclass
class Result:
    source_id: str
    status: str
    page_type: str
    reasons: list[str] = field(default_factory=list)
    record_count: int = 0

    @property
    def flagged(self) -> bool:
        return self.status in FLAGGED

    @property
    def writable(self) -> bool:
        """Whether the freshly extracted data is good enough to store. We write
        on OK, PAGE_CHANGED, NEEDS_REVIEW, and LLM_FALLBACK (all still
        schema-valid); we never overwrite good data on a hard failure."""
        return self.status in (OK, PAGE_CHANGED, NEEDS_REVIEW, LLM_FALLBACK)


def _valid_email(v) -> bool:
    return isinstance(v, str) and bool(_EMAIL_RE.match(v.strip()))


def _valid_url(v) -> bool:
    return isinstance(v, str) and bool(_URL_RE.match(v.strip()))


def _check_people(records) -> list[str]:
    errs = []
    for i, r in enumerate(records):
        if not (r.get("name") or "").strip():
            errs.append(f"people[{i}]: missing name")
        if r.get("email") and not _valid_email(r["email"]):
            errs.append(f"people[{i}]: bad email {r['email']!r}")
        if r.get("personal_website") and not _valid_url(r["personal_website"]):
            errs.append(f"people[{i}]: bad website {r['personal_website']!r}")
    return errs


def _check_topics(records) -> list[str]:
    errs = []
    for i, r in enumerate(records):
        if not (r.get("topic_description") or r.get("research_area") or "").strip():
            errs.append(f"topics[{i}]: no topic_description/research_area")
        if r.get("supervisor_email") and not _valid_email(r["supervisor_email"]):
            errs.append(f"topics[{i}]: bad supervisor_email {r['supervisor_email']!r}")
        if r.get("source_link") and not _valid_url(r["source_link"]):
            errs.append(f"topics[{i}]: bad source_link")
        if not r.get("topic_id"):
            errs.append(f"topics[{i}]: missing topic_id")
    return errs


def _check_process(records) -> list[str]:
    errs = []
    for i, r in enumerate(records):
        if not (r.get("degree_level") or "").strip():
            errs.append(f"process[{i}]: missing degree_level")
        if not _valid_url(r.get("source_url", "")):
            errs.append(f"process[{i}]: bad source_url")
        for j, link in enumerate(r.get("relevant_links") or []):
            if not _valid_url(link.get("url", "")):
                errs.append(f"process[{i}].links[{j}]: bad url")
    return errs


_SCHEMA = {"people": _check_people, "topics": _check_topics, "process": _check_process}


def _title_notes(records) -> tuple[list[str], list[str]]:
    """(flags, repairs) read off the bookkeeping keys `title_check` writes.

    `_title_check` marks a record whose title is implausible with no better
    candidate on the page — that raises NEEDS_REVIEW. `_title_repair` records an
    automatic correction; it is informational only, reported but never flagged,
    since the data ended up right."""
    flags, repairs = [], []
    for i, r in enumerate(records):
        note = r.get("_title_check")
        if note:
            reason = (note.get("reasons") or ["implausible"])[0]
            flags.append(f"records[{i}]: implausible title — {reason}")
        fixed = r.get("_title_repair")
        if fixed:
            repairs.append(
                f"records[{i}]: title repaired via {fixed.get('via')} — "
                f"{str(fixed.get('from'))[:40]!r} -> "
                f"{str(fixed.get('to'))[:60]!r}"
            )
    return flags, repairs


def classify(
    source_id: str,
    page_type: str,
    *,
    cached: bool,
    last_status: int,
    current_sha1: str | None,
    verified_sha1: str | None,
    records: list,
    llm_ok: bool = True,
    allow_empty: bool = False,
) -> Result:
    res = Result(source_id, OK, page_type, record_count=len(records))

    # 1. fetch
    if not cached or not (200 <= (last_status or 0) < 300):
        res.status = FETCH_FAILED
        res.reasons.append(f"no usable cache (last_status={last_status})")
        return res

    # 2. extraction produced something usable. `allow_empty` distinguishes a
    #    genuinely-empty source (e.g. a JSON thesis market with no open topics
    #    right now) from a template that silently matched nothing.
    if page_type in ("people", "topics") and not records and not allow_empty:
        res.status = EXTRACT_FAILED
        res.reasons.append("template matched 0 records")
        return res
    if page_type == "process":
        rec = records[0] if records else {}
        if not llm_ok or not (rec.get("process_description") or "").strip():
            res.status = EXTRACT_FAILED
            res.reasons.append("no usable process summary")
            return res

    # 3. schema
    errs = _SCHEMA.get(page_type, lambda _r: [])(records)
    if errs:
        res.status = SCHEMA_INVALID
        res.reasons = errs[:10]
        return res

    # 4. title plausibility. Repairs are informational (the data is right now);
    #    an unrepairable title is a real review item.
    title_flags, title_repairs = _title_notes(records)
    res.reasons.extend(title_repairs)

    # 5. page change (valid data, but flag for review)
    if verified_sha1 and current_sha1 and current_sha1 != verified_sha1:
        res.status = PAGE_CHANGED
        res.reasons.insert(0, f"hash {verified_sha1[:8]} -> {current_sha1[:8]}")
        return res

    # A questionable title only sets the status when nothing louder already did,
    # so a page_changed diff is never masked by it.
    if title_flags:
        res.status = NEEDS_REVIEW
        res.reasons = title_flags[:10] + res.reasons
    return res


def classify_llm_fallback(source_id: str, page_type: str, records: list) -> Result:
    """Classify the records recovered by the LLM fallback (used only after the
    deterministic template already failed with EXTRACT_FAILED). Same schema bar
    as a normal run: empty → still EXTRACT_FAILED; malformed → SCHEMA_INVALID;
    otherwise LLM_FALLBACK (stored, but flagged so the template gets fixed)."""
    res = Result(source_id, LLM_FALLBACK, page_type, record_count=len(records))
    if not records:
        res.status = EXTRACT_FAILED
        res.reasons.append("llm fallback produced 0 records")
        return res
    errs = _SCHEMA.get(page_type, lambda _r: [])(records)
    if errs:
        res.status = SCHEMA_INVALID
        res.reasons = [f"llm fallback: {e}" for e in errs[:10]]
        return res
    res.reasons.append(
        f"recovered {len(records)} record(s) via LLM fallback — review and fix the template"
    )
    return res


# --- record-level diff (for the run report on PAGE_CHANGED) -----------------


def _key_fn(page_type: str):
    if page_type == "topics":
        return lambda r: r.get("topic_id")
    if page_type == "people":
        return lambda r: r.get("email") or r.get("name")
    return lambda r: r.get("source_id")  # process: one record per source


def _norm(r: dict) -> dict:
    return {k: v for k, v in r.items() if not k.startswith("scraped") and not k.startswith("_")}


def is_empty_diff(diff: dict | None) -> bool:
    """True when a record-level diff shows no added/removed/modified records."""
    return bool(diff) and not (diff["added"] or diff["removed"] or diff["modified"])


def downgrade_if_unchanged(result: Result, diff: dict | None) -> bool:
    """A `page_changed` whose record-level diff is empty is a cosmetic-only change
    — the page's raw HTML moved (embedded tokens, timestamps) but its extracted
    records did not. Downgrade it to OK so it isn't flagged for review; the data
    is still refreshed. Returns True if it downgraded. Leaves the verified hash
    untouched, so this simply re-quiets on every run without needing re-onboarding."""
    if result.status == PAGE_CHANGED and is_empty_diff(diff):
        result.status = OK
        result.reasons = []
        return True
    return False


def diff_records(page_type: str, old: list, new: list) -> dict:
    key = _key_fn(page_type)
    old_by = {key(r): r for r in old}
    new_by = {key(r): r for r in new}
    added = [k for k in new_by if k not in old_by]
    removed = [k for k in old_by if k not in new_by]
    modified = [k for k in new_by if k in old_by and _norm(new_by[k]) != _norm(old_by[k])]
    return {
        "added": len(added),
        "removed": len(removed),
        "modified": len(modified),
        "added_keys": [str(k) for k in added][:20],
        "removed_keys": [str(k) for k in removed][:20],
        "modified_keys": [str(k) for k in modified][:20],
    }

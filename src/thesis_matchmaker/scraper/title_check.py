"""Title plausibility — the one generic check on *what* a value looks like.

A spec says *where* a title sits; it cannot say what a title should look like. So
a selector that keeps matching the wrong element — a posting date, a status word,
a section label — yields a wrong-but-well-formed value that no structural check
can catch: the page did not move, the template still matched, the record is
schema-valid. `ifi--5` stored `"November 3, 2021"` as a topic title that way.

This module closes that gap deterministically (no LLM, so routine runs stay
reproducible):

    score_title(text)        -> Verdict     is this plausibly a title?
    title_candidates(...)    -> [(text, provenance)]   what else could it be?
    repair(record, container)                fix in place, or flag for review

`repair` follows a reserve-then-replace contract: an implausible title is never
discarded blindly. Other material in the record's own container is scanned for a
plausible alternative; only if one is found does the original get demoted (parked
in `date_of_listing` when it is a date, else in `_title_rejected`). When nothing
better exists the original is kept and `_title_check` is set, which
`validate.classify` turns into a `needs_review` flag.

The bookkeeping keys are `_`-prefixed, so `dataset._clean_record` already strips
them from the public JSON.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from urllib.parse import unquote

# A title scoring below this is treated as implausible.
PLAUSIBLE_MIN = 0.5

_MONTHS = (
    r"Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|Jul(?:y)?|"
    r"Aug(?:ust)?|Sep(?:t)?(?:ember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?|"
    r"Januar|Februar|März|Maerz|Mai|Juni|Juli|Oktober|Dezember"
)

# A title that is *entirely* a date is never a title (it is a posting date that
# a page author put in the title slot).
_DATE_RES = [
    re.compile(rf"^(?:{_MONTHS})\.?\s+\d{{1,2}}(?:st|nd|rd|th)?,?\s*\d{{2,4}}$", re.I),
    re.compile(rf"^\d{{1,2}}\.?\s+(?:{_MONTHS})\.?,?\s*\d{{2,4}}$", re.I),
    re.compile(rf"^(?:{_MONTHS})\.?\s+\d{{4}}$", re.I),
    re.compile(r"^\d{1,2}[./-]\d{1,2}[./-]\d{2,4}$"),
    re.compile(r"^\d{4}-\d{1,2}-\d{1,2}$"),
]

# Bare availability words: the status span leaking into the title slot.
_STATUS_WORDS = {
    "taken",
    "vergeben",
    "open",
    "offen",
    "available",
    "verfügbar",
    "verfugbar",
    "reserved",
    "reserviert",
    "assigned",
    "zugewiesen",
    "besetzt",
    "closed",
    "filled",
    "frei",
    "occupied",
    "not available",
    "nicht verfügbar",
}

# Bare section labels / navigation text.
_LABEL_WORDS = {
    "thesis",
    "theses",
    "topic",
    "topics",
    "thema",
    "themen",
    "themenvorschläge",
    "bachelorarbeit",
    "masterarbeit",
    "bachelorarbeiten",
    "masterarbeiten",
    "bachelor thesis",
    "master thesis",
    "bachelor theses",
    "master theses",
    "project",
    "projekt",
    "projects",
    "projekte",
    "details",
    "detail",
    "more",
    "mehr",
    "pdf",
    "download",
    "abstract",
    "description",
    "beschreibung",
    "titel",
    "title",
    "info",
    "information",
    "supervisor",
    "betreuer",
    "kontakt",
    "contact",
    "weitere informationen",
}

_DEGREE_RE = re.compile(
    r"^(?:ba|ma|mp|map|bp|bsc|msc|b\.?\s?sc\.?|m\.?\s?sc\.?|bachelor|master|"
    r"bachelor\s*,?\s*(?:and\s*)?master|master\s*,?\s*(?:and\s*)?bachelor|"
    r"(?:ba|ma|bsc|msc)(?:\s*/\s*(?:ba|ma|mp|bsc|msc))+|"
    r"\d{1,3}\s*ects)$",
    re.I,
)

_SEMESTER_RE = re.compile(
    r"^(?:hs|fs|ws|ss)\s*\d{2,4}(?:\s*/\s*\d{2,4})?$|"
    r"^(?:herbst|frühjahr|fruehjahr|spring|fall|autumn|winter|summer)"
    r"(?:semester)?\s*\d{2,4}$",
    re.I,
)

_EMAIL_RE = re.compile(r"^[\w.\-+]+@[\w.\-]+\.\w{2,}$")
_URL_RE = re.compile(r"^(?:https?://|www\.)\S+$", re.I)

# A leading list marker ("1.", "a)") is decoration, not part of the title.
_ENUM_RE = re.compile(r"^(?:\d{1,2}[.)]|[a-zA-Z][.)])\s+")
_LETTER_RE = re.compile(r"[^\W\d_]", re.UNICODE)

# The longest real title in the committed corpus is 179 chars.
MAX_TITLE_CHARS = 300
SHORT_TITLE_CHARS = 12


@dataclass
class Verdict:
    """The outcome of scoring one candidate string."""

    plausible: bool
    score: float
    reasons: list[str] = field(default_factory=list)
    text: str = ""  # the normalized string that was scored

    def as_note(self) -> dict:
        """The compact form stored on a record for the run report."""
        return {"score": self.score, "reasons": list(self.reasons), "title": self.text}


def normalize(text) -> str:
    """Collapse whitespace and strip decoration (leading enumerator, trailing
    colon/dash) so the same title reads identically wherever it was found."""
    if not isinstance(text, str):
        return ""
    s = re.sub(r"\s+", " ", text).strip()
    s = _ENUM_RE.sub("", s)
    return s.strip(" \t:;-–—").strip()


def looks_like_date(text) -> bool:
    s = normalize(text)
    return bool(s) and any(rx.match(s) for rx in _DATE_RES)


def score_title(text, *, siblings=None) -> Verdict:
    """Plausibility of `text` as a topic title, in [0, 1].

    Hard rejects (score 0) are strings that are *entirely* something else: a
    date, an availability word, a section label, a degree/ECTS marker, a semester
    code, an email, a URL. Soft penalties cover the shapes that are suspicious
    but sometimes legitimate. `siblings` are values the title must not merely
    repeat (see `_siblings`) — a match usually means the selector grabbed that
    element instead of the title.
    """
    s = normalize(text)
    if not s:
        return Verdict(False, 0.0, ["empty"], s)
    if not _LETTER_RE.search(s):
        return Verdict(False, 0.0, ["contains no letters"], s)

    low = s.lower().rstrip(".").strip()
    for test, reason in (
        (looks_like_date(s), "is a date, not a title"),
        (low in _STATUS_WORDS, "is a bare availability word"),
        (low in _LABEL_WORDS, "is a bare section label"),
        (bool(_DEGREE_RE.match(low)), "is a bare degree/ECTS marker"),
        (bool(_SEMESTER_RE.match(low)), "is a bare semester code"),
        (bool(_EMAIL_RE.match(s)), "is an email address"),
        (bool(_URL_RE.match(s)), "is a URL"),
        (len(s) > MAX_TITLE_CHARS, f"is {len(s)} chars — a paragraph, not a title"),
    ):
        if test:
            return Verdict(False, 0.0, [f"{reason}: {s!r}"], s)

    score, reasons = 1.0, []
    if len(s.split()) == 1 and len(s) < SHORT_TITLE_CHARS:
        score -= 0.4
        reasons.append(f"single short token ({len(s)} chars)")
    for value in siblings or ():
        if isinstance(value, str) and value and normalize(value).lower() == low:
            score -= 0.5
            reasons.append("repeats the record's status value")
            break
    score = max(0.0, round(score, 3))
    return Verdict(score >= PLAUSIBLE_MIN, score, reasons, s)


# Candidate sources inside a record's container, most-title-like first.
_CANDIDATE_SELECTORS = (
    ("p > strong", "p>strong"),
    ("p > b", "p>b"),
    ("h1, h2, h3, h4, h5, h6", "heading"),
    ("strong", "strong"),
    ("b", "b"),
    ("dt", "dt"),
    ("caption", "caption"),
    ("[class*=title], [class*=Title]", "class-title"),
    ("a[href]", "link-text"),
)


def title_candidates(
    container, *, extra_selectors=None, source_link=None, description=None
) -> list[tuple[str, str]]:
    """Strings from the record's own container that could be its title, paired
    with where each came from. Ordered by selector priority, then document
    order, and de-duplicated — so the choice is deterministic."""
    out: list[tuple[str, str]] = []
    seen: set[str] = set()

    def add(text, provenance) -> None:
        t = normalize(text)
        if t and t.lower() not in seen:
            seen.add(t.lower())
            out.append((t, provenance))

    selectors = [(s, "spec") for s in (extra_selectors or [])]
    selectors += list(_CANDIDATE_SELECTORS)
    if container is not None:
        for selector, provenance in selectors:
            for node in container.select(selector):
                add(node.get_text(" ", strip=True), provenance)

    # A PDF filename usually names the topic ("Lempel_Ziv_entropy.pdf").
    if isinstance(source_link, str) and ".pdf" in source_link.lower():
        stem = re.sub(r"\.pdf$", "", source_link.rstrip("/").split("/")[-1], flags=re.I)
        add(re.sub(r"[_\-+]+", " ", unquote(stem)), "pdf-filename")

    # The description often opens with the title followed by a colon.
    if isinstance(description, str) and description.strip():
        lead = re.split(r"(?<=[.:])\s", normalize(description), maxsplit=1)[0]
        lead = lead.rstrip(":.").strip()
        if SHORT_TITLE_CHARS <= len(lead) <= 200:
            add(lead, "description-lead")
    return out


def _strip_leading(description: str, title: str) -> str:
    """Drop a title that the description repeats at its start (the usual shape
    when the real title lives in a bold paragraph inside the description block)."""
    desc = normalize(description)
    if desc.lower().startswith(title.lower()):
        return desc[len(title) :].lstrip(" :.–—-").strip() or desc
    return description


def _siblings(record: dict) -> list:
    """Fields whose value a title must not simply repeat. Deliberately only
    `status`: a title equal to the availability word means the selector grabbed
    the status span. A title equal to `research_area` is NOT suspicious — chair
    listings legitimately title a topic with its subject area (rfw--1's
    "Strafrecht"), and penalising that repaired good records into supervisor
    names."""
    return [record.get("status")]


def check_only(record: dict) -> Verdict | None:
    """Score a record's title without repairing it — for the JSON and LLM-fallback
    paths, where there is no DOM container to scan. Sets `_title_check` when the
    title is implausible. No-op when the record carries no title."""
    if not isinstance(record.get("title"), str) or not record["title"].strip():
        return None
    verdict = score_title(record["title"], siblings=_siblings(record))
    if not verdict.plausible:
        record["_title_check"] = verdict.as_note()
    return verdict


def repair(record: dict, container=None, *, extra_selectors=None) -> Verdict | None:
    """Reserve-then-replace: if the record's title is implausible, look for a
    better one in its container. Returns the verdict on the ORIGINAL title, or
    None when it was plausible (the common case, and a fast exit).

    Case A — a plausible alternative exists: it becomes the title, the original
    is parked (`date_of_listing` if it is a date, else `_title_rejected`), the
    swap is recorded in `_title_repair`, and a description that merely repeated
    the promoted title loses that prefix.

    Case B — nothing better: the original is kept, because a bad title still
    carries more information than no title, and `_title_check` is set so the run
    reports it for review.
    """
    siblings = _siblings(record)
    verdict = score_title(record.get("title"), siblings=siblings)
    if verdict.plausible:
        return None

    original = record.get("title")
    rejected = verdict.text.lower()
    candidates = title_candidates(
        container,
        extra_selectors=extra_selectors,
        source_link=record.get("source_link"),
        description=record.get("topic_description"),
    )

    chosen = None
    for text, provenance in candidates:
        # Skip anything that merely restates the rejected string (the element it
        # came from, and its ancestors, whose text contains it).
        if rejected and rejected in text.lower():
            continue
        candidate_verdict = score_title(text, siblings=siblings)
        if candidate_verdict.plausible:
            chosen = (text, provenance, candidate_verdict)
            break

    if chosen is None:
        record["_title_check"] = verdict.as_note()
        return verdict

    text, provenance, candidate_verdict = chosen
    record["title"] = text
    record["_title_repair"] = {
        "from": original,
        "to": text,
        "via": provenance,
        "score": candidate_verdict.score,
        "reasons": list(verdict.reasons),
    }
    # Park the demoted string rather than dropping it on the floor.
    if looks_like_date(verdict.text) and not record.get("date_of_listing"):
        record["date_of_listing"] = verdict.text
    else:
        record["_title_rejected"] = original

    if isinstance(record.get("topic_description"), str):
        record["topic_description"] = _strip_leading(record["topic_description"], text)
    return verdict

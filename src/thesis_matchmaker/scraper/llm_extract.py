"""Process-page extraction — the second (and only other) controlled LLM use.

A `process` source becomes one process record:
    degree_level, process_description, relevant_links: [{url, description}],
    source_url  (+ source_id, scraped_at)

Only `process_description` is written by the LLM, from the page's cleaned main
text. `degree_level` and `relevant_links` are extracted deterministically, so
the LLM's job is narrow and auditable. Without a configured LLM the record is
still produced (links + degree level); `process_description` is left null and
the reason is recorded under `_llm`.
"""

from __future__ import annotations

import hashlib
import re
from datetime import UTC, datetime
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from . import cache, llm, registry, title_check
from .config import get_settings

PROCESS_FIELDS = ["degree_level", "process_description", "relevant_links", "source_url"]

_MAIN_SELECTORS = ["main", "article", "[role=main]", ".Content", "#content", ".content"]

# Anchor text / href keywords that mark a link as process-relevant. Kept
# thesis-specific on purpose — broad terms like "bachelor"/"master" pulled in
# generic study-info links (info days, taster lectures).
_LINK_KEYWORDS = (
    "thesis",
    "theses",
    "guideline",
    "richtlinie",
    "merkblatt",
    "leitfaden",
    "registration",
    "register",
    "anmeld",
    "formular",
    "deadline",
    "frist",
    "colloquium",
    "kolloquium",
    "template",
    "vorlage",
    "wegleitung",
    "supervis",
    "betreu",
    "abschlussarbeit",
    "bachelorarbeit",
    "masterarbeit",
)
_DOC_EXT = (".pdf", ".doc", ".docx", ".odt")

_SYSTEM = (
    "You summarize a university department's thesis-process web page for a "
    "thesis-matching tool. Produce a concise, factual description (roughly "
    "80-150 words, plain text, no markdown, no preamble) of HOW a student "
    "obtains and completes a Bachelor's or Master's thesis in this unit: who is "
    "eligible, how to find/approach a supervisor, how to register, key formal "
    "requirements and deadlines, and where the official guidelines live. Use "
    "only information present in the page text. If the page does not describe a "
    "concrete process, say so in one sentence."
)


def _main_node(soup: BeautifulSoup):
    for sel in _MAIN_SELECTORS:
        node = soup.select_one(sel)
        if node and len(node.get_text(" ", strip=True)) > 150:
            return node
    return soup.body or soup


def _clean_text(node) -> str:
    for junk in node.select("nav, header, footer, script, style, .Breadcrumb"):
        junk.decompose()
    return re.sub(r"\s+", " ", node.get_text(" ", strip=True)).strip()


def _degree_level(url: str, notes: str, text: str) -> str:
    """URL path and the curator's note are authoritative (a BA process page
    naturally also mentions 'master'); only fall back to page text if neither
    the URL nor the note disambiguates."""
    for signal in (url.lower(), notes.lower()):
        ba = bool(re.search(r"/ba\.|/bachelor|\bba\b|bachelor", signal))
        ma = bool(re.search(r"/ma\.|/master|\bma\b|master", signal))
        if ba ^ ma:  # exactly one → decisive
            return "Bachelor" if ba else "Master"

    hay = text.lower()
    ba = bool(re.search(r"\bba\b|\bbs\b|bachelor|b\.?sc", hay))
    ma = bool(re.search(r"\bma\b|\bms\b|master|m\.?sc", hay))
    if ba and ma:
        return "Bachelor, Master"
    if ba:
        return "Bachelor"
    if ma:
        return "Master"
    return "Unspecified"


def _relevant_links(node, base_url: str) -> list[dict]:
    seen, out = set(), []
    for a in node.select("a[href]"):
        href = a.get("href", "")
        if not href or href.startswith(("#", "mailto:", "tel:", "javascript:")):
            continue
        text = re.sub(r"\s+", " ", a.get_text(" ", strip=True)).strip()
        low = f"{text} {href}".lower()
        is_doc = href.lower().split("?")[0].endswith(_DOC_EXT) or "dam/jcr" in href
        if not (is_doc or any(k in low for k in _LINK_KEYWORDS)):
            continue
        url = urljoin(base_url, href.strip()).replace(" ", "%20")
        if url in seen:
            continue
        seen.add(url)
        desc = text or a.get("title") or url
        out.append({"url": url, "description": desc})
    return out


_TOPIC_PDF_SYSTEM = (
    "You are given the text of a thesis/project proposal PDF. Write a concise "
    "2-4 sentence description (plain text, no markdown, no preamble) of the "
    "topic: the goal and what the student would actually do. Use only the "
    "provided text; do not invent supervisors, dates or requirements."
)


def _pdf_text_from_bytes(data: bytes) -> str:
    try:
        import io

        from pypdf import PdfReader

        reader = PdfReader(io.BytesIO(data))
        parts = [(page.extract_text() or "") for page in reader.pages]
    except Exception:  # noqa: BLE001 — corrupt/encrypted PDF
        return ""
    return re.sub(r"\s+", " ", "\n".join(parts)).strip()


def summarize_pdf_text(text: str) -> str | None:
    """LLM-summarize already-extracted PDF text into a topic description.
    Returns None if there's no text or no LLM configured."""
    if not text or not llm.is_available():
        return None
    try:
        budget = get_settings().llm_page_text_budget
        return llm.complete(_TOPIC_PDF_SYSTEM, text[:budget])
    except Exception:  # noqa: BLE001 — never fail a run on one PDF
        return None


def summarize_pdf_bytes(data: bytes) -> str | None:
    """Extract a PDF's text and LLM-summarize it into a topic description.
    Returns None if there's no text or no LLM configured."""
    return summarize_pdf_text(_pdf_text_from_bytes(data))


def _binary_text(source_id: str, meta: dict) -> str:
    """Extract text from a cached binary source. Only PDFs are supported for
    now (via pypdf); other types return ''."""
    path = cache.binary_file(source_id)
    if path is None:
        return ""
    if "pdf" not in meta.get("content_type", "").lower() and path.suffix.lower() != ".pdf":
        return ""
    try:
        from pypdf import PdfReader
    except ImportError:
        return ""
    try:
        reader = PdfReader(str(path))
        parts = [(page.extract_text() or "") for page in reader.pages]
    except Exception:  # noqa: BLE001 — corrupt/encrypted PDF → treat as no text
        return ""
    return re.sub(r"\s+", " ", "\n".join(parts)).strip()


def _urls_from_text(text: str, *, exclude: str = "") -> list[dict]:
    seen, out = {exclude}, []
    for m in re.findall(r"https?://[^\s)>\]]+", text):
        url = m.rstrip(".,;)")
        if url in seen:
            continue
        seen.add(url)
        out.append({"url": url, "description": "referenced in document"})
    return out


def build_prompt(source: registry.Source, page_text: str) -> tuple[str, str]:
    """The exact (system, user) pair sent to the LLM — exposed so onboarding can
    show it before spending a call."""
    prompt = (
        f"Unit: {source.unit}\n"
        f"Faculty: {source.faculty}\n"
        f"Page URL: {source.url}\n"
        f"Registry note: {source.notes}\n\n"
        f"Page text:\n{page_text[: get_settings().llm_page_text_budget]}"
    )
    return _SYSTEM, prompt


def extract_process(
    source_id: str, *, use_llm: bool = True, html: str | None = None, base_url: str | None = None
) -> dict:
    src = registry.get_source(source_id)

    # Explicit HTML override (one page of a multi-page source); otherwise the
    # source's cached main page.
    if html is not None:
        meta = {"binary": False}
        base_url = base_url or src.url
    else:
        if not cache.is_cached(source_id):
            raise RuntimeError(f"{source_id} not in cache — fetch it first")
        meta = cache.read_meta(source_id)
        base_url = meta.get("url", src.url)
    scraped_at = datetime.now(UTC).isoformat()

    if meta.get("binary"):
        text = _binary_text(source_id, meta)
        if not text:
            # Couldn't extract (unsupported format / scanned PDF): record shell.
            return {
                "degree_level": _degree_level(base_url, src.notes, src.notes),
                "process_description": None,
                "relevant_links": [{"url": base_url, "description": src.notes or "document"}],
                "source_url": src.url,
                "source_id": source_id,
                "scraped_at": scraped_at,
                "_llm": {"status": "binary_no_text"},
            }
        # The document itself is the primary link; plus any URLs it references.
        links = [{"url": base_url, "description": src.notes or "source document"}]
        links += _urls_from_text(text, exclude=base_url)
        degree = _degree_level(base_url, src.notes, text)
    else:
        page_html = html if html is not None else cache.read_page(source_id)
        soup = BeautifulSoup(page_html, "html.parser")
        node = _main_node(soup)
        text = _clean_text(node)
        links = _relevant_links(node, base_url)
        degree = _degree_level(base_url, src.notes, text)

    system, prompt = build_prompt(src, text)
    description, llm_info = None, {"provider": llm.provider_name(), "model": llm.model_name()}
    if use_llm and llm.is_available():
        # Cache the summary by page-text hash so re-running an unchanged process
        # page (e.g. to refresh its aux people) never re-spends the LLM.
        key = hashlib.sha1((system + "\n" + prompt).encode("utf-8")).hexdigest()[:16]
        if cache.has_subpage(source_id, "processsummary", key):
            description = cache.read_subpage(source_id, "processsummary", key)
            llm_info["status"] = "ok"
            llm_info["cached"] = True
        else:
            try:
                description = llm.complete(system, prompt)
                cache.write_subpage(source_id, "processsummary", key, description)
                llm_info["status"] = "ok"
            except Exception as exc:  # noqa: BLE001 — record, never crash a run
                llm_info["status"] = f"error: {type(exc).__name__}: {exc}"
    else:
        llm_info["status"] = "unavailable" if use_llm else "disabled"

    return {
        "degree_level": degree,
        "process_description": description,
        "relevant_links": links,
        "source_url": base_url,
        "source_id": source_id,
        "scraped_at": scraped_at,
        "_llm": llm_info,
    }


def to_preview(source: registry.Source, record: dict) -> dict:
    return {
        "faculty": source.faculty,
        "faculty_code": source.faculty_code,
        "unit": source.unit,
        "unit_id": source.unit_id,
        "source_id": source.source_id,
        "source_url": source.url,
        "page_type": "process",
        "process": [record],
    }


# --- LLM fallback extraction (topics/people) --------------------------------
#
# The THIRD, opt-out controlled LLM use: a rescue net for when the deterministic
# spec matched nothing (validate → EXTRACT_FAILED). It reads the cached page's
# cleaned main HTML and asks the model for target-model records as JSON. Output
# is always flagged for review (validate.LLM_FALLBACK) — it recovers a run that
# would otherwise drop a source, but the fix is to repair the template.

import json  # noqa: E402

_FALLBACK_TOPICS_SYSTEM = (
    "You extract open Bachelor's/Master's thesis topics from a university "
    "department web page for a thesis-matching tool. Return ONLY a JSON array "
    "(no markdown, no prose). Each element is one open topic:\n"
    '  {"title": str, "degree_level": "Bachelor"|"Master"|"Bachelor, Master"|null,'
    ' "research_area": str|null, "supervisors": [{"name": str|null,'
    ' "email": str|null}], "topic_description": str|null, "status": "open"|null,'
    ' "source_link": str|null}\n'
    "Use only information present on the page. Use null (not guesses) for missing "
    "fields. Copy hrefs verbatim from the HTML for source_link/emails. If the "
    "page lists no concrete open topics, return []."
)

_FALLBACK_PEOPLE_SYSTEM = (
    "You extract the people (supervisors / group leaders / academic staff) from "
    "a university department web page for a thesis-matching tool. Return ONLY a "
    "JSON array (no markdown, no prose). Each element is one person:\n"
    '  {"role": str|null, "name": str, "email": str|null,'
    ' "research_interest": str|null, "research_field": str|null,'
    ' "personal_website": str|null, "_profile_url": str|null}\n'
    "Use only information present on the page. Use null (not guesses) for missing "
    "fields. Copy hrefs verbatim from the HTML: put an institutional profile link "
    "in _profile_url and an external personal homepage in personal_website. Strip "
    "academic titles from name. If the page lists no people, return []."
)

_FALLBACK_SYSTEM = {"topics": _FALLBACK_TOPICS_SYSTEM, "people": _FALLBACK_PEOPLE_SYSTEM}

# Only these keys are kept from a fallback record, in target-model order.
_FALLBACK_KEEP = {
    "topics": [
        "title",
        "status",
        "degree_level",
        "date_of_listing",
        "research_area",
        "supervisors",
        "topic_description",
        "source_link",
    ],
    "people": [
        "role",
        "name",
        "email",
        "research_interest",
        "research_field",
        "bio",
        "personal_website",
        "_profile_url",
    ],
}
_FALLBACK_URL_FIELDS = ("source_link", "_profile_url", "personal_website")


def _clean_main_html(html: str) -> str:
    """The main content region as HTML (hrefs/emails intact), stripped of chrome
    and truncated to the token budget — the LLM reads links, not just text."""
    soup = BeautifulSoup(html, "html.parser")
    node = _main_node(soup)
    for junk in node.select("script, style, nav, header, footer, svg, form, .Breadcrumb, noscript"):
        junk.decompose()
    return node.decode()[: get_settings().llm_fallback_html_budget]


def _parse_json_array(text: str) -> list:
    """Best-effort parse of a JSON array from an LLM reply — tolerating a
    ```json fence or leading prose by slicing to the outermost brackets."""
    if not text:
        return []
    s = text.strip()
    if s.startswith("```"):
        s = re.sub(r"^```[a-zA-Z]*\s*|\s*```$", "", s).strip()
    try:
        data = json.loads(s)
    except json.JSONDecodeError:
        start, end = s.find("["), s.rfind("]")
        if start == -1 or end <= start:
            return []
        try:
            data = json.loads(s[start : end + 1])
        except json.JSONDecodeError:
            return []
    return data if isinstance(data, list) else []


def _coerce_records(raw: list, page_type: str, source_id: str, base_url: str) -> list[dict]:
    from . import spec_engine  # local import: spec_engine never imports us

    keep = _FALLBACK_KEEP[page_type]
    scraped_at = datetime.now(UTC).isoformat()
    out = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        rec = {f: item.get(f) for f in keep}
        # resolve any relative URLs the model copied verbatim
        for uf in _FALLBACK_URL_FIELDS:
            if isinstance(rec.get(uf), str) and rec[uf].strip():
                rec[uf] = urljoin(base_url, rec[uf].strip())
        rec["source_id"] = source_id
        rec["scraped_at"] = scraped_at
        if page_type == "people":
            if not (rec.get("name") or "").strip():
                continue
        else:  # topics
            if isinstance(rec.get("supervisors"), list):
                for sup in rec["supervisors"]:
                    if isinstance(sup, dict) and isinstance(sup.get("email"), str):
                        sup["email"] = sup["email"].replace("mailto:", "").split("?")[0].strip()
            spec_engine.normalize_supervisors(rec)
            rec["source_link"] = rec.get("source_link") or base_url
            rec["topic_id"] = spec_engine._topic_id(base_url, rec, ["title"])
            if not (rec.get("title") or rec.get("topic_description")):
                continue
            # Score only: a fallback record has no DOM container to repair from.
            title_check.check_only(rec)
        out.append(rec)
    return out


def extract_records_fallback(
    source_id: str, page_type: str, *, html: str | None = None, base_url: str | None = None
) -> tuple[list[dict], dict]:
    """Rescue extraction for a topics/people source whose deterministic template
    matched nothing. Returns (records, llm_info). Records are in target-model
    shape and always meant to be flagged for review. Degrades to ([], info) when
    no LLM is configured, the reply won't parse, or the call errors — never
    raises, so a run keeps going."""
    info = {"provider": llm.provider_name(), "model": llm.model_name(), "mode": "fallback"}
    if page_type not in _FALLBACK_SYSTEM:
        info["status"] = "unsupported_page_type"
        return [], info
    if not llm.is_available():
        info["status"] = "unavailable"
        return [], info

    if html is None:
        if not cache.is_cached(source_id):
            info["status"] = "not_cached"
            return [], info
        base_url = cache.read_meta(source_id).get("url", "")
        html = cache.read_page(source_id)
    base_url = base_url or ""
    cleaned = _clean_main_html(html)
    system = _FALLBACK_SYSTEM[page_type]

    # Cache the raw JSON reply by (system+content) hash so a repeat run of an
    # unchanged failing page doesn't re-spend the LLM.
    key = hashlib.sha1((system + "\n" + cleaned).encode("utf-8")).hexdigest()[:16]
    kind = "llmfallback"
    if cache.has_subpage(source_id, kind, key):
        reply = cache.read_subpage(source_id, kind, key)
        info["cached"] = True
    else:
        try:
            reply = llm.complete(system, cleaned)
        except Exception as exc:  # noqa: BLE001 — record, never crash a run
            info["status"] = f"error: {type(exc).__name__}: {exc}"
            return [], info
        cache.write_subpage(source_id, kind, key, reply)

    records = _coerce_records(_parse_json_array(reply), page_type, source_id, base_url)
    info["status"] = "ok"
    info["count"] = len(records)
    return records, info

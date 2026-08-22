"""Spec drafting — the second controlled LLM use (onboarding only).

Given a cached topics/people page, the LLM proposes a `spec.yaml` (container +
field selectors) matching the engine's documented schema. The draft is never
trusted blind: `onboard` immediately runs it through the deterministic
spec_engine and shows the human the spec next to the records it produced, so a
wrong selector is obvious and fixable via retry-with-hint or manual edit.

The LLM only ever authors a *template*; it never sees or shapes the data during
routine runs.
"""

from __future__ import annotations

import re

from bs4 import BeautifulSoup

from . import cache, llm, registry
from .config import get_settings

_SCHEMA_DOC = """\
The spec is YAML with this shape:

  source_id: <id>
  page_type: people | topics
  render: <true if the page needs a JS render; else omit>
  record:
    container: "<CSS selector matching ONE person/topic element>"
    fields:
      <target_field>:
        selector: "<CSS selector relative to container; omit to use container>"
        attr: text | html | <html-attribute like href>
        multi: <true to collect all matches>
        join: "<string to join multi values into one>"
        contains_i: "<keep only selected nodes whose text contains this,
                     case-insensitively — use for label-matched links like a
                     'Personal website' link instead of a case-sensitive
                     :-soup-contains() selector>"
        transform: <one or a list of: normalize_ws, strip_mailto, split_commas,
                    lower, absolute_url>
        regex: "<optional; capture group 1 or whole match>"
        default: <optional fallback>

Selectors run through BeautifulSoup/soupsieve, so `:-soup-contains("Label")` and
combinators like `dt:-soup-contains("Research Fields") + dd` are allowed.

For page_type people, use exactly these target fields where present: role, name,
email, research_interest, research_field, personal_website. Add a field named
`_profile_url` (attr: href, transform: absolute_url) for the link to each
person's own profile page. Then add a follow block:

  follow:
    url_field: _profile_url
    url_pattern: "<Python regex matching ONLY real profile URLs to follow>"
    render: <true if profile pages need a JS render; else omit>
    profile:                # how to read ONE profile page (second template)
      fields:
        email: {selector: "a[href^=mailto]", attr: href, transform: strip_mailto}
        bio: {selector: "...", attr: text, transform: normalize_ws}
        research_interest: {selector: "...", attr: text}
        research_field: {selector: "...", attr: text}
        personal_website: {selector: "...", attr: href, transform: absolute_url}

For page_type topics, use these target fields where present: degree_level,
date_of_listing, research_area, supervisor_name, supervisor_email,
topic_description, source_link. Optionally add `id_from: [field, ...]` to seed
the stable topic id.
"""

_SYSTEM = (
    "You write extraction specs for a deterministic web scraper. You are given "
    "one cached HTML page and its intended page_type. Infer the repeating "
    "record structure and output ONE spec.yaml.\n\n"
    + _SCHEMA_DOC
    + "\n\nOutput ONLY the YAML document — no markdown fences, no commentary. "
    "Prefer stable class/structure selectors over positional ones. Only include "
    "fields you can actually locate in the given HTML."
)


class DraftError(Exception):
    pass


def _clean_html(html: str, budget: int | None = None) -> str:
    """Strip the page to what the drafting model needs, truncated to
    `spec_draft_html_budget` chars unless a budget is given."""
    budget = get_settings().spec_draft_html_budget if budget is None else budget
    soup = BeautifulSoup(html, "html.parser")
    for junk in soup.select("script, style, noscript, svg, head, link, meta"):
        junk.decompose()
    body = soup.body or soup
    text = str(body)
    text = re.sub(r"\n\s*\n+", "\n", text)
    return text[:budget]


def _strip_fences(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\n", "", text)
        text = re.sub(r"\n```$", "", text.strip())
    return text.strip()


def draft_spec_yaml(source_id: str, page_type: str, hint: str = "") -> str:
    """Ask the LLM for a spec.yaml (as text). Raises if the LLM is unavailable."""
    if not llm.is_available():
        raise DraftError("LLM unavailable — cannot draft a spec (set OPENAI_API_KEY)")
    src = registry.get_source(source_id)
    html = _clean_html(cache.read_page(source_id))

    prompt = (
        f"page_type: {page_type}\n"
        f"source_id: {source_id}\n"
        f"unit: {src.unit}\n"
        f"url: {src.url}\n"
        f"registry note: {src.notes}\n"
    )
    if hint:
        prompt += f"operator hint: {hint}\n"
    prompt += f"\nCached HTML (cleaned, possibly truncated):\n{html}"

    raw = llm.complete(_SYSTEM, prompt)
    return _strip_fences(raw)

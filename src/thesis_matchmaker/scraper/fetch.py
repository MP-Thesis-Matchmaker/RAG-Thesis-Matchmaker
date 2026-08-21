"""Stage 1 — fetch.

Politeness is non-negotiable (the plan): sequential fetching, a delay between
requests (2s by default, `SCRAPER_POLITE_DELAY_SECONDS`), an honest User-Agent.
Strategy per URL: try a plain static `requests` GET first; if the response
looks empty or blocked (JS-rendered page, challenge, 403/429), fall back to a
Playwright chromium render. PDFs and other
binary sources are fetched and handed back as bytes for the cache to store.

This module only *fetches*; writing to disk and updating state is the caller's
job (main.fetch), which keeps fetch pure and easy to test.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass, field

import requests

from .config import get_settings

# The polite delay, the timeouts, the render waits and the User-Agent's contact
# address are all settings (see config.py) — nothing here is a literal.

# Content-types we treat as HTML/text; everything else is cached as bytes.
_TEXT_HINTS = ("text/html", "application/xhtml", "text/plain", "text/")

# Substrings that betray a bot-challenge / JS-wall rather than real content.
_BLOCK_MARKERS = (
    "captcha",
    "cf-browser-verification",
    "checking your browser",
    "enable javascript",
    "please enable js",
)


@dataclass
class FetchResult:
    source_id: str
    url: str
    ok: bool
    http_status: int
    method: str  # "requests" | "playwright" | "error"
    content_type: str = ""
    is_binary: bool = False
    text: str = ""
    raw_bytes: bytes = b""
    error: str = ""
    _fields: tuple = field(default=(), repr=False)


def _is_text(content_type: str) -> bool:
    ct = content_type.lower()
    return any(h in ct for h in _TEXT_HINTS)


def _looks_blocked_or_empty(status: int, text: str) -> bool:
    """Heuristic for 'the static fetch didn't really get the content'."""
    if status in (403, 429) or status >= 500:
        return True
    low = text.lower()
    if any(m in low for m in _BLOCK_MARKERS):
        return True
    # A near-empty body usually means the real content is rendered client-side.
    # Strip tags crudely and see how much visible text is left.
    visible = _strip_tags(text)
    return len(visible) < 200


def _strip_tags(html: str) -> str:
    out, depth = [], 0
    for ch in html:
        if ch == "<":
            depth += 1
        elif ch == ">":
            depth = max(0, depth - 1)
        elif depth == 0:
            out.append(ch)
    return "".join(out).strip()


def _session() -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": get_settings().user_agent, "Accept-Language": "en,de;q=0.8"})
    return s


def _fetch_static(url: str, sess: requests.Session):
    resp = sess.get(url, timeout=get_settings().http_timeout_seconds, allow_redirects=True)
    ct = resp.headers.get("Content-Type", "")
    if _is_text(ct) or (not ct and resp.text):
        return resp.status_code, ct, resp.text, None, False
    return resp.status_code, ct, "", resp.content, True


def _render_on_page(page, url):
    """Robust render: wait for DOM, then give the client-side JS a bounded
    chance to settle. Some UZH pages never reach 'networkidle', so we never
    block on it — we wait up to `render_idle_ms` and move on."""
    from playwright.sync_api import TimeoutError as PWTimeout

    s = get_settings()
    resp = page.goto(url, wait_until="domcontentloaded", timeout=s.http_timeout_seconds * 1000)
    status = resp.status if resp else 0
    try:
        page.wait_for_load_state("networkidle", timeout=s.render_idle_ms)
    except PWTimeout:
        pass  # page keeps a connection open; JS has usually rendered by now
    page.wait_for_timeout(s.render_settle_ms)
    return status, page.content()


def _fetch_render(url: str):
    """One-off Playwright render. Lazy import so the tool works without it."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return None  # Playwright not installed → caller keeps the static result.

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            page = browser.new_context(user_agent=get_settings().user_agent).new_page()
            return _render_on_page(page, url)
        finally:
            browser.close()


@contextmanager
def render_session():
    """A reusable chromium page for rendering many URLs in one batch (avoids a
    fresh browser launch per profile). Yields a `render(url) -> (status, html)`
    callable. No-op yielding None if Playwright is unavailable."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        yield None
        return
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_context(user_agent=get_settings().user_agent).new_page()
        try:
            yield lambda url: _render_on_page(page, url)
        finally:
            browser.close()


def fetch_one(
    source_id: str, url: str, sess: requests.Session | None = None, *, force_render: bool = False
) -> FetchResult:
    sess = sess or _session()

    if force_render:
        # Human-directed override (some UZH people/topic lists are JS-rendered
        # yet still return plenty of chrome text, so the heuristic can't see it).
        rendered = _fetch_render(url)
        if rendered is not None:
            r_status, r_html = rendered
            return FetchResult(
                source_id,
                url,
                ok=200 <= r_status < 300,
                http_status=r_status,
                method="playwright",
                content_type="text/html",
                text=r_html,
            )
        # Playwright unavailable → fall through to static.

    try:
        status, ct, text, raw, is_binary = _fetch_static(url, sess)
    except requests.RequestException as exc:
        return FetchResult(
            source_id,
            url,
            ok=False,
            http_status=0,
            method="error",
            error=f"{type(exc).__name__}: {exc}",
        )

    if is_binary:
        return FetchResult(
            source_id,
            url,
            ok=200 <= status < 300,
            http_status=status,
            method="requests",
            content_type=ct,
            is_binary=True,
            raw_bytes=raw,
        )

    if _looks_blocked_or_empty(status, text):
        rendered = _fetch_render(url)
        if rendered is not None:
            r_status, r_html = rendered
            return FetchResult(
                source_id,
                url,
                ok=200 <= r_status < 300,
                http_status=r_status,
                method="playwright",
                content_type="text/html",
                text=r_html,
            )

    return FetchResult(
        source_id,
        url,
        ok=200 <= status < 300,
        http_status=status,
        method="requests",
        content_type=ct or "text/html",
        text=text,
    )


def fetch_json(url: str, sess: requests.Session | None = None) -> tuple[int, str]:
    """GET a JSON API endpoint, returning (status, raw_text). Used for
    JSON-backed (SPA) sources whose data lives behind an API rather than in
    the rendered HTML."""
    sess = sess or _session()
    resp = sess.get(
        url,
        timeout=get_settings().http_timeout_seconds,
        headers={"Accept": "application/json"},
        allow_redirects=True,
    )
    return resp.status_code, resp.text


def html_fetcher(
    sess: requests.Session | None = None,
    delay: float | None = None,
    renderer: Callable[[str], tuple[int, str]] | None = None,
) -> Callable[[str, bool], tuple[int, str]]:
    """A polite `(url, render) -> (status, html)` fetcher for following sub-pages
    (e.g. person profiles). Sleeps `delay` before each request (the configured
    polite delay when None). When `render` is requested and a reusable `renderer`
    (from render_session) is supplied, it's used instead of launching a browser
    per call."""
    sess = sess or _session()
    wait = get_settings().polite_delay_seconds if delay is None else delay

    def _f(url: str, render: bool = False) -> tuple[int, str]:
        time.sleep(wait)
        if render and renderer is not None:
            return renderer(url)
        r = fetch_one("_subpage", url, sess, force_render=render)
        return r.http_status, ("" if r.is_binary else r.text)

    return _f

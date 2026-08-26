"""Cache layer — decouples fetching from extraction.

Every fetched page lands on disk so that extraction runs read from cache and
never touch the network. Content hashes stored in meta.json are the basis for
page-change detection later.

Layout (per the plan):
    cache/<source_id>/
      page.html          # latest fetched HTML
      meta.json          # fetched_at, http_status, content_sha1, fetch_method, url
      history/<ts>.html  # previous versions (keep `cache_history_keep`, default 3)
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, datetime
from pathlib import Path

from .config import get_settings


def _dir(source_id: str) -> Path:
    return get_settings().cache_dir / source_id


def page_path(source_id: str) -> Path:
    return _dir(source_id) / "page.html"


def meta_path(source_id: str) -> Path:
    return _dir(source_id) / "meta.json"


def history_dir(source_id: str) -> Path:
    return _dir(source_id) / "history"


def json_path(source_id: str) -> Path:
    return _dir(source_id) / "data.json"


def read_json_text(source_id: str) -> str:
    return json_path(source_id).read_text(encoding="utf-8")


def read_json(source_id: str):
    return json.loads(read_json_text(source_id))


def write_json(
    source_id: str, text: str, *, url: str, http_status: int, fetch_method: str = "requests"
) -> dict:
    """Cache a JSON API response (for SPA/JSON-backed sources). Stored as
    data.json next to a meta.json; content_sha1 (over the raw text) drives
    change detection just like HTML pages."""
    d = _dir(source_id)
    d.mkdir(parents=True, exist_ok=True)

    new_hash = content_sha1(text)
    prev_hash = None
    if meta_path(source_id).exists():
        try:
            prev_hash = read_meta(source_id).get("content_sha1")
        except (json.JSONDecodeError, OSError):
            prev_hash = None

    json_path(source_id).write_text(text, encoding="utf-8")
    meta = {
        "source_id": source_id,
        "url": url,
        "fetched_at": datetime.now(UTC).isoformat(),
        "http_status": http_status,
        "fetch_method": fetch_method,
        "content_sha1": new_hash,
        "source_type": "json",
        "content_changed": prev_hash is not None and prev_hash != new_hash,
    }
    with meta_path(source_id).open("w", encoding="utf-8") as fh:
        json.dump(meta, fh, indent=2, ensure_ascii=False)
        fh.write("\n")
    return meta


def _slug(value: str) -> str:
    s = re.sub(r"[^a-zA-Z0-9]+", "-", value).strip("-").lower()
    return s[:80] or hashlib.sha1(value.encode()).hexdigest()[:16]


def subpage_path(source_id: str, kind: str, slug: str) -> Path:
    return _dir(source_id) / kind / f"{slug}.html"


def has_subpage(source_id: str, kind: str, slug: str) -> bool:
    return subpage_path(source_id, kind, slug).exists()


def read_subpage(source_id: str, kind: str, slug: str) -> str:
    return subpage_path(source_id, kind, slug).read_text(encoding="utf-8")


def write_subpage(source_id: str, kind: str, slug: str, html: str) -> Path:
    """Cache a followed sub-page (e.g. a person's profile) under the source."""
    p = subpage_path(source_id, kind, slug)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(html, encoding="utf-8")
    return p


def binary_file(source_id: str) -> Path | None:
    """Path to the cached binary blob (e.g. page.pdf), or None if this source
    isn't a cached binary."""
    if not meta_path(source_id).exists():
        return None
    try:
        bp = read_meta(source_id).get("binary_path")
    except (json.JSONDecodeError, OSError):
        return None
    if not bp:
        return None
    p = _dir(source_id) / bp
    return p if p.exists() else None


def is_cached(source_id: str) -> bool:
    if not meta_path(source_id).exists():
        return False
    if page_path(source_id).exists() or json_path(source_id).exists():
        return True
    # Binary source: meta records the blob filename.
    try:
        bp = read_meta(source_id).get("binary_path")
    except (json.JSONDecodeError, OSError):
        return False
    return bool(bp) and (_dir(source_id) / bp).exists()


def content_sha1(html: str) -> str:
    return hashlib.sha1(html.encode("utf-8")).hexdigest()


def read_page(source_id: str) -> str:
    return page_path(source_id).read_text(encoding="utf-8")


def read_meta(source_id: str) -> dict:
    with meta_path(source_id).open(encoding="utf-8") as fh:
        return json.load(fh)


def _rotate_history(source_id: str) -> None:
    """Move the current page.html into history/, pruning to `cache_history_keep`."""
    current = page_path(source_id)
    if not current.exists():
        return
    hist = history_dir(source_id)
    hist.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    current.replace(hist / f"{ts}.html")

    versions = sorted(hist.glob("*.html"))
    for stale in versions[: -get_settings().cache_history_keep]:
        stale.unlink()


def write_page(source_id: str, html: str, *, url: str, http_status: int, fetch_method: str) -> dict:
    """Write a freshly fetched page + meta. Rotates the previous page into
    history only when the content actually changed. Returns the meta dict."""
    d = _dir(source_id)
    d.mkdir(parents=True, exist_ok=True)

    new_hash = content_sha1(html)
    prev_hash = None
    if meta_path(source_id).exists():
        try:
            prev_hash = read_meta(source_id).get("content_sha1")
        except (json.JSONDecodeError, OSError):
            prev_hash = None

    if page_path(source_id).exists() and prev_hash != new_hash:
        _rotate_history(source_id)

    page_path(source_id).write_text(html, encoding="utf-8")

    meta = {
        "source_id": source_id,
        "url": url,
        "fetched_at": datetime.now(UTC).isoformat(),
        "http_status": http_status,
        "fetch_method": fetch_method,
        "content_sha1": new_hash,
        "content_changed": prev_hash is not None and prev_hash != new_hash,
    }
    with meta_path(source_id).open("w", encoding="utf-8") as fh:
        json.dump(meta, fh, indent=2, ensure_ascii=False)
        fh.write("\n")
    return meta


def _binary_ext(content_type: str) -> str:
    ct = content_type.lower()
    if "pdf" in ct:
        return "pdf"
    if "word" in ct or "msword" in ct or "officedocument.word" in ct:
        return "docx"
    return "bin"


def write_binary(
    source_id: str, data: bytes, *, url: str, http_status: int, fetch_method: str, content_type: str
) -> dict:
    """Cache a non-HTML source (e.g. a PDF thesis-guidelines document).

    The bytes are stored verbatim next to a meta.json that records the
    content_type; there is no page.html for binary sources, and history
    rotation is skipped (deferred until a real change-detection need arises)."""
    d = _dir(source_id)
    d.mkdir(parents=True, exist_ok=True)

    ext = _binary_ext(content_type)
    blob = d / f"page.{ext}"
    blob.write_bytes(data)

    meta = {
        "source_id": source_id,
        "url": url,
        "fetched_at": datetime.now(UTC).isoformat(),
        "http_status": http_status,
        "fetch_method": fetch_method,
        "content_sha1": hashlib.sha1(data).hexdigest(),
        "content_type": content_type,
        "binary": True,
        "binary_path": blob.name,
    }
    with meta_path(source_id).open("w", encoding="utf-8") as fh:
        json.dump(meta, fh, indent=2, ensure_ascii=False)
        fh.write("\n")
    return meta

"""CLI entry point — `python -m thesis_matchmaker.scraper.main <command>`.

main.py stays orchestration-only: it wires argparse to stage functions, owns the
interrupt/resume/state loop, and drives the interactive `onboard` flow. Heavy
lifting lives in fetch / cache / spec_engine / spec_generator / llm_extract.
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import shutil
import subprocess
import sys
import time

import yaml

from thesis_matchmaker import db

from . import (
    cache,
    dataset,
    fetch,
    llm,
    llm_extract,
    registry,
    report,
    spec_engine,
    spec_generator,
    store,
    validate,
)
from .config import get_settings

# --- shared helpers ---------------------------------------------------------


def _select_sources(only: list[str] | None) -> list[registry.Source]:
    sources = registry.all_sources()
    if only:
        by_id = {s.source_id: s for s in sources}
        missing = set(only) - by_id.keys()
        if missing:
            raise SystemExit(f"unknown source_id(s): {', '.join(sorted(missing))}")
        return [by_id[sid] for sid in only]
    return sources


def _contract_spec(src: registry.Source) -> dict | None:
    """Parsed contracts/<id>/spec.yaml if it exists, else None."""
    p = spec_engine.spec_path(src.source_id)
    if not p.exists():
        return None
    return yaml.safe_load(p.read_text(encoding="utf-8"))


def _cache_json_source(state: dict, src: registry.Source, api_url: str):
    """Fetch + cache a JSON/API-backed source's data. Returns meta or None."""
    status, text = fetch.fetch_json(api_url)
    if not (200 <= status < 300):
        registry.update_source_state(
            state,
            src.source_id,
            run=registry.RUN_FAILED,
            last_fetch={"http_status": status, "method": "json"},
        )
        registry.save_state(state)
        return None
    meta = cache.write_json(src.source_id, text, url=api_url, http_status=status)
    registry.update_source_state(
        state,
        src.source_id,
        run=registry.RUN_FETCHED,
        last_fetch={
            "http_status": status,
            "method": "json",
            "content_sha1": meta["content_sha1"],
            "fetched_at": meta["fetched_at"],
        },
    )
    registry.save_state(state)
    return meta


def _fetch_and_cache(state: dict, src: registry.Source, sess, render: bool):
    """Fetch one source into the cache and persist run-state. Returns
    (FetchResult, meta|None). A contract may override the fetched URL via
    `page_url` (e.g. to pull an embedded people/list iframe instead of the wrapper
    page)."""
    url = (_contract_spec(src) or {}).get("page_url") or src.url
    result = fetch.fetch_one(src.source_id, url, sess, force_render=render)
    if not result.ok:
        registry.update_source_state(
            state,
            src.source_id,
            run=registry.RUN_FAILED,
            last_fetch={
                "http_status": result.http_status,
                "method": result.method,
                "error": result.error,
            },
        )
        registry.save_state(state)
        return result, None

    if result.is_binary:
        meta = cache.write_binary(
            src.source_id,
            result.raw_bytes,
            url=url,
            http_status=result.http_status,
            fetch_method=result.method,
            content_type=result.content_type,
        )
    else:
        meta = cache.write_page(
            src.source_id,
            result.text,
            url=url,
            http_status=result.http_status,
            fetch_method=result.method,
        )
    registry.update_source_state(
        state,
        src.source_id,
        run=registry.RUN_FETCHED,
        last_fetch={
            "http_status": result.http_status,
            "method": result.method,
            "content_sha1": meta["content_sha1"],
            "fetched_at": meta["fetched_at"],
        },
    )
    registry.save_state(state)
    return result, meta


# --- fetch command ----------------------------------------------------------


def cmd_fetch(args: argparse.Namespace) -> int:
    state = registry.load_state()
    targets = _select_sources(args.only)

    if args.resume:
        before = len(targets)
        targets = [
            s
            for s in targets
            if registry.source_state(state, s.source_id).get("run") != registry.RUN_FETCHED
            or not cache.is_cached(s.source_id)
        ]
        print(f"resume: {len(targets)} pending of {before} selected")

    if not targets:
        print("nothing to fetch.")
        return 0

    sess = fetch._session()
    fetched = failed = 0
    delay = get_settings().polite_delay_seconds
    print(f"fetching {len(targets)} source(s)  (delay {delay}s)\n")
    try:
        for i, src in enumerate(targets):
            print(f"[{i + 1}/{len(targets)}] {src.source_id}  {src.url}")
            contract = _contract_spec(src)
            # JSON-backed sources (a SPA whose listing lives behind an API) must
            # fetch that API, exactly as `onboard` does. Fetching the page URL
            # instead caches the pre-render HTML shell -- which then satisfies
            # cache.is_cached(), so `run` never fetches the API either and
            # extraction dies on the missing data.json. That is not hypothetical:
            # the first full fetch did it to all three json sources.
            if contract and contract.get("source_type") == "json":
                jmeta = _cache_json_source(state, src, contract["api_url"])
                if jmeta is None:
                    failed += 1
                    print("    FAILED  (json api)")
                else:
                    fetched += 1
                    changed = " (changed)" if jmeta.get("content_changed") else ""
                    print(
                        f"    ok  status={jmeta['http_status']} method=json-api"
                        f"{changed}  sha1={jmeta['content_sha1'][:12]}"
                    )
                if i < len(targets) - 1:
                    time.sleep(delay)
                continue
            # Honour the contract's `render: true` (a JS-rendered listing), so a
            # plain `fetch` doesn't capture the pre-render shell of those pages.
            render = args.render or bool(contract and contract.get("render"))
            result, meta = _fetch_and_cache(state, src, sess, render)
            if meta is None:
                failed += 1
                print(f"    FAILED  status={result.http_status} {result.error}")
            else:
                fetched += 1
                tag = "binary" if result.is_binary else "html"
                changed = " (changed)" if meta.get("content_changed") else ""
                print(
                    f"    ok  status={result.http_status} method={result.method} "
                    f"{tag}{changed}  sha1={meta['content_sha1'][:12]}"
                )
            if i < len(targets) - 1:
                time.sleep(delay)
    except KeyboardInterrupt:
        registry.save_state(state)
        print(f"\ninterrupted — state saved. fetched={fetched} failed={failed}")
        return 130

    print(f"\ndone. fetched={fetched} failed={failed}")
    return 1 if failed else 0


# --- onboard command --------------------------------------------------------

_PEOPLE_HINTS = (
    "people",
    "professor",
    "faculty",
    "supervis",
    "staff",
    "team",
    "contact person",
    "chair",
    "members",
)
_TOPIC_HINTS = ("topic", "open thesis", "list of", "concrete", "projects", "offered")


def propose_page_type(src: registry.Source) -> str:
    """Best-guess page_type from the registry classification + notes + url. The
    human confirms/overrides in onboard."""
    n, u, c = src.notes.lower(), src.url.lower(), src.classification.lower()
    if "no public" in c and not src.notes:
        return "none"
    if any(k in n for k in _PEOPLE_HINTS) or "/people" in u:
        return "people"
    if "concrete topics" in c or any(k in n for k in _TOPIC_HINTS):
        return "topics"
    return "process"


class _Prompter:
    """Wraps input(). When non-interactive (no TTY) or --yes, returns defaults so
    the flow is fully scriptable and testable."""

    def __init__(self, auto: bool):
        self.auto = auto or not sys.stdin.isatty()

    def ask(self, prompt: str, default: str = "") -> str:
        if self.auto:
            print(f"{prompt} [{default}] (auto)")
            return default
        try:
            resp = input(f"{prompt} [{default}]: ").strip()
        except EOFError:
            return default
        return resp or default


def _freeze_contract(
    src: registry.Source, page_type: str, records, meta: dict, spec_yaml: str | None
) -> None:
    cdir = get_settings().specs_dir / src.source_id
    cdir.mkdir(parents=True, exist_ok=True)
    # snapshot: the exact cached content the expectation was verified on.
    # JSON sources snapshot their data.json even if a stale page.html lingers.
    if meta.get("source_type") == "json" and cache.json_path(src.source_id).exists():
        shutil.copyfile(cache.json_path(src.source_id), cdir / "snapshot.json")
    elif cache.page_path(src.source_id).exists():
        shutil.copyfile(cache.page_path(src.source_id), cdir / "snapshot.html")
    elif cache.json_path(src.source_id).exists():
        shutil.copyfile(cache.json_path(src.source_id), cdir / "snapshot.json")
    elif cache.binary_file(src.source_id):
        shutil.copyfile(
            cache.binary_file(src.source_id), cdir / cache.binary_file(src.source_id).name
        )
    if spec_yaml is not None:
        (cdir / "spec.yaml").write_text(spec_yaml, encoding="utf-8")
    # expected.json is the onboarding PROVENANCE snapshot: the full approved
    # records at verification time, INCLUDING enrichment (followed profiles,
    # PDF-parsed supervisors) that is not reproducible offline. It is human-facing
    # documentation, not the test oracle — the contract-replay test asserts
    # against tests/golden_specs.json (the engine's deterministic, offline
    # core extraction). See tests/replay_util.py for why the two differ.
    expected = {
        "source_id": src.source_id,
        "page_type": page_type,
        "verified_content_sha1": meta.get("content_sha1"),
        "records": records,
    }
    (cdir / "expected.json").write_text(
        json.dumps(expected, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )


def _write_preview(preview: dict, source_id: str) -> None:
    out = get_settings().preview_dir / f"{source_id}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(preview, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"  wrote {out}")


def _onboard_process(src, meta, prompter) -> tuple[bool, dict | None]:
    while True:
        rec = llm_extract.extract_process(src.source_id)
        print(
            "\n--- process summary "
            f"(degree={rec['degree_level']}, llm={rec['_llm'].get('status')}) ---"
        )
        print(rec["process_description"] or "(no summary — LLM unavailable)")
        print("  relevant_links:")
        for link in rec["relevant_links"]:
            print(f"    - {link['description'][:50]!r} -> {link['url'][:80]}")
        choice = prompter.ask("[a]pprove / [r]etry / [s]kip", "a").lower()[:1]
        if choice == "a":
            return True, [rec]
        if choice == "s":
            return False, None
        # retry re-runs the LLM (e.g. after a transient failure)


def _onboard_spec(src, meta, page_type, args, prompter) -> tuple[bool, list | None, str | None]:
    hint = args.hint or ""
    spec_yaml = None
    existing = spec_engine.spec_path(src.source_id)
    if existing.exists() and not args.redraft:
        spec_yaml = existing.read_text(encoding="utf-8")
        print(f"  using existing spec at {existing} (pass --redraft to regenerate)")

    while True:
        if spec_yaml is None:
            print("  drafting spec via LLM ...")
            try:
                spec_yaml = spec_generator.draft_spec_yaml(src.source_id, page_type, hint)
            except spec_generator.DraftError as exc:
                print(f"  cannot draft: {exc}")
                return False, None, None

        try:
            spec = yaml.safe_load(spec_yaml)
            records = spec_engine.extract(src.source_id, spec)
        except Exception as exc:  # noqa: BLE001 — show the operator and let them retry
            print(f"  spec did not run: {type(exc).__name__}: {exc}")
            records = []

        print("\n=== drafted spec.yaml ===")
        print(spec_yaml)
        print(f"=== extracted {len(records)} record(s) ===")
        for r in records[:3]:
            print(
                json.dumps(
                    {k: v for k, v in r.items() if not k.startswith("scraped")}, ensure_ascii=False
                )[:400]
            )

        if page_type == "topics" and records:
            _report_title_checks(records, llm_review=getattr(args, "llm_title_review", False))

        if page_type == "people" and records and spec.get("follow"):
            _preview_follow(src, records, spec, args, prompter)

        choice = prompter.ask("[a]pprove / [e]dit / [r]etry-with-hint / [s]kip", "a").lower()[:1]
        if choice == "a":
            return True, records, spec_yaml
        if choice == "s":
            return False, None, None
        if choice == "e":
            spec_engine.spec_path(src.source_id).parent.mkdir(parents=True, exist_ok=True)
            spec_engine.spec_path(src.source_id).write_text(spec_yaml, encoding="utf-8")
            _open_editor(spec_engine.spec_path(src.source_id))
            spec_yaml = spec_engine.spec_path(src.source_id).read_text(encoding="utf-8")
        elif choice == "r":
            hint = prompter.ask("hint for the model", hint)
            spec_yaml = None


def _report_title_checks(records, *, llm_review: bool = False) -> None:
    """Surface title repairs and flags during onboarding, where a human is already
    reviewing. `--llm-title-review` additionally prints the model's opinion on the
    flagged ones — advisory output only, never written to the data, so routine
    runs stay fully deterministic."""
    repaired = [r for r in records if r.get("_title_repair")]
    flagged = [r for r in records if r.get("_title_check")]
    if not (repaired or flagged):
        return
    print(f"\n  title check: {len(repaired)} repaired, {len(flagged)} flagged")
    for r in repaired:
        fx = r["_title_repair"]
        print(f"    repaired via {fx['via']}: {str(fx['from'])[:40]!r} -> {str(fx['to'])[:60]!r}")
    for r in flagged:
        note = r["_title_check"]
        print(
            f"    flagged (score {note['score']}): {str(note.get('title'))[:46]!r}"
            f"  {(note.get('reasons') or [''])[0][:52]}"
        )
    if not (llm_review and flagged):
        return
    if not llm.is_available():
        print("    (--llm-title-review: no LLM configured)")
        return
    system = (
        "You review web-scraped Bachelor/Master thesis topic titles. For each "
        "numbered record answer on ONE line: 'PLAUSIBLE' or 'IMPLAUSIBLE', and "
        "when implausible, quote the string from that record which looks like "
        "the real title. No prose, no preamble."
    )
    prompt = "\n".join(
        f"{i}. title={r.get('title')!r} "
        f"description={str(r.get('topic_description'))[:200]!r} "
        f"research_area={r.get('research_area')!r}"
        for i, r in enumerate(flagged, 1)
    )
    print("    LLM opinion (advisory, not stored):")
    try:
        for line in llm.complete(system, prompt).splitlines():
            if line.strip():
                print(f"      {line.strip()[:110]}")
    except Exception as exc:  # noqa: BLE001 — advisory only, never fail onboarding
        print(f"      (failed: {type(exc).__name__}: {exc})")


def _preview_follow(src, records, spec, args, prompter) -> None:
    follow = spec["follow"]
    cands = spec_engine.follow_candidates(records, follow)
    print(
        f"\n  profile-following: pattern {follow['url_pattern']!r} matches "
        f"{len(cands)}/{len(records)} records"
    )
    for _, url in cands[:3]:
        print(f"    would follow: {url}")
    if args.no_follow:
        print("  (--no-follow: skipping)")
        return
    ok = prompter.ask("  follow a sample to verify the profile template? [y/n]", "y").lower()[:1]
    if ok != "y":
        return
    fetch_fn = fetch.html_fetcher()
    n = spec_engine.enrich_from_profiles(
        src.source_id, records, spec, fetch_fn=fetch_fn, limit=args.profile_limit, log=print
    )
    print(f"  enriched {n} sample profile(s). example:")
    for r in records:
        if r.get("email") or r.get("bio"):
            print(
                "   ",
                json.dumps(
                    {k: r.get(k) for k in ("name", "email", "research_interest", "bio")},
                    ensure_ascii=False,
                )[:400],
            )
            break


def _open_editor(path) -> None:
    editor = shutil.which("nano") or "vi"
    import os

    subprocess.call([os.environ.get("EDITOR", editor), str(path)])


def cmd_onboard(args: argparse.Namespace) -> int:
    state = registry.load_state()
    prompter = _Prompter(args.yes)

    # resolve target source
    if args.next:
        src = next(
            (
                s
                for s in registry.all_sources()
                if registry.source_state(state, s.source_id).get("onboarding")
                == registry.ONBOARD_UNVERIFIED
            ),
            None,
        )
        if src is None:
            print("no unverified sources left.")
            return 0
    elif args.source_id:
        src = registry.get_source(args.source_id)
    else:
        raise SystemExit("give a source_id or --next")

    print(f"onboarding {src.source_id}  ({src.unit})\n  {src.url}\n  note: {src.notes}")

    # 1. fetch + cache (JSON-backed sources fetch their API, not the SPA page)
    contract = _contract_spec(src)
    is_json = bool(contract and contract.get("source_type") == "json")
    if not cache.is_cached(src.source_id) or args.refetch:
        print(f"  fetching {'(json api)' if is_json else ''}...")
        if is_json:
            meta = _cache_json_source(state, src, contract["api_url"])
        else:
            _, meta = _fetch_and_cache(state, src, fetch._session(), args.render)
        if meta is None:
            print("  fetch failed — aborting.")
            return 1
    meta = cache.read_meta(src.source_id)

    # 2. propose page_type
    proposed = args.page_type or propose_page_type(src)
    pt = prompter.ask("page_type? (process/topics/people/none)", proposed).lower()

    # 3+4. branch + approval loop
    spec_yaml = None
    if pt == "process":
        approved, records = _onboard_process(src, meta, prompter)
        preview = llm_extract.to_preview(src, records[0]) if approved else None
    elif pt in ("topics", "people"):
        approved, records, spec_yaml = _onboard_spec(src, meta, pt, args, prompter)
        preview = spec_engine.to_preview(src, records) if approved else None
    elif pt == "none":
        approved, records = True, []
        preview = {
            "faculty": src.faculty,
            "unit": src.unit,
            "source_id": src.source_id,
            "page_type": "none",
            "records": [],
        }
    else:
        print(f"  unknown page_type {pt!r} — aborting.")
        return 1

    if not approved:
        print("  skipped — not verified.")
        return 0

    # 5. freeze contract + state + preview
    _freeze_contract(src, pt, records, meta, spec_yaml)
    registry.update_source_state(
        state,
        src.source_id,
        onboarding=registry.ONBOARD_VERIFIED,
        page_type=pt,
        verified_sha1=meta.get("content_sha1"),
    )
    registry.save_state(state)
    _write_preview(preview, src.source_id)
    print(f"  VERIFIED {src.source_id} as {pt}. contract frozen.")
    return 0


# --- run command ------------------------------------------------------------


def _page_html(src, url, fetch_fn):
    """HTML + base URL for one page of a multi-page source. The primary URL uses
    the already-cached main page; others are fetched once and cached as subpages."""
    if url == src.url and cache.is_cached(src.source_id):
        return cache.read_page(src.source_id), cache.read_meta(src.source_id).get("url", url)
    slug = spec_engine._link_slug(url)
    if cache.has_subpage(src.source_id, "pages", slug):
        return cache.read_subpage(src.source_id, "pages", slug), url
    try:
        st, html = fetch_fn(url, False)
    except Exception:  # noqa: BLE001
        return "", url
    if html and 200 <= st < 300:
        cache.write_subpage(src.source_id, "pages", slug, html)
        return html, url
    return "", url


def _combined_process_html(src, urls, fetch_fn):
    """Concatenate the main content of a process hub page and each listed
    subpage into a single <main> blob, so the LLM produces ONE process summary
    covering all of them (e.g. a Bachelor page whose two tracks live on
    dedicated subpages). Links from every page are preserved."""
    from bs4 import BeautifulSoup

    parts = []
    for url in [src.url, *urls]:
        html, _ = _page_html(src, url, fetch_fn)
        if not html:
            continue
        node = llm_extract._main_node(BeautifulSoup(html, "html.parser"))
        if node:
            parts.append(node.decode())
    return "<main>" + "\n".join(parts) + "</main>"


def _extract_multi_page(src, spec, fetch_fn):
    """Extract a source spread across several pages (e.g. master + bachelor topic
    lists). Each page's records get its `set:` field overrides; records are then
    merged by `merge_by` (default title), unioning degree_level across pages so a
    topic offered for both degrees becomes 'Bachelor, Master'."""
    key = spec.get("merge_by", "title")
    pages = list(spec.get("pages") or [])
    pf = spec.get("pages_from")  # auto-discover subpages from a directory page
    if pf:
        from urllib.parse import urljoin

        from bs4 import BeautifulSoup

        dhtml, dbase = _page_html(src, pf.get("url") or src.url, fetch_fn)
        pat = re.compile(pf["url_pattern"], re.I) if pf.get("url_pattern") else None
        seen = set()
        for a in BeautifulSoup(dhtml or "", "html.parser").select(pf.get("selector", "a[href]")):
            href = a.get("href", "")
            if not href:
                continue
            url = urljoin(dbase, href)
            if (pat and not pat.search(url)) or url in seen:
                continue
            seen.add(url)
            pages.append({"url": url})
    merged: dict[str, dict] = {}
    order: list[str] = []
    for pg in pages:
        html, base = _page_html(src, pg["url"], fetch_fn)
        if not html:
            continue
        for r in spec_engine.extract(src.source_id, spec, html=html, base_url=base):
            for k, v in (pg.get("set") or {}).items():
                r[k] = v
            mk = (r.get(key) or "").strip().lower()
            if mk not in merged:
                merged[mk] = r
                order.append(mk)
            else:
                degs: set[str] = set()
                for d in (merged[mk].get("degree_level"), r.get("degree_level")):
                    if d:
                        degs.update(x.strip() for x in re.split(r"[,/]", d) if x.strip())
                if degs:
                    merged[mk]["degree_level"] = (
                        "Bachelor, Master"
                        if {"Bachelor", "Master"} <= degs
                        else ", ".join(sorted(degs))
                    )
    return [merged[k] for k in order]


def _extract_source(src, page_type, fetch_fn):
    """Extract records for one verified source, routed by page_type. Returns
    (records, llm_ok)."""
    if page_type == "process":
        cspec = _contract_spec(src) or {}
        inc = cspec.get("include_pages")  # fold subpages into ONE combined summary
        if inc:
            html = _combined_process_html(src, inc, fetch_fn)
            rec = llm_extract.extract_process(src.source_id, html=html, base_url=src.url)
        else:
            rec = llm_extract.extract_process(src.source_id)
        override = cspec.get("degree_level")  # e.g. a page
        if override:  # that covers BA+MA
            rec["degree_level"] = override
        return [rec], rec["_llm"].get("status") == "ok"
    if page_type == "none":
        return [], True
    spec = spec_engine.load_spec(src.source_id)
    if spec.get("pages") or spec.get("pages_from"):
        records = _extract_multi_page(src, spec, fetch_fn)
    else:
        records = spec_engine.extract(src.source_id, spec)
    if spec.get("roster"):  # keep only people named on a roster page (+ tag them)
        records = _apply_roster_filter(src, records, spec["roster"], fetch_fn)
    if spec.get("keep_if"):  # keep only records whose field contains one of the strings
        kc = spec["keep_if"]
        subs = [s.lower() for s in (kc.get("contains_any") or [])]
        fld = kc["field"]
        records = [
            r
            for r in records
            if isinstance(r.get(fld), str) and any(s in r[fld].lower() for s in subs)
        ]
    if spec.get("directory"):  # resolve missing profile URLs via a people page
        _resolve_via_directory(src, records, spec["directory"], fetch_fn)
    if spec.get("follow"):
        kind = "people" if page_type == "people" else "topics"
        spec_engine.enrich_from_links(
            src.source_id,
            records,
            spec["follow"],
            kind=kind,
            fetch_fn=fetch_fn,
            limit=None,
            log=lambda m: None,
        )
    if spec.get("resolve_person_emails"):  # pick the personal (non-secretariat) email
        _resolve_person_emails(src, records, spec["resolve_person_emails"], fetch_fn)
    if page_type == "people":  # drop internal helper keys, but KEEP _profile_url
        for rec in records:
            for k in [k for k in rec if k.startswith("_") and k != "_profile_url"]:
                del rec[k]
    if spec.get("pdf_summary"):
        _summarize_topic_pdfs(src, records, spec["pdf_summary"])
    if spec.get("pdf_enrich"):
        _enrich_topic_pdfs(src, records, spec["pdf_enrich"])
    if spec.get("resolve_supervisors"):
        _resolve_supervisor_emails(src, records, spec["resolve_supervisors"], fetch_fn)
    if spec.get("pair_supervisor_emails"):
        _pair_supervisor_emails(records, spec["pair_supervisor_emails"])
    if page_type == "topics":
        for rec in records:
            spec_engine.normalize_supervisors(rec)
    return records, True


def _resolve_supervisors_via_directory(src, records, cfg, fetch_fn) -> None:
    """Fill each topic supervisor's email by matching their name against a
    people-directory page to find a profile, then following it for the email.
    Directory and profile fetches are cached and deduped."""
    import re
    from urllib.parse import urljoin

    from bs4 import BeautifulSoup

    dir_cfg = cfg["directory"]
    email_spec = cfg["email"]
    if cache.has_subpage(src.source_id, "aux", "directory"):
        html = cache.read_subpage(src.source_id, "aux", "directory")
    else:
        try:
            status, html = fetch_fn(dir_cfg["url"], bool(dir_cfg.get("render")))
        except Exception:  # noqa: BLE001
            return
        if not html or not (200 <= status < 300):
            return
        cache.write_subpage(src.source_id, "aux", "directory", html)

    def norm(n):
        n = re.sub(r"\b\w\.\s*", " ", n or "")  # drop single-letter initials
        return n.strip().lower().replace("  ", " ")

    def first_last(n):
        """First + last token key, so 'Juri Opitz' matches 'Juri Alexander Opitz'."""
        toks = norm(n).split()
        return f"{toks[0]} {toks[-1]}" if len(toks) >= 2 else None

    pat = re.compile(dir_cfg.get("profile_pattern", r"/[^/]+\.html$"))
    strip = dir_cfg.get("name_strip")
    dmap: dict[str, str] = {}
    flmap: dict[str, str] = {}
    for a in BeautifulSoup(html, "html.parser").select("a[href]"):
        href = a.get("href", "")
        if not pat.search(href):
            continue
        text = a.get_text(" ", strip=True)
        if strip:
            text = re.sub(strip, "", text).strip()
        text = spec_engine._t_strip_titles(text)
        if text and len(text) > 3 and not text.lower().startswith("http"):
            url = urljoin(dir_cfg["url"], href)
            dmap.setdefault(norm(text), url)
            fl = first_last(text)
            if fl:
                flmap.setdefault(fl, url)

    def lookup(name):
        return dmap.get(norm(name)) or flmap.get(first_last(name) or "")

    email_by_url: dict[str, str | None] = {}
    for rec in records:
        for sup in rec.get("supervisors") or []:
            if sup.get("email"):
                continue
            url = lookup(sup.get("name", ""))
            if not url:
                continue
            if url not in email_by_url:
                slug = spec_engine._link_slug(url)
                phtml = None
                if cache.has_subpage(src.source_id, "people", slug):
                    phtml = cache.read_subpage(src.source_id, "people", slug)
                else:
                    try:
                        st, phtml = fetch_fn(url, False)
                    except Exception:  # noqa: BLE001
                        phtml = None
                    if phtml and 200 <= st < 300:
                        cache.write_subpage(src.source_id, "people", slug, phtml)
                    else:
                        phtml = None
                email_by_url[url] = (
                    spec_engine._extract_field(BeautifulSoup(phtml, "html.parser"), email_spec, url)
                    if phtml
                    else None
                )
            if email_by_url[url]:
                sup["email"] = email_by_url[url]

    # Fallback: fill still-missing emails from mailto links on the topic's own
    # detail page, matched to the supervisor by surname. Catches people absent
    # from the directory (externals, alumni, cross-institute supervisors).
    def fold(s):
        s = (s or "").lower()
        for a, b in (("ä", "ae"), ("ö", "oe"), ("ü", "ue"), ("ß", "ss")):
            s = s.replace(a, b)
        return re.sub(r"[^a-z]", "", s)

    for rec in records:
        pending = [s for s in (rec.get("supervisors") or []) if not s.get("email")]
        link = rec.get("source_link")
        slug = spec_engine._link_slug(link) if link else None
        if not pending or not slug or not cache.has_subpage(src.source_id, "topics", slug):
            continue
        soup = BeautifulSoup(cache.read_subpage(src.source_id, "topics", slug), "html.parser")
        addrs = []
        for a in soup.select('main a[href^="mailto"]'):
            addr = a.get("href", "")[7:].split("?")[0].strip()
            if "@" in addr:
                addrs.append(addr)
        for sup in pending:
            toks = re.sub(r"[^a-zäöüß\s]", " ", (sup.get("name") or "").lower()).split()
            surname = fold(toks[-1]) if toks else ""
            if len(surname) < 3:
                continue
            for addr in addrs:
                if surname in fold(addr.split("@")[0]):
                    sup["email"] = addr
                    break


def _resolve_person_emails(src, records, cfg, fetch_fn) -> None:
    """Pick each person's PERSONAL email from their profile page: the mailto whose
    local part matches the person's name and is not a secretariat/office address
    (excluded prefixes). Leaves email null when no personal address is published
    (better than storing a secretariat or placeholder address). Profiles are
    cached (shared with the follow step)."""
    from bs4 import BeautifulSoup

    exclude = tuple(cfg.get("exclude_prefix", ["sek"]))
    for rec in records:
        url = rec.get("_profile_url")
        if not url:
            continue
        slug = spec_engine._link_slug(url)
        if cache.has_subpage(src.source_id, "people", slug):
            html = cache.read_subpage(src.source_id, "people", slug)
        else:
            try:
                st, html = fetch_fn(url, False)
            except Exception:  # noqa: BLE001
                html = None
            if html and 200 <= st < 300:
                cache.write_subpage(src.source_id, "people", slug, html)
            else:
                html = None
        if not html:
            continue
        soup = BeautifulSoup(html, "html.parser")
        main = soup.select_one("main") or soup
        name_parts = re.split(r"\s+", rec.get("name") or "")
        toks = [t for t in (_fold_name(x) for x in name_parts) if len(t) >= 3]
        best, best_score = None, 0
        for a in main.select('a[href^="mailto"]'):
            addr = a.get("href", "")[7:].split("?")[0].strip()
            if "@" not in addr:
                continue
            local = _fold_name(addr.split("@")[0])
            if not local or local.startswith(exclude):
                continue
            score = sum(1 for t in toks if t in local)
            if score == 0:
                continue
            if score > best_score or (
                score == best_score and best and len(local) < len(_fold_name(best.split("@")[0]))
            ):
                best, best_score = addr, score
        rec["email"] = best  # personal-only; null when none matches the name


def _apply_roster_filter(src, records, cfg, fetch_fn):
    """Keep only the people who appear on one or more 'roster' pages (e.g. the
    supervisor <select> on a thesis-booking form), matched by name. Each roster
    source may tag its members (e.g. supervises Bachelor/Master); tags are
    unioned onto the kept records. Roster pages are cached."""
    from bs4 import BeautifulSoup

    def norm(n):
        n = re.sub(r"\b\w\.\s*", " ", n or "")  # drop middle initials ("J.")
        return re.sub(r"\s+", " ", n).strip().lower()

    roster: dict[str, set] = {}
    for s in cfg.get("sources", []):
        url = s["url"]
        slug = spec_engine._link_slug(url)
        if cache.has_subpage(src.source_id, "aux", slug):
            html = cache.read_subpage(src.source_id, "aux", slug)
        else:
            try:
                st, html = fetch_fn(url, bool(s.get("render")))
            except Exception:  # noqa: BLE001
                html = None
            if html and 200 <= st < 300:
                cache.write_subpage(src.source_id, "aux", slug, html)
            else:
                html = None
        if not html:
            continue
        names_re = s.get("names_regex")
        for node in BeautifulSoup(html, "html.parser").select(s["selector"]):
            txt = node.get_text(" ", strip=True)
            if names_re:  # pull several names out of one element
                found = [
                    m.group(1) if m.groups() else m.group(0) for m in re.finditer(names_re, txt)
                ]
            elif "," in txt:  # one name per element ("Lastname, Firstname")
                found = [txt]
            else:  # skip "(bitte wählen)" / non-name options
                found = []
            for nm in found:
                roster.setdefault(norm(nm), set())
                if s.get("supervises"):
                    roster[norm(nm)].add(s["supervises"])
    field = cfg.get("match", "name")
    kept = []
    for r in records:
        tags = roster.get(norm(r.get(field)))
        if tags is None:
            continue
        if tags:
            r["supervises"] = sorted(tags)
        kept.append(r)
    return kept


def _resolve_via_directory(src, records, cfg, fetch_fn) -> None:
    """Set each person's profile URL by matching their name against a people-
    directory page (used when the topics page names people but only some link
    to their profile). Fetched directory is cached to avoid refetching."""
    import re
    from urllib.parse import urljoin

    from bs4 import BeautifulSoup

    url_field = cfg.get("url_field", "_profile_url")
    if all(r.get(url_field) for r in records):
        return
    if cache.has_subpage(src.source_id, "aux", "directory"):
        html = cache.read_subpage(src.source_id, "aux", "directory")
    else:
        try:
            status, html = fetch_fn(cfg["url"], bool(cfg.get("render")))
        except Exception:  # noqa: BLE001
            return
        if not html or not (200 <= status < 300):
            return
        cache.write_subpage(src.source_id, "aux", "directory", html)

    def norm(n):
        return re.sub(r"\b\w\.\s*", " ", n or "").strip().lower().replace("  ", " ")

    pat = re.compile(cfg.get("profile_pattern", r"/[^/]+\.html$"))
    strip = cfg.get("name_strip")
    dmap = {}
    for a in BeautifulSoup(html, "html.parser").select("a[href]"):
        href = a.get("href", "")
        if not pat.search(href):
            continue
        text = a.get_text(" ", strip=True)
        if strip:
            text = re.sub(strip, "", text).strip()
        if text and len(text) > 3 and not text.lower().startswith("http"):
            dmap.setdefault(norm(text), urljoin(cfg["url"], href))
    for rec in records:
        if not rec.get(url_field):
            hit = dmap.get(norm(rec.get("name", "")))
            if hit:
                rec[url_field] = hit


def _resolve_supervisor_emails(src, records, cfg, fetch_fn) -> None:
    """Fill each supervisor's email by following its profile link (one fetch per
    distinct profile, cached). Used when a topic lists reference persons as
    links but not their emails (e.g. SCG's table)."""
    from bs4 import BeautifulSoup

    list_field = cfg.get("list_field", "supervisors")
    url_key = cfg.get("url_key", "_url")
    email_spec = cfg.get("email")
    render = bool(cfg.get("render"))

    # record_url mode: follow ONE page per record (e.g. a group leader's page,
    # derived from the topic URL) and set the topic's single supervisor's name
    # and email from it.
    rec_url_key = cfg.get("record_url")
    if rec_url_key:
        name_spec = cfg.get("name")
        seen: dict[str, dict] = {}
        for rec in records:
            url = rec.get(rec_url_key)
            if not url or not url.lower().startswith("http"):
                continue
            if url not in seen:
                slug = spec_engine._link_slug(url)
                html = (
                    cache.read_subpage(src.source_id, "people", slug)
                    if cache.has_subpage(src.source_id, "people", slug)
                    else None
                )
                if html is None and fetch_fn is not None:
                    try:
                        st, html = fetch_fn(url, render)
                    except Exception:  # noqa: BLE001
                        html = None
                    if html and 200 <= st < 300:
                        cache.write_subpage(src.source_id, "people", slug, html)
                    else:
                        html = None
                soup = BeautifulSoup(html, "html.parser") if html else None
                seen[url] = {
                    "name": (
                        spec_engine._extract_field(soup, name_spec, url)
                        if (soup and name_spec)
                        else None
                    ),
                    "email": spec_engine._extract_field(soup, email_spec, url) if soup else None,
                }
            info = seen[url]
            if info["name"] or info["email"]:
                rec["supervisors"] = [{"name": info["name"], "email": info["email"]}]
        return

    resolved: dict[str, str | None] = {}
    for rec in records:
        for sup in rec.get(list_field) or []:
            url = sup.pop(url_key, None)
            if not url or not url.lower().startswith("http"):
                continue  # e.g. a mailto contact already carries its email
            if url not in resolved:
                slug = spec_engine._link_slug(url)
                html = None
                if cache.has_subpage(src.source_id, "people", slug):
                    html = cache.read_subpage(src.source_id, "people", slug)
                elif fetch_fn is not None:
                    try:
                        status, html = fetch_fn(url, render)
                    except Exception:  # noqa: BLE001
                        html = None
                    if html and 200 <= status < 300:
                        cache.write_subpage(src.source_id, "people", slug, html)
                    else:
                        html = None
                email = None
                if html:
                    email = spec_engine._extract_field(
                        BeautifulSoup(html, "html.parser"), email_spec, url
                    )
                resolved[url] = email
            if resolved[url]:
                sup["email"] = resolved[url]


def _summarize_topic_pdfs(src, records, cfg) -> int:
    """For records whose link field is a PDF, fetch it, LLM-summarize it into
    the target field, and cache the summary so re-runs don't re-fetch/re-spend."""
    url_field = cfg.get("url_field", "source_link")
    into = cfg.get("into", "topic_description")
    sess = fetch._session()
    n = 0
    for rec in records:
        if rec.get(into):
            continue  # keep an inline description; PDF summary is a fallback
        url = rec.get(url_field)
        if not url or ".pdf" not in url.lower():
            continue
        slug = spec_engine._link_slug(url)
        if cache.has_subpage(src.source_id, "pdfsummary", slug):
            rec[into] = cache.read_subpage(src.source_id, "pdfsummary", slug)
            n += 1
            continue
        try:
            result = fetch.fetch_one("_pdf", url, sess)
        except Exception:  # noqa: BLE001
            continue
        if not result.is_binary or not result.raw_bytes:
            continue
        summary = llm_extract.summarize_pdf_bytes(result.raw_bytes)
        if summary:
            cache.write_subpage(src.source_id, "pdfsummary", slug, summary)
            rec[into] = summary
            n += 1
    return n


_ROLE_NAME_RE = re.compile(
    r"([A-ZÄÖÜ][a-zäöü]+(?:\s+[A-ZÄÖÜ]\.)?(?:\s+[A-ZÄÖÜ][a-zäöü]+){1,2})"
    r"\s*\((?:assistant|supervisor|co-supervisor)\)",
    re.I,
)


def _fold_name(s: str) -> str:
    s = (s or "").lower()
    for a, b in (("ä", "ae"), ("ö", "oe"), ("ü", "ue"), ("ß", "ss")):
        s = s.replace(a, b)
    return re.sub(r"[^a-z]", "", s)


def _pdf_supervisor_names(text: str) -> list[str]:
    """Names tagged '(assistant)'/'(supervisor)' in a PDF's Supervision block.
    The professor prefix in run-together lines like 'Prof. Dr. R. Pajarola <Name>
    (assistant)' is stripped by detecting 'Prof. Dr. ...' names generically."""
    prof_tokens: set[str] = set()
    # exactly First+Last, so a run-together "Prof. Dr. R. Pajarola <Assistant>"
    # line doesn't swallow the assistant's given name into the professor.
    for m in re.finditer(
        r"Prof\.?\s*Dr\.?\s+"
        r"([A-ZÄÖÜ][a-zäöü]+\s+[A-ZÄÖÜ][a-zäöü]+)",
        text,
    ):
        prof_tokens.update(m.group(1).split())
    region_m = re.search(r"Supervision(.{0,400}?)(?:Contact|References)", text, re.S | re.I)
    region = region_m.group(1) if region_m else text
    out: list[str] = []
    for c in _ROLE_NAME_RE.findall(region):
        toks = c.split()
        while toks and toks[0] in prof_tokens:  # drop leading professor name
            toks.pop(0)
        name = " ".join(toks).strip()
        if name and name not in out:
            out.append(name)
    return out


def _name_for_email(email: str, names: list[str]) -> str | None:
    """Match an email to a supervision name by the local part (a name token,
    e.g. surname or first name, appears in it: xtan→Tan, haiyan→Haiyan)."""
    lp = _fold_name(email.split("@")[0])
    best = None
    for c in names:
        for tok in c.split():
            ft = _fold_name(tok)
            if len(ft) >= 3 and ft in lp and (best is None or len(ft) > best[1]):
                best = (c, len(ft))
    return best[0] if best else None


def _pair_supervisor_emails(records, cfg) -> None:
    """Attach emails (from a parallel list field, e.g. a detail page's Email
    cell) to a record's named supervisors: by surname/first-name match against
    each address's local part, falling back to positional order when counts
    match. The helper email-list field is dropped afterwards."""
    field = cfg.get("emails_field", "_emails")
    for rec in records:
        emails = rec.pop(field, None) or []
        sups = rec.get("supervisors") or []
        if not emails or not sups:
            continue
        used = set()
        for sup in sups:
            if sup.get("email"):
                continue
            toks = [_fold_name(t) for t in re.split(r"\s+", sup.get("name") or "")]
            toks = [t for t in toks if len(t) >= 3]
            for e in emails:
                if e in used:
                    continue
                lp = _fold_name(e.split("@")[0])
                if any(t in lp for t in toks):
                    sup["email"] = e
                    used.add(e)
                    break
        # positional fallback when nothing matched but the lists line up 1:1
        if len(sups) == len(emails) and not any(s.get("email") for s in sups):
            for sup, e in zip(sups, emails, strict=True):
                sup["email"] = e


def _enrich_topic_pdfs(src, records, cfg) -> int:
    """For topic records that link to a project PDF, parse the PDF once to fill
    a fuller LLM description, the supervisor email(s), and the degree level.
    The extracted PDF text and its summary are cached so re-runs don't re-spend."""
    import re

    url_field = cfg.get("url_field", "source_link")
    summary_into = cfg.get("summary_into", "topic_description")
    want_sups = bool(cfg.get("supervisors"))
    degree_into = cfg.get("degree_into")
    # emails on any uzh.ch subdomain (ifi.uzh.ch, uzh.ch, ...)
    email_re = re.compile(r"[\w.\-]+@(?:[\w-]+\.)*uzh\.ch", re.I)
    sess = fetch._session()
    n = 0
    for rec in records:
        url = rec.get(url_field)
        if not url or ".pdf" not in url.lower():
            continue
        slug = spec_engine._link_slug(url)
        # PDF text (cached), ligatures normalised so emails parse cleanly.
        if cache.has_subpage(src.source_id, "pdftext", slug):
            text = cache.read_subpage(src.source_id, "pdftext", slug)
        else:
            try:
                result = fetch.fetch_one("_pdf", url, sess)
            except Exception:  # noqa: BLE001
                continue
            if not result.is_binary or not result.raw_bytes:
                continue
            text = (
                llm_extract._pdf_text_from_bytes(result.raw_bytes)
                .replace("ﬁ", "fi")
                .replace("ﬂ", "fl")
            )
            if not text:
                continue
            cache.write_subpage(src.source_id, "pdftext", slug, text)
        n += 1

        if summary_into and not rec.get(summary_into):
            if cache.has_subpage(src.source_id, "pdfsummary", slug):
                rec[summary_into] = cache.read_subpage(src.source_id, "pdfsummary", slug)
            else:
                summ = llm_extract.summarize_pdf_text(text)
                if summ:
                    cache.write_subpage(src.source_id, "pdfsummary", slug, summ)
                    rec[summary_into] = summ

        # By default PDF supervisors only fill when the record has none. With
        # `supervisors_from_pdf`, a PDF that yields an email REPLACES the record's
        # (e.g. surname-only) supervisors with the PDF's fuller name+email pairs;
        # records whose PDF has no email keep their existing supervisors.
        if want_sups and (cfg.get("supervisors_from_pdf") or not rec.get("supervisors")):
            # `supervisor_email_any` widens the match beyond @uzh.ch (some PDFs
            # list external co-supervisors).
            er = (
                re.compile(r"[\w.\-]+@[\w.\-]+\.\w{2,}")
                if cfg.get("supervisor_email_any")
                else email_re
            )
            emails = list(dict.fromkeys(er.findall(text)))
            if emails:
                names = _pdf_supervisor_names(text)
                # also names after a Dr./Prof. title (e.g. "Supervision: Dr X ...")
                for m in re.finditer(
                    r"(?:Dr|Prof)\.?\s+(?:Dr\.?\s+)?"
                    r"([A-ZÄÖÜ][A-Za-zäöü'\-]+(?:\s+[A-ZÄÖÜ][A-Za-zäöü'\-]+){1,2})",
                    text,
                ):
                    nm = m.group(1).strip()
                    if nm not in names:
                        names.append(nm)
                rec["supervisors"] = [
                    {"name": _name_for_email(e, names), "email": e} for e in emails
                ]

        if degree_into and not rec.get(degree_into):
            low = text.lower()
            # explicit phrases only (avoid incidental "master"/"bachelor" words)
            has_m = bool(re.search(r"master(?:'s)?\s+(?:thesis|project)|\bmsc\b|\bm\.sc", low))
            has_b = bool(re.search(r"bachelor(?:'s)?\s+(?:thesis|project)|\bbsc\b|\bb\.sc", low))
            rec[degree_into] = (
                "Bachelor, Master"
                if has_m and has_b
                else "Master"
                if has_m
                else "Bachelor"
                if has_b
                else cfg.get("degree_default")
            )
    return n


def _flag_llm_outage(result, spec) -> None:
    """Name the LLM as the suspect when it plausibly is.

    A spec whose descriptions come from LLM enrichment (pdf_enrich / pdf_summary)
    fails schema validation whenever the LLM is down -- the records extract fine,
    only the enriched field is None. Without this hint the flag reads exactly like
    page drift; it misdiagnosed ifi--17 that way on the first full run (the LLM was
    out of credits, the page had not changed at all). Prepended, not appended: the
    per-source line and the summary table print only reasons[0], and when the LLM
    is down this IS the headline. No new status, no state change.
    """
    if (
        result.status == validate.SCHEMA_INVALID
        and spec
        and (spec.get("pdf_enrich") or spec.get("pdf_summary"))
        and not llm.is_available()
    ):
        result.reasons.insert(
            0,
            "LLM unavailable for pdf enrichment -- descriptions may be "
            "missing because of that, not because the page changed",
        )


def _ensure_main_cached(state, src, page_type) -> bool:
    """Guarantee a usable cached main page, fetching once if needed (retry-once
    for fetch_failed). Returns True if the source is now cached."""
    if cache.is_cached(src.source_id):
        return True
    contract = _contract_spec(src)
    if contract and contract.get("source_type") == "json":
        return _cache_json_source(state, src, contract["api_url"]) is not None
    render = bool(contract.get("render")) if contract else False
    _, meta = _fetch_and_cache(state, src, fetch._session(), render)
    return meta is not None


# Deterministic statuses that warrant an LLM rescue attempt: the template either
# matched nothing (extract_failed) or matched only malformed records
# (schema_invalid). page_changed / ok already produced schema-valid data.
_FALLBACK_TRIGGERS = (validate.EXTRACT_FAILED, validate.SCHEMA_INVALID)


def _should_try_fallback(use_fallback: bool, page_type: str, status: str) -> bool:
    return use_fallback and page_type in ("topics", "people") and status in _FALLBACK_TRIGGERS


def cmd_run(args: argparse.Namespace) -> int:
    state = registry.load_state()
    selected = _select_sources(args.only)
    verified = [
        s
        for s in selected
        if registry.source_state(state, s.source_id).get("onboarding") == registry.ONBOARD_VERIFIED
    ]
    skipped = [s for s in selected if s not in verified]
    if skipped:
        print(
            f"skipping {len(skipped)} not-yet-verified source(s): "
            + ", ".join(s.source_id for s in skipped[:8])
            + (" ..." if len(skipped) > 8 else "")
        )

    # Captured before --resume filtering, which legitimately empties `verified`
    # once every source is done. "Nothing was verified in the first place" is a
    # different situation and must not share its exit code.
    none_verified = not verified

    if args.resume:
        before = len(verified)
        verified = [
            s
            for s in verified
            if registry.source_state(state, s.source_id).get("run") != registry.RUN_DONE
        ]
        print(f"resume: {len(verified)} of {before} verified sources still pending")

    if not verified:
        if none_verified and selected:
            # Loud, because this is the shape a fresh deployment has: verification
            # lives only in var/state.json, which is gitignored, so a pod with an
            # empty volume sees 0 verified sources however many specs are committed.
            # Exiting 0 here made a CronJob that wrote nothing report Success.
            print(
                f"nothing to run: none of the {len(selected)} selected source(s) is marked "
                f"verified in {get_settings().state_path}. Onboard them "
                f"(`onboard --next`) or restore that file -- the committed specs under "
                f"{get_settings().specs_dir} are not sufficient on their own."
            )
            return 1
        print("nothing to run.")
        return 0

    # LLM rescue for sources whose template matched nothing (on by default;
    # no-ops gracefully when no LLM is configured).
    use_fallback = not getattr(args, "no_llm_fallback", False)
    if use_fallback and not llm.is_available():
        use_fallback = False
        print("(no LLM configured — extract_failed sources won't be rescued)\n")

    data = dataset.load()
    rep = report.new_report()

    def _wants_browser(s):
        sp = _contract_spec(s) or {}
        follows = [f for f in (sp.get("follow"), sp.get("people", {}).get("follow")) if f]
        if any(f.get("render") for f in follows):
            return True
        # a JS-rendered people listing implies its profiles likely render too
        return sp.get("page_type") == "people" and bool(sp.get("render"))

    needs_browser = any(_wants_browser(s) for s in verified)

    print(
        f"running {len(verified)} verified source(s)"
        f"{' (browser on for profile following)' if needs_browser else ''}\n"
    )

    with fetch.render_session() if needs_browser else _null_ctx() as renderer:
        fetch_fn = fetch.html_fetcher(renderer=renderer)  # always available for follows
        try:
            for i, src in enumerate(verified):
                st = registry.source_state(state, src.source_id)
                page_type = st.get("page_type", "process")
                print(f"[{i + 1}/{len(verified)}] {src.source_id} ({page_type})")

                if not _ensure_main_cached(state, src, page_type):
                    result = validate.Result(
                        src.source_id,
                        validate.FETCH_FAILED,
                        page_type,
                        reasons=["fetch failed / not cached"],
                    )
                    _apply_result(state, data, src, page_type, result, [], rep)
                    continue

                meta = cache.read_meta(src.source_id)
                try:
                    records, llm_ok = _extract_source(src, page_type, fetch_fn)
                except Exception as exc:  # noqa: BLE001
                    result = validate.Result(
                        src.source_id,
                        validate.EXTRACT_FAILED,
                        page_type,
                        reasons=[f"{type(exc).__name__}: {exc}"],
                    )
                    _apply_result(state, data, src, page_type, result, [], rep)
                    continue

                sp = _contract_spec(src)
                result = validate.classify(
                    src.source_id,
                    page_type,
                    cached=True,
                    last_status=meta.get("http_status", 0),
                    current_sha1=meta.get("content_sha1"),
                    verified_sha1=st.get("verified_sha1"),
                    records=records,
                    llm_ok=llm_ok,
                    allow_empty=bool(
                        sp and (sp.get("source_type") == "json" or sp.get("allow_empty"))
                    ),
                )

                _flag_llm_outage(result, sp)

                # LLM fallback: the deterministic template failed — either it
                # matched nothing (extract_failed) or what it matched was
                # malformed (schema_invalid). Rescue this run with an LLM
                # extraction (flagged for review). A rescue only replaces the
                # result if it is itself schema-valid, so a worse fallback never
                # clobbers the original diagnosis.
                if _should_try_fallback(use_fallback, page_type, result.status):
                    fb_records, fb_info = llm_extract.extract_records_fallback(
                        src.source_id, page_type
                    )
                    if fb_records:
                        fb_result = validate.classify_llm_fallback(
                            src.source_id, page_type, fb_records
                        )
                        print(
                            f"    LLM fallback ({result.status}): "
                            f"{fb_info.get('status')} -> {len(fb_records)} "
                            f"record(s) [{fb_result.status}]"
                        )
                        if fb_result.writable:
                            records, result = fb_records, fb_result
                    elif fb_info.get("status") not in ("ok", "unavailable"):
                        print(f"    LLM fallback: {fb_info.get('status')}")

                _apply_result(
                    state,
                    data,
                    src,
                    page_type,
                    result,
                    records,
                    rep,
                    group=(sp or {}).get("group"),
                    scope=(sp or {}).get("scope"),
                )

                # Auxiliary outputs from the same page:
                #  - a "Gruppen"-style people table on a process page, or
                #  - a process summary on a topics/people page (dual pages).
                contract = _contract_spec(src)
                group = (sp or {}).get("group")
                if contract and contract.get("people") and page_type != "people":
                    _run_aux_people(state, data, src, contract, fetch_fn, rep)
                if contract and contract.get("also_process") and page_type != "process":
                    _run_aux_process(state, data, src, group, rep)
                if contract and contract.get("topics") and page_type != "topics":
                    _run_aux_topics(state, data, src, contract, group, fetch_fn, rep)
        except KeyboardInterrupt:
            registry.save_state(state)
            dataset.save(data)
            print("\ninterrupted — state and partial data saved.")
            return 130

    dataset.save(data)
    written = store.write_dataset(data)
    report.finalize(rep)
    path = report.write(rep)
    report.print_table(rep)
    print(
        f"\nwrote {dataset.data_path()}"
        f"\ndatabase: {written.postings} postings, {written.profiles} profiles, "
        f"{written.processes} processes"
        + (f" ({written.pruned} stale rows removed)" if written.pruned else "")
        + f"\nrun report: {path}"
    )
    sm = rep["summary"]
    report.notify(f"run complete: {sm['total']} sources, {sm['flagged']} flagged")
    return 1 if sm["flagged"] else 0


def _run_aux_people(state, data, src, contract, fetch_fn, rep) -> None:
    """Extract + store a source's auxiliary people (same cached page, people
    block), following profile links. Stored in the people bucket alongside the
    source's primary output; reported as a separate people entry."""
    pspec = dict(contract["people"])
    pspec["page_type"] = "people"
    pspec["source_id"] = src.source_id
    try:
        records = spec_engine.extract(src.source_id, pspec)
        if pspec.get("follow"):
            spec_engine.enrich_from_profiles(
                src.source_id, records, pspec, fetch_fn=fetch_fn, limit=None, log=lambda m: None
            )
    except Exception as exc:  # noqa: BLE001
        result = validate.Result(
            src.source_id,
            validate.EXTRACT_FAILED,
            "people",
            reasons=[f"aux people: {type(exc).__name__}: {exc}"],
        )
        report.add_source(rep, result, action="kept_previous")
        print(f"    + aux people: FAILED {exc}")
        return
    result = validate.classify(
        src.source_id,
        "people",
        cached=True,
        last_status=200,
        current_sha1=None,
        verified_sha1=None,
        records=records,
        llm_ok=True,
    )
    emails = sum(1 for r in records if r.get("email"))
    print(f"    + aux people: {len(records)} ({result.status}, {emails} emails)")
    if result.writable:
        dataset.upsert_source(data, src, "people", records)
    report.add_source(rep, result, action="updated" if result.writable else "kept_previous")


def _run_aux_topics(state, data, src, contract, group, fetch_fn, rep) -> None:
    """Extract + store a source's auxiliary topics (a page that's primarily
    people/process but also lists topics). Follows detail pages, dedupes by
    topic_id, and normalizes supervisors."""
    tspec = dict(contract["topics"])
    tspec["page_type"] = "topics"
    tspec["source_id"] = src.source_id
    try:
        records = spec_engine.extract(src.source_id, tspec)
        if tspec.get("follow"):
            spec_engine.enrich_from_links(
                src.source_id,
                records,
                tspec["follow"],
                kind="topics",
                fetch_fn=fetch_fn,
                limit=None,
            )
        if tspec.get("pdf_summary"):
            _summarize_topic_pdfs(src, records, tspec["pdf_summary"])
    except Exception as exc:  # noqa: BLE001
        result = validate.Result(
            src.source_id,
            validate.EXTRACT_FAILED,
            "topics",
            reasons=[f"aux topics: {type(exc).__name__}: {exc}"],
        )
        report.add_source(rep, result, action="kept_previous")
        print(f"    + aux topics: FAILED {exc}")
        return
    seen, deduped = set(), []
    for r in records:  # dedupe (broad containers can repeat a topic link)
        tid = r.get("topic_id")
        if tid in seen:
            continue
        seen.add(tid)
        deduped.append(r)
    records = deduped
    if tspec.get("resolve_supervisors_via_directory"):
        _resolve_supervisors_via_directory(
            src, records, tspec["resolve_supervisors_via_directory"], fetch_fn
        )
    for r in records:
        spec_engine.normalize_supervisors(r)
    result = validate.classify(
        src.source_id,
        "topics",
        cached=True,
        last_status=200,
        current_sha1=None,
        verified_sha1=None,
        records=records,
        allow_empty=True,
    )
    print(f"    + aux topics: {len(records)} ({result.status})")
    if result.writable:
        dataset.upsert_source(data, src, "topics", records, group=group)
    report.add_source(rep, result, action="updated" if result.writable else "kept_previous")


def _run_aux_process(state, data, src, group, rep) -> None:
    """Summarize a dual page's thesis process (a topics/people page that also
    describes how to get a thesis) and store it alongside its topics/people. A
    multi-page source (e.g. IMRG master + bachelor) yields one process per page."""
    spec = _contract_spec(src) or {}
    pages = spec.get("pages")
    if pages:
        fetch_fn = fetch.html_fetcher()
        recs = []
        for pg in pages:
            html, base = _page_html(src, pg["url"], fetch_fn)
            if html:
                recs.append(llm_extract.extract_process(src.source_id, html=html, base_url=base))
    else:
        recs = [llm_extract.extract_process(src.source_id)]

    llm_ok = all(r["_llm"].get("status") == "ok" for r in recs) if recs else False
    result = validate.classify(
        src.source_id,
        "process",
        cached=True,
        last_status=200,
        current_sha1=None,
        verified_sha1=None,
        records=recs,
        llm_ok=llm_ok,
    )
    print(f"    + aux process ({result.status}, {len(recs)} page(s))")
    if result.writable:
        dataset.upsert_source(data, src, "process", recs, group=group)
    report.add_source(rep, result, action="updated" if result.writable else "kept_previous")


def _apply_result(
    state, data, src, page_type, result, records, rep, group=None, scope=None
) -> None:
    diff = None
    if result.writable and result.status == validate.PAGE_CHANGED:
        # Compare against what's stored. If the extracted records are unchanged,
        # the page_changed flag is cosmetic noise (raw HTML moved, data didn't) —
        # quiet it to OK so only real content changes are flagged for review.
        old = dataset.records_for_source(data, src, page_type, scope=scope)
        diff = validate.diff_records(page_type, old, records)
        if validate.downgrade_if_unchanged(result, diff):
            diff = None
    print(
        f"    {result.status}  records={len(records)}"
        + (f"  [{group['id']}]" if group else "")
        + ("  [faculty]" if scope == "faculty" else "")
        + (f"  {result.reasons[0]}" if result.reasons else "")
    )
    if result.writable:
        dataset.upsert_source(data, src, page_type, records, group=group, scope=scope)
        action = "updated"
    else:
        action = "kept_previous"  # never overwrite good data with garbage

    # page_changed stays verified (data is good, keep scraping — the flag is just
    # a review note); hard failures and llm_fallback quarantine until re-onboarded.
    onboarding = (
        registry.ONBOARD_QUARANTINED
        if validate.quarantines(result.status)
        else registry.ONBOARD_VERIFIED
    )
    run_state = registry.RUN_DONE if result.writable else registry.RUN_FAILED
    registry.update_source_state(state, src.source_id, onboarding=onboarding, run=run_state)
    registry.save_state(state)
    report.add_source(rep, result, action=action, diff=diff)


class _null_ctx:
    def __enter__(self):
        return None

    def __exit__(self, *a):
        return False


# --- status ----------------------------------------------------------------


def cmd_status(args: argparse.Namespace) -> int:
    """A no-network snapshot: where every source sits in its lifecycle, plus the
    last run's summary. Reads var/state.json + output/runs/ only."""
    from collections import Counter

    state = registry.load_state()
    sources = registry.all_sources()
    ids = {s.source_id for s in sources}

    onb, runc, ptc = Counter(), Counter(), Counter()
    quarantined, unverified = [], []
    for s in sources:
        st = registry.source_state(state, s.source_id)
        o = st.get("onboarding", registry.ONBOARD_UNVERIFIED)
        r = st.get("run", registry.RUN_PENDING)
        onb[o] += 1
        runc[r] += 1
        ptc[st.get("page_type", "-")] += 1
        if o == registry.ONBOARD_QUARANTINED:
            quarantined.append((s.source_id, st.get("page_type", "-"), r))
        elif o == registry.ONBOARD_UNVERIFIED:
            unverified.append(s.source_id)

    print(
        f"registry: {len(sources)} sources | "
        f"{len({s.faculty_code for s in sources})} faculties | "
        f"{len({s.unit_id for s in sources})} units\n"
    )
    print("  onboarding:  " + "  ".join(f"{k}={v}" for k, v in sorted(onb.items())))
    print("  run:         " + "  ".join(f"{k}={v}" for k, v in sorted(runc.items())))
    print("  page_type:   " + "  ".join(f"{k}={v}" for k, v in sorted(ptc.items())))

    if quarantined:
        print(f"\n{len(quarantined)} quarantined:")
        for sid, pt, r in quarantined:
            print(f"  {sid:32} {pt:8} run={r}")
    if unverified:
        shown = ", ".join(unverified[:12]) + (" ..." if len(unverified) > 12 else "")
        print(f"\n{len(unverified)} unverified: {shown}")
    if not quarantined and not unverified:
        print("\nall registry sources verified.")

    orphans = sorted(set(state.get("sources", {})) - ids)
    if orphans:
        print(
            f"\n{len(orphans)} orphaned state entries (not in current registry): "
            + ", ".join(orphans[:6])
            + (" ..." if len(orphans) > 6 else "")
        )

    runs = get_settings().runs_dir
    run_files = sorted(runs.glob("*.json")) if runs.exists() else []
    if run_files:
        rr = json.loads(run_files[-1].read_text(encoding="utf-8"))
        sm = rr.get("summary", {})
        by = "  ".join(f"{k}={v}" for k, v in (sm.get("by_status") or {}).items())
        print(
            f"\nlast run: {run_files[-1].name}"
            f"\n  total={sm.get('total')}  flagged={sm.get('flagged')}  {by}"
        )
    else:
        print("\nno runs recorded yet.")

    if dataset.data_path().exists():
        print(f"\noutput: {dataset.data_path()}")
    return 0


# --- check (dry-run one source, diff vs stored) ----------------------------


def cmd_check(args: argparse.Namespace) -> int:
    """Re-extract one source from cache, classify it, and diff against the data
    already in output/extracted_data.json — writing nothing. The single-source dry run
    for verifying a spec change before committing to a full run."""
    state = registry.load_state()
    try:
        src = registry.get_source(args.source_id)
    except KeyError as exc:
        print(exc)
        return 2

    st = registry.source_state(state, src.source_id)
    if st.get("onboarding") != registry.ONBOARD_VERIFIED:
        print(
            f"{src.source_id} is not verified (onboarding={st.get('onboarding')}). "
            f"Onboard it first."
        )
        return 2
    if not cache.is_cached(src.source_id):
        print(
            f"{src.source_id} not cached — run "
            f"`python -m thesis_matchmaker.scraper.main fetch --only {src.source_id}` first."
        )
        return 2

    page_type = st.get("page_type", "process")
    meta = cache.read_meta(src.source_id)
    fetch_fn = fetch.html_fetcher()  # cache-first; only followed links may hit network
    try:
        records, llm_ok = _extract_source(src, page_type, fetch_fn)
    except Exception as exc:  # noqa: BLE001
        print(f"{src.source_id} ({page_type}): extract failed — {type(exc).__name__}: {exc}")
        return 1

    sp = _contract_spec(src)
    result = validate.classify(
        src.source_id,
        page_type,
        cached=True,
        last_status=meta.get("http_status", 0),
        current_sha1=meta.get("content_sha1"),
        verified_sha1=st.get("verified_sha1"),
        records=records,
        llm_ok=llm_ok,
        allow_empty=bool(sp and sp.get("source_type") == "json"),
    )

    print(f"{src.source_id} ({page_type}): {result.status}  records={len(records)}")
    for reason in result.reasons:
        print(f"  - {reason}")

    old = dataset.records_for_source(dataset.load(), src, page_type, scope=(sp or {}).get("scope"))
    diff = validate.diff_records(page_type, old, records)
    print(
        f"\ndiff vs stored:  +{diff['added']} / -{diff['removed']} / ~{diff['modified']}"
        f"   (stored {len(old)}, extracted {len(records)})"
    )
    for label, key in (
        ("added", "added_keys"),
        ("removed", "removed_keys"),
        ("modified", "modified_keys"),
    ):
        keys = diff.get(key) or []
        if keys:
            print(f"  {label}: " + ", ".join(keys[:10]) + (" ..." if len(keys) > 10 else ""))

    print("\n(dry run — nothing written)")
    return 1 if result.flagged else 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m thesis_matchmaker.scraper.main", description="UZH thesis-posting scraper"
    )
    sub = p.add_subparsers(dest="command", required=True)

    f = sub.add_parser("fetch", help="stage 1: fetch sources into the cache")
    f.add_argument("--only", nargs="+", metavar="ID")
    f.add_argument("--resume", action="store_true")
    f.add_argument("--render", action="store_true", help="force Playwright render")
    f.set_defaults(func=cmd_fetch)

    o = sub.add_parser("onboard", help="interactive verification of one source")
    o.add_argument("source_id", nargs="?")
    o.add_argument("--next", action="store_true", help="pick the next unverified source")
    o.add_argument("--page-type", choices=["process", "topics", "people", "none"])
    o.add_argument("--hint", default="", help="initial hint for the spec-drafting LLM")
    o.add_argument("--render", action="store_true", help="force render on fetch")
    o.add_argument("--refetch", action="store_true", help="refetch even if cached")
    o.add_argument("--redraft", action="store_true", help="ignore existing spec")
    o.add_argument("--no-follow", action="store_true", help="skip profile following")
    o.add_argument(
        "--profile-limit",
        type=int,
        default=get_settings().profile_limit,
        help="profiles to follow during onboarding (default SCRAPER_PROFILE_LIMIT, 3)",
    )
    o.add_argument(
        "--llm-title-review",
        action="store_true",
        help="ask the LLM for an advisory opinion on flagged titles (printed only, never stored)",
    )
    o.add_argument("--yes", action="store_true", help="auto-approve (non-interactive)")
    o.set_defaults(func=cmd_onboard)

    r = sub.add_parser("run", help="extract verified sources, validate, store")
    r.add_argument("--only", nargs="+", metavar="ID")
    r.add_argument("--resume", action="store_true", help="skip sources already done")
    r.add_argument(
        "--no-llm-fallback",
        action="store_true",
        help="disable the LLM rescue for sources whose template matched nothing",
    )
    r.set_defaults(func=cmd_run)

    s = sub.add_parser("status", help="state + last-run summary")
    s.set_defaults(func=cmd_status)

    c = sub.add_parser("check", help="dry-run one source, diff vs stored")
    c.add_argument("source_id")
    c.set_defaults(func=cmd_check)

    return p


def main(argv: list[str] | None = None) -> int:
    # pypdf logs recoverable PDF-xref quirks ("Ignoring wrong pointing object …")
    # at WARNING for some source PDFs; they don't affect extraction, so quiet
    # them (real pypdf errors still surface at ERROR).
    logging.getLogger("pypdf").setLevel(logging.ERROR)
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    finally:
        # `run` writes Postgres through scraper/store.py, which opens a pooled
        # connection. The pool runs background worker threads, so without this
        # the process complains on the way out ("couldn't stop thread
        # 'pool-1-worker-0' within 5.0 seconds") and waits for the stop timeout.
        # Same reasoning as zora/harvest.py, and in `finally` for the same
        # reason: a crash mid-run is exactly when the pool is open. Harmless for
        # the subcommands that never touch the database -- close_pools() on an
        # empty pool registry is a no-op.
        db.close_pools()


if __name__ == "__main__":
    sys.exit(main())

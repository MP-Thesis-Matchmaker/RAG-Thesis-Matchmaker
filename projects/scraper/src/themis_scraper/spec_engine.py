"""Spec engine — deterministic, LLM-free extraction from cached HTML.

A spec (`contracts/<source_id>/spec.yaml`) is a small declarative document:
a `container` CSS selector that picks out each record, plus a `fields` map that
says how to pull each target-model field out of a container. Same cached HTML +
same spec ⇒ identical records, every run. All page-specific knowledge lives in
the spec; this engine stays generic.

Spec shape (YAML):

    source_id: ivw--4
    page_type: people            # people | topics
    render: true                 # informational: page needs Playwright
    record:
      container: ".PersonCard"   # one match per person/topic
      fields:
        name:
          selector: ".PersonCard--name"   # relative to container; omit = container
          attr: text                       # text | html | <attribute name>
          transform: normalize_ws          # optional, single or list
          multi: false                     # collect all matches?
          join: ", "                       # if multi: join into one string
          contains_i: "website"            # keep only nodes whose text contains
                                           #   this, case-insensitively
          regex: "..."                     # optional capture (group 1 if present)
          default: null

A field may also be written in shorthand as just a CSS-selector string.
`topics` specs may add `id_from: [field, ...]` to choose which fields seed the
stable topic_id (defaults to the source url + topic_description).
"""

from __future__ import annotations

import hashlib
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

import yaml
from bs4 import BeautifulSoup

from . import cache, registry, title_check
from .config import get_settings

# Target-model field sets, so the preview always has a complete record shape.
PEOPLE_FIELDS = [
    "role",
    "name",
    "email",
    "research_interest",
    "research_field",
    "bio",
    "personal_website",
    "_profile_url",
]
TOPIC_FIELDS = [
    "title",
    "status",
    "degree_level",
    "date_of_listing",
    "research_area",
    "supervisors",
    "topic_description",
    "source_link",
]


class SpecError(Exception):
    pass


# --- Transforms -------------------------------------------------------------


def _t_normalize_ws(v: str) -> str:
    return re.sub(r"\s+", " ", v).strip() if isinstance(v, str) else v


def _t_strip_mailto(v: str) -> str:
    if not isinstance(v, str):
        return v
    v = re.sub(r"^mailto:", "", v.strip(), flags=re.I)
    return v.split("?", 1)[0].strip()


def _t_drop_if_email(v: str):
    """Return None when the value is/contains an email address — e.g. a mailto
    link whose visible text is the address itself, so it isn't stored as a name."""
    if isinstance(v, str) and "@" in v:
        return None
    return v


def _t_dirname_html(v: str):
    """'.../caflisch/master-theses.html' -> '.../caflisch.html' — the parent page
    of a subpage (drop the last path segment, keep the directory as a .html page)."""
    if not isinstance(v, str):
        return v
    return re.sub(r"/[^/]+$", ".html", v)


def _t_mailto_email(v: str):
    """Return the address from a mailto: href (dropping any ?subject=…), or None
    for a non-mailto href. Lets one `each` handle both profile and mailto links."""
    if isinstance(v, str) and v.lower().startswith("mailto:"):
        return _t_strip_mailto(v)
    return None


def _t_split_commas(v: str):
    if not isinstance(v, str):
        return v
    return [p.strip() for p in v.split(",") if p.strip()]


def _t_lower(v: str) -> str:
    return v.lower() if isinstance(v, str) else v


_TITLES = (
    r"(?:Prof\.?|Dres\.?|Dr\.?|PD\.?|em\.?|emer\.?|habil\.?|iur\.?|rer\.?|nat\.?"
    r"|pol\.?|oec\.?|soc\.?|phil\.?|sc\.?|med\.?|h\.?\s?c\.?|Dipl\.?[\w-]*\.?)"
)
_TITLE_LEAD_RE = re.compile(rf"^(?:{_TITLES}\s*)+", re.I)
_TITLE_TRAIL_RE = re.compile(rf"[,\s]\s*(?:{_TITLES}\s*)+$", re.I)  # ", Prof. Dr." or " Prof. Dr."


def _t_strip_titles(v: str) -> str:
    """Drop leading OR trailing academic titles: 'Prof. Dr. Hui Chen' and
    'Francisco Amaral, Prof. Dr.' both -> the plain name."""
    if not isinstance(v, str):
        return v
    return _TITLE_LEAD_RE.sub("", _TITLE_TRAIL_RE.sub("", v)).strip()


def _t_name_lastfirst(v: str) -> str:
    """'Backhaus, Norman, Prof. Dr.' -> 'Norman Backhaus'. Strips trailing
    titles, then swaps a leading 'Surname, Given' into 'Given Surname'."""
    if not isinstance(v, str):
        return v
    v = _t_strip_titles(v)
    parts = [p.strip() for p in v.split(",") if p.strip()]
    if len(parts) >= 2:
        return f"{' '.join(parts[1:])} {parts[0]}".strip()
    return v


def _t_titlecase(v: str) -> str:
    """'ALTMEYER' -> 'Altmeyer', 'SCHARL_FAZILATY' -> 'Scharl Fazilaty'. Turns an
    all-caps, underscore-joined surname prefix into a readable name."""
    if not isinstance(v, str):
        return v
    return v.replace("_", " ").title()


def _t_name_lastfirst_space(v: str) -> str:
    """'Altmeyer Matthias' -> 'Matthias Altmeyer', 'Guerreiro Stücklin Ana' ->
    'Ana Guerreiro Stücklin'. For 'Surname(s) Given' lists with no comma, moves
    the last token (the given name) to the front."""
    if not isinstance(v, str):
        return v
    toks = v.split()
    return f"{toks[-1]} {' '.join(toks[:-1])}" if len(toks) >= 2 else v


def _t_pi_surname(v: str):
    """Extract a leading all-caps, underscore-joined surname prefix from a label
    like 'MORSCHER_Pediatric Cancer Metabolism' or 'SCHARL_FAZILATY_...' and
    return it title-cased ('Morscher', 'Scharl Fazilaty'). None if absent. Does
    the match and title-casing in one step so the caps pattern survives (a plain
    titlecase transform would run before any `regex:` and destroy it)."""
    if not isinstance(v, str):
        return v
    m = re.match(r"^([A-Z]+(?:_[A-Z]+)*)_", v)
    return m.group(1).replace("_", " ").title() if m else None


def _t_date_only(v: str) -> str:
    """ISO timestamp -> YYYY-MM-DD (first 10 chars); pass through otherwise."""
    if isinstance(v, str) and len(v) >= 10 and v[4] == "-" and v[7] == "-":
        return v[:10]
    return v


def _t_supervisor_list(v: str):
    """Parse a 'Supervisor(s): A, B and C' line into [{name}, ...], stripping
    the label and academic titles."""
    if not isinstance(v, str):
        return v
    v = re.sub(r"^\s*supervisor(?:\(s\))?s?\s*:?\s*", "", v, flags=re.I)
    out = []
    for part in re.split(r"\s*[,;&]\s*|\s+\band\b\s+", v):
        part = re.sub(r"\s*\([^)]*\)", "", part)  # drop "(contact point)" etc.
        name = _t_strip_titles(part.strip())
        if name and len(name) > 2:
            out.append({"name": name})
    return out or None


def _t_deobfuscate_email(v: str):
    """Recover an anti-spam-obfuscated email from text, e.g.
    'hui.chen[at]business.uzh.ch' or 'name (at) x dot ch' -> real address.
    Returns None if no valid email can be recovered."""
    if not isinstance(v, str):
        return v
    s = re.sub(r"\s*[\[(]\s*at\s*[\])]\s*", "@", v, flags=re.I)
    s = re.sub(r"\s*[\[(]\s*dot\s*[\])]\s*", ".", s, flags=re.I)
    s = re.sub(r"\s+at\s+", "@", s, flags=re.I)
    s = re.sub(r"\s+dot\s+", ".", s, flags=re.I)
    m = re.search(r"[\w.\-+]+@[\w.\-]+\.\w{2,}", s)
    return m.group(0) if m else None


def _t_contact_supervisors(v: str):
    """Parse an inline contact line listing 'Name [email], Name (email), Name
    email ...' into [{name, email}, ...]. Names are the capitalised tokens
    immediately preceding each email (any bracket style, or none); academic
    titles are stripped."""
    if not isinstance(v, str):
        return v
    # de-obfuscate inline "(at)"/"(dot)" addresses before pairing.
    v = re.sub(r"\s*[\[(]\s*at\s*[\])]\s*", "@", v, flags=re.I)
    v = re.sub(r"\s*[\[(]\s*dot\s*[\])]\s*", ".", v, flags=re.I)
    out, seen = [], set()
    pair = re.compile(
        r"([A-ZÄÖÜ][A-Za-zäöüéèA-ZÄÖÜ.\-]*(?:\s+[A-ZÄÖÜ][A-Za-zäöüéè.\-]+){1,3})"
        r"\s*[\[(<]?\s*([\w.\-]+@[\w.\-]+\.\w{2,})"
    )
    for m in pair.finditer(v):
        name = _t_strip_titles(re.sub(r"\s+", " ", m.group(1)).strip())
        email = m.group(2)
        if email in seen:
            continue
        seen.add(email)
        out.append({"name": name or None, "email": email})
    # bare emails with no preceding name (e.g. an email-only contact cell)
    for e in re.findall(r"[\w.\-]+@[\w.\-]+\.\w{2,}", v):
        if e not in seen:
            seen.add(e)
            out.append({"name": None, "email": e})
    return out or None


def _t_degree_from_type(v: str):
    """Derive Bachelor/Master from a short thesis-type code cell, e.g. 'MA',
    'BA/MA/MP', 'MA/MAP'. IS (independent study) alone yields None."""
    if not isinstance(v, str):
        return v
    codes = set(re.split(r"[\s/,;]+", v.upper()))
    m = bool(codes & {"MA", "MP", "MAP", "MSC", "MT"}) or "MASTER" in v.upper()
    b = bool(codes & {"BA", "BP", "BSC", "BT"}) or "BACHELOR" in v.upper()
    return "Bachelor, Master" if m and b else "Master" if m else "Bachelor" if b else None


def _t_split_emails(v: str):
    """Extract every email address from a string into a list (keeps order)."""
    if not isinstance(v, str):
        return v
    return re.findall(r"[\w.\-+]+@[\w.\-]+\.\w{2,}", v) or None


def _t_academic_role(v: str):
    """Derive an academic role from a person's title prefix, e.g.
    'Prof. em. Dr. X' -> Professor Emeritus, 'PD Dr. Y' -> Privatdozent."""
    if not isinstance(v, str):
        return v
    low = v.lower()
    if "em." in low or "emerit" in low:
        return "Professor Emeritus"
    if re.match(r"\s*pd\b", low):
        return "Privatdozent"
    if "prof" in low:
        return "Professor"
    return None


def _t_norm_status(v: str):
    """Map a free-text availability word to the canonical open/taken."""
    if not isinstance(v, str):
        return v
    s = v.strip().lower()
    if "avail" in s or "open" in s:
        return "open"
    if any(k in s for k in ("taken", "filled", "assigned", "reserved", "closed")):
        return "taken"
    return s or None


def _t_degree_from_text(v: str):
    """Derive Bachelor/Master from a free-text thesis-type line."""
    if not isinstance(v, str):
        return v
    low = v.lower()
    m = "master" in low or "msc" in low
    b = "bachelor" in low or "bsc" in low
    return "Bachelor, Master" if m and b else "Master" if m else "Bachelor" if b else None


_TRANSFORMS = {
    "normalize_ws": _t_normalize_ws,
    "strip_mailto": _t_strip_mailto,
    "split_commas": _t_split_commas,
    "lower": _t_lower,
    "date_only": _t_date_only,
    "strip_titles": _t_strip_titles,
    "name_lastfirst": _t_name_lastfirst,
    "titlecase": _t_titlecase,
    "name_lastfirst_space": _t_name_lastfirst_space,
    "pi_surname": _t_pi_surname,
    "supervisor_list": _t_supervisor_list,
    "deobfuscate_email": _t_deobfuscate_email,
    "mailto_email": _t_mailto_email,
    "dirname_html": _t_dirname_html,
    "drop_if_email": _t_drop_if_email,
    "contact_supervisors": _t_contact_supervisors,
    "norm_status": _t_norm_status,
    "academic_role": _t_academic_role,
    "degree_from_text": _t_degree_from_text,
    "degree_from_type": _t_degree_from_type,
    "split_emails": _t_split_emails,
    # absolute_url is handled specially (needs base_url).
}


# --- Spec loading -----------------------------------------------------------


def spec_path(source_id: str) -> Path:
    return get_settings().specs_dir / source_id / "spec.yaml"


def load_spec(source_id: str) -> dict:
    p = spec_path(source_id)
    if not p.exists():
        raise SpecError(f"no spec for {source_id} at {p}")
    with p.open(encoding="utf-8") as fh:
        spec = yaml.safe_load(fh)
    source_type = spec.get("source_type")
    if source_type == "grouped_people":
        if "grouped" not in spec:
            raise SpecError(f"spec {source_id} (grouped_people) missing `grouped`")
    elif source_type == "sectioned_people":
        if "sections" not in spec:
            raise SpecError(f"spec {source_id} (sectioned_people) missing `sections`")
    elif "record" not in spec or "fields" not in spec["record"]:
        raise SpecError(f"spec {source_id} missing record.fields")
    return spec


# --- Field extraction -------------------------------------------------------


def _normalize_field_spec(fs: Any) -> dict:
    if isinstance(fs, str):
        return {"selector": fs, "attr": "text"}
    if not isinstance(fs, dict):
        raise SpecError(f"field spec must be str or dict, got {type(fs).__name__}")
    fs = dict(fs)
    fs.setdefault("attr", "text")
    return fs


def _node_for_value(node, fs: dict):
    """Return the node to read, honouring `exclude`: a detached copy with the
    matching sub-elements removed (e.g. drop the h3/supervisor/pdf lines before
    reading a block's description text)."""
    excl = fs.get("exclude")
    if not excl:
        return node
    clone = BeautifulSoup(str(node), "html.parser")
    for sel in [excl] if isinstance(excl, str) else excl:
        for e in clone.select(sel):
            e.decompose()
    return clone


def _value_from_node(node, attr: str) -> str:
    if attr == "text":
        return node.get_text(" ", strip=True)
    if attr == "html":
        return node.decode_contents()
    val = node.get(attr)
    if isinstance(val, list):  # e.g. class="a b"
        return " ".join(val)
    return (val or "").strip()  # stray whitespace in an href is never meaningful


def _apply_transforms(value, transforms, base_url: str):
    if transforms is None:
        return value
    if isinstance(transforms, str):
        transforms = [transforms]
    for name in transforms:
        if name == "absolute_url":

            def _abs(v: str) -> str:
                return urljoin(base_url, v.strip()).replace(" ", "%20")

            if isinstance(value, list):
                value = [_abs(v) for v in value]
            elif value:
                value = _abs(value)
        elif name in _TRANSFORMS:
            fn = _TRANSFORMS[name]
            value = [fn(v) for v in value] if isinstance(value, list) else fn(value)
        else:
            raise SpecError(f"unknown transform: {name}")
    return value


def _extract_field(container, fs, base_url: str):
    # A list of specs is a fallback chain: return the first non-empty result
    # (e.g. email: try a mailto link, then an obfuscated address string).
    if isinstance(fs, list):
        for sub in fs:
            v = _extract_field(container, sub, base_url)
            if v not in (None, "", []):
                return v
        return None
    fs = _normalize_field_spec(fs)
    if "value" in fs:  # literal constant (e.g. role: {value: "Professor"})
        return fs["value"]
    if "each" in fs:  # list of sub-objects, one per node matched by `each`
        subfields = fs.get("fields", {})
        out = []
        for item in container.select(fs["each"]):
            obj = {name: _extract_field(item, sub, base_url) for name, sub in subfields.items()}
            if any(v not in (None, "", []) for v in obj.values()):
                out.append(obj)
        return out or fs.get("default")
    selector = fs.get("selector")
    nodes = container.select(selector) if selector else [container]

    # contains_i: keep only nodes whose visible text contains one of these
    # strings, case-insensitively. Accepts a single string or a list (match
    # ANY). Fixes the fragility of soupsieve's case-sensitive :-soup-contains()
    # for label-matched links whose wording/casing varies ("Personal website",
    # "Personal Homepage", "Private Website", ...).
    ci = fs.get("contains_i")
    if ci is not None:
        needles = [str(ci).lower()] if isinstance(ci, str) else [str(x).lower() for x in ci]
        nodes = [
            n
            for n in nodes
            if any(needle in n.get_text(" ", strip=True).lower() for needle in needles)
        ]

    attr = fs["attr"]
    if fs.get("multi"):
        values = [_value_from_node(_node_for_value(n, fs), attr) for n in nodes]
        values = [v for v in values if v]
        values = _apply_transforms(values, fs.get("transform"), base_url)
        if fs.get("regex"):
            values = [_regex_pick(v, fs["regex"]) for v in values]
        if "join" in fs and isinstance(values, list):
            return fs["join"].join(v for v in values if v)
        return values or fs.get("default")

    if not nodes:
        return fs.get("default")
    value = _value_from_node(_node_for_value(nodes[0], fs), attr)
    value = _apply_transforms(value, fs.get("transform"), base_url)
    if fs.get("regex"):
        value = _regex_pick(value, fs["regex"])
    return value if value not in ("", None) else fs.get("default")


def _regex_pick(value: str, pattern: str):
    if not isinstance(value, str):
        return value
    m = re.search(pattern, value)
    if not m:
        return None
    return m.group(1) if m.groups() else m.group(0)


# --- Extraction -------------------------------------------------------------


def _topic_id(source_url: str, record: dict, id_from: list[str] | None) -> str:
    if id_from:
        seed = " ".join(str(record.get(f, "")) for f in id_from)
    else:
        seed = str(record.get("topic_description", ""))
    norm = re.sub(r"\s+", " ", seed).strip().lower()
    return hashlib.sha1(f"{source_url}\n{norm}".encode()).hexdigest()


# --- JSON extraction (SPA / API-backed sources) ----------------------------

_PATH_TOKEN = re.compile(r"([^.\[\]]+)|\[(\d+)\]")


def _json_get(obj, path: str):
    """Navigate a dotted/bracketed path like 'topicArea.name' or
    'supervisedBy[0].supervisor.email'. Returns None if any step is missing."""
    cur = obj
    for key, idx in _PATH_TOKEN.findall(path):
        if cur is None:
            return None
        if idx != "" or (isinstance(cur, list) and key.isdigit()):
            i = int(idx or key)
            cur = cur[i] if isinstance(cur, list) and i < len(cur) else None
        else:
            cur = cur.get(key) if isinstance(cur, dict) else None
    return cur


def _extract_json_field(item: dict, fs, base_url: str):
    if isinstance(fs, str):
        fs = {"path": fs}
    if "template" in fs:
        try:
            value = fs["template"].format(**item)
        except (KeyError, IndexError):
            value = fs.get("default")
    else:
        value = _json_get(item, fs["path"])
    value = _apply_transforms(value, fs.get("transform"), base_url)
    if isinstance(value, str):
        value = value.strip()
    return value if value not in ("", None) else fs.get("default")


def _extract_json(source_id: str, spec: dict) -> list[dict]:
    data = cache.read_json(source_id)
    rec_spec = spec["record"]
    items = _json_get(data, rec_spec["path"]) or []
    if not isinstance(items, list):
        raise SpecError(f"{source_id}: record.path did not resolve to a list")

    flt = rec_spec.get("filter") or {}
    base_url = spec.get("api_url", "")
    scraped_at = datetime.now(UTC).isoformat()
    known = TOPIC_FIELDS if spec.get("page_type") == "topics" else PEOPLE_FIELDS

    records = []
    for item in items:
        if any(_json_get(item, k) != v for k, v in flt.items()):
            continue
        raw = {
            name: _extract_json_field(item, fs, base_url) for name, fs in rec_spec["fields"].items()
        }
        record = {f: raw.get(f) for f in known}
        for k, v in raw.items():
            if k not in record:
                record[k] = v
        record["source_id"] = source_id
        record["scraped_at"] = scraped_at
        if spec.get("page_type") == "topics":
            link = record.get("source_link") or base_url
            record["source_link"] = link
            record["topic_id"] = _topic_id(link, record, spec.get("id_from"))
            if spec.get("title_check", True) is not False:
                title_check.check_only(record)  # no container to repair from
        records.append(record)
    return records


def _extract_grouped_people(source_id: str, spec: dict) -> list[dict]:
    """People listed grouped by a heading (e.g. research field → contacts):
    each container holds one field label plus several person links. Inverted to
    one record per person, collecting all their fields, with the email from an
    inline mailto or (for profile links) a `_profile_url` to resolve by follow.
    """
    g = spec["grouped"]
    base_url = cache.read_meta(source_id).get("url", "")
    soup = BeautifulSoup(cache.read_page(source_id), "html.parser")
    field_sel = g.get("field", "strong")
    person_sel = g.get("person", "a[href]")
    exclude_sel = g.get("exclude_person")
    scraped_at = datetime.now(UTC).isoformat()

    order: list[str] = []
    people: dict[str, dict] = {}
    for block in soup.select(g["container"]):
        fe = block.select_one(field_sel)
        field = fe.get_text(" ", strip=True) if fe else None
        excluded = {id(x) for x in (block.select(exclude_sel) if exclude_sel else [])}
        for a in block.select(person_sel):
            if id(a) in excluded:
                continue
            name = re.sub(r"\s+", " ", a.get_text(" ", strip=True)).strip()
            if not name:
                continue
            href = (a.get("href") or "").strip()
            if name not in people:
                rec = {f: None for f in PEOPLE_FIELDS}
                rec.update(
                    {
                        "name": name,
                        "role": "Contact",
                        "_fields": [],
                        "_profile_url": None,
                        "source_id": source_id,
                        "scraped_at": scraped_at,
                    }
                )
                people[name] = rec
                order.append(name)
            rec = people[name]
            if field and field not in rec["_fields"]:
                rec["_fields"].append(field)
            if href.startswith("mailto:"):
                rec["email"] = re.sub(r"^mailto:", "", href).split("?")[0].strip()
            elif href and not href.startswith("#"):
                rec["_profile_url"] = urljoin(base_url, href)

    records = []
    for name in order:
        rec = people[name]
        rec["research_field"] = ", ".join(rec.pop("_fields")) or None
        records.append(rec)
    return records


def _split_containers(soup, rec_spec: dict):
    """Build one synthetic container per record for flat, marker-delimited content
    (e.g. a research-area block whose projects are just <p><strong>title</strong>,
    <p>desc</p>, <p>Contact ...</p> repeated). Each record spans a `split_on`
    marker up to (not including) the next marker. If `section_heading` is given,
    the nearest preceding heading's text is injected as <span class="_section">
    so a field can pick up the research area."""
    import bisect

    markers = soup.select(rec_spec["split_on"])
    marker_ids = {id(m) for m in markers}
    sec_sel = rec_spec.get("section_heading")
    order = {id(e): i for i, e in enumerate(soup.find_all(True))} if sec_sel else {}
    heads = (
        sorted((order[id(h)], h.get_text(" ", strip=True)) for h in soup.select(sec_sel))
        if sec_sel
        else []
    )
    hpos = [p for p, _ in heads]

    out = []
    for mk in markers:
        grp = soup.new_tag("div")
        if sec_sel:
            mp = order.get(id(mk))
            idx = bisect.bisect_right(hpos, mp) - 1 if mp is not None else -1
            if idx >= 0:
                span = soup.new_tag("span")
                span["class"] = ["_section"]
                span.string = heads[idx][1]
                grp.append(span)
        grp.append(BeautifulSoup(str(mk), "html.parser"))
        for sib in mk.next_siblings:
            if getattr(sib, "name", None) is None:
                continue
            if id(sib) in marker_ids:
                break
            grp.append(BeautifulSoup(str(sib), "html.parser"))
        out.append(grp)
    return out


def _extract_sectioned_people(source_id: str, spec: dict) -> list[dict]:
    """People listed under function headings in flat document order (headings and
    person links are not nested, e.g. an orphaned-table people iframe). Each
    person is assigned to the nearest preceding heading; only sections in
    `include` are kept, and the section name can be written into a field."""
    import bisect

    cfg = spec["sections"]
    base_url = cache.read_meta(source_id).get("url", "")
    soup = BeautifulSoup(cache.read_page(source_id), "html.parser")
    include = set(cfg.get("include") or [])
    section_into = cfg.get("section_into")
    fields = cfg["fields"]
    scraped_at = datetime.now(UTC).isoformat()

    pos = {id(el): i for i, el in enumerate(soup.find_all(True))}
    heads = sorted((pos[id(h)], h.get_text(" ", strip=True)) for h in soup.select(cfg["heading"]))
    hpos = [p for p, _ in heads]

    records = []
    for a in soup.select(cfg["person"]):
        p = pos.get(id(a))
        if p is None:
            continue
        idx = bisect.bisect_right(hpos, p) - 1
        section = heads[idx][1] if idx >= 0 else None
        if include and section not in include:
            continue
        raw = {name: _extract_field(a, fs, base_url) for name, fs in fields.items()}
        rec = {f: raw.get(f) for f in PEOPLE_FIELDS}
        for k, v in raw.items():
            if k not in rec:
                rec[k] = v
        if section_into:
            rec[section_into] = section
        rec["source_id"] = source_id
        rec["scraped_at"] = scraped_at
        records.append(rec)
    return records


def extract(
    source_id: str,
    spec: dict | None = None,
    *,
    html: str | None = None,
    base_url: str | None = None,
) -> list[dict]:
    """Run the spec against a page and return target-model records. Defaults to
    the source's cached main page; `html`/`base_url` override it (multi-page)."""
    spec = spec or load_spec(source_id)

    if spec.get("source_type") == "json":
        return _extract_json(source_id, spec)
    if spec.get("source_type") == "grouped_people":
        return _extract_grouped_people(source_id, spec)
    if spec.get("source_type") == "sectioned_people":
        return _extract_sectioned_people(source_id, spec)

    if html is None:
        if not cache.is_cached(source_id):
            raise SpecError(f"{source_id} not in cache — fetch it first")
        meta = cache.read_meta(source_id)
        base_url = meta.get("url", "")
        html = cache.read_page(source_id)
    soup = BeautifulSoup(html, "html.parser")

    page_type = spec.get("page_type")
    rec_spec = spec["record"]
    if rec_spec.get("split_on"):
        containers = _split_containers(soup, rec_spec)
    else:
        containers = soup.select(rec_spec["container"])
    # Optionally drop containers at/after a marker element in document order
    # (e.g. only current topics, before a "Themenarchiv" heading).
    stop = rec_spec.get("stop_before")
    if stop:
        stop_el = soup.select_one(stop)
        if stop_el is not None:
            order = {id(e): i for i, e in enumerate(soup.find_all(True))}
            sp = order.get(id(stop_el), 1 << 30)
            containers = [c for c in containers if order.get(id(c), 0) < sp]
    # Drop containers whose text contains any of these strings (e.g. a "†"
    # deceased marker).
    drop = rec_spec.get("drop_if_contains")
    if drop:
        needles = [drop] if isinstance(drop, str) else list(drop)
        containers = [c for c in containers if not any(n in c.get_text() for n in needles)]
    scraped_at = datetime.now(UTC).isoformat()

    known = PEOPLE_FIELDS if page_type == "people" else TOPIC_FIELDS
    title_declared = "title" in (rec_spec.get("fields") or {})
    records = []
    for c in containers:
        raw = {name: _extract_field(c, fs, base_url) for name, fs in rec_spec["fields"].items()}
        # Keep spec-declared extras (like _profile_url) but present target
        # fields in a stable order with None fill.
        record = {f: raw.get(f) for f in known}
        for k, v in raw.items():
            if k not in record:
                record[k] = v
        record["source_id"] = source_id
        record["scraped_at"] = scraped_at
        if page_type == "topics":
            # A spec says WHERE the title is, never what a title looks like, so a
            # selector matching the wrong element yields a wrong-but-well-formed
            # value. Repair it from the container before the id is computed, so
            # topic_id is seeded from the corrected title. Only specs that
            # actually declare a title are touched; `title_check: false` opts out.
            if title_declared and spec.get("title_check", True) is not False:
                title_check.repair(record, c, extra_selectors=spec.get("title_candidates"))
            record["source_link"] = record.get("source_link") or base_url
            record["topic_id"] = _topic_id(base_url, record, spec.get("id_from"))
        for f in spec.get("omit_fields", ()):  # e.g. drop a renamed default field
            record.pop(f, None)
        records.append(record)
    return records


# --- People profile following (one step deep) ------------------------------


def follow_candidates(records: list[dict], follow: dict) -> list[tuple[dict, str]]:
    """Records whose _profile_url matches the follow pattern, paired with the
    URL. Used to SHOW the operator what would be followed before following."""
    pattern = re.compile(follow["url_pattern"])
    field = follow.get("url_field", "_profile_url")
    out = []
    for r in records:
        url = r.get(field)
        if url and pattern.match(url):
            out.append((r, url))
    return out


def _link_slug(url: str) -> str:
    # Readable filename prefix + hash of the full URL. The hash disambiguates
    # URLs that share a filename but differ upstream (e.g. UZH DAM links, where
    # the distinguishing part is the jcr:UUID segment, not the trailing name).
    import hashlib

    tail = url.rstrip("/").split("/")[-1] or url
    return cache._slug(tail) + "-" + hashlib.sha1(url.encode()).hexdigest()[:8]


def _follow_fields(follow: dict) -> dict:
    """Where a follow block lists the fields to pull from the followed page:
    `fields:` (generic), or `profile.fields:` / `detail.fields:` (named)."""
    return (
        follow.get("fields")
        or follow.get("profile", {}).get("fields")
        or follow.get("detail", {}).get("fields")
        or {}
    )


def enrich_from_links(
    source_id: str,
    records: list[dict],
    follow: dict,
    *,
    kind: str,
    fetch_fn=None,
    limit: int | None = None,
    log=lambda m: None,
) -> int:
    """Follow each record's link one step deep (cached under
    cache/<id>/<kind>/<slug>.html), run the follow sub-template, and merge
    non-empty fields into the record. Generic over people profiles and topic
    detail pages. `fetch_fn(url, render) -> (status, html)` is injected so
    extraction stays testable and cache-first."""
    fields = _follow_fields(follow)
    if not fields:
        return 0
    render = bool(follow.get("render"))
    candidates = follow_candidates(records, follow)
    if limit is not None:
        candidates = candidates[:limit]

    enriched = 0
    for record, url in candidates:
        slug = _link_slug(url)
        if cache.has_subpage(source_id, kind, slug):
            html = cache.read_subpage(source_id, kind, slug)
        elif fetch_fn is not None:
            try:
                status, html = fetch_fn(url, render)
            except Exception as exc:  # noqa: BLE001 — one bad page must not
                log(f"    {kind} fetch error: {url}: {exc}")  # abort the rest
                continue
            if not html or not (200 <= status < 300):
                log(f"    {kind} fetch failed ({status}): {url}")
                continue
            cache.write_subpage(source_id, kind, slug, html)
        else:
            continue  # not cached and no fetcher → skip

        soup = BeautifulSoup(html, "html.parser")
        got = False
        for fname, fs in fields.items():
            value = _extract_field(soup, fs, url)
            if value not in (None, "", []):
                record[fname] = value
                got = True
        if got:
            enriched += 1
    return enriched


def enrich_from_profiles(
    source_id: str,
    records: list[dict],
    spec: dict,
    *,
    fetch_fn=None,
    limit: int | None = None,
    log=lambda m: None,
) -> int:
    """People-profile following (thin wrapper over enrich_from_links)."""
    follow = spec.get("follow")
    if not follow:
        return 0
    return enrich_from_links(
        source_id, records, follow, kind="people", fetch_fn=fetch_fn, limit=limit, log=log
    )


# --- Preview (target-model nesting) ----------------------------------------


def normalize_supervisors(rec: dict) -> None:
    """Collapse a topic's supervisor info into a single `supervisors` list of
    {name, email}. Uses an explicit `supervisors` list when the spec produced
    one (e.g. HASEL's tiles); otherwise wraps the scalar supervisor_name /
    supervisor_email into a one-element list. The scalar keys are then dropped
    so the output has one uniform shape everywhere."""
    sups = rec.get("supervisors")
    if not sups:
        name, email = rec.get("supervisor_name"), rec.get("supervisor_email")
        sups = [{"name": name, "email": email}] if (name or email) else []
    for sup in sups:
        nm = sup.get("name")
        # a "name" that is actually an email (broken source link) belongs in email
        if isinstance(nm, str) and "@" in nm and not sup.get("email"):
            sup["email"] = nm.strip()
            sup["name"] = None
        elif isinstance(nm, str):  # drop academic titles ("Dr. X" -> "X")
            sup["name"] = _t_strip_titles(nm) or None
    rec["supervisors"] = sups
    rec.pop("supervisor_name", None)
    rec.pop("supervisor_email", None)


def to_preview(source: registry.Source, records: list[dict]) -> dict:
    page_type = load_spec(source.source_id).get("page_type")
    key = {"people": "people", "topics": "concrete_topics"}.get(page_type, "records")
    return {
        "faculty": source.faculty,
        "faculty_code": source.faculty_code,
        "unit": source.unit,
        "unit_id": source.unit_id,
        "source_id": source.source_id,
        "source_url": source.url,
        "page_type": page_type,
        key: records,
    }

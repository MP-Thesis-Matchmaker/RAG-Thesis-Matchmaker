"""Person-name canonicalisation, shared by the scraper and the matcher.

Two members need the same operations on human names and they need them for
different reasons, which is what puts this on the shared floor rather than in
either one. The scraper cleans names on the way *in* -- a page says
"Prof. Dr. Hui Chen" and the record should say "Hui Chen". The matcher compares
names *across sources* -- a posting says "Davide Scaramuzza" and a paper says
"Scaramuzza, Davide", and those have to resolve to one person.

Everything here operates on a single `str` and returns a `str`. Callers that
accept arbitrary values (the scraper's spec transforms, which run over whatever
a YAML spec points at) keep their own isinstance guard; this module does not
pretend a list is a name.

## Two folds, deliberately

`fold_german` and `fold_ascii` are not interchangeable and neither is the
"better" one:

- `fold_german` expands umlauts the way German does (``ü`` -> ``ue``) and then
  drops everything that is not a letter, **including spaces**. It yields one
  compact token for whole-string comparison, which is what pairing a name
  against an email local-part needs.
- `fold_ascii` decomposes and drops combining marks (``ü`` -> ``u``) and
  **preserves word boundaries**, so a name can still be split into given and
  family parts afterwards.

The consequence worth knowing: `fold_ascii` makes ``Müller`` equal ``Muller``
but *not* ``Mueller``, while `fold_german` does the reverse. Both ZORA and the
scraped postings write ``Müller`` with the umlaut, so the matcher takes
`fold_ascii` and the trade is accepted rather than hidden.
"""

from __future__ import annotations

import re
import unicodedata

__all__ = [
    "TITLE_PATTERN",
    "fold_ascii",
    "fold_german",
    "flip_family_given",
    "flip_trailing_given",
    "strip_initials",
    "strip_titles",
]

# Academic titles as they appear in UZH pages and ZORA records. Kept as one
# alternation rather than a token set so that multi-part forms survive: "h. c."
# carries an internal space, and "Dipl.-Ing." carries a hyphen, so neither
# survives a naive split-on-whitespace.
TITLE_PATTERN = (
    r"(?:Prof\.?|Dres\.?|Dr\.?|PD\.?|em\.?|emer\.?|habil\.?|iur\.?|rer\.?|nat\.?"
    r"|pol\.?|oec\.?|soc\.?|phil\.?|sc\.?|med\.?|h\.?\s?c\.?|Dipl\.?[\w-]*\.?)"
)

_TITLE_LEAD_RE = re.compile(rf"^(?:{TITLE_PATTERN}\s*)+", re.I)
# ", Prof. Dr." or " Prof. Dr." -- trailing titles are comma-separated as often
# as not, and the comma has to go with them or it looks like a "Family, Given".
_TITLE_TRAIL_RE = re.compile(rf"[,\s]\s*(?:{TITLE_PATTERN}\s*)+$", re.I)

_UMLAUTS = (("ä", "ae"), ("ö", "oe"), ("ü", "ue"), ("ß", "ss"))

# A single-letter token followed by a period: the "J." in "Anna J. Meier".
_INITIAL_RE = re.compile(r"\b\w\.\s*")


def strip_titles(value: str) -> str:
    """Drop leading *or* trailing academic titles, preserving the name's case.

    Both ends matter: pages write "Prof. Dr. Hui Chen" and
    "Francisco Amaral, Prof. Dr.", and only stripping the front leaves the
    second one with a comma that later reads as a "Family, Given" separator.
    """
    return _TITLE_LEAD_RE.sub("", _TITLE_TRAIL_RE.sub("", value)).strip()


def flip_family_given(value: str) -> str:
    """``"Backhaus, Norman, Prof. Dr."`` -> ``"Norman Backhaus"``.

    Titles are stripped first, so the trailing ", Prof. Dr." does not get
    mistaken for a third name part. A string with no comma is returned as-is
    (after title stripping) rather than guessed at -- `flip_trailing_given` is
    the deliberate choice for that case.
    """
    value = strip_titles(value)
    parts = [p.strip() for p in value.split(",") if p.strip()]
    if len(parts) >= 2:
        return f"{' '.join(parts[1:])} {parts[0]}".strip()
    return value


def flip_trailing_given(value: str) -> str:
    """``"Guerreiro Stücklin Ana"`` -> ``"Ana Guerreiro Stücklin"``.

    For comma-less "Family... Given" listings, where the *last* token is the
    given name. This is a guess about a source's convention and only correct
    where that convention holds, which is why it is a separate function a caller
    opts into rather than a fallback inside `flip_family_given`.
    """
    tokens = value.split()
    if len(tokens) >= 2:
        return f"{tokens[-1]} {' '.join(tokens[:-1])}"
    return value


def strip_initials(value: str) -> str:
    """Drop single-letter initials and normalise case and spacing.

    ``"Juri A. Opitz"`` -> ``"juri opitz"``, so it compares equal to
    ``"Juri Opitz"``. Collapses *runs* of whitespace, not just doubles: removing
    a middle initial leaves two spaces behind, and removing two adjacent ones
    leaves three.
    """
    return re.sub(r"\s+", " ", _INITIAL_RE.sub(" ", value)).strip().lower()


def fold_german(value: str) -> str:
    """Fold to one compact ASCII token, expanding umlauts the German way.

    ``"Müller-Schmidt"`` -> ``"muellerschmidt"``. Whitespace and punctuation are
    dropped, not preserved, so the result is a comparison token and never a
    name. Use it where the other side of the comparison is also shapeless -- an
    email local-part, a URL slug. Use `fold_ascii` where the parts still matter.
    """
    value = value.lower()
    for umlaut, expansion in _UMLAUTS:
        value = value.replace(umlaut, expansion)
    return re.sub(r"[^a-z]", "", value)


def fold_ascii(value: str) -> str:
    """Fold to lowercase ASCII words, preserving boundaries.

    ``"Müller-Schmidt, Anna"`` -> ``"muller schmidt anna"``. Decomposes with
    NFKD and drops combining marks, so accents vanish rather than expand; every
    non-alphanumeric becomes a space, so hyphens and periods split rather than
    glue. The surviving spaces are the point: a caller can still tell given from
    family afterwards.
    """
    decomposed = unicodedata.normalize("NFKD", value)
    stripped = "".join(c for c in decomposed if not unicodedata.combining(c))
    spaced = "".join(c if c.isalnum() or c.isspace() else " " for c in stripped.lower())
    return " ".join(spaced.split())

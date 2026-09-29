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
    "DEGREE_PATTERN",
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
#
# The trailing `\b\.?` is load-bearing and the period must stay OUTSIDE the
# alternation for it to work. With the period optional and inside -- as this
# read until 2026-09-03 -- "sc\.?" matches the first two letters of
# "Scaramuzza" and the leading-title regex happily strips them, yielding
# "aramuzza". "med" does the same to "Medina", "nat" to "Nathalie", "em" to
# "Emma", "pol" to "Polanski". Requiring a word boundary after the title makes
# each alternative match only a whole token.
TITLE_PATTERN = (
    r"(?:Prof|Dres|Dr|PD|em|emer|habil|iur|rer|nat"
    r"|pol|oec|soc|phil|sc|med|h\.?\s?c|Dipl\.?[\w-]*)\b\.?"
)

_TITLE_LEAD_RE = re.compile(rf"^(?:{TITLE_PATTERN}\s*)+", re.I)
# ", Prof. Dr." or " Prof. Dr." -- trailing titles are comma-separated as often
# as not, and the comma has to go with them or it looks like a "Family, Given".
_TITLE_TRAIL_RE = re.compile(rf"[,\s]\s*(?:{TITLE_PATTERN}\s*)+$", re.I)

# Degrees written after a name: "Lidia Borkovic, MSc", "Caviezel, Giuanna, M.A.".
# Left in place, the comma reads as a "Family, Given" separator and the degree
# becomes somebody's given name.
#
# Stricter than TITLE_PATTERN on purpose, in two ways. Case-SENSITIVE, because
# "MA" and "BA" are also names: case-folded, "Lin, Ma" would lose its given name
# and "Ba, Amadou" would be left with nothing to key on. And only after a COMMA,
# because pages that capitalise family names write "Lin MA", where a whitespace-
# anchored match would strip the family name. `(?!\w)` rather than `\b`, since a
# boundary cannot follow the trailing period of "M.Sc.".
DEGREE_PATTERN = (
    r"(?:MSc|M\.\s?Sc\.?|BSc|B\.\s?Sc\.?|PhD|Ph\.\s?D\.?|MAS|MBA|MA|M\.\s?A\.?"
    r"|BA|B\.\s?A\.?|M\.\s?Ed\.?|MEd|LL\.\s?M\.?)(?!\w)"
)

_DEGREE_TRAIL_RE = re.compile(rf"(?:\s*,\s*{DEGREE_PATTERN})+\s*$")

# Degrees written before a name: "M. Sc. Anna Beispiel". A narrower set than
# DEGREE_PATTERN, because in front of a name "M. A." and "B. A." are far more
# often a person's initials than a degree, and "MA" or "MAS" can be a family
# name written first. Only forms no initial pair can spell come off here.
_LEAD_DEGREE_PATTERN = (
    r"(?:MSc|M\.\s?Sc\.?|BSc|B\.\s?Sc\.?|PhD|Ph\.\s?D\.?|MBA|M\.\s?Ed\.?|MEd"
    r"|LL\.\s?M\.?)(?!\w)"
)

_DEGREE_LEAD_RE = re.compile(rf"^(?:{_LEAD_DEGREE_PATTERN}\s*)+")


def _strip_degrees(value: str) -> str:
    """Drop trailing degrees, but only when a whole name is left behind.

    ZORA writes given initials after the comma -- "Müller, M. A." -- which is
    exactly the spelling of the degree "M.A.". Stripping it would leave a bare
    family name, so a degree comes off only when what remains still carries a
    comma ("Caviezel, Giuanna") or at least two tokens ("Lidia Borkovic").
    """
    remainder = _DEGREE_TRAIL_RE.sub("", value)
    if remainder != value and ("," in remainder or len(remainder.split()) >= 2):
        return remainder
    return value


def _strip_lead_degrees(value: str) -> str:
    """Drop leading degrees under the same guard: a whole name must remain.

    Left in place, "M. Sc. Anna Beispiel" keys as a person whose given name is
    "M" -- the period splits "M." off as an initial.
    """
    remainder = _DEGREE_LEAD_RE.sub("", value)
    if remainder != value and ("," in remainder or len(remainder.split()) >= 2):
        return remainder
    return value


_UMLAUTS = (("ä", "ae"), ("ö", "oe"), ("ü", "ue"), ("ß", "ss"))

# A single-letter token followed by a period: the "J." in "Anna J. Meier".
_INITIAL_RE = re.compile(r"\b\w\.\s*")


def strip_titles(value: str) -> str:
    """Drop leading *or* trailing academic titles, preserving the name's case.

    Both ends matter: pages write "Prof. Dr. Hui Chen" and
    "Francisco Amaral, Prof. Dr.", and only stripping the front leaves the
    second one with a comma that later reads as a "Family, Given" separator.

    Trailing degrees go too ("Lidia Borkovic, MSc"), between two trailing-title
    passes, so either order of the two suffixes comes off: "X, Prof. Dr., PhD"
    and "X, PhD, Prof. Dr." both reduce to "X". Leading degrees are sandwiched
    the same way ("Dr. M.Sc. X" and "M.Sc. Dr. X").
    """
    value = _TITLE_TRAIL_RE.sub("", value)
    value = _TITLE_TRAIL_RE.sub("", _strip_degrees(value))
    value = _TITLE_LEAD_RE.sub("", value.strip())
    return _TITLE_LEAD_RE.sub("", _strip_lead_degrees(value)).strip()


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

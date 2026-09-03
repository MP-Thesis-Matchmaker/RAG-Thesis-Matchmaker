"""Resolving a person named on a posting to the same person named on a paper.

The two sources spell people differently and neither is wrong: ZORA writes
``"Scaramuzza, Davide"`` because DSpace stores a family and a given name, and a
department page writes ``"Davide Scaramuzza"`` because that is how a human reads
it. Grouping on the raw string therefore returns one researcher twice, and
`SupervisorMatch.publication_count` and `posting_count` are never both non-zero.

The asymmetry is the whole design. The ZORA side is *structured* -- the comma
says which half is the family name -- so it supplies the anchors, and the free
text is resolved against them. Guessing where a free-text name splits is only
safe when the guess can be checked against a name that did not need guessing;
99.9% of `uzh_authors` strings carry a comma, so there is nearly always
something to check against.

## Strict on purpose

A merge credits one person with another's publications, and the matcher shows
those to a student as evidence for a recommendation. A wrong merge is therefore
fabricated evidence that looks entirely plausible, so this module refuses far
more than it accepts:

- the full first given token must agree; an initial is never enough
- a family-name match alone is never enough
- a name whose split is ambiguous against the anchors is not merged at all

Measured over the live corpus with this implementation on 2026-09-03: 403
distinct supervisor names, **103 resolved** (25.6%), 0 refused as ambiguous. Of
2,411 anchor keys, 4 collapse authors whose given names differ -- and two of
those are ``maria``/``maría``, the same person twice. Only ``Meier, Pascal
Felix`` against ``Meier, Pascal Flurin`` is a genuine conflation, and **no
supervisor name reaches any of the four**.

The 300 that do not resolve mostly *cannot*: 251 have no ZORA record at all,
being PhD students, postdocs, or externals (``vogelwarte.ch``, ``eawag.ch``,
``agroscope.admin.ch``). That is a limit of the data, not of the rule.

**103 is a ceiling, not a yield.** `retrieve` fetches `top_k` postings and
`top_k` publications separately, so a merge needs one person in both slices at
once. Measured over five probes: **0 of 25 returned matches at `top_k=5`**, 1 of
100 at 20, 7 of 250 at 50. At the default width this join effectively never
fires, and no coverage claim may be made from the corpus figure alone. See
`docs/person-key-resolution.md`.

**Not a swappable seam.** The matcher's `base.py` + `build_*(settings)` idiom
exists for things `MatcherSettings` chooses between. There is one implementation
here and no setting, so there is no Protocol and no factory. The omission is
deliberate.
"""

from __future__ import annotations

from dataclasses import dataclass

from themis_shared.names import flip_family_given, fold_ascii, strip_titles


@dataclass(frozen=True)
class PersonKey:
    """One person, as far as a name can identify one.

    `given` is the **first** given token only, which is what lets
    ``"Alexandra M. Freund"`` and ``"Alexandra Freund"`` be the same person
    without needing an email to prove it. `family` may be several tokens, because
    ``"De Luca"`` and ``"Leal Neto"`` are.
    """

    given: str
    family: str


def _tokens(text: str) -> list[str]:
    return fold_ascii(text).split()


def key_of(name: str) -> PersonKey | None:
    """The key for a name whose shape is already known.

    Titles come off **first**, before the comma is looked for: a trailing
    ", Prof. Dr." is a title, not a name part, and reading it as the given half
    of "Family, Given" turns Francisco Amaral into a person called Prof.

    A surviving comma means DSpace already did the splitting, so everything
    before it is the family name however many tokens that is. Without one the
    name is read in natural order and keyed on its **last** token alone --
    "Alexandra M. Freund" and "Alexandra Freund" have to land together, and they
    only do if the middle name is discarded rather than folded into the family.
    """
    cleaned = strip_titles(name)
    if "," in cleaned:
        family, _, given = cleaned.partition(",")
        family_tokens, given_tokens = _tokens(family), _tokens(given)
        if not family_tokens or not given_tokens:
            return None
        return PersonKey(given_tokens[0], " ".join(family_tokens))

    tokens = _tokens(cleaned)
    if len(tokens) < 2:
        return None
    return PersonKey(tokens[0], tokens[-1])


def candidates(name: str) -> list[PersonKey]:
    """Every way a free-text name might split into given and family.

    ``"Alessandro De Luca"`` is either Alessandro De / Luca or Alessandro / De
    Luca, and nothing in the string says which. Both are offered and `resolve`
    picks the one the structured side recognises, so the particle problem is
    settled by evidence rather than by a list of particles to special-case.
    """
    tokens = _tokens(strip_titles(name))
    return [PersonKey(tokens[0], " ".join(tokens[-take:])) for take in (1, 2) if len(tokens) > take]


def resolve(name: str, anchors: set[PersonKey]) -> PersonKey | None:
    """The one anchor this free-text name matches, or None.

    None covers both "no anchor recognised it" and "more than one did". The
    second is a refusal rather than a failure, and the caller treats them the
    same way -- the person is grouped under their own spelling instead. Merging
    on a coin flip is the outcome this returns None to avoid.
    """
    matched = [key for key in candidates(name) if key in anchors]
    return matched[0] if len(matched) == 1 else None


def display_name(spellings: list[str]) -> str:
    """The most readable spelling seen for one person.

    A natural-order spelling is returned **verbatim**, titles and all: it came
    off a page written for humans and there is nothing to fix. A comma form has
    to be reordered to be readable at all, and reordering it is the one
    transformation applied. Longest wins within each shape, as a proxy for most
    complete -- ``"Scaramuzza, Davide"`` beats ``"Scaramuzza, D"``.
    """
    natural = [s for s in spellings if "," not in s]
    if natural:
        return max(natural, key=len)
    return max((flip_family_given(s) for s in spellings), key=len)

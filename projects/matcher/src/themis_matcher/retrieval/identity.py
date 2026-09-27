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
- only `uzh_authors` supply anchors; a plain author of an unaffiliated paper
  never vouches for a posting name, because against 331,301 distinct author keys
  a namesake is likely
- nor does that plain author join a UZH author of the same name: `author_key`
  keeps them apart, so a stranger's paper cannot become a UZH researcher's
  evidence. The cost is visible and accepted: a UZH researcher whose papers are
  partly ORCID-only (empty `uzh_authors`) can appear twice under one name, once
  affiliated and once demoted below every UZH match
- a name whose split is ambiguous against the anchors is not merged at all
- any posting name no single anchor vouches for -- ambiguous or unmatched --
  gets an `unresolved` key that no publication person can equal, so it cannot
  merge by coincidence of key either
- a posting name that already writes ``"Family, Given"`` is read by its comma,
  never split by guessing

Measured over the live corpus on 2026-09-03, the ceiling re-measured on
2026-09-27: 403 distinct supervisor names, **105 resolved** (26.1%), 0 refused as
ambiguous. Of
2,411 anchor keys, 4 collapse authors whose given names differ -- and two of
those are ``maria``/``maría``, the same person twice. Only ``Meier, Pascal
Felix`` against ``Meier, Pascal Flurin`` is a genuine conflation, and **no
supervisor name reaches any of the four**.

The 298 that do not resolve mostly *cannot*: 251 have no registered-author
record -- no CRIS `person` row with even a matching family name, and only 6 of
them among the `uzh_authors` anchors -- being PhD students, postdocs, or
externals (``vogelwarte.ch``, ``eawag.ch``, ``agroscope.admin.ch``). Many still
name-match *some* ZORA author string; that is not a record this rule can trust.
That is a limit of the data, not of the rule.

**105 is a ceiling, not a yield.** `retrieve` fetches `top_k` postings and
`top_k` publications separately, so a merge needs one person in both slices at
once. Measured over five probes: **0 of 25 returned matches at `top_k=5`**, 1 of
100 at 20, 5 of 250 at 50 (2026-09-27). At the default width this join effectively never
fires, and no coverage claim may be made from the corpus figure alone. See
`docs/person-key-resolution.md`.

**Not a swappable seam.** The matcher's `base.py` + `build_*(settings)` idiom
exists for things `MatcherSettings` chooses between. There is one implementation
here and no setting, so there is no Protocol and no factory. The omission is
deliberate.
"""

from __future__ import annotations

from dataclasses import dataclass, replace

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
    # Set only by `posting_key`, for a posting name no single anchor vouches for.
    # It takes part in equality, so such a key can never equal a publication
    # person's -- those come from `key_of`, which never sets it.
    unresolved: bool = False
    # Set only by `author_key`, for an author credited through the `authors`
    # fallback of a paper with no `uzh_authors`. Also part of equality, so a
    # namesake on an unaffiliated paper never joins a UZH author or a posting.
    unaffiliated: bool = False


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


def author_key(name: str) -> PersonKey | None:
    """The key for an author credited only through a paper's `authors` fallback.

    `key_of`'s reading, marked `unaffiliated`. Two unaffiliated papers by the same
    author string still group together; neither joins the UZH author or the
    posting person who happens to share the name.
    """
    key = key_of(name)
    return None if key is None else replace(key, unaffiliated=True)


def resolve(name: str, anchors: set[PersonKey]) -> PersonKey | None:
    """The anchor this posting name merges into, or None.

    None covers both "no anchor recognised it" and "more than one did". The
    second is a refusal rather than a failure. Merging on a coin flip is the
    outcome this returns None to avoid.

    Defined through `posting_key` rather than beside it, so the coverage
    measurement counts exactly the rule the retriever ships. It used to re-derive
    the answer from `candidates`, which ignores commas, and so disagreed with the
    retriever on every comma-form posting name.
    """
    key = posting_key(name, anchors)
    return None if key is None or key.unresolved else key


def posting_key(name: str, anchors: set[PersonKey]) -> PersonKey | None:
    """The grouping key for a free-text name, as the retriever uses it.

    A posting that writes ``"Family, Given"`` is checked **first**, by its comma
    alone: `candidates` ignores commas, so it would read ``"Thomas, Martin"`` in
    natural order and could match the anchor for ``"Martin, Thomas"``, a
    different person. The comma is the same evidence ZORA's own names carry, so
    the structured reading either is an anchor or is nothing.

    A free-text name merges only when exactly one candidate split is an anchor.
    Every other outcome -- no split matched, or several did -- gets `key_of`'s
    reading marked `unresolved`. The plain reading is not safe in either case:
    when several matched, ``(first, last)`` is one of the anchors that made the
    name ambiguous; when none did, it can still equal an unaffiliated paper's
    author key, which is a publication person but not an anchor, and the
    retriever groups on key equality alone. Two postings spelling a name the same
    way still group together; neither joins a publication person.
    """
    if "," in strip_titles(name):
        key = key_of(name)
        if key is not None:
            return key if key in anchors else replace(key, unresolved=True)
        # A comma with nothing on one side ("Sofia Forss,", a real scraped name)
        # is punctuation, not structure. Returning None here dropped the person
        # from every result; read the name as free text instead.
        name = strip_titles(name).replace(",", " ")
    matched = [key for key in candidates(name) if key in anchors]
    if len(matched) == 1:
        return matched[0]
    key = key_of(name)
    return None if key is None else replace(key, unresolved=True)


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

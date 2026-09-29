"""Tests for resolving a posting's supervisor to a paper's author.

The rule these pin is deliberately strict, and the test that matters most is the
one asserting a *refusal*. A merge credits one person with another's
publications and the matcher shows those to a student as evidence, so a wrong
merge is fabricated evidence that looks entirely plausible.
"""

from __future__ import annotations

from themis_matcher.retrieval.identity import (
    PersonKey,
    anchors_of,
    author_key,
    author_keys,
    candidates,
    compatible,
    display_name,
    key_of,
    posting_key,
    resolve,
)


def test_the_two_spellings_of_one_person_produce_the_same_key() -> None:
    """The defect this module exists to fix, in one assertion."""
    assert key_of("Scaramuzza, Davide") == PersonKey("davide", "scaramuzza")
    assert resolve("Davide Scaramuzza", anchors_of(["Scaramuzza, Davide"])) == PersonKey(
        "davide", "scaramuzza"
    )


def test_a_shared_family_name_is_never_enough_to_merge() -> None:
    """The failure this whole change exists to prevent.

    Measured on the live corpus: 46 of 403 supervisor names share a family name
    with a *different* ZORA author. "Daniel Müller" against "Müller, Mathias" is
    the real shape of it. A rule that merged on the family name alone would
    credit a supervisor with a stranger's papers, and nothing downstream could
    tell.
    """
    anchors = anchors_of(["Müller, Mathias", "Müller, Sabrina", "Müller, Thomas"])
    assert resolve("Daniel Müller", anchors) is None
    assert resolve("Qianyu Liu", anchors_of(["Liu, Tingting"])) is None
    assert resolve("Gian Ege", anchors_of(["Ege, Moritz"])) is None


def test_an_initial_matches_only_an_initial() -> None:
    """ "D. Scaramuzza" could be Davide or Dominik; the rule declines to guess.

    Two initials are different: both sources say the same thing, and nothing is
    inferred. The audit found none of these among the live corpus's merges.
    """
    assert resolve("D. Scaramuzza", anchors_of(["Scaramuzza, Davide"])) is None
    assert resolve("Davide Scaramuzza", anchors_of(["Scaramuzza, D."])) is None
    assert resolve("A. Beispiel", anchors_of(["Beispiel, A."])) == PersonKey("a", "beispiel")


def test_a_middle_name_does_not_split_one_person_in_two() -> None:
    """Only the FIRST given token keys a person.

    "Alexandra M. Freund" and "Alexandra Freund" are one researcher writing her
    name two ways on two pages -- confirmed by both spellings carrying the same
    email. Keying on the full given string would return her twice.
    """
    assert key_of("Alexandra M. Freund") == key_of("Alexandra Freund")
    assert key_of("Horn, Andrea B.") == key_of("Horn, Andrea")


def test_accents_fold_away() -> None:
    assert key_of("Müller, Anna") == key_of("Muller, Anna")
    assert key_of("Sánchez, Marcelo") == key_of("Sanchez, Marcelo")


def test_titles_do_not_reach_the_key() -> None:
    assert key_of("Prof. Dr. Rico Sennrich") == key_of("Rico Sennrich")
    assert key_of("Francisco Amaral, Prof. Dr.") == PersonKey("francisco", "amaral")


def test_a_particle_family_name_is_settled_by_the_anchor_not_by_a_particle_list() -> None:
    """ "Alessandro De Luca" splits two ways and the string does not say which.

    Both readings are offered; the structured side decides. This is why there is
    no hardcoded list of "de", "van", "von" to maintain.
    """
    # The given half is always the first token; only the family boundary moves.
    assert candidates("Alessandro De Luca") == [
        PersonKey("alessandro", "luca"),
        PersonKey("alessandro", "de luca"),
    ]

    assert resolve("Alessandro De Luca", anchors_of(["De Luca, Alessandro"])) == PersonKey(
        "alessandro", "de luca"
    )
    assert resolve("Onicio Leal Neto", anchors_of(["Leal Neto, Onicio"])) == PersonKey(
        "onicio", "leal neto"
    )


def test_an_ambiguous_split_is_refused_rather_than_guessed() -> None:
    """Both readings match a real person, so neither is chosen.

    `resolve` returns None for "nothing matched" and "several matched" alike;
    `posting_key` is what tells the two apart for the caller.
    """
    both = anchors_of(["Luca, Alessandro", "De Luca, Alessandro"])
    assert resolve("Alessandro De Luca", both) is None


def test_an_ambiguous_posting_key_is_none_of_the_anchors() -> None:
    """The refusal has to survive the fallback.

    `key_of("Alessandro De Luca")` is (alessandro, luca) -- one of the very
    anchors that made the name ambiguous -- so falling back to it merged the
    refused name after all. The unresolved key equals no anchor, and the same
    spelling seen twice still groups with itself.
    """
    both = anchors_of(["Luca, Alessandro", "De Luca, Alessandro"])
    assert key_of("Alessandro De Luca") in both

    key = posting_key("Alessandro De Luca", both)
    assert key is not None
    assert key not in both
    assert key == posting_key("Alessandro De Luca", both)


def test_a_posting_key_takes_the_one_anchor_that_matches() -> None:
    anchors = anchors_of(["De Luca, Alessandro"])
    assert posting_key("Alessandro De Luca", anchors) == PersonKey("alessandro", "de luca")


def test_an_unmatched_posting_key_is_its_own_reading_marked_unresolved() -> None:
    """Unmatched is not the same as safe to key plainly.

    Anchors come from `uzh_authors` only, so an unaffiliated paper's "Müller,
    Daniel" is a publication person but no anchor. A plain (daniel, muller) would
    equal that person's key and merge with them on equality alone.
    """
    anchors = anchors_of(["Müller, Mathias"])
    key = posting_key("Daniel Müller", anchors)
    assert key == PersonKey("daniel", "muller", unresolved=True)
    assert key != key_of("Müller, Daniel")
    assert key not in anchors


def test_a_comma_posting_name_matches_its_structured_anchor() -> None:
    anchors = anchors_of(["Scaramuzza, Davide"])
    assert posting_key("Scaramuzza, Davide", anchors) == PersonKey("davide", "scaramuzza")


def test_a_comma_posting_name_is_never_read_in_natural_order() -> None:
    """ "Thomas, Martin" is Martin Thomas, not Thomas Martin.

    `candidates` ignores commas, so consulting it first read this name as given
    Thomas / family Martin -- exactly the anchor for "Martin, Thomas", a
    different person -- and merged them.
    """
    anchors = anchors_of(["Martin, Thomas"])
    assert set(anchors) == {PersonKey("thomas", "martin")}

    key = posting_key("Thomas, Martin", anchors)
    assert key == PersonKey("martin", "thomas", unresolved=True)
    assert key not in anchors


def test_a_stray_comma_does_not_drop_a_posting_name() -> None:
    """ "Sofia Forss," is on a real posting; the comma has no given half after it.

    Read as "Family, Given" it yields no key at all, and a posting name with no
    key credits nobody -- the supervisor vanished from every result. It is read
    as free text instead, so it resolves like the comma-less spelling would.
    """
    anchor = PersonKey("sofia", "forss")
    assert posting_key("Sofia Forss,", anchors_of(["Forss, Sofia"])) == anchor
    assert resolve("Sofia Forss,", anchors_of(["Forss, Sofia"])) == anchor
    assert posting_key("Sofia Forss,", {}) == PersonKey("sofia", "forss", unresolved=True)


def test_a_trailing_degree_is_not_read_as_a_given_name() -> None:
    """ "Lidia Borkovic, MSc" is in data/samples/theses.jsonl.

    The comma-first reading used to key her as given "msc", family "lidia
    borkovic" -- unresolvable even with her anchor present -- and display her as
    "MSc Lidia Borkovic".
    """
    anchor = PersonKey("lidia", "borkovic")
    assert posting_key("Lidia Borkovic, MSc", anchors_of(["Borkovic, Lidia"])) == anchor
    assert display_name(["Lidia Borkovic, MSc"]) == "Lidia Borkovic"


def test_an_unaffiliated_author_key_joins_no_anchor_and_no_posting() -> None:
    """A plain author of a paper with no `uzh_authors` is keyed apart.

    Same name, three key spaces: the UZH author's anchor, the posting person who
    resolved to it, and the unaffiliated namesake. Only the first two may meet.
    """
    anchor = key_of("Müller, Daniel")
    assert anchor is not None
    assert posting_key("Daniel Müller", anchors_of(["Müller, Daniel"])) == anchor

    stranger = author_key("Müller, Daniel")
    assert stranger == PersonKey("daniel", "muller", unaffiliated=True)
    assert stranger != anchor
    assert stranger != posting_key("Daniel Müller", {})
    # Two unaffiliated papers by the same author string still group together.
    assert stranger == author_key("Müller, Daniel")
    assert author_key("Madonna") is None


def test_resolve_counts_exactly_what_posting_key_merges() -> None:
    """The coverage figure has to measure the shipped rule.

    `resolve` used to re-derive its answer from `candidates`, which ignores
    commas, so it disagreed with the retriever in both directions on comma-form
    posting names: missed a merge the retriever makes, and counted one it refuses.
    """
    assert resolve("Scaramuzza, Davide", anchors_of(["Scaramuzza, Davide"])) == PersonKey(
        "davide", "scaramuzza"
    )
    assert resolve("Thomas, Martin", anchors_of(["Martin, Thomas"])) is None

    anchors = anchors_of(["Scaramuzza, Davide", "Martin, Thomas"])
    for name in ["Scaramuzza, Davide", "Thomas, Martin", "Davide Scaramuzza", "Daniel Müller"]:
        key = posting_key(name, anchors)
        merged = key if key is not None and not key.unresolved else None
        assert resolve(name, anchors) == merged


def test_a_single_token_name_yields_no_key() -> None:
    assert key_of("Madonna") is None
    assert key_of("") is None
    assert candidates("Madonna") == []


def test_display_prefers_a_natural_spelling_verbatim() -> None:
    """A page written for humans needs no fixing; a comma form does.

    Titles survive on the natural spelling on purpose -- it is the string the
    source actually published -- while the ZORA form is reordered because
    "Sennrich, Rico" is not how anyone reads a name.
    """
    assert display_name(["Sennrich, Rico", "Prof. Rico Sennrich"]) == "Prof. Rico Sennrich"
    assert display_name(["Sennrich, Rico"]) == "Rico Sennrich"
    # Longest wins as a proxy for most complete.
    assert display_name(["Scaramuzza, D", "Scaramuzza, Davide"]) == "Davide Scaramuzza"


def test_later_given_names_may_be_omitted_or_abbreviated() -> None:
    """ZORA spells one person several ways; 312 anchor keys differ only like this.

    Refusing these would split well-published researchers who are plainly one
    person, so a later given name only has to *fit*, not to be identical.
    """
    assert compatible(["m"], ["max"])
    assert compatible([], ["andreas"])
    # A dropped middle name shifts positions; order is what counts, not index.
    assert compatible(["guerreiro"], ["s", "guerreiro"])
    # A particle: "C" is the Carvalho, "de" just goes unmatched.
    assert compatible(["c"], ["de", "carvalho"])
    spellings = ["Beispiel, Markus", "Beispiel, Markus A", "Beispiel, Markus Andreas"]
    assert set(author_keys(anchors_of(spellings)).values()) == {PersonKey("markus", "beispiel")}


def test_a_contradicting_later_given_name_is_never_the_same_person() -> None:
    """ "M. A." and "M. B." cannot resolve to one name; nor can Felix and Flurin."""
    assert not compatible(["a"], ["b"])
    assert not compatible(["felix"], ["flurin"])
    # One dropped interior letter is a typo; a substitution or an ending is a name.
    assert compatible(["christian"], ["cristian"])
    assert not compatible(["maria"], ["mario"])
    assert not compatible(["daniel"], ["daniela"])


def test_an_anchor_whose_spellings_contradict_is_split() -> None:
    """Grouping is on key equality, so the split has to happen in the key.

    "Beispiel, Pascal" fits both, and so is given to neither: the split is per
    spelling, not a guess about which person the short form means.
    """
    anchors = anchors_of(["Beispiel, M. A.", "Beispiel, M. B."])
    assert len(set(author_keys(anchors).values())) == 2

    spellings = ["Beispiel, Pascal Felix", "Beispiel, Pascal Flurin", "Beispiel, Pascal"]
    keys = author_keys(anchors_of(spellings))
    assert len(set(keys.values())) == 3
    assert keys["Beispiel, Pascal Felix"] == PersonKey("pascal", "beispiel", variant=("felix",))


def test_a_posting_name_joins_a_split_anchor_only_where_exactly_one_part_fits() -> None:
    anchors = anchors_of(["Beispiel, Pascal Felix", "Beispiel, Pascal Flurin"])
    felix = PersonKey("pascal", "beispiel", variant=("felix",))
    assert posting_key("Pascal Felix Beispiel", anchors) == felix
    assert posting_key("Pascal F. Beispiel", anchors).unresolved
    assert posting_key("Pascal Beispiel", anchors).unresolved
    assert resolve("Pascal Beispiel", anchors) is None


def test_a_posting_name_that_contradicts_its_anchor_is_refused() -> None:
    anchors = anchors_of(["Beispiel, Pascal F."])
    assert resolve("Pascal Anton Beispiel", anchors) is None
    assert resolve("Pascal Felix Beispiel", anchors) == PersonKey("pascal", "beispiel")
    assert resolve("Pascal Beispiel", anchors) == PersonKey("pascal", "beispiel")
    assert resolve("Beispiel, Pascal A.", anchors) is None

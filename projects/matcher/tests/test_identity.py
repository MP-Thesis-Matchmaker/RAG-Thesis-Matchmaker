"""Tests for resolving a posting's supervisor to a paper's author.

The rule these pin is deliberately strict, and the test that matters most is the
one asserting a *refusal*. A merge credits one person with another's
publications and the matcher shows those to a student as evidence, so a wrong
merge is fabricated evidence that looks entirely plausible.
"""

from __future__ import annotations

from themis_matcher.retrieval.identity import (
    PersonKey,
    candidates,
    display_name,
    key_of,
    posting_key,
    resolve,
)


def test_the_two_spellings_of_one_person_produce_the_same_key() -> None:
    """The defect this module exists to fix, in one assertion."""
    assert key_of("Scaramuzza, Davide") == PersonKey("davide", "scaramuzza")
    assert resolve("Davide Scaramuzza", {PersonKey("davide", "scaramuzza")}) == PersonKey(
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
    anchors = {
        PersonKey("mathias", "muller"),
        PersonKey("sabrina", "muller"),
        PersonKey("thomas", "muller"),
    }
    assert resolve("Daniel Müller", anchors) is None
    assert resolve("Qianyu Liu", {PersonKey("tingting", "liu")}) is None
    assert resolve("Gian Ege", {PersonKey("moritz", "ege")}) is None


def test_an_initial_is_never_enough_to_merge() -> None:
    """ "D. Scaramuzza" could be Davide or Dominik; the rule declines to guess."""
    assert resolve("D. Scaramuzza", {PersonKey("davide", "scaramuzza")}) is None


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

    assert resolve("Alessandro De Luca", {PersonKey("alessandro", "de luca")}) == PersonKey(
        "alessandro", "de luca"
    )
    assert resolve("Onicio Leal Neto", {PersonKey("onicio", "leal neto")}) == PersonKey(
        "onicio", "leal neto"
    )


def test_an_ambiguous_split_is_refused_rather_than_guessed() -> None:
    """Both readings match a real person, so neither is chosen.

    `resolve` returns None for "nothing matched" and "several matched" alike;
    `posting_key` is what tells the two apart for the caller.
    """
    both = {PersonKey("alessandro", "luca"), PersonKey("alessandro", "de luca")}
    assert resolve("Alessandro De Luca", both) is None


def test_an_ambiguous_posting_key_is_none_of_the_anchors() -> None:
    """The refusal has to survive the fallback.

    `key_of("Alessandro De Luca")` is (alessandro, luca) -- one of the very
    anchors that made the name ambiguous -- so falling back to it merged the
    refused name after all. The unresolved key equals no anchor, and the same
    spelling seen twice still groups with itself.
    """
    both = {PersonKey("alessandro", "luca"), PersonKey("alessandro", "de luca")}
    assert key_of("Alessandro De Luca") in both

    key = posting_key("Alessandro De Luca", both)
    assert key is not None
    assert key not in both
    assert key == posting_key("Alessandro De Luca", both)


def test_a_posting_key_takes_the_one_anchor_that_matches() -> None:
    anchors = {PersonKey("alessandro", "de luca")}
    assert posting_key("Alessandro De Luca", anchors) == PersonKey("alessandro", "de luca")


def test_an_unmatched_posting_key_is_its_own_reading_marked_unresolved() -> None:
    """Unmatched is not the same as safe to key plainly.

    Anchors come from `uzh_authors` only, so an unaffiliated paper's "Müller,
    Daniel" is a publication person but no anchor. A plain (daniel, muller) would
    equal that person's key and merge with them on equality alone.
    """
    anchors = {PersonKey("mathias", "muller")}
    key = posting_key("Daniel Müller", anchors)
    assert key == PersonKey("daniel", "muller", unresolved=True)
    assert key != key_of("Müller, Daniel")
    assert key not in anchors


def test_a_comma_posting_name_matches_its_structured_anchor() -> None:
    anchors = {PersonKey("davide", "scaramuzza")}
    assert posting_key("Scaramuzza, Davide", anchors) == PersonKey("davide", "scaramuzza")


def test_a_comma_posting_name_is_never_read_in_natural_order() -> None:
    """ "Thomas, Martin" is Martin Thomas, not Thomas Martin.

    `candidates` ignores commas, so consulting it first read this name as given
    Thomas / family Martin -- exactly the anchor for "Martin, Thomas", a
    different person -- and merged them.
    """
    anchors = {key_of("Martin, Thomas")}
    assert anchors == {PersonKey("thomas", "martin")}

    key = posting_key("Thomas, Martin", anchors)
    assert key == PersonKey("martin", "thomas", unresolved=True)
    assert key not in anchors


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

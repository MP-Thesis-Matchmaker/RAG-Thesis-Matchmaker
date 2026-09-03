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

    `resolve` returns None for "nothing matched" and "several matched" alike:
    the caller keys the person on their own spelling either way, and a
    coin-flip merge never happens.
    """
    both = {PersonKey("alessandro", "luca"), PersonKey("alessandro", "de luca")}
    assert resolve("Alessandro De Luca", both) is None


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

"""Tests for the shared person-name primitives.

The first four tests are ported verbatim from the scraper's own suite
(`projects/scraper/tests/test_units.py`, class `TransformTest`), which is where
this code lived until it was promoted to the shared floor. They are duplicated
rather than moved on purpose: the scraper's copies still run against its
transform registry, and two suites asserting the same strings is what makes the
promotion provably behaviour-preserving rather than merely believed to be.
"""

from __future__ import annotations

from themis_shared.names import (
    flip_family_given,
    flip_trailing_given,
    fold_ascii,
    fold_german,
    strip_initials,
    strip_titles,
)


def test_titles_are_stripped_from_either_end() -> None:
    assert strip_titles("Prof. Dr. Hui Chen") == "Hui Chen"
    assert strip_titles("Francisco Amaral, Prof. Dr.") == "Francisco Amaral"


def test_family_given_flips_and_a_comma_less_name_does_not() -> None:
    assert flip_family_given("Backhaus, Norman, Prof. Dr.") == "Norman Backhaus"
    # No comma: returned as-is rather than guessed at.
    assert flip_family_given("Norman Backhaus") == "Norman Backhaus"


def test_trailing_given_moves_the_last_token_to_the_front() -> None:
    assert flip_trailing_given("Altmeyer Matthias") == "Matthias Altmeyer"
    assert flip_trailing_given("Guerreiro Stücklin Ana") == "Ana Guerreiro Stücklin"


def test_a_multi_part_title_survives_stripping() -> None:
    """The reason TITLE_PATTERN is one alternation and not a set of tokens.

    "h. c." carries an internal space and "Dipl.-Ing." a hyphen, so neither
    survives splitting the name on whitespace and filtering known words.
    """
    assert strip_titles("Prof. Dr. h. c. Anna Meier") == "Anna Meier"
    assert strip_titles("Dipl.-Ing. Anna Meier") == "Anna Meier"


def test_initials_are_dropped_and_whitespace_runs_collapse() -> None:
    """Two adjacent initials leave three spaces behind, not two.

    The scraper carried three copies of this helper and two of them collapsed
    only a single double-space, so "Anna J. K. Meier" kept a gap. Collapsing
    runs is the behaviour that survived promotion.
    """
    assert strip_initials("Juri A. Opitz") == "juri opitz"
    assert strip_initials("Anna J. K. Meier") == "anna meier"
    assert strip_initials("  Anna   Meier  ") == "anna meier"


def test_the_two_folds_disagree_about_umlauts_on_purpose() -> None:
    """Neither fold is the better one; they answer different questions.

    `fold_ascii` keeps the words separable, which is what splitting a name into
    given and family needs. `fold_german` yields one token that matches the
    German transliteration, which is what pairing against an email local-part
    needs. A caller picking the wrong one gets plausible-looking wrong answers,
    so the divergence is pinned here rather than left to be rediscovered.
    """
    assert fold_ascii("Müller") == "muller"
    assert fold_german("Müller") == "mueller"

    assert fold_ascii("Müller-Schmidt, Anna") == "muller schmidt anna"
    assert fold_german("Müller-Schmidt") == "muellerschmidt"


def test_fold_ascii_preserves_word_boundaries() -> None:
    """Punctuation becomes a separator, never glue.

    "Fuentes Perez, Lizeth J" has to fold into four comparable tokens; if the
    comma glued "perez" to "lizeth" the family name would never match its
    counterpart on a posting.
    """
    assert fold_ascii("Fuentes Perez, Lizeth J") == "fuentes perez lizeth j"
    assert fold_ascii("O'Brien") == "o brien"
    assert fold_ascii("  spaced   out  ") == "spaced out"


def test_fold_german_drops_everything_that_is_not_a_letter() -> None:
    assert fold_german("Anna-Lena Groß 3") == "annalenagross"

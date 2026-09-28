"""Tests for the answer synthesiser (offline template path and factory)."""

from themis_matcher.config import MatcherSettings
from themis_matcher.synthesis import TemplateSynthesizer, build_synthesizer
from themis_shared.contracts import Evidence, SupervisorMatch


def _match(
    name: str,
    title: str,
    score: float = 0.9,
    source: str = "publication",
    source_scores: dict[str, float] | None = None,
) -> SupervisorMatch:
    scores = source_scores or {source: score}
    return SupervisorMatch(
        supervisor=name,
        department="Dept of X",
        score=max(scores.values()),
        source_scores=scores,
        matched_topics=["nlp"],
        publication_count=3,
        posting_count=1,
        # One item per scored source, as the retriever builds them from the same hits.
        evidence=[
            Evidence(source_type=kind, source_id=f"{kind}:1", title=title) for kind in scores
        ],
    )


def test_template_grounds_in_matches():
    matches = [_match("Prof. A", "Paper One"), _match("Dr. B", "Paper Two")]
    text = TemplateSynthesizer().synthesize("nlp thesis", matches)
    assert "Prof. A" in text
    assert "Dr. B" in text
    assert "Paper One" in text
    # nothing invented: a name not in the matches must not appear
    assert "Prof. C" not in text


def test_template_handles_no_matches():
    text = TemplateSynthesizer().synthesize("obscure topic", [])
    assert "No suitable supervisors" in text


def test_build_synthesizer_offline_by_default():
    assert isinstance(
        build_synthesizer(MatcherSettings(_env_file=None, llm_base_url=None)), TemplateSynthesizer
    )


def test_build_synthesizer_uses_llm_when_endpoint_set():
    from themis_matcher.synthesis.llm import LLMSynthesizer

    synth = build_synthesizer(
        MatcherSettings(
            _env_file=None, llm_base_url="http://localhost:11434/v1", llm_model="llama3.1"
        )
    )
    assert isinstance(synth, LLMSynthesizer)


def test_build_synthesizer_passes_both_min_scores():
    synth = build_synthesizer(
        MatcherSettings(
            _env_file=None,
            llm_base_url="http://localhost:11434/v1",
            llm_model="llama3.1",
            synthesis_min_score_publication=0.7,
            synthesis_min_score_posting=0.5,
        )
    )
    assert synth._min_scores == {"publication": 0.7, "thesis_posting": 0.5}


def test_thresholds_are_per_source_at_the_same_score():
    """Two matches, identical scores, different sources, different verdicts.

    This is the whole reason the threshold was split. Publications and postings
    are not on a common scale -- an out-of-domain query peaks at 0.542 against
    214,756 abstracts but only 0.431 against 695 postings -- so 0.52 is weak for
    a publication and strong for a posting. A single-threshold implementation
    cannot pass this test whatever value it picks.
    """
    from themis_matcher.llm import LLMClient
    from themis_matcher.synthesis.llm import LLMSynthesizer

    synth = LLMSynthesizer(
        LLMClient("http://localhost:1", "none"),
        min_score_publication=0.57,
        min_score_posting=0.48,
    )
    weak_paper = _match("Prof. Paper", "A Paper", score=0.52, source="publication")
    strong_posting = _match("Dr. Posting", "A Topic", score=0.52, source="thesis_posting")

    # The posting clears its threshold, so an answer is attempted rather than
    # degraded -- and the endpoint does not exist, so it falls back to the
    # template. What matters is which candidates reached it.
    text = synth.synthesize("nlp thesis", [weak_paper, strong_posting])
    assert "Dr. Posting" in text
    assert "Prof. Paper" not in text

    # The publication alone is below its own threshold: no candidate survives.
    only_paper = synth.synthesize("nlp thesis", [weak_paper])
    assert "no supervisor" in only_paper.lower()


def test_a_person_found_in_both_sources_passes_if_either_does():
    """The winning source must not veto the other one.

    0.56 is under the publication bar and 0.50 over the posting bar. Thresholding
    only the higher score -- the publication -- dropped this person, although the
    posting alone would have passed: being found twice made them look worse.
    """
    from themis_matcher.llm import LLMClient
    from themis_matcher.synthesis.llm import LLMSynthesizer

    synth = LLMSynthesizer(
        LLMClient("http://localhost:1", "none"),
        min_score_publication=0.57,
        min_score_posting=0.48,
    )
    both = _match(
        "Dr. Both", "A Topic", source_scores={"publication": 0.56, "thesis_posting": 0.50}
    )
    text = synth.synthesize("nlp thesis", [both])
    assert "Dr. Both" in text
    assert "no supervisor" not in text.lower()

    paper_only = _match("Dr. Paper", "A Paper", score=0.56, source="publication")
    assert "no supervisor" in synth.synthesize("nlp thesis", [paper_only]).lower()


def test_llm_synthesizer_flags_weak_matches_without_calling_llm():
    from themis_matcher.llm import LLMClient
    from themis_matcher.synthesis.llm import LLMSynthesizer

    # client is never called: everything is below the threshold
    client = LLMClient("http://localhost:1", "none")
    synth = LLMSynthesizer(client, min_score_publication=0.8)
    weak = _match("Prof. Weak", "Unrelated Paper", score=0.2)
    text = synth.synthesize("history of dentistry in Switzerland", [weak])
    assert "no supervisor" in text.lower()
    assert "strong match" in text.lower()
    assert "Prof. Weak" in text
    assert "long shot" in text


def test_template_says_nothing_about_availability_when_no_posting():
    """Absence of posting data has to render as absence. The old text printed "no
    open position listed", which asserts that a named academic is not taking
    students -- something the retriever cannot know, because its posting query is
    unthresholded and a zero count only means none of theirs reached the top-k."""
    match = _match("Prof. A", "Paper One").model_copy(update={"posting_count": 0})
    text = TemplateSynthesizer().synthesize("nlp thesis", [match]).lower()
    assert "prof. a" in text
    for phrase in ("open position", "no open", "accepting", "available"):
        assert phrase not in text


def test_template_reports_a_posting_when_there_is_one():
    text = TemplateSynthesizer().synthesize("nlp thesis", [_match("Prof. A", "Paper One")])
    assert "1 open thesis posting" in text


def test_llm_candidate_block_omits_missing_postings():
    """The same rule inside the prompt: given "no open position" the model wrote
    "not currently accepting new students", so the line it must not paraphrase is
    simply not written."""
    from themis_matcher.synthesis.llm import _format_candidates

    zero = _match("Prof. A", "Paper One").model_copy(update={"posting_count": 0})
    block = _format_candidates([zero, _match("Dr. B", "Paper Two")])
    assert "no open position" not in block
    assert block.count("open thesis posting") == 1  # only Dr. B has one


def test_the_long_shot_is_closest_to_its_own_threshold():
    """Raw score compares across scales; margin to each source's bar does not.

    The publication is 0.02 under 0.57, the posting 0.01 under 0.48. By raw score
    the publication wins only because publications score higher everywhere.
    """
    from themis_matcher.llm import LLMClient
    from themis_matcher.synthesis.llm import LLMSynthesizer

    synth = LLMSynthesizer(
        LLMClient("http://localhost:1", "none"),
        min_score_publication=0.57,
        min_score_posting=0.48,
    )
    paper = _match("Prof. Paper", "A Paper", score=0.55, source="publication")
    posting = _match("Dr. Posting", "A Topic", score=0.47, source="thesis_posting")

    text = synth.synthesize("nlp thesis", [paper, posting])
    assert "long shot" in text
    assert "The closest is Dr. Posting" in text


def test_work_from_a_source_below_its_bar_is_labelled_weaker():
    """A person who passes on their posting is not vouched for by their papers."""
    from themis_matcher.synthesis.llm import LLMSynthesizer

    class _Recorder:
        def chat(self, system: str, user: str) -> str:
            self.user = user
            return "ok"

    client = _Recorder()
    synth = LLMSynthesizer(client, min_score_publication=0.57, min_score_posting=0.48)
    both = _match(
        "Dr. Both", "Title", source_scores={"publication": 0.56, "thesis_posting": 0.50}
    ).model_copy(
        update={
            "evidence": [
                Evidence(source_type="publication", source_id="z:1", title="Below-Bar Paper"),
                Evidence(source_type="thesis_posting", source_id="p:1", title="Open Topic"),
            ]
        }
    )

    assert synth.synthesize("nlp thesis", [both]) == "ok"
    assert "work: Open Topic" in client.user
    assert "weaker-matching work: Below-Bar Paper" in client.user


def _both_sources_person() -> SupervisorMatch:
    """Passes on the posting (0.50 over 0.48), not on the paper (0.56 under 0.57)."""
    return _match(
        "Dr. Both", "Title", source_scores={"publication": 0.56, "thesis_posting": 0.50}
    ).model_copy(
        update={
            "evidence": [
                Evidence(source_type="publication", source_id="z:1", title="Below-Bar Paper"),
                Evidence(source_type="thesis_posting", source_id="p:1", title="Open Topic"),
            ]
        }
    )


def test_the_template_fallback_still_labels_weaker_work():
    """An LLM outage must not turn a below-bar paper into the reason someone fits.

    The fallback used to be a bare TemplateSynthesizer, which printed every
    evidence item alike -- so the separation the LLM prompt makes held only while
    the endpoint was up, which in a demo is exactly when it matters least.
    """
    from themis_matcher.llm import LLMError
    from themis_matcher.synthesis.llm import LLMSynthesizer

    class _Down:
        def chat(self, system: str, user: str) -> str:
            raise LLMError("endpoint unreachable")

    synth = LLMSynthesizer(_Down(), min_score_publication=0.57, min_score_posting=0.48)
    text = synth.synthesize("nlp thesis", [_both_sources_person()])

    before, _, after = text.partition("Weaker-matching work:")
    assert "Open Topic" in before
    assert "Below-Bar Paper" not in before
    assert "Below-Bar Paper" in after


def test_the_template_without_bars_lists_all_work_alike():
    """The offline default thresholds nothing, so it labels nothing either."""
    text = TemplateSynthesizer().synthesize("nlp thesis", [_both_sources_person()])

    assert "Weaker-matching" not in text
    assert "Below-Bar Paper" in text
    assert "Open Topic" in text

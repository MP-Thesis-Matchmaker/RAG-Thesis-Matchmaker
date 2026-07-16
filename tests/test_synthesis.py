"""Tests for the answer synthesiser (offline template path and factory)."""

from thesis_matchmaker.config import Settings
from thesis_matchmaker.contracts import Evidence, SupervisorMatch
from thesis_matchmaker.synthesis import TemplateSynthesizer, build_synthesizer


def _match(name: str, title: str, score: float = 0.9) -> SupervisorMatch:
    return SupervisorMatch(
        supervisor=name,
        department="Dept of X",
        score=score,
        matched_topics=["nlp"],
        publication_count=3,
        has_open_position=True,
        evidence=[Evidence(source_type="publication", source_id="z:1", title=title)],
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
    assert isinstance(build_synthesizer(Settings(llm_base_url=None)), TemplateSynthesizer)


def test_build_synthesizer_uses_llm_when_endpoint_set():
    from thesis_matchmaker.synthesis.llm import LLMSynthesizer

    synth = build_synthesizer(
        Settings(llm_base_url="http://localhost:11434/v1", llm_model="llama3.1")
    )
    assert isinstance(synth, LLMSynthesizer)


def test_build_synthesizer_passes_min_score():
    synth = build_synthesizer(
        Settings(
            llm_base_url="http://localhost:11434/v1",
            llm_model="llama3.1",
            synthesis_min_score=0.7,
        )
    )
    assert synth._min_score == 0.7


def test_llm_synthesizer_flags_weak_matches_without_calling_llm():
    from thesis_matchmaker.llm import LLMClient
    from thesis_matchmaker.synthesis.llm import LLMSynthesizer

    # client is never called: everything is below the threshold
    client = LLMClient("http://localhost:1", "none")
    synth = LLMSynthesizer(client, min_score=0.8)
    weak = _match("Prof. Weak", "Unrelated Paper", score=0.2)
    text = synth.synthesize("history of dentistry in Switzerland", [weak])
    assert "no supervisor" in text.lower()
    assert "strong match" in text.lower()
    assert "Prof. Weak" in text
    assert "long shot" in text

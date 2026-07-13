# Example run

This page shows the recommendation pipeline end to end: a student query goes in,
the retriever returns candidate supervisors with supporting evidence, and the
synthesis step turns that into a written recommendation.

The output below is real, produced with a local model (`qwen2.5:7b` via Ollama)
over the checked-in sample data. The pipeline also runs fully offline with a
deterministic template, so the shape can be reproduced with no model and no key.
Model output varies between runs, so treat the wording as illustrative.

## Example 1: coherent candidates

**Query:** "I want a master's thesis in NLP on retrieval-augmented generation and
misinformation detection"

**What the retriever handed to synthesis:**

- Prof. A. Müller, Computational Linguistics, score 0.91, open position, 12 publications
  - Retrieval-Augmented Generation for Low-Resource QA
  - MSc thesis: grounding LLM answers with retrieval
- Dr. B. Rossi, Informatics, score 0.78, no open position, 7 publications
  - Dense Retrieval Benchmarks for German Text
- Prof. C. Schmid, Informatics, score 0.64, open position, 5 publications
  - Detecting Misinformation with Weak Supervision

**Final answer (LLM synthesis):**

> I would recommend you consider working with either Prof. A. Müller or Prof. C.
> Schmid for your master's thesis.
>
> Prof. A. Müller is an excellent choice because his research focuses on
> retrieval-augmented generation, which aligns closely with the first part of
> your project. His work, "Retrieval-Augmented Generation for Low-Resource QA,"
> demonstrates his expertise in this area. Additionally, he has a position open.
>
> Prof. C. Schmid is also highly suitable given his focus on misinformation
> detection, the second part of your project. His work, "Detecting Misinformation
> with Weak Supervision," directly relates to your interests. He has an open
> position as well.

The model split the two halves of the query across the two best-matched
supervisors, cited their real work by title, mentioned the open positions, and
dropped the weakest candidate (Rossi). It named only people from the retrieved
list.

**Same input, offline template fallback (no model):**

```
1. Prof. A. Müller (Department of Computational Linguistics)
   Works on retrieval-augmented generation, nlp; 12 related publications; has an open thesis position.
   - Retrieval-Augmented Generation for Low-Resource QA (https://www.zora.uzh.ch/id/eprint/12345)
   - MSc thesis: grounding LLM answers with retrieval (https://www.cl.uzh.ch/theses/rag-grounding)
2. Dr. B. Rossi (Department of Informatics)
   Works on information retrieval; 7 related publications; no open position listed.
   - Dense Retrieval Benchmarks for German Text (https://www.zora.uzh.ch/id/eprint/22890)
3. Prof. C. Schmid (Department of Informatics)
   Works on misinformation detection; 5 related publications; has an open thesis position.
   - Detecting Misinformation with Weak Supervision (https://www.zora.uzh.ch/id/eprint/30011)
```

## Example 2: through the real retriever

This one runs through the real Chroma retriever over the sample data, to show the
actual retrieval code path rather than fixed test data. It uses the offline
`hash-fake` embedder, which ranks randomly instead of semantically, so the
candidate ordering is not meaningful here. With the real `bge-m3` embedder the
ranking is semantic.

**Query:** "a thesis on multilingual embeddings and machine translation"

**What the retriever handed to synthesis:**

- Prof. Anna Meierhans, Computational Linguistics, score 0.44, open position
  - MSc thesis: Evaluating Multilingual Embedding Models on Swiss German
  - Neural Machine Translation for Swiss German Dialects
- Prof. Daniel Keller, Informatics, score 0.34, open position
  - PhD position: Fair Machine Learning in Education
  - Algorithmic Fairness in University Admission Decisions
- Prof. Matthias Huber, Informatics, score 0.24, no open position
  - Reinforcement Learning for Adaptive Energy Management in Smart Grids

**Final answer (LLM synthesis):**

> I would highly recommend working with either Prof. Anna Meierhans or Prof.
> Daniel Keller for your thesis on multilingual embeddings and machine
> translation.
>
> Prof. Anna Meierhans has directly worked on evaluating multilingual embedding
> models and neural machine translation for Swiss German dialects, which align
> closely with your research interests.
>
> Prof. Daniel Keller also focuses on multilingual embeddings and machine
> translation. His background in algorithmic fairness could provide interesting
> interdisciplinary perspectives. Although his current focus is more on fair
> machine learning in education, his expertise in the relevant areas makes him
> another strong candidate.

## Notes

- **Grounding.** The synthesis names only supervisors that are in the retrieved
  list; it does not invent people. This is enforced by the prompt (the model is
  given only the candidates) and is grounded by construction in the offline
  template.
- **Retrieval quality drives answer quality.** In example 2, Meierhans is a
  strong match, but Keller was surfaced only by the random `hash-fake` ranking,
  and the model strains to justify him rather than flag the weak fit. With real
  embeddings the candidate list is clean, and a planned prompt refinement will
  have the model call out weak or partial fits instead of smoothing over them.

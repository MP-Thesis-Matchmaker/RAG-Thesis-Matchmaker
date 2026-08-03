# Ground-truth query set

This is how we measure whether the recommendations are any good. Everyone adds
queries here, then we review them together and check we agree on what a good
answer looks like.

## Adding your queries

Take half an hour, think of queries a real student might send, and add one JSON
object per line to `ground_truth.jsonl`. Aim for a handful each, and please
include at least one where the right answer is that nobody suitable exists.

```json
{"id": "gt-010", "query": "thesis on privacy in machine learning", "relevant_supervisors": ["Surname, Firstname"], "no_match": false, "difficulty": "easy", "degree_level": "master", "department": null, "notes": "why these people", "contributor": "yourname"}
```

| Field | Meaning |
| --- | --- |
| `id` | Unique, `gt-0NN`. Pick a number nobody is using. |
| `query` | What a student would actually type, not a paper title. |
| `relevant_supervisors` | Names as they appear in the data (ZORA uses `Surname, Firstname`). Empty when `no_match` is true. |
| `no_match` | `true` when no UZH supervisor should be recommended. |
| `difficulty` | `easy` for clearly in or out of scope, `hard` for near-domain, thin evidence, or only-ineligible matches. |
| `degree_level` | `bachelor`, `master`, `phd`, or omit. |
| `department` | Only when the query implies one. |
| `notes` | Why this is the answer. This is what we discuss in the review round. |
| `contributor` | Your name, so we know who to ask. |

Two things worth knowing. The supervisor list does not have to be exhaustive:
other relevant people may exist, which is why we measure recall rather than
punishing anything unlisted. And the hard cases and the no-match cases are the
valuable ones. Easy hits mostly confirm the system is plugged in.

## Running it

```
thesis-matchmaker index
thesis-matchmaker evaluate
```

Answerable and no-match queries are reported separately, since ranking quality
and knowing when to stay quiet are different things. `--min-score` mirrors the
synthesis threshold and decides when a query counts as abstained.

Note that the numbers only mean something once the index is built from real
data with real embeddings. With the `hash-fake` embedder the ranking is random,
so the run checks the plumbing, not the quality.

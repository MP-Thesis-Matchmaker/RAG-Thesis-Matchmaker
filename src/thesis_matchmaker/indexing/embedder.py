"""The embedding seam: text in, vectors out.

The model choice is a swappable decision (see CLAUDE.md invariant 3), so
everything downstream depends only on the Embedder protocol. The real
implementation wraps sentence-transformers; the hash-based fake keeps tests
and CI free of the multi-gigabyte model download.
"""

from __future__ import annotations

import hashlib
import re
import struct
from typing import Protocol


class Embedder(Protocol):
    """What indexing and retrieval depend on for embeddings.

    Documents and queries must be embedded by the same model — vectors from
    different models are not comparable.
    """

    @property
    def model_name(self) -> str:
        """Identifier stored in the index manifest to detect model changes."""
        ...

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        """Embed documents for indexing."""
        ...

    def embed_query(self, text: str) -> list[float]:
        """Embed a search query into the same vector space."""
        ...


class HashEmbedder:
    """Deterministic fake: hashes each word and sums the token vectors.

    No real semantics, but texts sharing words end up with similar vectors,
    so tests can assert on ranking and filtering without downloading a model.
    """

    def __init__(self, dim: int = 64) -> None:
        self.dim = dim

    @property
    def model_name(self) -> str:
        return "hash-fake"

    def _token_vector(self, token: str) -> list[float]:
        vector: list[float] = []
        counter = 0
        while len(vector) < self.dim:
            digest = hashlib.sha256(f"{counter}:{token}".encode()).digest()
            for chunk_start in range(0, len(digest) - 3, 4):
                (value,) = struct.unpack_from(">i", digest, chunk_start)
                vector.append(value / 2**31)
                if len(vector) == self.dim:
                    break
            counter += 1
        return vector

    def _embed(self, text: str) -> list[float]:
        tokens = re.findall(r"[a-zäöüß0-9]+", text.lower())
        if not tokens:
            return [0.0] * self.dim
        summed = [0.0] * self.dim
        for token in tokens:
            for i, value in enumerate(self._token_vector(token)):
                summed[i] += value
        norm = sum(v * v for v in summed) ** 0.5 or 1.0
        return [v / norm for v in summed]

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return [self._embed(t) for t in texts]

    def embed_query(self, text: str) -> list[float]:
        return self._embed(text)


class SentenceTransformerEmbedder:
    """Real embedder over a local sentence-transformers model (default BGE-M3).

    The model loads lazily on first use, so importing this module (and
    constructing the object) stays cheap. Requires the `embeddings` extra.
    """

    def __init__(self, model_name: str) -> None:
        self._model_name = model_name
        self._model = None

    @property
    def model_name(self) -> str:
        return self._model_name

    def _load(self):
        if self._model is None:
            try:
                from sentence_transformers import SentenceTransformer
            except ImportError as exc:  # pragma: no cover - environment-dependent
                raise RuntimeError(
                    "sentence-transformers is not installed; "
                    "install the 'embeddings' extra: uv sync --extra embeddings"
                ) from exc
            self._model = SentenceTransformer(self._model_name)
        return self._model

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        model = self._load()
        return model.encode(texts, normalize_embeddings=True).tolist()

    def embed_query(self, text: str) -> list[float]:
        return self.embed_documents([text])[0]

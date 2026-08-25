"""The embedding seam: text in, vectors out.

The model choice is a swappable decision (see CLAUDE.md invariant 3), so
everything downstream depends only on the Embedder protocol. The real
implementation wraps sentence-transformers; the hash-based fake keeps tests
and CI free of the multi-gigabyte model download.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import struct
from pathlib import Path
from typing import Protocol

logger = logging.getLogger(__name__)

# Width of the vectors the index stores. Fixed project-wide because pgvector
# needs the dimension in the DDL (`vector(n)`) and HNSW cannot index a column of
# unspecified width. 1024 is BAAI/bge-m3's output size. Switching to a model of
# a different width is therefore a schema migration, not a config change.
EMBEDDING_DIM = 1024


class Embedder(Protocol):
    """What indexing and retrieval depend on for embeddings.

    Documents and queries must be embedded by the same model — vectors from
    different models are not comparable.
    """

    @property
    def model_name(self) -> str:
        """Identifier stored in the index manifest to detect model changes."""
        ...

    @property
    def dimensions(self) -> int:
        """Vector width; must match the store's `vector(n)` column."""
        ...

    @property
    def max_seq_length(self) -> int | None:
        """Token cap applied before embedding; None if there is no limit.

        Recorded in the index manifest and guarded there, for the same reason
        `model_name` is: changing it changes every vector while leaving each
        document's content hash untouched, so nothing would be re-embedded and the
        index would quietly end up holding two incompatible generations of vector.
        """
        ...

    @property
    def last_truncated(self) -> int:
        """Documents in the most recent `embed_documents` call that hit the cap."""
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

    def __init__(self, dim: int = EMBEDDING_DIM) -> None:
        self.dim = dim

    @property
    def model_name(self) -> str:
        return "hash-fake"

    @property
    def dimensions(self) -> int:
        return self.dim

    @property
    def max_seq_length(self) -> int | None:
        # Hashes every token it is given, so there is no window to fall out of.
        return None

    @property
    def last_truncated(self) -> int:
        return 0

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


_CGROUP_ROOT = Path("/sys/fs/cgroup")

# Read at OpenMP initialisation rather than per call, which is why they have to be
# set before torch is first imported. torch.set_num_threads() covers the case
# where that already happened.
_THREAD_ENV_VARS = ("OMP_NUM_THREADS", "MKL_NUM_THREADS")


def _cores_from_quota(quota: str, period: str) -> int | None:
    """Whole cores implied by a CFS quota/period pair, or None if unlimited."""
    # cgroup v2 spells unlimited "max"; v1 spells it "-1".
    if quota in {"max", "-1"}:
        return None
    try:
        quota_us, period_us = int(quota), int(period)
    except ValueError:
        return None
    if quota_us <= 0 or period_us <= 0:
        return None
    # Floor at one: a fractional allowance (cpu: 500m) is still one thread's
    # worth of work, and 0 threads would mean "use every core" to torch.
    return max(1, quota_us // period_us)


def cpu_limit(cgroup_root: Path = _CGROUP_ROOT) -> int | None:
    """Cores this process may use per its cgroup CPU quota; None if unlimited.

    A Kubernetes `resources.limits.cpu` is a CFS bandwidth quota, **not** a core
    mask, so nothing in the standard library reports it: `os.cpu_count()` and
    `os.sched_getaffinity()` both return the node's core count. Measured in the
    indexer image under `--cpus 2`, both said 18 while the real allowance was 2 --
    so torch started 18 threads to contend over two cores' worth of runtime, which
    cost a clean 2x on a full index run. The quota is only discoverable here.
    """
    try:
        fields = (cgroup_root / "cpu.max").read_text().split()
    except OSError:
        pass
    else:
        # v2: "<quota> <period>", e.g. "200000 100000" for two cores.
        if len(fields) >= 2:
            return _cores_from_quota(fields[0], fields[1])
        return None

    try:  # cgroup v1, still what some managed clusters expose
        quota = (cgroup_root / "cpu" / "cpu.cfs_quota_us").read_text().strip()
        period = (cgroup_root / "cpu" / "cpu.cfs_period_us").read_text().strip()
    except OSError:
        return None
    return _cores_from_quota(quota, period)


def _limit_thread_pools() -> int | None:
    """Pin the BLAS/OpenMP pools to the cgroup CPU quota. Returns the limit used.

    Never overrides a value the operator set: an explicit OMP_NUM_THREADS in the
    environment is a deliberate choice and outranks anything inferred here.
    """
    limit = cpu_limit()
    if limit is None:
        return None
    for var in _THREAD_ENV_VARS:
        os.environ.setdefault(var, str(limit))
    return limit


class SentenceTransformerEmbedder:
    """Real embedder over a local sentence-transformers model (default BGE-M3).

    The model loads lazily on first use, so importing this module (and
    constructing the object) stays cheap. Requires the `embeddings` extra.
    """

    def __init__(
        self,
        model_name: str,
        max_seq_length: int = 1024,
        batch_size: int = 16,
        device: str | None = None,
    ) -> None:
        self._model_name = model_name
        self._max_seq_length = max_seq_length
        self._batch_size = batch_size
        self._device = device
        self._model = None
        self._last_truncated = 0

    @property
    def model_name(self) -> str:
        return self._model_name

    @property
    def max_seq_length(self) -> int | None:
        return self._max_seq_length

    @property
    def last_truncated(self) -> int:
        return self._last_truncated

    @property
    def dimensions(self) -> int:
        # get_embedding_dimension replaced get_sentence_embedding_dimension, which
        # warned on every call. It is declared `-> int | None`, and None cannot be
        # reconciled with the store's vector(1024) column, so say so plainly rather
        # than letting int(None) raise a bare TypeError.
        width = self._load().get_embedding_dimension()
        if width is None:
            raise RuntimeError(
                f"{self._model_name!r} does not report an embedding width, so it "
                f"cannot be checked against the index's vector({EMBEDDING_DIM}) column"
            )
        return int(width)

    def _load(self):
        if self._model is None:
            # Before the import, not after: OpenMP reads OMP_NUM_THREADS when its
            # runtime initialises, and that happens on the first torch import,
            # which this line triggers. Safe only because nothing in this package
            # imports torch at module level.
            limit = _limit_thread_pools()
            try:
                from sentence_transformers import SentenceTransformer
            except ImportError as exc:  # pragma: no cover - environment-dependent
                raise RuntimeError(
                    "sentence-transformers is not installed; "
                    "install the 'embeddings' extra: uv sync --extra embeddings"
                ) from exc
            if limit is not None:  # pragma: no cover - needs torch installed
                import torch

                torch.set_num_threads(limit)
                logger.info(
                    "cgroup CPU quota is %d core(s); torch threads set to %d",
                    limit,
                    torch.get_num_threads(),
                )
            # device=None is this constructor's own "auto-detect" (it calls
            # get_device_name() itself), so passing it through unconditionally
            # keeps today's behaviour when nothing is configured. Configuring it
            # matters on a Mac: auto-detect picks mps, and mps aborts the process
            # rather than raising when the machine is short of memory.
            if self._device:
                logger.info("loading %s on device %s", self._model_name, self._device)
            model = SentenceTransformer(self._model_name, device=self._device)
            # bge-m3 reports 8192 here. Left alone, the attention buffer is
            # batch x heads x seq^2, and encode() batches longest-first, so the one
            # 6822-token abstract in the corpus sized the very first batch at
            # 88.77 GiB and killed the run. Assigning the property propagates to
            # the Transformer module (model[0]), which is what actually truncates.
            logger.info(
                "%s reports max_seq_length %s; capping at %d",
                self._model_name,
                model.max_seq_length,
                self._max_seq_length,
            )
            model.max_seq_length = self._max_seq_length
            self._model = model
        return self._model

    def _count_truncated(self, model, texts: list[str]) -> int:
        """How many of `texts` reach the cap, and so lose their tail.

        One extra pass over the (Rust) tokenizer, which costs well under a percent
        of the forward pass it precedes. Slightly conservative: a document landing
        exactly on the cap is counted, though nothing was actually dropped.
        """
        encoded = model.tokenizer(texts, truncation=True, max_length=self._max_seq_length)
        return sum(1 for ids in encoded["input_ids"] if len(ids) >= self._max_seq_length)

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        model = self._load()
        self._last_truncated = self._count_truncated(model, texts)
        # normalize_embeddings is redundant -- the model's own modules.json ends in
        # a 2_Normalize stage, and measured output norms are already 1.0 -- but it
        # is kept as the explicit statement that cosine distance in the store
        # assumes unit vectors. Harmless, and it stops the assumption being silent.
        return model.encode(
            texts,
            batch_size=self._batch_size,
            normalize_embeddings=True,
        ).tolist()

    def embed_query(self, text: str) -> list[float]:
        return self.embed_documents([text])[0]

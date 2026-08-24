"""The raw-response cache: one JSONL file per harvest step, under data/raw/.

Keeps ingestion reproducible without re-hitting ZORA. A full publication harvest is
~215k records and roughly two hours of requests, and the dump is written *before*
anything reaches Postgres, so a run that fetched successfully but failed on the
write does not have to fetch again (`harvest.py --from-dump`).

Its own module rather than part of `harvest.py` because `entities.py` writes dumps
too, and `harvest.py` imports `entities.py` -- sharing it the other way round would
be a circular import.

What lands here is *normalized* records, not raw API responses. That is a real
limitation: a dump can only repopulate fields the normaliser already extracted at
the time it was written, which is why the fields added on 2026-08-24 need a fresh
API harvest rather than a replay of an older dump.
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Iterator
from datetime import UTC, datetime

from . import config

logger = logging.getLogger(__name__)


def write_raw_dump(records: list[dict], kind: str) -> str:
    """Write one JSONL dump and return its path.

    @param kind: what this dump holds -- a publication harvest mode
                  ("full"/"incremental") or an entity kind
                  ("persons"/"orgunits"). It becomes part of the filename, so a
                  replay can tell the kinds apart.
    """
    os.makedirs(config.RAW_DIR, exist_ok=True)
    ts = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    dump_path = os.path.join(config.RAW_DIR, f"{ts}_{kind}.jsonl")
    with open(dump_path, "w", encoding="utf-8") as f:
        for record in records:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    return dump_path


def read_raw_dump(path: str) -> Iterator[dict]:
    """Yield the normalized records of a dump written by an earlier run.

    @raise RuntimeError: if the file cannot be read, or a line is not valid
        JSON. Both are operator mistakes (wrong path, truncated file) rather
        than bugs, so they surface as the clean one-line failure `main` prints
        instead of a traceback.
    """
    try:
        with open(path, encoding="utf-8") as f:
            for line_no, line in enumerate(f, start=1):
                line = line.strip()
                if not line:
                    continue
                try:
                    yield json.loads(line)
                except json.JSONDecodeError as exc:
                    raise RuntimeError(f"{path}: line {line_no} is not valid JSON: {exc}") from exc
    except OSError as exc:
        raise RuntimeError(f"--from-dump {path} could not be read: {exc}") from exc

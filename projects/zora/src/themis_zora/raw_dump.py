"""The raw-response cache: one JSONL file per harvest step, under data/raw/.

**Opt-in** (`ZORA_WRITE_RAW_DUMP=true`), off by default and off in the cluster. It
exists so a local run can be replayed without re-hitting ZORA (`harvest --from-dump`):
a full publication harvest is ~215k records and roughly two hours of requests. In the
cluster it would land on an emptyDir that is discarded with the pod, so it bought
nothing there but scratch storage; Postgres is the record.

The dump is streamed record by record, alongside the batched database writes, and
lands under a `.partial` name that is renamed to its final one only once the step
finished fetching. That rename is what keeps a crashed run from leaving a truncated
dump that looks complete -- and a truncated `_full` dump replayed in full mode would
prune every publication missing from it.

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
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import TextIO

from . import config

logger = logging.getLogger(__name__)

# The four kinds of dump a run can write. The publication modes double as
# `--mode` values; the entity kinds are what `entities.py` passes. Kept here
# rather than in entities.py because this module is what parses them back out of
# a filename, and entities.py imports this one.
PUBLICATION_KINDS = ("full", "incremental")
PERSONS = "persons"
ORG_UNITS = "orgunits"
KINDS = (*PUBLICATION_KINDS, PERSONS, ORG_UNITS)

# What a dump is called until the step that writes it has finished fetching.
PARTIAL_SUFFIX = ".partial"


def enabled() -> bool:
    """Whether this run writes dumps at all (`ZORA_WRITE_RAW_DUMP`)."""
    return config.get_settings().write_raw_dump


def is_partial(path: str) -> bool:
    """Whether `path` is a dump whose run never finished fetching."""
    return path.endswith(PARTIAL_SUFFIX)


class RawDumpWriter:
    """One open dump, written a record at a time. Obtained from `open_raw_dump`."""

    def __init__(self, path: str, file: TextIO) -> None:
        # The final name, which only exists once the `with` block exited cleanly.
        self.path = path
        self._file = file

    def write(self, record: dict) -> None:
        self._file.write(json.dumps(record, ensure_ascii=False) + "\n")


@contextmanager
def open_raw_dump(kind: str) -> Iterator[RawDumpWriter]:
    """Stream one JSONL dump, renamed to its final name only on a clean exit.

    @param kind: what this dump holds -- a publication harvest mode
                  ("full"/"incremental") or an entity kind
                  ("persons"/"orgunits"). It becomes part of the filename, so a
                  replay can tell the kinds apart.

    An exception inside the block leaves the file behind as `<name>.partial`,
    which `dump_kind` cannot route and the CLI refuses to replay.
    """
    # Resolved per call, not at import: ZORA_DATA_DIR used to be read once when
    # this module was first imported, which is why three test modules had to
    # patch the constant by attribute instead of setting the variable.
    raw_dir = config.get_settings().raw_dir
    os.makedirs(raw_dir, exist_ok=True)
    ts = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    dump_path = os.path.join(raw_dir, f"{ts}_{kind}.jsonl")
    partial_path = dump_path + PARTIAL_SUFFIX
    with open(partial_path, "w", encoding="utf-8") as f:
        yield RawDumpWriter(dump_path, f)
    os.replace(partial_path, dump_path)
    logger.info("Wrote raw dump %s", dump_path)


def write_raw_dump(records: Iterable[dict], kind: str) -> str:
    """Write one JSONL dump in one go and return its path. See `open_raw_dump`."""
    with open_raw_dump(kind) as dump:
        for record in records:
            dump.write(record)
    return dump.path


def dump_kind(path: str) -> str:
    """Which harvest step a dump belongs to, read off its filename.

    `write_raw_dump` puts the kind in the name for exactly this purpose, so a
    replay can route `<ts>_persons.jsonl` to the person step and `<ts>_full.jsonl`
    to the publications. Nothing about a dump's *contents* says which it is --
    both are just JSONL objects -- so the name is the only signal available.

    That makes a renamed or hand-copied dump unroutable, which is why this raises
    rather than guessing: feeding a person dump to the publication validator would
    fail anyway, several steps later and with a far worse message. `--dump-kind`
    is the way out.

    @raise RuntimeError: if the filename carries no recognisable kind.
    """
    if is_partial(path):
        raise RuntimeError(
            f"{path} is an incomplete dump: the run that wrote it never finished "
            "fetching, and replaying it would treat a truncated snapshot as a whole one."
        )
    stem = os.path.basename(path).removesuffix(".jsonl")
    for kind in KINDS:
        if stem.endswith(f"_{kind}"):
            return kind
    raise RuntimeError(
        f"cannot tell what {path} holds: its name ends in neither "
        f"{', '.join('_' + kind for kind in KINDS)}. Pass --dump-kind to say which "
        "harvest step should replay it."
    )


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

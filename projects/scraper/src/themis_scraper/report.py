"""Run report + notification.

Every run writes output/runs/<timestamp>.json and prints a table of flagged
(quarantined) sources with reasons. `notify(summary)` is the single, swappable
hook — it just prints today; later it can email or POST a webhook without any
caller change. A run with flags exits non-zero so a scheduler can alert.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

from . import validate
from .config import get_settings


def new_report() -> dict:
    return {"started_at": datetime.now(UTC).isoformat(), "sources": [], "summary": {}}


def add_source(
    report: dict, result: validate.Result, *, action: str, diff: dict | None = None
) -> None:
    entry = {
        "source_id": result.source_id,
        "page_type": result.page_type,
        "status": result.status,
        "action": action,
        "record_count": result.record_count,
        "reasons": result.reasons,
    }
    if diff:
        entry["diff"] = diff
    report["sources"].append(entry)


def finalize(report: dict) -> dict:
    counts: dict[str, int] = {}
    for s in report["sources"]:
        counts[s["status"]] = counts.get(s["status"], 0) + 1
    report["summary"] = {
        "total": len(report["sources"]),
        "by_status": counts,
        "flagged": sum(1 for s in report["sources"] if s["status"] in validate.FLAGGED),
    }
    report["finished_at"] = datetime.now(UTC).isoformat()
    return report


def write(report: dict) -> str:
    runs = get_settings().runs_dir
    runs.mkdir(parents=True, exist_ok=True)
    ts = report["started_at"].replace(":", "").replace("-", "").split(".")[0]
    path = runs / f"{ts}.json"
    path.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return str(path)


def print_table(report: dict) -> None:
    flagged = [s for s in report["sources"] if s["status"] in validate.FLAGGED]
    sm = report["summary"]
    print("\n" + "=" * 68)
    print(
        f"RUN SUMMARY  total={sm['total']}  flagged={sm['flagged']}  "
        + "  ".join(f"{k}={v}" for k, v in sm["by_status"].items())
    )
    if not flagged:
        print("all clear — no quarantined sources.")
        return
    print("-" * 68)
    print(f"{'source_id':14} {'type':8} {'status':14} reason")
    print("-" * 68)
    for s in flagged:
        reason = (s["reasons"][0] if s["reasons"] else "")[:34]
        if s.get("diff"):
            d = s["diff"]
            reason = f"+{d['added']}/-{d['removed']}/~{d['modified']}  {reason}"
        print(f"{s['source_id']:14} {s['page_type']:8} {s['status']:14} {reason}")
    print("=" * 68)


def notify(summary: str) -> None:
    """The one notification hook. Default: print. Swap the body for email /
    webhook later; callers never change."""
    print(f"\n[notify] {summary}")

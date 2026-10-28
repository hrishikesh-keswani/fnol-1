"""One page for a human to accept or reject pending drafts.

Each row shows the draft stored on the review itself, because that exact
object is what Accept approves. The output store only adds the analyst's
status for the same draft hash, and the trace adds Gemma's paragraph for the
same input revision. Gemma's paragraph is reference text and is never part of
the approved draft.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from fnol_foundation.db import open_db, using_postgres
from fnol_foundation.stores import state_dir

PAGE = Path(__file__).with_name("review_page.html").read_text(encoding="utf-8")


def _text(value) -> str | None:
    return value if isinstance(value, str) and value.strip() else None


def _draft_body(raw) -> dict:
    try:
        body = json.loads(raw) if isinstance(raw, str) else None
    except ValueError:
        return {}
    return body if isinstance(body, dict) else {}


def _placeholders(values) -> str:
    return ",".join("?" for _ in values)


def _store_rows(kind: str, sql: str, params: list) -> list:
    """Run one read against the output or trace store. A failure is logged, not raised."""
    if not params:
        return []
    path = state_dir() / f"{kind}.sqlite"
    if not using_postgres() and not path.exists():
        return []
    try:
        with open_db(path, kind=kind) as db:
            return db.execute(sql, params).fetchall()
    except Exception as exc:
        print(f"review queue: {kind} store unavailable: {exc}", file=sys.stderr, flush=True)
        return []


def _stored_drafts(hashes: list[str]) -> dict[str, dict]:
    """Newest output-store draft for each draft hash."""
    rows = _store_rows(
        "output",
        f"""SELECT draft_hash, case_id, input_revision, status, outcome FROM drafts
            WHERE draft_hash IN ({_placeholders(hashes)})
            ORDER BY created_at, rowid""",
        hashes,
    )
    return {row["draft_hash"]: dict(row) for row in rows}


def _gemma_paragraphs(case_ids: list[str]) -> tuple[dict[tuple[str, int], str], dict[str, str], dict[str, str]]:
    """Gemma paragraphs from the trace.

    A paragraph recorded with an input revision is kept for that revision.
    A paragraph recorded without one is kept for the case. The newest
    paragraph for the case is the fallback when the review's revision is unknown.
    """
    rows = _store_rows(
        "trace",
        f"""SELECT case_id, input_revision, shadow_narrative FROM trace_events
            WHERE case_id IN ({_placeholders(case_ids)}) AND shadow_narrative IS NOT NULL
            ORDER BY started_at, rowid""",
        case_ids,
    )
    by_revision: dict[tuple[str, int], str] = {}
    unscoped: dict[str, str] = {}
    newest: dict[str, str] = {}
    for row in rows:
        text = _text(row["shadow_narrative"])
        case_id = row["case_id"]
        if text is None or not case_id:
            continue
        newest[case_id] = text
        revision = row["input_revision"]
        if revision is None:
            unscoped[case_id] = text
        else:
            by_revision[(case_id, int(revision))] = text
    return by_revision, unscoped, newest


def _gemma_for(review_case: str, revision, paragraphs) -> str | None:
    by_revision, unscoped, newest = paragraphs
    if revision is not None:
        matched = by_revision.get((review_case, int(revision)))
        if matched is not None:
            return matched
    # A summary written without a revision still belongs to the case.
    # A different revision's summary does not.
    if review_case in unscoped:
        return unscoped[review_case]
    if revision is None:
        return newest.get(review_case)
    return None


def queue_rows(db) -> list[dict]:
    """One row per case: the newest review, with its draft and Gemma paragraph.

    Older duplicate reviews for the same case_id are omitted so the queue
    counts cases, not leftover tickets from resubmits.
    """
    reviews = db.execute(
        "SELECT id, case_id, draft, hash, status, reviewer FROM reviews ORDER BY rowid DESC"
    ).fetchall()
    latest = []
    seen = set()
    for review in reviews:
        case_id = review["case_id"]
        if not case_id or case_id in seen:
            continue
        seen.add(case_id)
        latest.append(review)
    stored = _stored_drafts(sorted({r["hash"] for r in latest if r["hash"]}))
    gemma = _gemma_paragraphs(sorted(seen))
    rows = []
    for review in latest:
        body = _draft_body(review["draft"])
        record = stored.get(review["hash"]) or {}
        revision = record.get("input_revision")
        rows.append(
            {
                "review_id": review["id"],
                "case_id": review["case_id"],
                "status": review["status"],
                "reviewer": review["reviewer"],
                "draft_hash": review["hash"],
                "classification": _text(body.get("classification")),
                "routing": _text(body.get("routing")),
                "narrative": _text(body.get("narrative")),
                "evidence_summary": _text(body.get("evidence_summary")),
                "draft_status": record.get("status"),
                "outcome": record.get("outcome"),
                "shadow_narrative": _gemma_for(review["case_id"], revision, gemma),
            }
        )
    return rows

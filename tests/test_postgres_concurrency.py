"""Concurrency and immutability for the state stores.

These run on SQLite by default and on PostgreSQL when FNOL_DATABASE_URL is set.
The Postgres job in CI sets that variable and FNOL_PG_NAMESPACE=isolate.
"""

from __future__ import annotations

import json
import os
import socket
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from fnol_foundation.canonical import digest
from fnol_foundation.migrate import apply_migrations
from fnol_foundation.stores import ImmutableRecordError, Stores, TRACE_COLUMNS
from tools.runtime import canonical, connect, create_server, digest as mock_digest

ROOT = Path(__file__).resolve().parents[1]


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _post(port: int, path: str, body: dict, headers: dict | None = None):
    raw = json.dumps(body).encode()
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}",
        data=raw,
        method="POST",
        headers={"Content-Type": "application/json", **(headers or {})},
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, json.loads(response.read().decode())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode())


class ConcurrencyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.stores = Stores(self.tmp.name)

    def tearDown(self):
        self.stores.drop()
        self.tmp.cleanup()

    def test_concurrent_revisions_have_no_gaps(self):
        errors: list[BaseException] = []
        barrier = threading.Barrier(8)

        def worker():
            try:
                barrier.wait(timeout=5)
                self.stores.input.append_revision("AUTO-CONC", {"loss_type": "collision"})
            except BaseException as exc:
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=20)
        self.assertEqual(errors, [])
        with self.stores.input.connect() as db:
            rows = db.execute(
                "SELECT revision FROM input_revisions WHERE case_id = ? ORDER BY revision",
                ("AUTO-CONC",),
            ).fetchall()
        self.assertEqual([row["revision"] for row in rows], list(range(1, 9)))

    def test_concurrent_trace_events_stay_chained(self):
        errors: list[BaseException] = []
        barrier = threading.Barrier(6)

        def worker(index: int):
            try:
                barrier.wait(timeout=5)
                self.stores.trace.append_event(
                    trace_id="trace-conc",
                    span_id=f"span-{index}",
                    step="1",
                    actor="intake",
                    status="ok",
                    event="started",
                )
            except BaseException as exc:
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(index,)) for index in range(6)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=20)
        self.assertEqual(errors, [])
        with self.stores.trace.connect() as db:
            rows = [dict(row) for row in db.execute("SELECT * FROM trace_events ORDER BY rowid")]
        self.assertEqual(len(rows), 6)
        self.assertIsNone(rows[0]["prev_hash"])
        for previous, current in zip(rows, rows[1:]):
            self.assertEqual(current["prev_hash"], previous["event_hash"])
            body = {name: current[name] for name in TRACE_COLUMNS if name not in {"event_hash", "prev_hash"}}
            self.assertEqual(current["event_hash"], digest({"prev_hash": current["prev_hash"], "event": body}))

    def test_conflicting_payload_loses_one_writer(self):
        revision = self.stores.input.append_revision("AUTO-CONC", {"loss_type": "collision"})
        barrier = threading.Barrier(2)
        results: list[tuple[str, str | None]] = []
        lock = threading.Lock()

        def worker(body: dict):
            try:
                barrier.wait(timeout=5)
                sha = self.stores.input.put_tool_payload("AUTO-CONC", "policy", body, revision.revision)
                with lock:
                    results.append(("ok", sha))
            except ImmutableRecordError:
                with lock:
                    results.append(("immutable", None))
            except BaseException as exc:
                with lock:
                    results.append((f"error:{exc}", None))

        threads = [
            threading.Thread(target=worker, args=({"source_id": "A", "version": "1"},)),
            threading.Thread(target=worker, args=({"source_id": "B", "version": "2"},)),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=20)
        kinds = sorted(kind for kind, _sha in results)
        self.assertEqual(kinds, ["immutable", "ok"])
        stored = self.stores.input.get_tool_payload("AUTO-CONC", "policy", revision.revision)
        self.assertIsNotNone(stored)

    def test_same_payload_is_idempotent_under_contention(self):
        revision = self.stores.input.append_revision("AUTO-CONC", {"loss_type": "theft"})
        body = {"source_id": "AUTO-CONC-POLICY", "version": "2"}
        barrier = threading.Barrier(4)
        hashes: list[str] = []
        errors: list[BaseException] = []
        lock = threading.Lock()

        def worker():
            try:
                barrier.wait(timeout=5)
                sha = self.stores.input.put_tool_payload("AUTO-CONC", "policy", body, revision.revision)
                with lock:
                    hashes.append(sha)
            except BaseException as exc:
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=20)
        self.assertEqual(errors, [])
        self.assertEqual(set(hashes), {digest(body)})

    def test_concurrent_record_filing_keeps_one_logical_record(self):
        state = Path(self.tmp.name) / "mock.sqlite"
        port = _free_port()
        server = create_server(ROOT / "data" / "dev", state, host="127.0.0.1", port=port)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            draft = {"case_id": "AUTO-D01", "summary": "concurrent filing"}
            status, created = _post(port, "/v1/reviews", {"case_id": "AUTO-D01", "draft": draft})
            self.assertEqual(status, 201)
            self.assertEqual(created["draft_hash"], mock_digest(draft))
            with connect(state) as db:
                db.execute(
                    "UPDATE reviews SET status = ?, reviewer = ? WHERE id = ?",
                    ("approved", "reviewer-name", created["review_id"]),
                )
            barrier = threading.Barrier(2)
            outcomes: list[tuple[str, tuple[int, dict]]] = []
            lock = threading.Lock()

            def file_record(key: str):
                barrier.wait(timeout=5)
                result = _post(
                    port,
                    "/v1/records",
                    {"case_id": "AUTO-D01", "draft": draft, "review_id": created["review_id"]},
                    {"Idempotency-Key": key},
                )
                with lock:
                    outcomes.append((key, result))

            workers = [
                threading.Thread(target=file_record, args=("key-a",)),
                threading.Thread(target=file_record, args=("key-b",)),
            ]
            for worker in workers:
                worker.start()
            for worker in workers:
                worker.join(timeout=20)
            codes = sorted(code for _key, (code, _body) in outcomes)
            self.assertEqual(codes, [201, 409], outcomes)
            conflict = next(body for _key, (code, body) in outcomes if code == 409)
            self.assertEqual(conflict["error"], "duplicate_case")
            winner = next(key for key, (code, _body) in outcomes if code == 201)
            replay_status, replay = _post(
                port,
                "/v1/records",
                {"case_id": "AUTO-D01", "draft": draft, "review_id": created["review_id"]},
                {"Idempotency-Key": winner},
            )
            self.assertEqual(replay_status, 200)
            self.assertEqual(replay["record_id"], "auto-loss-D01")
            with connect(state) as db:
                records = db.execute("SELECT id, case_id FROM records").fetchall()
            self.assertEqual([(row["id"], row["case_id"]) for row in records], [("auto-loss-D01", "AUTO-D01")])
        finally:
            server.shutdown()
            server.server_close()


class MigrationIdempotenceTests(unittest.TestCase):
    def test_second_apply_is_a_no_op(self):
        if not os.environ.get("FNOL_DATABASE_URL"):
            self.skipTest("PostgreSQL URL is not configured")
        with tempfile.TemporaryDirectory() as tmp:
            from fnol_foundation.db import namespace_suffix

            suffix = namespace_suffix(Path(tmp))
            apply_migrations(suffix)
            self.assertEqual(apply_migrations(suffix), [])
            # Touch the schema through a store so the namespace is real.
            Stores(tmp).input.append_revision("AUTO-MIG", {"loss_type": "glass"})
            self.assertEqual(apply_migrations(suffix), [])


class CanonicalLockTests(unittest.TestCase):
    def test_mock_digest_matches_foundation(self):
        draft = {"case_id": "AUTO-D01", "summary": "concurrent filing"}
        self.assertEqual(mock_digest(draft), digest(draft))
        self.assertEqual(canonical(draft), json.dumps(draft, sort_keys=True, separators=(",", ":")))


if __name__ == "__main__":
    unittest.main()

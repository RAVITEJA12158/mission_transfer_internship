"""Persistent upload queue + background worker.

States: PENDING -> UPLOADING -> VERIFIED | RETRY_PENDING (-> UPLOADING ...) | FAILED
A row is VERIFIED only after the backend confirms storage. Unfinished rows survive reboot.
"""
import json
import logging
import os
import random
import threading
import time

from api_client import PermanentError, TransientError

log = logging.getLogger("queue")

PENDING, UPLOADING, RETRY, VERIFIED, FAILED = "PENDING", "UPLOADING", "RETRY_PENDING", "VERIFIED", "FAILED"

class TransferQueue:
    def __init__(self, db, keep_local_logs=False):
        self.db = db
        self.keep_local_logs = keep_local_logs

        self.db.execute("UPDATE transfer_queue SET state=?, updated_at=? WHERE state=?", (RETRY, time.time(), UPLOADING))

    def enqueue(self, kind, key, payload=None, file_path=None):
        """Returns False if an item with this idempotency key already exists (no duplicate rows)."""
        now = time.time()
        cur = self.db.execute(
            "INSERT OR IGNORE INTO transfer_queue(kind, idempotency_key, payload, file_path, state, created_at, updated_at) "
            "VALUES(?,?,?,?,?,?,?)",
            (kind, key, json.dumps(payload, allow_nan=False) if payload is not None else None, file_path, PENDING, now, now))
        return cur.rowcount > 0

    def claim_next(self):
        with self.db.lock:
            row = self.db.one(
                "SELECT * FROM transfer_queue WHERE state IN (?,?) AND next_attempt_at<=? ORDER BY id LIMIT 1",
                (PENDING, RETRY, time.time()))
            if row:
                self.db.execute("UPDATE transfer_queue SET state=?, updated_at=? WHERE id=?", (UPLOADING, time.time(), row["id"]))
            return row

    def mark_verified(self, row):
        self.db.execute("UPDATE transfer_queue SET state=?, last_error=NULL, payload=NULL, updated_at=? WHERE id=?",
                        (VERIFIED, time.time(), row["id"]))
        if row["kind"] == "log" and row["file_path"] and not self.keep_local_logs:
            try:
                os.remove(row["file_path"])
            except FileNotFoundError:
                pass
            except OSError as exc:
                log.warning("could not delete verified local log %s: %s", row["file_path"], exc)

    def mark_retry(self, row, error):
        attempts = row["attempts"] + 1
        delay = min(120.0, 2.0 ** min(attempts, 7)) * random.uniform(0.7, 1.3)
        self.db.execute(
            "UPDATE transfer_queue SET state=?, attempts=?, next_attempt_at=?, last_error=?, updated_at=? WHERE id=?",
            (RETRY, attempts, time.time() + delay, error[:500], time.time(), row["id"]))
        return attempts

    def mark_failed(self, row, error):
        self.db.execute("UPDATE transfer_queue SET state=?, attempts=attempts+1, last_error=?, updated_at=? WHERE id=?",
                        (FAILED, error[:500], time.time(), row["id"]))

    def requeue_failed(self):
        cur = self.db.execute("UPDATE transfer_queue SET state=?, next_attempt_at=0, updated_at=? WHERE state=?",
                              (RETRY, time.time(), FAILED))
        return cur.rowcount

    def purge_verified(self, older_than_s=86400):
        self.db.execute("DELETE FROM transfer_queue WHERE state=? AND updated_at<?", (VERIFIED, time.time() - older_than_s))

    def counts(self):
        return {r["state"]: r["n"] for r in self.db.query("SELECT state, COUNT(*) n FROM transfer_queue GROUP BY state")}

class UploadWorker(threading.Thread):
    def __init__(self, queue, api, stop_event):
        super().__init__(name="upload-worker", daemon=True)
        self.queue, self.api, self.stop = queue, api, stop_event

    def _dispatch(self, row):
        kind = row["kind"]
        payload = json.loads(row["payload"] or "{}")
        if kind == "mission":
            self.api.post_mission(payload)
        elif kind == "mission_status":
            self.api.post_mission_status(payload)
        elif kind == "telemetry":
            self.api.post_telemetry(payload)
        elif kind == "log":
            resp = self.api.upload_log(row["file_path"], payload)
            ok = (resp.get("status") == "verified"
                  and str(resp.get("checksum_sha256", "")).lower() == payload["checksum_sha256"]
                  and int(resp.get("file_size_bytes", -1)) == payload["file_size_bytes"])
            if not ok:
                raise TransientError(f"backend verification mismatch: {resp}")
        else:
            raise PermanentError(f"unknown queue item kind {kind}")

    def run(self):
        while not self.stop.is_set():
            row = self.queue.claim_next()
            if row is None:
                self.stop.wait(1.0)
                continue
            try:
                self._dispatch(row)
                self.queue.mark_verified(row)
                log.info("verified %s #%s", row["kind"], row["id"])
            except TransientError as exc:
                attempts = self.queue.mark_retry(row, str(exc))
                log.warning("%s #%s transient failure (attempt %d): %s", row["kind"], row["id"], attempts, exc)
                if exc.network:
                    self.stop.wait(min(30.0, 2.0 * attempts))
            except PermanentError as exc:
                self.queue.mark_failed(row, str(exc))
                log.error("%s #%s permanently rejected: %s", row["kind"], row["id"], exc)
            except Exception as exc:
                attempts = self.queue.mark_retry(row, f"unexpected: {exc}")
                log.exception("%s #%s unexpected error (attempt %d)", row["kind"], row["id"], attempts)
                self.stop.wait(5.0)

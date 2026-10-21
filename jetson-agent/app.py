"""UAV data-collection agent: orchestrates mission sync, telemetry, log download and uploads.

Run:   python app.py            (service mode)
       python app.py status     (show queue / flight state)
       python app.py requeue-failed
The agent never arms, changes modes, or alters flight behaviour; flight safety is independent of it.
"""
import json
import logging
import os
import logging.handlers
import signal
import sys
import threading
import time
import uuid
from datetime import datetime, timezone

import log_downloader
import mission_sync
from api_client import ApiClient
from config import load_config
from local_db import FlightStore, LocalDB
from mavlink_client import ConnectionLost, MavlinkClient
from telemetry_collector import TelemetryCollector
from transfer_queue import TransferQueue, UploadWorker

log = logging.getLogger("agent")

MAX_RESUME_AGE_S = 4 * 3600

def setup_logging(cfg):
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    for h in (logging.StreamHandler(),
              logging.handlers.RotatingFileHandler(cfg.data_dir / "agent.log", maxBytes=5_000_000, backupCount=5)):
        h.setFormatter(fmt)
        root.addHandler(h)

def iso(ts=None):
    return datetime.fromtimestamp(ts or time.time(), timezone.utc).isoformat(timespec="seconds")

class Agent:
    def __init__(self):
        self.cfg = load_config()
        setup_logging(self.cfg)
        self.db = LocalDB(self.cfg.data_dir / "agent.db")
        self.flights = FlightStore(self.db)
        self.queue = TransferQueue(self.db, keep_local_logs=self.cfg.keep_local_logs)
        self.api = ApiClient(self.cfg)
        self.stop = threading.Event()
        self.worker = UploadWorker(self.queue, self.api, self.stop)
        self.client = MavlinkClient(self.cfg)
        self.telemetry = TelemetryCollector(self.cfg, self.queue)
        self.client.add_listener(self.telemetry.on_message)
        self.client.add_listener(self._on_message)
        self.flight = None
        self.prev_armed = False
        self._last_tick = 0.0
        self._last_mission_sync = 0.0
        self._last_interval_req = 0.0
        self._last_purge = 0.0
        self._last_status = 0.0
        self._last_hb_sent = 0.0
        self._auto_seen = False

    def _on_message(self, msg):
        if msg.get_type() == "HEARTBEAT" and self.client.armed and self.client.mode_name() == "AUTO":
            if self.flight is not None:
                self._check_auto()
            else:
                self._auto_seen = True
        if msg.get_type() == "MISSION_ITEM_REACHED" and self.flight is not None:
            if msg.seq > self.flight["max_reached"]:
                self.flight["max_reached"] = msg.seq
                self.flights.update_progress(self.flight["id"], msg.seq)

    def _current_mission(self):
        return json.loads(self.db.kv_get("current_mission", "null") or "null")

    def _handle_arm(self):
        if self.cfg.resync_on_arm:
            try:
                mission_sync.sync_once(self.client, self.queue, self.db, self.cfg.aircraft_id)
            except Exception as exc:
                log.warning("mission re-sync on arm failed: %s", exc)
        mission = self._current_mission()
        flight_id = str(uuid.uuid4())
        self.flight = {"id": flight_id, "armed_at": time.time(), "saw_auto": False, "max_reached": -1,
                       "mission_hash": mission["hash"] if mission else None,
                       "final_nav_seq": mission["final_nav_seq"] if mission else None}
        self.flights.start(flight_id, self.cfg.aircraft_id, self.flight["mission_hash"],
                           self.flight["final_nav_seq"], self.flight["armed_at"])
        self.telemetry.start(flight_id, self.flight["mission_hash"])
        if self._auto_seen:
            self._auto_seen = False
            self._mark_auto()
        log.info("ARMED: flight %s (mission %s)", flight_id[:8], (self.flight["mission_hash"] or "none")[:8])

    def _handle_disarm(self):
        f = self.flight
        self.telemetry.stop()
        self._auto_seen = False
        if f is None:
            return
        completed = (f["saw_auto"] and f["final_nav_seq"] is not None and f["max_reached"] >= f["final_nav_seq"])
        outcome = "completed" if completed else ("ended_incomplete" if f["saw_auto"] else "no_mission_flown")
        now = time.time()
        if f["saw_auto"] and f["mission_hash"]:
            payload = {"aircraft_id": self.cfg.aircraft_id, "mission_hash": f["mission_hash"], "status": outcome,
                       "event_time": iso(now)}
            if outcome == "completed":
                payload["completed_at"] = iso(now)
            self.queue.enqueue("mission_status", f"mstat:{f['id']}:{outcome}", payload)
        self.flights.end(f["id"], now, outcome, f["max_reached"], now + self.cfg.post_disarm_delay)
        log.info("DISARMED: flight %s outcome=%s; log download in %.0fs", f["id"][:8], outcome, self.cfg.post_disarm_delay)
        self.flight = None

    def _recover_flights(self):
        """After a restart: resume the flight still in progress, or close flights left open by a crash."""
        now = time.time()
        due = now + self.cfg.post_disarm_delay
        row = self.flights.open_flight()
        if self.client.armed and row is not None and now - row["armed_at"] < MAX_RESUME_AGE_S:
            self.flight = {"id": row["flight_id"], "armed_at": row["armed_at"], "saw_auto": bool(row["saw_auto"]),
                           "max_reached": row["max_reached"] if row["max_reached"] is not None else -1,
                           "mission_hash": row["mission_hash"], "final_nav_seq": row["final_nav_seq"]}
            self.flights.close_orphans_except(row["flight_id"], now, due)
            self.telemetry.start(row["flight_id"], row["mission_hash"])
            self.prev_armed = True
            log.info("RESUMED in-progress flight %s after restart", row["flight_id"][:8])
        else:

            self.flights.close_orphans(now, due)

    def _check_auto(self):
        f = self.flight
        if f and not f["saw_auto"] and self.client.mode_name() == "AUTO":
            self._mark_auto()

    def _mark_auto(self):
        f = self.flight
        if not f or f["saw_auto"]:
            return
        f["saw_auto"] = True
        self.flights.mark_auto(f["id"])
        if f["mission_hash"]:
            self.queue.enqueue("mission_status", f"mstat:{f['id']}:in_progress", {
                "aircraft_id": self.cfg.aircraft_id, "mission_hash": f["mission_hash"], "status": "in_progress",
                "event_time": iso(), "started_at": iso()})
        log.info("flight %s entered AUTO (mission execution)", f["id"][:8])

    def _process_pending_log(self):
        row = self.flights.next_pending_log(time.time())
        if row is None:
            return False
        fid = row["flight_id"]
        try:
            entries = log_downloader.list_logs(self.client)
            entry = log_downloader.choose_log_for_flight(
                entries, row["armed_at"], row["disarmed_at"], self.cfg.min_log_bytes, self.flights.is_latest(fid))
            stamp = datetime.fromtimestamp(row["armed_at"], timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            filename = f"log{entry['id']:05d}_{stamp}.bin"
            dest = str(self.cfg.data_dir / "logs" / f"{fid[:8]}_{filename}")
            result = log_downloader.download_log(self.client, entry, dest)

            after = [e for e in log_downloader.list_logs(self.client) if e["id"] == entry["id"]]
            if not after or after[0]["size"] != entry["size"]:
                os.remove(dest)
                raise log_downloader.LogTransferError("log size changed during download; will retry")
            meta = {"aircraft_id": self.cfg.aircraft_id, "filename": filename, "file_size_bytes": result["size"],
                    "checksum_sha256": result["sha256"], "mission_hash": row["mission_hash"], "flight_id": fid,
                    "log_number": entry["id"]}
            queued = self.queue.enqueue("log", f"log:{self.cfg.aircraft_id}:{result['sha256']}", meta, file_path=dest)
            if not queued and not self.cfg.keep_local_logs:
                os.remove(dest)
            self.flights.log_queued(fid, entry["id"], result["size"], result["sha256"])
        except ConnectionLost:
            raise
        except log_downloader.VehicleArmed:
            self.flights.log_defer(fid, time.time() + self.cfg.post_disarm_delay)
            log.info("vehicle re-armed during log download for flight %s; will retry after landing", fid[:8])
        except Exception as exc:
            attempts = row["log_attempts"] + 1
            failed = attempts >= self.cfg.log_max_attempts
            self.flights.log_retry(fid, str(exc), time.time() + min(300, 10 * 2 ** attempts), failed)
            (log.error if failed else log.warning)("log download for flight %s failed (attempt %d): %s", fid[:8], attempts, exc)
        return True

    def _tick(self):
        armed = self.client.armed
        if armed != self.prev_armed:
            self.prev_armed = armed
            self._handle_arm() if armed else self._handle_disarm()
        now = time.monotonic()
        if now - self._last_tick < 0.5:
            return
        self._last_tick = now
        if now - self._last_hb_sent >= 1.0:
            self._last_hb_sent = now
            self.client.send_heartbeat()
        self.telemetry.flush()
        if armed:
            self._check_auto()
        if now - self._last_interval_req > 60:
            self._last_interval_req = now
            self.client.request_telemetry(self.cfg.telemetry_hz)
        if not armed and self.flight is None:
            if self._process_pending_log():
                return
            if now - self._last_mission_sync >= self.cfg.mission_sync_interval:
                try:
                    mission_sync.sync_once(self.client, self.queue, self.db, self.cfg.aircraft_id)
                except ConnectionLost:
                    raise
                except Exception as exc:
                    log.warning("mission sync failed: %s", exc)
                self._last_mission_sync = time.monotonic()
        if now - self._last_purge > 3600:
            self._last_purge = now
            self.queue.purge_verified()
        if now - self._last_status > 300:
            self._last_status = now
            log.info("queue: %s", self.queue.counts())

    def run(self):
        signal.signal(signal.SIGINT, lambda *_: self.stop.set())
        signal.signal(signal.SIGTERM, lambda *_: self.stop.set())
        self.worker.start()
        backoff = 1.0
        while not self.stop.is_set():
            try:
                self.client.connect()
                backoff = 1.0
                self.client.request_telemetry(self.cfg.telemetry_hz)
                self._last_interval_req = time.monotonic()
                if self.flight is None:
                    self._recover_flights()
                while not self.stop.is_set():
                    self.client.poll(timeout=0.5)
                    self._tick()
            except ConnectionLost as exc:
                log.warning("flight controller link lost: %s (retry in %.0fs)", exc, backoff)
            except Exception:
                log.exception("agent loop error")
            self.telemetry.flush(force=True)
            self.stop.wait(backoff)
            backoff = min(30.0, backoff * 2)
        self.telemetry.flush(force=True)
        log.info("agent stopped")

def main():
    if len(sys.argv) > 1 and sys.argv[1] in ("status", "requeue-failed"):
        cfg = load_config()
        db = LocalDB(cfg.data_dir / "agent.db")
        queue = TransferQueue(db, keep_local_logs=True)
        if sys.argv[1] == "requeue-failed":
            print(f"requeued {queue.requeue_failed()} failed item(s)")
            return
        print("queue:", queue.counts())
        for r in FlightStore(db).summary():
            print(dict(r))
        return
    Agent().run()

if __name__ == "__main__":
    main()

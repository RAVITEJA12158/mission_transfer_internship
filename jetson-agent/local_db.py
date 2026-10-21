"""Persistent local state (SQLite): transfer queue, flight records, key/value.

Everything the agent must not lose across a Jetson reboot lives here.
"""
import sqlite3
import threading

SCHEMA = """
CREATE TABLE IF NOT EXISTS transfer_queue (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,
    idempotency_key TEXT NOT NULL UNIQUE,
    payload TEXT,
    file_path TEXT,
    state TEXT NOT NULL DEFAULT 'PENDING',
    attempts INTEGER NOT NULL DEFAULT 0,
    next_attempt_at REAL NOT NULL DEFAULT 0,
    last_error TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_queue_state ON transfer_queue(state, next_attempt_at);

CREATE TABLE IF NOT EXISTS flights (
    flight_id TEXT PRIMARY KEY,
    aircraft_id INTEGER NOT NULL,
    mission_hash TEXT,
    final_nav_seq INTEGER,
    armed_at REAL NOT NULL,
    disarmed_at REAL,
    saw_auto INTEGER NOT NULL DEFAULT 0,
    max_reached INTEGER,
    outcome TEXT,
    log_state TEXT NOT NULL DEFAULT 'none',
    log_due_at REAL,
    log_attempts INTEGER NOT NULL DEFAULT 0,
    log_error TEXT,
    log_number INTEGER,
    log_size INTEGER,
    log_sha256 TEXT
);

CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT);
"""

class LocalDB:
    def __init__(self, path):
        self.conn = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.lock = threading.RLock()
        with self.lock:
            self.conn.execute("PRAGMA journal_mode=WAL")
            self.conn.execute("PRAGMA synchronous=FULL")
            self.conn.executescript(SCHEMA)

    def execute(self, sql, params=()):
        with self.lock:
            return self.conn.execute(sql, params)

    def query(self, sql, params=()):
        with self.lock:
            return self.conn.execute(sql, params).fetchall()

    def one(self, sql, params=()):
        rows = self.query(sql, params)
        return rows[0] if rows else None

    def kv_get(self, key, default=None):
        row = self.one("SELECT value FROM kv WHERE key=?", (key,))
        return row["value"] if row else default

    def kv_set(self, key, value):
        self.execute("INSERT INTO kv(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                     (key, value))

class FlightStore:
    """One row per armed->disarmed period, plus the state of its log download."""

    def __init__(self, db: LocalDB):
        self.db = db

    def start(self, flight_id, aircraft_id, mission_hash, final_nav_seq, armed_at):
        self.db.execute(
            "INSERT OR IGNORE INTO flights(flight_id, aircraft_id, mission_hash, final_nav_seq, armed_at) VALUES(?,?,?,?,?)",
            (flight_id, aircraft_id, mission_hash, final_nav_seq, armed_at))

    def mark_auto(self, flight_id):
        self.db.execute("UPDATE flights SET saw_auto=1 WHERE flight_id=?", (flight_id,))

    def end(self, flight_id, disarmed_at, outcome, max_reached, log_due_at):
        self.db.execute(
            "UPDATE flights SET disarmed_at=?, outcome=?, max_reached=?, log_state='pending', log_due_at=? WHERE flight_id=?",
            (disarmed_at, outcome, max_reached, log_due_at, flight_id))

    def close_orphans(self, now, log_due_at):
        """Flights left open by a crash/reboot: the vehicle is disarmed now, so close them and fetch their logs."""
        self.db.execute(
            "UPDATE flights SET disarmed_at=?, outcome='interrupted', log_state='pending', log_due_at=? WHERE disarmed_at IS NULL",
            (now, log_due_at))

    def open_flight(self):
        """Most recent flight that has no disarm recorded (agent restarted mid-flight)."""
        return self.db.one("SELECT * FROM flights WHERE disarmed_at IS NULL ORDER BY armed_at DESC LIMIT 1")

    def close_orphans_except(self, keep_flight_id, now, log_due_at):
        self.db.execute(
            "UPDATE flights SET disarmed_at=?, outcome='interrupted', log_state='pending', log_due_at=? "
            "WHERE disarmed_at IS NULL AND flight_id<>?", (now, log_due_at, keep_flight_id))

    def update_progress(self, flight_id, max_reached):
        self.db.execute("UPDATE flights SET max_reached=? WHERE flight_id=?", (max_reached, flight_id))

    def is_latest(self, flight_id):
        row = self.db.one("SELECT COUNT(*) n FROM flights WHERE armed_at > (SELECT armed_at FROM flights WHERE flight_id=?)", (flight_id,))
        return row["n"] == 0

    def next_pending_log(self, now):
        return self.db.one(
            "SELECT * FROM flights WHERE log_state='pending' AND log_due_at<=? ORDER BY armed_at LIMIT 1", (now,))

    def log_defer(self, flight_id, due_at):
        """Postpone without counting a failed attempt (e.g. vehicle re-armed)."""
        self.db.execute("UPDATE flights SET log_due_at=? WHERE flight_id=?", (due_at, flight_id))

    def log_retry(self, flight_id, error, due_at, failed):
        self.db.execute(
            "UPDATE flights SET log_attempts=log_attempts+1, log_error=?, log_due_at=?, "
            "log_state=CASE WHEN ? THEN 'failed' ELSE 'pending' END WHERE flight_id=?",
            (error[:500], due_at, 1 if failed else 0, flight_id))

    def log_queued(self, flight_id, number, size, sha):
        self.db.execute(
            "UPDATE flights SET log_state='queued', log_error=NULL, log_number=?, log_size=?, log_sha256=? WHERE flight_id=?",
            (number, size, sha, flight_id))

    def summary(self):
        return self.db.query("SELECT flight_id, outcome, log_state, log_attempts, log_error FROM flights ORDER BY armed_at DESC LIMIT 10")

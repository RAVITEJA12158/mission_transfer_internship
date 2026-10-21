
import logging
import math
import threading
import time
import uuid
from datetime import datetime, timezone

log = logging.getLogger("telemetry")

UINT16_UNKNOWN = 65535


def _gpi(msg):
    have_fix = not (msg.lat == 0 and msg.lon == 0)  # 0,0 means "no position", not the Gulf of Guinea
    return {
        "latitude": msg.lat / 1e7 if have_fix else None,
        "longitude": msg.lon / 1e7 if have_fix else None,
        "altitude_m": msg.alt / 1000.0 if have_fix else None,          # mm AMSL -> m
        "relative_altitude_m": msg.relative_alt / 1000.0,              # mm above home -> m
        "speed_mps": math.hypot(msg.vx, msg.vy) / 100.0,               # cm/s ground speed -> m/s
        "heading_deg": msg.hdg / 100.0 if msg.hdg != UINT16_UNKNOWN else None,  # cdeg -> deg
        "raw_data": {"time_boot_ms": msg.time_boot_ms, "vx_cms": msg.vx, "vy_cms": msg.vy, "vz_cms": msg.vz},
    }


def _vfr(msg):
    return {
        "speed_mps": float(msg.groundspeed),
        "heading_deg": float(msg.heading) % 360,
        "raw_data": {"airspeed": msg.airspeed, "alt": msg.alt, "climb": msg.climb, "throttle": msg.throttle},
    }


def _sys(msg):
    return {
        "battery_voltage": msg.voltage_battery / 1000.0 if msg.voltage_battery != UINT16_UNKNOWN else None,  # mV -> V
        "raw_data": {"current_battery_cA": msg.current_battery, "battery_remaining_pct": msg.battery_remaining},
    }


def _clean(value):
    """JSON cannot carry NaN/inf; store them as null instead of losing the whole batch."""
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, dict):
        return {k: _clean(v) for k, v in value.items()}
    return value


HANDLERS = {"GLOBAL_POSITION_INT": _gpi, "VFR_HUD": _vfr, "SYS_STATUS": _sys}


class TelemetryCollector:
    def __init__(self, cfg, queue):
        self.cfg, self.queue = cfg, queue
        self.min_interval = 1.0 / cfg.telemetry_hz
        self.lock = threading.Lock()
        self.buffer = []
        self.buffer_started = None
        self.last_emit = {}
        self.active = False
        self.flight_id = None
        self.mission_hash = None

    def start(self, flight_id, mission_hash):
        with self.lock:
            self.flight_id, self.mission_hash, self.active = flight_id, mission_hash, True
            self.last_emit.clear()

    def stop(self):
        self.active = False
        self.flush(force=True)

    def on_message(self, msg):
        if not self.active:
            return
        mtype = msg.get_type()
        handler = HANDLERS.get(mtype)
        if handler is None:
            return
        now = time.monotonic()
        if now - self.last_emit.get(mtype, 0.0) < self.min_interval:
            return
        self.last_emit[mtype] = now
        record = {"timestamp": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
                  "message_type": mtype, **_clean(handler(msg))}
        with self.lock:
            if not self.buffer:
                self.buffer_started = now
            self.buffer.append(record)

    def flush(self, force=False):
        with self.lock:
            if not self.buffer:
                return
            due = len(self.buffer) >= self.cfg.telemetry_batch_max or \
                time.monotonic() - self.buffer_started >= self.cfg.telemetry_batch_seconds
            if not (force or due):
                return
            records, self.buffer = self.buffer, []
            flight_id, mission_hash = self.flight_id, self.mission_hash
        batch_id = str(uuid.uuid4())
        payload = {"aircraft_id": self.cfg.aircraft_id, "batch_id": batch_id, "flight_id": flight_id,
                   "mission_hash": mission_hash, "records": records}
        try:
            # Persisted immediately; the backend de-duplicates on batch_id so retries are safe.
            self.queue.enqueue("telemetry", f"telemetry:{batch_id}", payload)
        except Exception:  # noqa: BLE001
            log.exception("failed to persist telemetry batch of %d records", len(records))

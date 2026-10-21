"""Configuration loaded from environment / .env (nothing is hardcoded in other modules)."""
import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

def _get(name, default=None, required=False):
    val = os.environ.get(name, default)
    if required and (val is None or val == ""):
        raise RuntimeError(f"Missing required setting {name} (see .env.example)")
    return val

def _bool(name, default):
    return str(_get(name, str(default))).strip().lower() in ("1", "true", "yes", "on")

@dataclass(frozen=True)
class Config:
    mavlink_connection: str
    mavlink_baud: int
    mavlink_target_sysid: int
    mavlink_source_system: int
    mavlink_source_component: int
    heartbeat_timeout: float
    backend_url: str
    api_key: str
    aircraft_id: int
    http_timeout: float
    data_dir: Path
    keep_local_logs: bool
    telemetry_hz: float
    telemetry_batch_seconds: float
    telemetry_batch_max: int
    mission_sync_interval: float
    resync_on_arm: bool
    post_disarm_delay: float
    min_log_bytes: int
    log_max_attempts: int

def load_config() -> Config:
    load_dotenv(Path(__file__).with_name(".env"))
    data_dir = Path(_get("DATA_DIR", "./data")).expanduser().resolve()
    (data_dir / "logs").mkdir(parents=True, exist_ok=True)
    cfg = Config(
        mavlink_connection=_get("MAVLINK_CONNECTION", "/dev/ttyUSB0"),
        mavlink_baud=int(_get("MAVLINK_BAUD", "57600")),
        mavlink_target_sysid=int(_get("MAVLINK_TARGET_SYSID", "1")),
        mavlink_source_system=int(_get("MAVLINK_SOURCE_SYSTEM", "254")),
        mavlink_source_component=int(_get("MAVLINK_SOURCE_COMPONENT", "191")),
        heartbeat_timeout=float(_get("HEARTBEAT_TIMEOUT", "10")),
        backend_url=_get("BACKEND_URL", required=True).rstrip("/"),
        api_key=_get("API_KEY", required=True),
        aircraft_id=int(_get("AIRCRAFT_ID", "1")),
        http_timeout=float(_get("HTTP_TIMEOUT", "15")),
        data_dir=data_dir,
        keep_local_logs=_bool("KEEP_LOCAL_LOGS", False),
        telemetry_hz=float(_get("TELEMETRY_HZ", "2")),
        telemetry_batch_seconds=float(_get("TELEMETRY_BATCH_SECONDS", "5")),
        telemetry_batch_max=int(_get("TELEMETRY_BATCH_MAX", "500")),
        mission_sync_interval=float(_get("MISSION_SYNC_INTERVAL", "20")),
        resync_on_arm=_bool("RESYNC_ON_ARM", True),
        post_disarm_delay=float(_get("POST_DISARM_DELAY", "10")),
        min_log_bytes=int(_get("MIN_LOG_BYTES", "4096")),
        log_max_attempts=int(_get("LOG_MAX_ATTEMPTS", "10")),
    )
    if cfg.telemetry_hz <= 0:
        raise RuntimeError("TELEMETRY_HZ must be greater than 0")
    if cfg.aircraft_id < 1:
        raise RuntimeError("AIRCRAFT_ID must be a positive database aircraft id")
    if not 0 <= cfg.mavlink_target_sysid <= 255:
        raise RuntimeError("MAVLINK_TARGET_SYSID must be between 0 and 255 (0 enables autodetection)")
    if not 1 <= cfg.mavlink_source_system <= 255:
        raise RuntimeError("MAVLINK_SOURCE_SYSTEM must be between 1 and 255")
    if not 0 <= cfg.mavlink_source_component <= 255:
        raise RuntimeError("MAVLINK_SOURCE_COMPONENT must be between 0 and 255")
    return cfg

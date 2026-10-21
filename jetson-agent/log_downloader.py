"""Download DataFlash logs from ArduPilot using the MAVLink log-transfer protocol
(LOG_REQUEST_LIST / LOG_ENTRY / LOG_REQUEST_DATA / LOG_DATA / LOG_REQUEST_END) - the same mechanism
Mission Planner's "Download Dataflash Log via MAVLink" uses.

Only valid while disarmed. Lost packets are detected and re-requested. A transfer is accepted only if the
received byte count equals the size the autopilot reported for that log.
"""
import hashlib
import logging
import os
import shutil
import time

from mavlink_client import ConnectionLost

log = logging.getLogger("logdl")

PACKET_BYTES = 90
WINDOW_BYTES = PACKET_BYTES * 800
STALL_TIMEOUT = 1.5
MAX_STALLS = 8

class LogTransferError(Exception):
    pass

class VehicleArmed(LogTransferError):
    """The vehicle armed during a download (normal between quick flights): not a failure, just try later."""

def list_logs(client, timeout=2.0, retries=4):
    mav, sysid, compid = client.master.mav, client.target_system, client.target_component
    for _ in range(retries):
        mav.log_request_list_send(sysid, compid, 0, 0xFFFF)
        entries, expected = {}, None
        last = time.monotonic()
        while time.monotonic() - last < timeout:
            msg = client.poll({"LOG_ENTRY"}, timeout=0.5)
            if msg is None:
                continue
            last = time.monotonic()
            if msg.num_logs == 0:
                return []
            expected = msg.num_logs
            entries[msg.id] = {"id": msg.id, "num_logs": msg.num_logs, "last_log_num": msg.last_log_num,
                               "time_utc": msg.time_utc, "size": msg.size}
            if len(entries) >= expected:
                break
        if entries:
            return [entries[k] for k in sorted(entries)]
    raise LogTransferError("no LOG_ENTRY replies (is a log backend / SD card configured?)")

def _fetch_range(client, log_id, start, length):
    """Fetch [start, start+length) reliably, re-requesting whatever is still missing."""
    mav, sysid, compid = client.master.mav, client.target_system, client.target_component
    buf, have = bytearray(length), bytearray(length)
    received, stalls = 0, 0
    while received < length:
        if client.armed:
            raise VehicleArmed("vehicle armed during log download; aborting")
        before = received
        missing = have.find(b"\x00")
        mav.log_request_data_send(sysid, compid, log_id, start + missing, length - missing)
        last_progress = time.monotonic()
        while received < length:
            msg = client.poll({"LOG_DATA"}, timeout=0.5)
            if msg is None:
                if time.monotonic() - last_progress > STALL_TIMEOUT:
                    break
                continue
            if msg.id != log_id:
                continue
            rel = msg.ofs - start
            n = min(msg.count, length - rel)
            if rel < 0 or n <= 0:
                continue
            new = have[rel:rel + n].count(0)
            if new:
                buf[rel:rel + n] = bytes(msg.data[:n])
                have[rel:rel + n] = b"\x01" * n
                received += new
                last_progress = time.monotonic()
            if msg.ofs + msg.count >= start + length:
                break
        stalls = 0 if received > before else stalls + 1
        if stalls >= MAX_STALLS:
            stuck_at = start + have.find(b"\x00")
            raise LogTransferError(f"log transfer stalled at offset {stuck_at}")
    return bytes(buf)

def download_log(client, entry, dest_path):
    """Download one log to dest_path (via .part + atomic rename). Returns dict(size, sha256)."""
    size = entry["size"]
    if size <= 0:
        raise LogTransferError("log reports zero size")
    free = shutil.disk_usage(os.path.dirname(dest_path) or ".").free
    if free < size * 2 + 50_000_000:
        raise LogTransferError(f"not enough free disk space on the Jetson ({free // 1_000_000} MB) for a {size // 1000} KB log")
    part = dest_path + ".part"
    sha = hashlib.sha256()
    t0 = time.monotonic()
    try:
        with open(part, "wb") as fh:
            ofs = 0
            while ofs < size:
                length = min(WINDOW_BYTES, size - ofs)
                chunk = _fetch_range(client, entry["id"], ofs, length)
                fh.write(chunk)
                sha.update(chunk)
                ofs += length
            fh.flush()
            os.fsync(fh.fileno())
        if os.path.getsize(part) != size:
            raise LogTransferError("size mismatch after download")
        os.replace(part, dest_path)
    except (LogTransferError, ConnectionLost):
        if os.path.exists(part):
            os.remove(part)
        raise
    finally:
        try:
            client.master.mav.log_request_end_send(client.target_system, client.target_component)
        except Exception:
            pass
    secs = max(time.monotonic() - t0, 0.001)
    log.info("downloaded log %d: %d bytes in %.1fs (%.1f KB/s)", entry["id"], size, secs, size / secs / 1024)
    return {"size": size, "sha256": sha.hexdigest()}

MIN_PLAUSIBLE_EPOCH = 1577836800

def choose_log_for_flight(entries, armed_at, disarmed_at, min_bytes, is_latest_flight, slack=600):
    """Pick the log belonging to one specific flight.

    LOG_ENTRY.time_utc is the log's last-modified time, so it should fall between arm and (shortly after)
    disarm. If the autopilot has no valid clock, the only safe guess is "newest log", and only for the most
    recent flight; an older flight's log cannot be identified that way and is reported as an error.
    """
    if not entries:
        raise LogTransferError("autopilot has no logs")
    newest = max(entries, key=lambda e: e["id"])
    if newest["time_utc"] < MIN_PLAUSIBLE_EPOCH and is_latest_flight:
        entry = newest
    else:
        timed = [e for e in entries if e["time_utc"] >= MIN_PLAUSIBLE_EPOCH
                 and armed_at - slack <= e["time_utc"] <= disarmed_at + slack]
        if not timed:
            raise LogTransferError("no log on the autopilot can be matched to this flight's time window")
        entry = min(timed, key=lambda e: abs(e["time_utc"] - disarmed_at))
    if entry["size"] < min_bytes:
        raise LogTransferError(f"log {entry['id']} is only {entry['size']} bytes")
    return entry

"""Download the mission stored on the Pixhawk via the MAVLink mission protocol and queue it for upload."""
import hashlib
import json
import logging
import math
import time
from datetime import datetime, timezone

from mavlink_client import mavlink

log = logging.getLogger("mission")

MISSION_TYPE = mavlink.MAV_MISSION_TYPE_MISSION
FRAME_MISSION = mavlink.MAV_FRAME_MISSION

NO_POSITION_COMMANDS = {
    20, 93, 112, 113, 114, 115, 176, 177, 178, 181, 182, 183, 184, 191, 200, 202, 203, 205, 206, 207, 208, 211, 2000, 2001, 2003,
}

COMPLETION_NAV_COMMANDS = {16, 17, 18, 19, 21, 22, 82}

class MissionSyncError(Exception):
    pass

def _finite(x):
    x = float(x)
    return x if math.isfinite(x) else None

def _convert(msg):
    positional = msg.frame != FRAME_MISSION and msg.command not in NO_POSITION_COMMANDS
    return {
        "sequence_number": msg.seq,
        "command": msg.command,
        "frame": msg.frame,
        "param1": _finite(msg.param1), "param2": _finite(msg.param2),
        "param3": _finite(msg.param3), "param4": _finite(msg.param4),
        "latitude": msg.x / 1e7 if positional else None,
        "longitude": msg.y / 1e7 if positional else None,
        "altitude": _finite(msg.z) if positional else None,
        "autocontinue": bool(msg.autocontinue),

        "raw_item": {"x": msg.x, "y": msg.y, "z": _finite(msg.z), "current": msg.current,
                     "mission_type": msg.mission_type},
    }

def download_mission(client, timeout=2.0, retries=5):
    """Full MAVLink mission download with per-item retries. Raises MissionSyncError if incomplete."""
    mav, sysid, compid = client.master.mav, client.target_system, client.target_component

    count = None
    for _ in range(retries):
        mav.mission_request_list_send(sysid, compid, MISSION_TYPE)
        end = time.monotonic() + timeout
        while time.monotonic() < end and count is None:
            msg = client.poll({"MISSION_COUNT"}, timeout=max(0.1, end - time.monotonic()))
            if msg is not None and msg.mission_type == MISSION_TYPE:
                count = msg.count
        if count is not None:
            break
    if count is None:
        raise MissionSyncError("no MISSION_COUNT reply")

    items = []
    for seq in range(count):
        got = None
        for _ in range(retries):
            mav.mission_request_int_send(sysid, compid, seq, MISSION_TYPE)
            end = time.monotonic() + timeout
            while time.monotonic() < end and got is None:
                msg = client.poll({"MISSION_ITEM_INT"}, timeout=max(0.1, end - time.monotonic()))
                if msg is not None and msg.seq == seq and msg.mission_type == MISSION_TYPE:
                    got = msg
            if got is not None:
                break
        if got is None:
            raise MissionSyncError(f"mission item {seq}/{count} not received after {retries} tries")
        items.append(_convert(got))

    mav.mission_ack_send(sysid, compid, mavlink.MAV_MISSION_ACCEPTED, MISSION_TYPE)

    if [i["sequence_number"] for i in items] != list(range(count)):
        raise MissionSyncError("mission items out of sequence")
    return items

def mission_hash(items):
    """Stable content hash over the raw item fields (order-sensitive).

    Item 0 is excluded: on ArduPilot it is the vehicle's *home* position, which moves with GPS/home updates
    and on arm. Hashing it would make the same flight plan look like a new mission after every home change."""
    canon = [[i["sequence_number"], i["frame"], i["command"], i["param1"], i["param2"], i["param3"], i["param4"],
              i["raw_item"]["x"], i["raw_item"]["y"], i["raw_item"]["z"], i["autocontinue"]]
             for i in items if i["sequence_number"] > 0]
    return hashlib.sha256(json.dumps(canon, separators=(",", ":"), allow_nan=False).encode()).hexdigest()

def final_nav_seq(items):
    seqs = [i["sequence_number"] for i in items if i["sequence_number"] > 0 and i["command"] in COMPLETION_NAV_COMMANDS]
    return max(seqs) if seqs else None

def sync_once(client, queue, db, aircraft_id):
    """Download the mission; queue it for upload. Returns the current mission info (or None if empty).

    enqueue() is idempotent on the mission hash and the backend de-duplicates on (aircraft, hash),
    so re-syncing an unchanged mission creates nothing new.
    """
    items = download_mission(client)
    if len(items) <= 1:
        db.kv_set("current_mission", "null")
        return None
    digest = mission_hash(items)
    payload = {
        "aircraft_id": aircraft_id,
        "mission_name": f"Mission {digest[:8]} {datetime.now(timezone.utc):%Y-%m-%d %H:%M}Z",
        "mission_hash": digest,
        "items": items,
    }
    if queue.enqueue("mission", f"mission:{aircraft_id}:{digest}", payload):
        log.info("new mission %s queued (%d items)", digest[:8], len(items))
    info = {"hash": digest, "final_nav_seq": final_nav_seq(items), "item_count": len(items)}
    db.kv_set("current_mission", json.dumps(info))
    return info

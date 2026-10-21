import sys, tempfile, types
import os; sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))
import mission_sync, log_downloader
from local_db import LocalDB, FlightStore
from transfer_queue import TransferQueue
from telemetry_collector import TelemetryCollector
from pathlib import Path
ok = True
def check(name, cond):
    global ok; ok &= bool(cond); print(("PASS " if cond else "FAIL ") + name)

def item(seq, x=1, cmd=16):
    return {"sequence_number": seq, "frame": 3, "command": cmd, "param1": 0.0, "param2": 0.0, "param3": 0.0, "param4": 0.0,
            "raw_item": {"x": x, "y": 2, "z": 3.0}, "autocontinue": True}
a = [item(0, 100), item(1, 5), item(2, 6)]; b = [item(0, 999), item(1, 5), item(2, 6)]; c = [item(0, 100), item(1, 5), item(2, 7)]
check("A  same plan + moved home -> same hash", mission_sync.mission_hash(a) == mission_sync.mission_hash(b))
check("A  changed waypoint -> different hash", mission_sync.mission_hash(a) != mission_sync.mission_hash(c))

now = 1_800_000_000
E = lambda i, t, size=50000: {"id": i, "size": size, "time_utc": t}
ch = log_downloader.choose_log_for_flight
flightA = dict(armed_at=now, disarmed_at=now + 60, min_bytes=1000)
entries = [E(1, now + 65), E(2, now + 1000)]
check("B1 older flight A gets ITS log (id 1) even though a newer log exists", ch(entries, is_latest_flight=False, **flightA)["id"] == 1)
check("B1 newer flight B gets id 2", ch(entries, armed_at=now + 900, disarmed_at=now + 995, min_bytes=1000, is_latest_flight=True)["id"] == 2)
check("B2 no-RTC autopilot: latest flight accepted (newest log)", ch([E(1, 946684800)], is_latest_flight=True, **flightA)["id"] == 1)
try: ch([E(1, 946684800), E(2, 946684800)], is_latest_flight=False, **flightA); r = False
except log_downloader.LogTransferError: r = True
check("B2 no-RTC + older flight: refuses to guess (no mis-association)", r)
try: ch([E(1, now - 99999)], is_latest_flight=True, **flightA); r = False
except log_downloader.LogTransferError: r = True
check("B3 stale log from days ago is rejected", r)
try: ch([E(1, now + 65, size=10)], is_latest_flight=True, **flightA); r = False
except log_downloader.LogTransferError: r = True
check("B4 tiny log rejected", r)

d = tempfile.mkdtemp(); db = LocalDB(Path(d) / "x.db"); q = TransferQueue(db)
cfg = types.SimpleNamespace(telemetry_hz=2, telemetry_batch_seconds=0, telemetry_batch_max=500, aircraft_id=1)
tc = TelemetryCollector(cfg, q); tc.start("fid", None)
tc.on_message(types.SimpleNamespace(get_type=lambda: "VFR_HUD", groundspeed=4.0, heading=90, airspeed=float("nan"), alt=1.0, climb=float("inf"), throttle=10))
tc.flush(force=True)
check("C  NaN/inf no longer drops the batch", q.counts().get("PENDING") == 1)

fs = FlightStore(db)
fs.start("f1", 1, "h", 4, 1000.0); fs.start("f2", 1, "h", 4, 2000.0)
check("D  is_latest works", (not fs.is_latest("f1")) and fs.is_latest("f2"))
check("D  open_flight returns newest open", fs.open_flight()["flight_id"] == "f2")
fs.close_orphans_except("f2", 3000.0, 3003.0)
check("D  older open flights closed, current kept open", fs.open_flight()["flight_id"] == "f2" and fs.next_pending_log(4000.0)["flight_id"] == "f1")
print("\nALL PASS" if ok else "\nSOME FAILED"); sys.exit(0 if ok else 1)

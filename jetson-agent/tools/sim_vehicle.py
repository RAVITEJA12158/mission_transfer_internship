"""Chaos ArduPilot sim: one NEW log per flight, radio noise, packet loss, re-arm, and a full link blackout."""
import os, random, sys, time
os.environ["MAVLINK20"] = "1"
from pymavlink import mavutil
mav = mavutil.mavlink
m = mavutil.mavlink_connection("udpout:127.0.0.1:14560", source_system=1, source_component=1)
ARMS = [(float(a), float(d)) for a, d in (x.split("-") for x in sys.argv[1].split(","))]
BLACKOUT = tuple(float(x) for x in sys.argv[2].split("-")) if len(sys.argv) > 2 else (1e9, 1e9)
LOG_SIZE = int(os.environ.get("LOG_SIZE", "1000000")); LOSS = float(os.environ.get("LOSS", "0.05"))
ITEMS = [(0, 0, 16, 0, 0, 0, 0, 170000000, 820000000, 5.0), (1, 3, 22, 0, 0, 0, 0, 0, 0, 20.0),
         (2, 3, 16, 0, 0, 0, 0, 170010000, 820010000, 30.0), (3, 2, 183, 5, 1500, 0, 0, 0, 0, 0.0),
         (4, 3, 16, 0, 0, 0, 0, 170020000, 820020000, 30.0), (5, 3, 20, 0, 0, 0, 0, 0, 0, 0.0)]
t0 = time.time(); armed = False; logs = []; rnd = random.Random(3); drop = random.Random(1)
last_hb = last_tel = last_noise = 0; reached = set(); cur_arm = None
def pkt_send(fn, *a):
    if BLACKOUT[0] <= time.time() - t0 < BLACKOUT[1]: return
    fn(*a)
def log_data(lid, ofs, count):
    data = logs[lid - 1][1]; end = min(ofs + count, len(data)); o = ofs
    while o < end:
        n = min(90, end - o)
        if drop.random() > LOSS: pkt_send(m.mav.log_data_send, lid, o, n, list(data[o:o+n]) + [0] * (90 - n))
        o += n
        if (o // 90) % 50 == 0: time.sleep(0.001)
while True:
    now = time.time() - t0
    want = any(a <= now < d for a, d in ARMS)
    if want and not armed:
        armed = True; cur_arm = now; reached.clear(); print(f"SIM armed t={now:.0f}", flush=True)
    if armed and not want:
        armed = False; logs.append((len(logs) + 1, rnd.randbytes(LOG_SIZE + len(logs) * 777), int(time.time()))); print(f"SIM disarmed t={now:.0f} log{len(logs)} created", flush=True)
    if now - last_hb >= 1:
        last_hb = now
        pkt_send(m.mav.heartbeat_send, mav.MAV_TYPE_QUADROTOR, mav.MAV_AUTOPILOT_ARDUPILOTMEGA, mav.MAV_MODE_FLAG_CUSTOM_MODE_ENABLED | (mav.MAV_MODE_FLAG_SAFETY_ARMED if armed else 0), 3 if armed else 0, 4)
    if armed and now - last_tel >= 0.2:
        last_tel = now
        pkt_send(m.mav.global_position_int_send, int(now*1000), 170000000 + int(now*1000), 820000000, 35000, 30000, 300, 0, 10, 9000)
        pkt_send(m.mav.vfr_hud_send, 4.2, 4.2, 90, 50, 35.0, float("nan") if int(now) % 7 == 0 else 0.1)
        pkt_send(m.mav.sys_status_send, 0, 0, 0, 500, 15800, 120, 80, 0, 0, 0, 0, 0, 0)
        for seq in (2, 4):
            if now - cur_arm > 2 * seq and seq not in reached: reached.add(seq); pkt_send(m.mav.mission_item_reached_send, seq)
    if now - last_noise >= 0.5:
        last_noise = now
        try: m.port.sendto(rnd.randbytes(rnd.randint(1, 200)), ("127.0.0.1", 14560))
        except Exception: pass
    msg = m.recv_match(blocking=True, timeout=0.02)
    if msg is None or BLACKOUT[0] <= now < BLACKOUT[1]: continue
    t = msg.get_type()
    if t == "MISSION_REQUEST_LIST": pkt_send(m.mav.mission_count_send, msg.get_srcSystem(), msg.get_srcComponent(), len(ITEMS), 0)
    elif t == "MISSION_REQUEST_INT" and rnd.random() > 0.1:
        s = ITEMS[msg.seq]
        pkt_send(m.mav.mission_item_int_send, msg.get_srcSystem(), msg.get_srcComponent(), s[0], s[1], s[2], 0, 1, s[3], s[4], s[5], s[6], s[7], s[8], s[9], 0)
    elif t == "LOG_REQUEST_LIST" and not armed:
        for i, (lid, data, tu) in enumerate(logs): pkt_send(m.mav.log_entry_send, lid, len(logs), len(logs), tu, len(data))
        if not logs: pkt_send(m.mav.log_entry_send, 0, 0, 0, 0, 0)
    elif t == "LOG_REQUEST_DATA" and not armed and 1 <= msg.id <= len(logs): log_data(msg.id, msg.ofs, msg.count)

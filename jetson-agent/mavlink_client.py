"""MAVLink connection to the ArduPilot flight controller.

Single-threaded by design: all MAVLink I/O happens through poll(), which dispatches every message to
listeners (telemetry, state tracking) even while mission/log operations are waiting for their replies.
This agent is read-only with respect to flight behaviour: it never arms, changes modes or sends commands
other than data requests (message intervals, mission/log downloads).
"""
import logging
import os
import time

os.environ.setdefault("MAVLINK20", "1")
from pymavlink import mavutil

mavlink = mavutil.mavlink
log = logging.getLogger("mavlink")

class ConnectionLost(Exception):
    pass

class MavlinkClient:
    def __init__(self, cfg):
        self.cfg = cfg
        self.master = None
        self.listeners = []
        self.target_system = cfg.mavlink_target_sysid
        self.target_component = mavlink.MAV_COMP_ID_AUTOPILOT1
        self.armed = False
        self.custom_mode = None
        self.vehicle_type = None
        self.last_heartbeat = 0.0

    def add_listener(self, fn):
        self.listeners.append(fn)

    def close(self):
        if self.master is not None:
            try:
                self.master.close()
            except Exception:
                pass
            self.master = None

    def connect(self):
        self.close()
        log.info("connecting to %s @ %d", self.cfg.mavlink_connection, self.cfg.mavlink_baud)
        self.master = mavutil.mavlink_connection(
            self.cfg.mavlink_connection,
            baud=self.cfg.mavlink_baud,
            source_system=self.cfg.mavlink_source_system,
            source_component=self.cfg.mavlink_source_component,
            autoreconnect=True,
        )
        self._wait_heartbeat()

    def _is_target_autopilot(self, msg):
        return (msg.get_type() == "HEARTBEAT"
                and msg.autopilot == mavlink.MAV_AUTOPILOT_ARDUPILOTMEGA
                and msg.get_srcComponent() == mavlink.MAV_COMP_ID_AUTOPILOT1
                and (not self.target_system or msg.get_srcSystem() == self.target_system))

    def _wait_heartbeat(self):
        deadline = time.monotonic() + self.cfg.heartbeat_timeout
        while time.monotonic() < deadline:
            msg = self.master.recv_match(type="HEARTBEAT", blocking=True, timeout=1.0)
            if msg is not None and self._is_target_autopilot(msg):
                if not self.target_system:
                    self.target_system = msg.get_srcSystem()
                self.master.target_system = self.target_system
                self.master.target_component = self.target_component
                self._handle(msg)
                log.info("heartbeat from system %d (vehicle type %d, armed=%s)",
                         self.target_system, msg.type, self.armed)
                return
        raise ConnectionLost("no ArduPilot heartbeat received")

    def _handle(self, msg):
        if msg.get_type() == "HEARTBEAT":
            if not self._is_target_autopilot(msg):
                return
            self.last_heartbeat = time.monotonic()
            self.armed = bool(msg.base_mode & mavlink.MAV_MODE_FLAG_SAFETY_ARMED)
            self.custom_mode = msg.custom_mode
            self.vehicle_type = msg.type
        for fn in self.listeners:
            try:
                fn(msg)
            except Exception:
                log.exception("listener failed on %s", msg.get_type())

    def check_alive(self):
        if time.monotonic() - self.last_heartbeat > self.cfg.heartbeat_timeout:
            raise ConnectionLost(f"no heartbeat for {self.cfg.heartbeat_timeout:.0f}s")

    def poll(self, types=None, timeout=1.0):
        """Return the next message (optionally only of the given types) or None on timeout.
        Every received message from the target vehicle is dispatched to listeners."""
        end = time.monotonic() + timeout
        while True:
            remaining = end - time.monotonic()
            if remaining <= 0:
                self.check_alive()
                return None
            msg = self.master.recv_match(blocking=True, timeout=min(remaining, 0.5))
            if msg is None:
                self.check_alive()
                continue
            mtype = msg.get_type()
            if mtype == "BAD_DATA" or msg.get_srcSystem() != self.target_system:
                continue
            self._handle(msg)
            if types is None or mtype in types:
                return msg

    def mode_name(self):
        try:
            for name, num in self.master.mode_mapping().items():
                if num == self.custom_mode:
                    return name
        except Exception:
            pass
        return None

    def send_heartbeat(self):
        """1 Hz companion-computer heartbeat so the autopilot sees a live MAVLink peer (does not affect flight)."""
        self.master.mav.heartbeat_send(mavlink.MAV_TYPE_ONBOARD_CONTROLLER, mavlink.MAV_AUTOPILOT_INVALID,
                                       0, 0, mavlink.MAV_STATE_ACTIVE)

    def request_message_interval(self, msg_id, hz):
        interval_us = int(1_000_000 / hz) if hz > 0 else -1
        self.master.mav.command_long_send(
            self.target_system, self.target_component,
            mavlink.MAV_CMD_SET_MESSAGE_INTERVAL, 0, msg_id, interval_us, 0, 0, 0, 0, 0)

    def request_telemetry(self, hz):
        """Ask the autopilot to stream the messages we use (resets on autopilot reboot, so re-sent periodically)."""
        for msg_id in (mavlink.MAVLINK_MSG_ID_GLOBAL_POSITION_INT, mavlink.MAVLINK_MSG_ID_VFR_HUD,
                       mavlink.MAVLINK_MSG_ID_SYS_STATUS):
            self.request_message_interval(msg_id, hz)

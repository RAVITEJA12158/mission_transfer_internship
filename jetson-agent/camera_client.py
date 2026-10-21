"""Parrot Sequoia integration (Phase 8) - intentionally not implemented yet.

The core flight-data pipeline must be working first. Before implementing this, confirm how the Sequoia
data will be retrieved in your setup (its Wi-Fi/HTTP interface, USB mass-storage, or SD-card offload) and
how captures will be tied to a mission (flight_id / mission_hash, plus the autopilot's camera-trigger events).
"""

class CameraClient:
    def __init__(self, cfg):
        self.cfg = cfg

    def collect_for_flight(self, flight_id, mission_hash):
        raise NotImplementedError("Sequoia integration is planned for Phase 8")

# UAV Mission and Flight Data Automation System

Automates collection and storage of UAV flight data:

```
Mission Planner -> Pixhawk V6X (ArduPilot) --MAVLink--> Jetson agent (Python) --HTTP--> Workstation backend (Express) -> PostgreSQL + flight-logs/
```

Normal operation needs no manual file handling: upload a mission in Mission Planner, fly, and the mission,
telemetry and `.bin` log appear on the workstation.

## Verification status (read this first)

| Area | Status |
|---|---|
| Backend API against a real PostgreSQL 16: `npm test` (23 checks) and `npm run test:adversarial` (55 checks: SQL injection, path traversal, malformed/oversized input, 8-10 simultaneous duplicate uploads, client abort mid-upload, auth), also on a DB created from the original spec schema; server `kill -9` mid-upload | Tested |
| Jetson agent against a **simulated** ArduPilot over UDP (`jetson-agent/tools/sim_vehicle.py`): mission download with lost requests, NaN telemetry, random radio noise, 5% log packet loss, re-arm during a log download, a 14 s total radio blackout mid-download, backend outage, agent killed mid-flight and restarted; no duplicates, correct log-to-flight matching, SHA-256 match | Tested |
| Real CUAV Pixhawk V6X + real ArduPilot firmware, serial wiring, real log-protocol behaviour/throughput | **Not tested** |
| Jetson Orin Nano, systemd service, Parrot Sequoia | **Not tested / not implemented** (`camera_client.py` is a stub) |

The simulator mimics the MAVLink messages as documented; real firmware may differ in details. Follow the
staged bench test at the bottom before trusting it in flight.

## Quick start

### Workstation (backend)
```bash
cd backend
cp .env.example .env            # set DATABASE_URL and a long random API_KEY
npm install
# create the DB once:  psql -c "CREATE DATABASE uav_system;"
npm run db:init                 # applies sql/schema.sql (idempotent; seeds aircraft id 1)
npm start                       # listens on 0.0.0.0:$PORT
BASE_URL=http://localhost:5000 API_KEY=<key> npm test   # API smoke test
```
Open the backend port in the workstation firewall. Run it under systemd/pm2 so it starts on boot.

### Jetson (agent)
```bash
cd jetson-agent
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env            # MAVLINK_CONNECTION/BAUD, BACKEND_URL, API_KEY, AIRCRAFT_ID
python app.py                   # or install uav-agent.service (adjust paths/user)
python app.py status            # queue + recent flights
python app.py requeue-failed    # after fixing whatever caused permanent rejections
```

## How the automation works

1. **Connect**: waits for an ArduPilot heartbeat from the configured system id (component 1); reconnects with backoff.
2. **Mission sync** (while disarmed, every `MISSION_SYNC_INTERVAL`s, and once more on arm): full MAVLink mission download
   with per-item retries -> content hash -> queued upload. Same hash = same mission (no duplicates).
3. **Arm** starts a *flight* (UUID, persisted locally) tagged with the mission on the vehicle, and starts telemetry.
4. **Telemetry** (`GLOBAL_POSITION_INT`, `VFR_HUD`, `SYS_STATUS`) is throttled, unit-converted, batched, and persisted to the queue.
5. **Disarm** ends the flight. Outcome: `completed` (AUTO seen and the last navigation item reached), `ended_incomplete`
   (AUTO seen, not finished), or `no_mission_flown` (never in AUTO; mission status untouched).
6. **Log retrieval** after `POST_DISARM_DELAY`: list logs, take the newest, download via `LOG_REQUEST_DATA/LOG_DATA` with gap
   re-requests, require received size == reported size, compute SHA-256, queue upload. Retries with backoff; pending
   downloads survive reboots; a flight left open by a crash is closed and its log fetched on the next start.
7. **Upload worker** drains a persistent SQLite queue (`PENDING -> UPLOADING -> VERIFIED | RETRY_PENDING | FAILED`).
   Network errors/5xx/401/403/409 are retried with backoff forever; 4xx validation errors become `FAILED`.
   A local log file is deleted only after the backend confirms matching size + checksum (`KEEP_LOCAL_LOGS=true` keeps it).

### Duplicate prevention
| Data | Key |
|---|---|
| Mission | `UNIQUE (aircraft_id, mission_hash)`; queue key `mission:<aircraft>:<hash>` |
| Telemetry | Jetson `batch_id` UUID stored in `telemetry_batches` (PK); a repeat returns `duplicate:true` |
| Log | `UNIQUE (aircraft_id, checksum_sha256)`; a verified match is returned as duplicate |

### Association
The Jetson never relies on database ids it might not know yet. Telemetry/logs/status carry `mission_hash` + `flight_id`;
the backend resolves the mission id, or answers `409` (retried later) if the mission upload hasn't landed yet.

## Backend API (all `/api/*` need header `X-API-Key`)
`GET /health` · `POST/GET /api/missions` · `GET /api/missions/:id` · `POST /api/missions/status` ·
`POST/GET /api/telemetry` (filters: `aircraft_id, mission_id, flight_id, from, to, message_type, limit, offset`) ·
`POST /api/logs/upload` (multipart, streamed to a temp file, verified, atomically renamed) · `GET /api/logs` · `GET /api/logs/:id/download`

## Changes from the written spec (deliberate)
- Added `telemetry_batches`, `flight_id`, `batch_id`, `waypoints.raw_item` (lossless x/y/z), `missions.item_count/status_at`,
  `flight_logs.flight_id/log_number`, `UNIQUE` constraints, and `POST /api/missions/status` - needed for idempotent retries and association.
- `mission_hash` is required. Non-positional mission items (e.g. `DO_SET_SERVO`, frame `MISSION`) store null lat/lon/alt.
- Extra files: `config/env.js`, `middleware/auth.middleware.js`, `utils/validate.js`, `local_db.py`, `scripts/*`, `uav-agent.service`.
- Telemetry is only collected while armed. Timestamps are Jetson UTC at receipt (keep the clock synced with chrony/GPS); the
  autopilot's `time_boot_ms` is kept in `raw_data`. GPS 0,0 is stored as null. `VFR_HUD.alt` is only kept in `raw_data` (reference unclear).

## Code-review fixes (found by reproducing each bug first, then verifying the fix)
| # | Bug | Fix |
|---|---|---|
| 1 | Mission hash included item 0, which on ArduPilot is the *home* position and moves with GPS/arm, so the same plan became a "new mission" after home changes | Hash excludes item 0; home-only = no mission |
| 2 | Log chosen as "newest", so an older flight's retried download could take a newer flight's log; an autopilot with no valid clock had every log rejected | Log matched by arm/disarm time window; no-clock fallback only for the most recent flight, otherwise refuses to guess |
| 3 | A single NaN/inf in telemetry made JSON encoding fail and silently dropped the whole batch | Non-finite values stored as null |
| 4 | Agent restart mid-flight created a second flight and left an orphan | Open flight is resumed (progress persisted); older orphans closed |
| 5 | Duplicate log left a surplus copy on the Jetson disk; log growing during download wasn't detected; no disk-space check | Surplus removed; size re-checked after download; free-space guard |
| 6 | Two simultaneous uploads of the same log could orphan a file | Finalisation only if the request still owns the row, else 409 retry |
| 7 | Databases created from the original spec lacked new columns/unique indexes, breaking the API | `schema.sql` now upgrades old databases (idempotent) |
| 8 | Agent sent no heartbeat | 1 Hz companion heartbeat (does not affect flight) |

## Extra fixes from chaos testing
| Bug | Fix |
|---|---|
| Server crash mid-upload left an orphan temp file (only cleaned after 1 h) | All temp files removed at startup |
| Client disconnect mid-upload logged as an "Unhandled error" stack trace | Logged as a one-line warning |
| Re-arming during a log download counted as a failed attempt (long backoff) | Deferred without counting; retried after landing |
| Loss recovery waited for a stall timer (~19 KB/s on a lossy link) | Re-requests holes as soon as a burst ends (~370 KB/s in the simulator) |
| A short flight whose arm-time mission re-sync outlasted it was recorded as "no mission flown" | AUTO is tracked from heartbeats, including during the re-sync |
| Retry backoff after a transient failure was up to 10 min | 20 s to 5 min, capped |

Run the simulator yourself (no hardware): in one terminal `python3 jetson-agent/tools/sim_vehicle.py 8-16,24-27 33-47`
(arm/disarm windows in seconds, then an optional radio-blackout window), and point the agent at it with
`MAVLINK_CONNECTION=udpin:127.0.0.1:14560`. Unit tests: `python3 jetson-agent/tests/test_units.py`.

## Known limitations
- "Completed" is a heuristic (last waypoint-type item reached while in AUTO; RTL is excluded). Validate against your vehicle type/missions.
- Because item 0 (home) is not part of the mission hash, the same plan flown from two different home positions is stored once (with the first home).
- A flight is tagged with the mission on the vehicle at arm time (re-synced on arm). Mode `AUTO` detection uses pymavlink's mode table.
- An interrupted log download restarts from byte 0. Up to `TELEMETRY_BATCH_SECONDS` of telemetry in RAM is lost if the agent crashes.
- Plain HTTP + shared API key: fine on an isolated LAN; use TLS/VPN otherwise. No disk quota management or backups yet (checklist item).
- The Jetson queue grows while the workstation is offline; monitor disk space.

## ArduPilot / hardware checklist (verify, don't assume)
- Telemetry port: `SERIALn_PROTOCOL=2` (MAVLink2) and a matching `SERIALn_BAUD`; confirm V6X connector/pinout and voltage levels in CUAV docs. TX->RX, RX->TX, common GND, never connect the connector's power pin to Jetson GPIO.
- Logging to the SD card enabled (`LOG_BACKEND_TYPE` includes file) and `LOG_DISARMED=0` so each flight is one log that closes on disarm.
- Log download over a 57600-baud link is slow (~5 KB/s); measure it. A higher baud or USB is far better for large logs.
- `MAVLINK_SOURCE_SYSTEM`/`COMPONENT` default to 254/191; if the autopilot ignores requests, test other values. Mission Planner on the same link may compete for replies - use a separate port.

## Staged bench test
1. `npm test` passes; `curl http://<workstation>:5000/health` works from the Jetson.
2. Agent logs a heartbeat from the real V6X. 3. Upload a mission in Mission Planner -> appears in `/api/missions/<id>` with all items.
4. Props off: arm, switch to AUTO, disarm -> telemetry rows, status update, log downloaded and listed; compare `sha256sum` with the file on disk.
5. Repeat with the workstation off during/after the flight, then power it on; and with a Jetson reboot before upload. Confirm no duplicates.

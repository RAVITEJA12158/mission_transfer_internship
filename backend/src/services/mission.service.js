const { pool, withTransaction } = require('../config/database');
const { HttpError } = require('../middleware/error.middleware');
const v = require('../utils/validate');

const STATUSES = ['received', 'in_progress', 'completed', 'ended_incomplete'];
const MAX_ITEMS = 5000;

async function assertAircraft(client, aircraftId) {
  const r = await client.query('SELECT id FROM aircraft WHERE id = $1', [aircraftId]);
  if (!r.rowCount) throw new HttpError(404, 'aircraft_not_found', `Aircraft ${aircraftId} does not exist`);
}

async function resolveMissionId(client, aircraftId, missionHash) {
  if (!missionHash) return null;
  const r = await client.query('SELECT id FROM missions WHERE aircraft_id = $1 AND mission_hash = $2', [aircraftId, missionHash]);
  if (!r.rowCount) {
    throw new HttpError(409, 'mission_not_found', 'Mission for this hash has not been stored yet; retry after the mission upload');
  }
  return r.rows[0].id;
}

function normalizeItem(raw, i) {
  if (!raw || typeof raw !== 'object') throw v.bad(`items[${i}] must be an object`);
  const p = `items[${i}]`;
  return {
    sequence_number: v.intField(raw.sequence_number, `${p}.sequence_number`, { min: 0, max: 65535 }),
    command: v.intField(raw.command, `${p}.command`, { min: 0, max: 65535 }),
    frame: v.intField(raw.frame, `${p}.frame`, { min: 0, max: 255, nullable: true }),
    param1: v.numField(raw.param1, `${p}.param1`, { nullable: true }),
    param2: v.numField(raw.param2, `${p}.param2`, { nullable: true }),
    param3: v.numField(raw.param3, `${p}.param3`, { nullable: true }),
    param4: v.numField(raw.param4, `${p}.param4`, { nullable: true }),
    latitude: v.numField(raw.latitude, `${p}.latitude`, { min: -90, max: 90, nullable: true }),
    longitude: v.numField(raw.longitude, `${p}.longitude`, { min: -180, max: 180, nullable: true }),
    altitude: v.numField(raw.altitude, `${p}.altitude`, { nullable: true }),
    autocontinue: v.boolField(raw.autocontinue, `${p}.autocontinue`),
    raw_item: v.objectField(raw.raw_item, `${p}.raw_item`),
  };
}

async function createMission(body) {
  body = body || {};
  const aircraftId = v.intField(body.aircraft_id, 'aircraft_id', { min: 1 });
  const missionHash = v.sha256Field(body.mission_hash, 'mission_hash');
  const missionName = v.strField(body.mission_name, 'mission_name', { max: 150, nullable: true }) || `Mission ${missionHash.slice(0, 8)}`;
  if (!Array.isArray(body.items) || body.items.length === 0) throw v.bad('items must be a non-empty array');
  if (body.items.length > MAX_ITEMS) throw v.bad(`items may not exceed ${MAX_ITEMS}`);

  const items = body.items.map(normalizeItem);
  const seqs = items.map((i) => i.sequence_number).sort((a, b) => a - b);
  for (let k = 0; k < seqs.length; k += 1) {
    if (seqs[k] !== k) throw v.bad('sequence_number values must be unique and contiguous starting at 0');
  }

  return withTransaction(async (client) => {
    await assertAircraft(client, aircraftId);
    const ins = await client.query(
      `INSERT INTO missions (aircraft_id, mission_name, mission_hash, item_count)
       VALUES ($1, $2, $3, $4)
       ON CONFLICT (aircraft_id, mission_hash) DO NOTHING
       RETURNING id`,
      [aircraftId, missionName, missionHash, items.length],
    );
    if (!ins.rowCount) {
      const existing = await client.query('SELECT id, status FROM missions WHERE aircraft_id = $1 AND mission_hash = $2', [aircraftId, missionHash]);
      return { id: existing.rows[0].id, status: existing.rows[0].status, duplicate: true, item_count: items.length };
    }
    const missionId = ins.rows[0].id;
    await client.query(
      `INSERT INTO waypoints (mission_id, sequence_number, command, frame, param1, param2, param3, param4,
                              latitude, longitude, altitude, autocontinue, raw_item)
       SELECT $1, x.sequence_number, x.command, x.frame, x.param1, x.param2, x.param3, x.param4,
              x.latitude, x.longitude, x.altitude, x.autocontinue, x.raw_item
       FROM jsonb_to_recordset($2::jsonb) AS x(
         sequence_number int, command int, frame int, param1 float8, param2 float8, param3 float8, param4 float8,
         latitude float8, longitude float8, altitude float8, autocontinue boolean, raw_item jsonb)`,
      [missionId, JSON.stringify(items)],
    );
    return { id: missionId, status: 'received', duplicate: false, item_count: items.length };
  });
}

async function listMissions(query) {
  const limit = Math.min(v.intField(query.limit, 'limit', { min: 1, max: 500, nullable: true }) || 100, 500);
  const offset = v.intField(query.offset, 'offset', { min: 0, nullable: true }) || 0;
  const aircraftId = v.intField(query.aircraft_id, 'aircraft_id', { min: 1, nullable: true });
  const r = await pool.query(
    `SELECT id, aircraft_id, mission_name, status, mission_hash, item_count, created_at, started_at, completed_at
     FROM missions WHERE ($1::bigint IS NULL OR aircraft_id = $1)
     ORDER BY id DESC LIMIT $2 OFFSET $3`,
    [aircraftId, limit, offset],
  );
  return r.rows;
}

async function getMission(idRaw) {
  const id = v.intField(idRaw, 'id', { min: 1 });
  const m = await pool.query('SELECT * FROM missions WHERE id = $1', [id]);
  if (!m.rowCount) throw new HttpError(404, 'mission_not_found', `Mission ${id} not found`);
  const w = await pool.query('SELECT * FROM waypoints WHERE mission_id = $1 ORDER BY sequence_number', [id]);
  return { ...m.rows[0], items: w.rows };
}

async function updateStatus(body) {
  body = body || {};
  const aircraftId = v.intField(body.aircraft_id, 'aircraft_id', { min: 1 });
  const missionHash = v.sha256Field(body.mission_hash, 'mission_hash');
  const status = body.status;
  if (!STATUSES.includes(status)) throw v.bad(`status must be one of ${STATUSES.join(', ')}`);
  const eventTime = v.dateField(body.event_time, 'event_time');
  const startedAt = v.dateField(body.started_at, 'started_at', { nullable: true });
  const completedAt = v.dateField(body.completed_at, 'completed_at', { nullable: true });

  return withTransaction(async (client) => {
    const missionId = await resolveMissionId(client, aircraftId, missionHash);

    const r = await client.query(
      `UPDATE missions SET
              started_at = COALESCE($3::timestamptz, started_at),
              completed_at = COALESCE($4::timestamptz, completed_at),
              status = CASE WHEN status_at IS NULL OR status_at <= $2::timestamptz THEN $1 ELSE status END,
              status_at = CASE WHEN status_at IS NULL OR status_at <= $2::timestamptz THEN $2::timestamptz ELSE status_at END
       WHERE id = $5
       RETURNING status, status_at = $2::timestamptz AS applied`,
      [status, eventTime, startedAt, completedAt, missionId],
    );
    return { id: missionId, applied: r.rows[0].applied, status: r.rows[0].status };
  });
}

module.exports = { createMission, listMissions, getMission, updateStatus, resolveMissionId, assertAircraft };

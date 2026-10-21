const { pool, withTransaction } = require('../config/database');
const v = require('../utils/validate');
const { resolveMissionId, assertAircraft } = require('./mission.service');

const MAX_RECORDS = 2000;

function normalizeRecord(r, i) {
  if (!r || typeof r !== 'object') throw v.bad(`records[${i}] must be an object`);
  const p = `records[${i}]`;
  const ts = v.dateField(r.timestamp, `${p}.timestamp`);
  if (new Date(ts).getTime() > Date.now() + 24 * 3600 * 1000) throw v.bad(`${p}.timestamp is more than a day in the future`);
  return {
    timestamp: ts,
    message_type: v.strField(r.message_type, `${p}.message_type`, { max: 60 }),
    latitude: v.numField(r.latitude, `${p}.latitude`, { min: -90, max: 90, nullable: true }),
    longitude: v.numField(r.longitude, `${p}.longitude`, { min: -180, max: 180, nullable: true }),
    altitude_m: v.numField(r.altitude_m, `${p}.altitude_m`, { min: -1000, max: 100000, nullable: true }),
    relative_altitude_m: v.numField(r.relative_altitude_m, `${p}.relative_altitude_m`, { min: -1000, max: 100000, nullable: true }),
    speed_mps: v.numField(r.speed_mps, `${p}.speed_mps`, { min: 0, max: 1000, nullable: true }),
    heading_deg: v.numField(r.heading_deg, `${p}.heading_deg`, { min: 0, max: 360, nullable: true }),
    battery_voltage: v.numField(r.battery_voltage, `${p}.battery_voltage`, { min: 0, max: 1000, nullable: true }),
    raw_data: v.objectField(r.raw_data, `${p}.raw_data`),
  };
}

async function ingest(body) {
  body = body || {};
  const aircraftId = v.intField(body.aircraft_id, 'aircraft_id', { min: 1 });
  const batchId = v.uuidField(body.batch_id, 'batch_id');
  const flightId = v.uuidField(body.flight_id, 'flight_id', { nullable: true });
  const missionHash = v.sha256Field(body.mission_hash, 'mission_hash', { nullable: true });
  if (!Array.isArray(body.records) || body.records.length === 0) throw v.bad('records must be a non-empty array');
  if (body.records.length > MAX_RECORDS) throw v.bad(`records may not exceed ${MAX_RECORDS}`);
  const records = body.records.map(normalizeRecord);

  return withTransaction(async (client) => {
    await assertAircraft(client, aircraftId);
    const missionId = await resolveMissionId(client, aircraftId, missionHash);

    const b = await client.query(
      `INSERT INTO telemetry_batches (batch_id, aircraft_id, mission_id, flight_id, record_count)
       VALUES ($1, $2, $3, $4, $5) ON CONFLICT (batch_id) DO NOTHING RETURNING batch_id`,
      [batchId, aircraftId, missionId, flightId, records.length],
    );
    if (!b.rowCount) return { batch_id: batchId, duplicate: true, stored: 0 };
    await client.query(
      `INSERT INTO telemetry (aircraft_id, mission_id, flight_id, batch_id, "timestamp", message_type, latitude, longitude,
                              altitude_m, relative_altitude_m, speed_mps, heading_deg, battery_voltage, raw_data)
       SELECT $1, $2, $3, $4, x."timestamp", x.message_type, x.latitude, x.longitude, x.altitude_m,
              x.relative_altitude_m, x.speed_mps, x.heading_deg, x.battery_voltage, x.raw_data
       FROM jsonb_to_recordset($5::jsonb) AS x(
         "timestamp" timestamptz, message_type text, latitude float8, longitude float8, altitude_m float8,
         relative_altitude_m float8, speed_mps float8, heading_deg float8, battery_voltage float8, raw_data jsonb)`,
      [aircraftId, missionId, flightId, batchId, JSON.stringify(records)],
    );
    return { batch_id: batchId, duplicate: false, stored: records.length };
  });
}

async function query(q) {
  const aircraftId = v.intField(q.aircraft_id, 'aircraft_id', { min: 1, nullable: true });
  const missionId = v.intField(q.mission_id, 'mission_id', { min: 1, nullable: true });
  const flightId = v.uuidField(q.flight_id, 'flight_id', { nullable: true });
  const from = v.dateField(q.from, 'from', { nullable: true });
  const to = v.dateField(q.to, 'to', { nullable: true });
  const messageType = v.strField(q.message_type, 'message_type', { max: 60, nullable: true });
  const limit = Math.min(v.intField(q.limit, 'limit', { min: 1, max: 10000, nullable: true }) || 1000, 10000);
  const offset = v.intField(q.offset, 'offset', { min: 0, nullable: true }) || 0;
  const r = await pool.query(
    `SELECT id, aircraft_id, mission_id, flight_id, batch_id, "timestamp", message_type, latitude, longitude,
            altitude_m, relative_altitude_m, speed_mps, heading_deg, battery_voltage, raw_data
     FROM telemetry
     WHERE ($1::bigint IS NULL OR aircraft_id = $1)
       AND ($2::bigint IS NULL OR mission_id = $2)
       AND ($3::uuid IS NULL OR flight_id = $3)
       AND ($4::timestamptz IS NULL OR "timestamp" >= $4)
       AND ($5::timestamptz IS NULL OR "timestamp" <= $5)
       AND ($6::text IS NULL OR message_type = $6)
     ORDER BY "timestamp", id LIMIT $7 OFFSET $8`,
    [aircraftId, missionId, flightId, from, to, messageType, limit, offset],
  );
  return r.rows;
}

module.exports = { ingest, query };

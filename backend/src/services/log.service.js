const fs = require('fs');
const fsp = require('fs/promises');
const path = require('path');
const crypto = require('crypto');
const env = require('../config/env');
const { pool } = require('../config/database');
const { HttpError } = require('../middleware/error.middleware');
const v = require('../utils/validate');
const { resolveMissionId, assertAircraft } = require('./mission.service');

function hashFile(filePath) {
  return new Promise((resolve, reject) => {
    const h = crypto.createHash('sha256');
    let size = 0;
    fs.createReadStream(filePath)
      .on('data', (c) => { h.update(c); size += c.length; })
      .on('error', reject)
      .on('end', () => resolve({ sha256: h.digest('hex'), size }));
  });
}

async function safeUnlink(p) {
  if (!p) return;
  try { await fsp.unlink(p); } catch (e) { if (e.code !== 'ENOENT') console.error('unlink failed', p, e.message); }
}

function resolveStoragePath(rel) {
  const abs = path.resolve(env.logStorageDir, rel);
  if (!abs.startsWith(env.logStorageDir + path.sep)) throw new HttpError(500, 'bad_storage_path', 'Stored path is invalid');
  return abs;
}

function safeName(name) {
  const base = path.basename(String(name || 'flight.bin')).replace(/[^A-Za-z0-9._-]/g, '_').slice(0, 100);
  return base || 'flight.bin';
}

async function storeUploadedLog(file, body) {
  body = body || {};
  const aircraftId = v.intField(body.aircraft_id, 'aircraft_id', { min: 1 });
  const claimedSha = v.sha256Field(body.checksum_sha256, 'checksum_sha256');
  const claimedSize = v.intField(body.file_size_bytes, 'file_size_bytes', { min: 1 });
  const missionHash = v.sha256Field(body.mission_hash, 'mission_hash', { nullable: true });
  const flightId = v.uuidField(body.flight_id, 'flight_id', { nullable: true });
  const logNumber = v.intField(body.log_number, 'log_number', { min: 0, nullable: true });
  const filename = safeName(body.filename || file.originalname);
  if (!/\.bin$/i.test(filename)) throw new HttpError(415, 'unsupported_file_type', 'Only .bin flight logs are accepted');

  const { sha256, size } = await hashFile(file.path);
  if (size !== claimedSize || sha256 !== claimedSha) {
    throw new HttpError(422, 'integrity_mismatch', 'Received file does not match declared size/checksum', {
      declared: { size: claimedSize, sha256: claimedSha }, received: { size, sha256 },
    });
  }

  await assertAircraft(pool, aircraftId);
  const missionId = await resolveMissionId(pool, aircraftId, missionHash);

  const rel = path.join(`aircraft-${aircraftId}`, `mission-${missionId ?? 'unassigned'}`, `${crypto.randomUUID()}_${filename}`);
  const abs = resolveStoragePath(rel);

  const claim = await pool.query(
    `INSERT INTO flight_logs (aircraft_id, mission_id, flight_id, log_number, filename, storage_path,
                              file_size_bytes, checksum_sha256, status)
     VALUES ($1,$2,$3,$4,$5,$6,$7,$8,'transferring')
     ON CONFLICT (aircraft_id, checksum_sha256) DO UPDATE
       SET status = 'transferring', storage_path = EXCLUDED.storage_path, filename = EXCLUDED.filename,
           mission_id = COALESCE(EXCLUDED.mission_id, flight_logs.mission_id),
           flight_id = COALESCE(EXCLUDED.flight_id, flight_logs.flight_id)
       WHERE flight_logs.status <> 'verified'
     RETURNING id`,
    [aircraftId, missionId, flightId, logNumber, filename, rel, size, sha256],
  );

  if (!claim.rowCount) {
    const existing = await pool.query(
      'SELECT id, file_size_bytes, checksum_sha256 FROM flight_logs WHERE aircraft_id = $1 AND checksum_sha256 = $2',
      [aircraftId, sha256],
    );
    await safeUnlink(file.path);
    const e = existing.rows[0];
    return { id: e.id, status: 'verified', duplicate: true, file_size_bytes: e.file_size_bytes, checksum_sha256: e.checksum_sha256 };
  }

  const id = claim.rows[0].id;
  try {
    await fsp.mkdir(path.dirname(abs), { recursive: true });
    await fsp.rename(file.path, abs);

    const done = await pool.query(
      `UPDATE flight_logs SET status = 'verified', verified_at = NOW()
       WHERE id = $1 AND storage_path = $2 AND status = 'transferring' RETURNING id`,
      [id, rel],
    );
    if (!done.rowCount) {
      await safeUnlink(abs);
      throw new HttpError(409, 'upload_conflict', 'Same log is being stored by another request; retry');
    }
  } catch (err) {
    await safeUnlink(abs);
    await pool.query(`UPDATE flight_logs SET status = 'failed' WHERE id = $1 AND storage_path = $2 AND status = 'transferring'`, [id, rel]).catch(() => {});
    throw err;
  }
  return { id, status: 'verified', duplicate: false, file_size_bytes: size, checksum_sha256: sha256 };
}

async function listLogs(q) {
  const aircraftId = v.intField(q.aircraft_id, 'aircraft_id', { min: 1, nullable: true });
  const missionId = v.intField(q.mission_id, 'mission_id', { min: 1, nullable: true });
  const status = v.strField(q.status, 'status', { max: 30, nullable: true });
  const limit = Math.min(v.intField(q.limit, 'limit', { min: 1, max: 500, nullable: true }) || 100, 500);
  const offset = v.intField(q.offset, 'offset', { min: 0, nullable: true }) || 0;
  const r = await pool.query(
    `SELECT id, aircraft_id, mission_id, flight_id, log_number, filename, file_size_bytes, checksum_sha256, status, created_at, verified_at
     FROM flight_logs
     WHERE ($1::bigint IS NULL OR aircraft_id = $1) AND ($2::bigint IS NULL OR mission_id = $2)
       AND ($3::text IS NULL OR status = $3)
     ORDER BY id DESC LIMIT $4 OFFSET $5`,
    [aircraftId, missionId, status, limit, offset],
  );
  return r.rows;
}

async function getDownload(idRaw) {
  const id = v.intField(idRaw, 'id', { min: 1 });
  const r = await pool.query('SELECT id, filename, storage_path, status FROM flight_logs WHERE id = $1', [id]);
  if (!r.rowCount) throw new HttpError(404, 'log_not_found', `Log ${id} not found`);
  const row = r.rows[0];
  if (row.status !== 'verified') throw new HttpError(409, 'log_not_verified', 'Log is not verified yet');
  const abs = resolveStoragePath(row.storage_path);
  try { await fsp.access(abs, fs.constants.R_OK); } catch (_) { throw new HttpError(404, 'file_missing', 'Log file is missing on disk'); }
  return { abs, filename: row.filename };
}

module.exports = { storeUploadedLog, listLogs, getDownload, safeUnlink };

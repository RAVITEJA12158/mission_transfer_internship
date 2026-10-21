
require('dotenv').config();
const crypto = require('crypto');
const fs = require('fs');
const net = require('net');
const path = require('path');
const { Pool } = require('pg');

const BASE = process.env.BASE_URL || 'http://localhost:5000';
const KEY = process.env.API_KEY;
const STORE = path.resolve(process.env.LOG_STORAGE_DIR || './storage/flight-logs');
const pool = new Pool({ connectionString: process.env.DATABASE_URL });
let failed = 0;
const check = (name, cond, extra) => { console.log(`${cond ? 'PASS' : 'FAIL'}  ${name}${cond ? '' : ' -> ' + JSON.stringify(extra)}`); if (!cond) failed++; };

async function call(method, p, body, headers = {}) {
  const isForm = body instanceof FormData;
  const res = await fetch(BASE + p, {
    method, headers: { 'X-API-Key': KEY, ...(body && !isForm && typeof body !== 'string' ? { 'Content-Type': 'application/json' } : {}), ...headers },
    body: isForm || typeof body === 'string' ? body : body !== undefined ? JSON.stringify(body) : undefined,
  });
  const text = Buffer.from(await res.arrayBuffer());
  let json = null; try { json = JSON.parse(text.toString()); } catch (_) {  }
  return { status: res.status, body: json, raw: text };
}
const sha = (b) => crypto.createHash('sha256').update(b).digest('hex');
const hex = () => crypto.randomBytes(32).toString('hex');
function form(buf, o = {}) {
  const f = new FormData();
  f.append('aircraft_id', String(o.aircraft_id ?? 1)); f.append('checksum_sha256', o.sha || sha(buf));
  f.append('file_size_bytes', String(o.size ?? buf.length)); if (o.filename) f.append('filename', o.filename);
  if (o.mission_hash) f.append('mission_hash', o.mission_hash);
  f.append('file', new Blob([buf]), o.name || 'a.bin');
  return f;
}
const walk = (d) => (fs.existsSync(d) ? fs.readdirSync(d, { withFileTypes: true }).flatMap((e) => (e.isDirectory() ? walk(path.join(d, e.name)) : [path.join(d, e.name)])) : []);
const goodItems = [{ sequence_number: 0, command: 16, frame: 0, latitude: 1, longitude: 2, altitude: 3 }, { sequence_number: 1, command: 16, frame: 3, latitude: 1, longitude: 2, altitude: 3 }];

(async () => {

  const evil = "x'); DROP TABLE missions; --";
  let r = await call('POST', '/api/missions', { aircraft_id: 1, mission_hash: hex(), mission_name: evil, items: goodItems });
  check('injection in mission_name is stored as plain text', r.status === 201, r.body);
  for (const q of [`/api/missions?aircraft_id=${encodeURIComponent("1 OR 1=1")}`, `/api/telemetry?message_type=${encodeURIComponent("' OR '1'='1")}`, `/api/missions/${encodeURIComponent('1;DROP TABLE missions')}`, '/api/logs?status=' + encodeURIComponent("x' OR 1=1--")]) {
    r = await call('GET', q);
    check(`hostile query rejected or empty: ${q.slice(0, 50)}`, r.status === 400 || (r.status === 200 && Array.isArray(r.body) && r.body.length === 0), r.body);
  }
  check('missions table still exists', (await pool.query("SELECT to_regclass('public.missions') AS t")).rows[0].t !== null);

  for (const [name, body] of [['null body', 'null'], ['array body', '[]'], ['broken json', '{"a":'], ['string body', '"x"']]) {
    r = await call('POST', '/api/missions', body, { 'Content-Type': 'application/json' });
    check(`${name} -> 4xx not 500`, r.status >= 400 && r.status < 500, r.status);
  }
  r = await call('POST', '/api/missions', '{"aircraft_id":1,"mission_hash":"' + hex() + '","items":[{"sequence_number":0,"command":16,"param1":1e999}]}', { 'Content-Type': 'application/json' });
  check('1e999 (Infinity) param rejected', r.status === 400, r);
  r = await call('POST', '/api/missions', { aircraft_id: 1, mission_hash: hex(), items: [{ ...goodItems[0], sequence_number: -1 }, goodItems[1]] });
  check('negative sequence rejected', r.status === 400, r);
  r = await call('POST', '/api/missions', { aircraft_id: 1, mission_hash: hex(), items: [goodItems[0], goodItems[0]] });
  check('duplicate sequence numbers rejected', r.status === 400, r);
  r = await call('POST', '/api/missions', { aircraft_id: 999999, mission_hash: hex(), items: goodItems });
  check('unknown aircraft -> 404 (no partial rows)', r.status === 404, r);
  r = await call('POST', '/api/missions', { aircraft_id: 1, mission_hash: hex(), items: Array.from({ length: 5001 }, (_, i) => ({ ...goodItems[0], sequence_number: i })) });
  check('5001 items rejected', r.status === 400, r.status);
  r = await call('POST', '/api/missions', '{"x":"' + 'a'.repeat(11 * 1024 * 1024) + '"}', { 'Content-Type': 'application/json' });
  check('11 MB JSON -> 413', r.status === 413, r.status);
  const base = { aircraft_id: 1, batch_id: crypto.randomUUID(), records: [{ timestamp: '2026-10-18T08:00:00Z', message_type: 'X' }] };
  for (const [name, ts] of [['month 13', '2026-13-45T00:00:00Z'], ['year 275760', '+275760-09-13T00:00:00.000Z'], ['number', 12345], ['far future', '2099-01-01T00:00:00Z']]) {
    r = await call('POST', '/api/telemetry', { ...base, batch_id: crypto.randomUUID(), records: [{ ...base.records[0], timestamp: ts }] });
    check(`timestamp ${name} rejected`, r.status === 400, r);
  }
  r = await call('POST', '/api/telemetry', { ...base, batch_id: crypto.randomUUID(), records: Array.from({ length: 2001 }, () => base.records[0]) });
  check('2001 telemetry records rejected', r.status === 400, r.status);
  r = await call('POST', '/api/telemetry', { ...base, batch_id: 'not-a-uuid' });
  check('bad batch_id rejected', r.status === 400, r);

  const before = new Set(walk(STORE));
  for (const fn of ['../../../../tmp/pwned.bin', '..\\..\\pwned.bin', '/etc/cron.d/x.bin', 'a\u0000b.bin', 'é\u202Efdp.bin', '.bin']) {
    const buf = crypto.randomBytes(300);
    r = await call('POST', '/api/logs/upload', form(buf, { filename: fn }));
    if (r.status === 201) {
      const row = (await pool.query('SELECT storage_path FROM flight_logs WHERE id = $1', [r.body.id])).rows[0];
      const abs = path.resolve(STORE, row.storage_path);
      check(`filename ${JSON.stringify(fn)} stays inside storage`, abs.startsWith(STORE + path.sep) && fs.existsSync(abs), row);
    } else check(`filename ${JSON.stringify(fn)} -> clean 4xx`, r.status >= 400 && r.status < 500, r);
  }
  check('nothing written outside storage', !fs.existsSync('/tmp/pwned.bin') && !fs.existsSync('/etc/cron.d/x.bin'));
  r = await call('POST', '/api/logs/upload', form(Buffer.alloc(0), { size: 0 }));
  check('empty log rejected', r.status >= 400 && r.status < 500, r);
  const noFile = new FormData(); noFile.append('aircraft_id', '1');
  r = await call('POST', '/api/logs/upload', noFile);
  check('multipart without file -> 400', r.status === 400, r);
  r = await call('POST', '/api/logs/upload', '{"json":"not multipart"}', { 'Content-Type': 'application/json' });
  check('JSON sent to upload endpoint -> 400', r.status === 400, r);
  for (const id of ['abc', '-1', '0', '99999999999', '1 OR 1=1']) {
    r = await call('GET', `/api/logs/${encodeURIComponent(id)}/download`);
    check(`download id ${JSON.stringify(id)} -> 400/404`, r.status === 400 || r.status === 404, r.status);
  }

  const mh = hex();
  let rs = await Promise.all(Array.from({ length: 10 }, () => call('POST', '/api/missions', { aircraft_id: 1, mission_hash: mh, items: goodItems })));
  check('10 concurrent identical missions -> 1 row', (await pool.query('SELECT count(*) FROM missions WHERE mission_hash=$1', [mh])).rows[0].count == 1 && rs.every((x) => [200, 201].includes(x.status)), rs.map((x) => x.status));
  check('...and exactly 2 waypoints', (await pool.query('SELECT count(*) FROM waypoints w JOIN missions m ON m.id=w.mission_id WHERE m.mission_hash=$1', [mh])).rows[0].count == 2);

  const bid = crypto.randomUUID();
  rs = await Promise.all(Array.from({ length: 10 }, () => call('POST', '/api/telemetry', { aircraft_id: 1, batch_id: bid, mission_hash: mh, records: [base.records[0], base.records[0]] })));
  check('10 concurrent identical telemetry batches -> 2 rows', (await pool.query('SELECT count(*) FROM telemetry WHERE batch_id=$1', [bid])).rows[0].count == 2, rs.map((x) => x.status));

  const big = crypto.randomBytes(2 * 1024 * 1024); const bsha = sha(big);
  const filesBefore = new Set(walk(STORE));
  rs = await Promise.all(Array.from({ length: 8 }, () => call('POST', '/api/logs/upload', form(big, { mission_hash: mh }))));
  const codes = rs.map((x) => x.status).sort();
  const rows = (await pool.query('SELECT id,status,storage_path FROM flight_logs WHERE checksum_sha256=$1', [bsha])).rows;
  check('8 concurrent identical log uploads -> 1 row', rows.length === 1, rows);
  check('...only 200/201/409 responses (never 500)', rs.every((x) => [200, 201, 409].includes(x.status)), codes);

  for (const x of rs.filter((y) => y.status === 409)) {
    const again = await call('POST', '/api/logs/upload', form(big, { mission_hash: mh }));
    check('...a 409 loser succeeds as duplicate on retry', again.status === 200 && again.body.duplicate, again);
    break;
  }
  const rowAfter = (await pool.query('SELECT status,storage_path FROM flight_logs WHERE checksum_sha256=$1', [bsha])).rows[0];
  check('...row verified and exactly 1 new file on disk (no orphans)', rowAfter.status === 'verified' && walk(STORE).filter((f) => !filesBefore.has(f) && !f.includes('.tmp')).length === 1, rowAfter);
  check('...tmp dir empty', walk(path.join(STORE, '.tmp')).length === 0, walk(path.join(STORE, '.tmp')));

  const rowsBefore = (await pool.query('SELECT count(*) FROM flight_logs')).rows[0].count;
  await new Promise((resolve) => {
    const u = new URL(BASE); const boundary = '----x' + crypto.randomBytes(4).toString('hex');
    const head = `--${boundary}\r\nContent-Disposition: form-data; name="aircraft_id"\r\n\r\n1\r\n--${boundary}\r\nContent-Disposition: form-data; name="file"; filename="a.bin"\r\nContent-Type: application/octet-stream\r\n\r\n`;
    const s = net.connect(+u.port, u.hostname, () => {
      s.write(`POST /api/logs/upload HTTP/1.1\r\nHost: x\r\nX-API-Key: ${KEY}\r\nContent-Type: multipart/form-data; boundary=${boundary}\r\nContent-Length: ${head.length + 5_000_000}\r\n\r\n${head}`);
      s.write(crypto.randomBytes(1_000_000));
      setTimeout(() => { s.destroy(); setTimeout(resolve, 800); }, 300);
    });
    s.on('error', () => {});
  });
  check('aborted upload leaves no DB row', (await pool.query('SELECT count(*) FROM flight_logs')).rows[0].count === rowsBefore);
  check('aborted upload leaves no temp file', walk(path.join(STORE, '.tmp')).length === 0, walk(path.join(STORE, '.tmp')));
  check('server still healthy after abort', (await fetch(BASE + '/health')).status === 200);

  for (const k of ['', 'x', KEY.slice(0, -1), KEY.toUpperCase(), KEY + 'x']) {
    r = await fetch(BASE + '/api/missions', { headers: { 'X-API-Key': k } });
    check(`bad key ${JSON.stringify(k.slice(0, 12))} -> 401`, r.status === 401, r.status);
  }
  r = await fetch(BASE + '/api/missions', { headers: { 'X-API-Key': 'A'.repeat(100000) } }).catch(() => ({ status: 0 }));
  check('100 KB key header refused (431/401/closed)', [401, 431, 0].includes(r.status), r.status);
  r = await fetch(BASE + '/api/logs/upload', { method: 'POST', body: form(big) });
  check('upload without key -> 401 (before reading body)', r.status === 401, r.status);

  console.log(failed ? `\n${failed} FAILED` : '\nAll adversarial checks passed');
  await pool.end();
  process.exit(failed ? 1 : 0);
})().catch((e) => { console.error(e); process.exit(1); });

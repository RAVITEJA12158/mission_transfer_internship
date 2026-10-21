
const crypto = require('crypto');
const BASE = process.env.BASE_URL || 'http://localhost:5000';
const KEY = process.env.API_KEY;
let failed = 0;
const check = (name, cond, extra) => { console.log(`${cond ? 'PASS' : 'FAIL'}  ${name}${cond ? '' : ' -> ' + JSON.stringify(extra)}`); if (!cond) failed++; };

async function call(method, path, body, headers = {}) {
  const res = await fetch(BASE + path, {
    method, headers: { 'X-API-Key': KEY, ...(body && !(body instanceof FormData) ? { 'Content-Type': 'application/json' } : {}), ...headers },
    body: body instanceof FormData ? body : body ? JSON.stringify(body) : undefined,
  });
  const ct = res.headers.get('content-type') || '';
  return { status: res.status, body: ct.includes('json') ? await res.json() : Buffer.from(await res.arrayBuffer()) };
}

function logForm(buf, over = {}) {
  const sha = crypto.createHash('sha256').update(buf).digest('hex');
  const f = new FormData();
  f.append('aircraft_id', '1'); f.append('checksum_sha256', over.sha || sha); f.append('file_size_bytes', String(over.size ?? buf.length));
  f.append('filename', 'log00001.bin'); if (over.mission_hash) f.append('mission_hash', over.mission_hash);
  f.append('file', new Blob([buf]), over.name || 'log00001.bin');
  return { f, sha };
}

(async () => {
  const hash = crypto.randomBytes(32).toString('hex');
  const items = [
    { sequence_number: 0, command: 16, frame: 0, latitude: 17.0, longitude: 82.0, altitude: 10, param1: 0, param2: 0, param3: 0, param4: null, autocontinue: true, raw_item: { x: 170000000 } },
    { sequence_number: 1, command: 183, frame: 2, latitude: null, longitude: null, altitude: null, param1: 5, param2: 1500, autocontinue: true, raw_item: { x: 0, y: 0, z: 0 } },
    { sequence_number: 2, command: 16, frame: 3, latitude: 17.001, longitude: 82.001, altitude: 30, autocontinue: true },
  ];

  check('health open without key', (await fetch(BASE + '/health')).status === 200);
  check('api rejects missing key', (await call('GET', '/api/missions', null, { 'X-API-Key': 'wrong' })).status === 401);

  let r = await call('POST', '/api/missions', { aircraft_id: 1, mission_hash: hash, items });
  check('mission create 201', r.status === 201 && r.body.item_count === 3, r);
  const mid = r.body.id;
  r = await call('POST', '/api/missions', { aircraft_id: 1, mission_hash: hash, items });
  check('mission retry is duplicate, same id', r.status === 200 && r.body.duplicate && r.body.id === mid, r);
  r = await call('GET', `/api/missions/${mid}`);
  check('mission items in order, non-nav command kept', r.body.items.map((i) => i.sequence_number).join() === '0,1,2' && r.body.items[1].command === 183 && r.body.items[1].latitude === null, r.body);

  const badHash = crypto.randomBytes(32).toString('hex');
  r = await call('POST', '/api/missions', { aircraft_id: 1, mission_hash: badHash, items: [...items.slice(0, 2), { ...items[2], latitude: 999 }] });
  check('invalid item rejected', r.status === 400, r);
  r = await call('GET', '/api/missions?aircraft_id=1&limit=500');
  check('no partial mission left behind', !r.body.some((m) => m.mission_hash === badHash), r.body.map((m) => m.mission_hash));
  r = await call('POST', '/api/missions', { aircraft_id: 1, mission_hash: badHash, items: [{ ...items[0], sequence_number: 1 }] });
  check('non-contiguous sequence rejected', r.status === 400, r);

  const batch = crypto.randomUUID(), flight = crypto.randomUUID();
  const tel = { aircraft_id: 1, batch_id: batch, flight_id: flight, mission_hash: hash, records: [
    { timestamp: '2026-10-18T08:00:00.000Z', message_type: 'GLOBAL_POSITION_INT', latitude: 17.0, longitude: 82.0, altitude_m: 35, relative_altitude_m: 30, speed_mps: 4.2, heading_deg: 90, raw_data: { a: 1 } },
    { timestamp: '2026-10-18T08:00:01.000Z', message_type: 'SYS_STATUS', battery_voltage: 15.8 }] };
  r = await call('POST', '/api/telemetry', tel);
  check('telemetry stored', r.status === 201 && r.body.stored === 2, r);
  r = await call('POST', '/api/telemetry', tel);
  check('telemetry retry not duplicated', r.status === 200 && r.body.duplicate === true, r);
  r = await call('GET', `/api/telemetry?mission_id=${mid}&limit=100`);
  check('telemetry query exactly 2 rows', r.body.length === 2 && r.body[0].mission_id === mid, r.body);
  r = await call('POST', '/api/telemetry', { ...tel, batch_id: crypto.randomUUID(), mission_hash: crypto.randomBytes(32).toString('hex') });
  check('telemetry for unknown mission -> 409 (retryable)', r.status === 409, r);
  r = await call('POST', '/api/telemetry', { ...tel, batch_id: crypto.randomUUID(), records: [{ ...tel.records[0], latitude: 123 }] });
  check('telemetry bad latitude rejected', r.status === 400, r);

  r = await call('POST', '/api/missions/status', { aircraft_id: 1, mission_hash: hash, status: 'completed', event_time: '2026-10-18T09:00:00Z', completed_at: '2026-10-18T09:00:00Z' });
  check('status applied', r.status === 200 && r.body.applied, r);
  r = await call('POST', '/api/missions/status', { aircraft_id: 1, mission_hash: hash, status: 'in_progress', event_time: '2026-10-18T08:00:00Z' });
  check('older status event ignored', r.status === 200 && r.body.applied === false, r);

  const buf = crypto.randomBytes(200000);
  let { f, sha } = logForm(buf, { mission_hash: hash });
  r = await call('POST', '/api/logs/upload', f);
  check('log upload verified', r.status === 201 && r.body.status === 'verified' && r.body.checksum_sha256 === sha, r);
  const lid = r.body.id;
  ({ f } = logForm(buf, { mission_hash: hash }));
  r = await call('POST', '/api/logs/upload', f);
  check('log retry is duplicate, same id', r.status === 200 && r.body.duplicate && r.body.id === lid, r);
  r = await call('GET', `/api/logs/${lid}/download`);
  check('download matches upload byte-for-byte', Buffer.isBuffer(r.body) && Buffer.compare(r.body, buf) === 0);
  ({ f } = logForm(crypto.randomBytes(1000), { sha: '0'.repeat(64) }));
  r = await call('POST', '/api/logs/upload', f);
  check('checksum mismatch rejected 422', r.status === 422, r);
  ({ f } = logForm(crypto.randomBytes(1000), { name: 'evil.txt' }));
  r = await call('POST', '/api/logs/upload', f);
  check('non-.bin rejected', r.status === 415, r);
  ({ f } = logForm(crypto.randomBytes(6 * 1024 * 1024)));
  r = await call('POST', '/api/logs/upload', f);
  check('oversize rejected 413', r.status === 413, r);
  ({ f } = logForm(crypto.randomBytes(500), { mission_hash: crypto.randomBytes(32).toString('hex') }));
  r = await call('POST', '/api/logs/upload', f);
  check('log for unknown mission -> 409 (retryable)', r.status === 409, r);
  r = await call('GET', `/api/logs?mission_id=${mid}`);
  check('log listed under mission', r.body.length === 1 && r.body[0].status === 'verified', r.body);

  console.log(failed ? `\n${failed} FAILED` : '\nAll checks passed');
  process.exit(failed ? 1 : 0);
})().catch((e) => { console.error(e); process.exit(1); });

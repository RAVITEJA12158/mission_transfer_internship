const fs = require('fs');
const path = require('path');
const env = require('./config/env');
const { pool } = require('./config/database');
const app = require('./app');

function prepareStorage() {
  fs.mkdirSync(env.tmpDir, { recursive: true });

  let removed = 0;
  for (const name of fs.readdirSync(env.tmpDir)) {
    try { fs.unlinkSync(path.join(env.tmpDir, name)); removed += 1; } catch (_) {  }
  }
  if (removed) console.warn(`Removed ${removed} leftover partial upload(s) from a previous run`);
}

async function main() {
  prepareStorage();
  await pool.query('SELECT 1');
  const server = app.listen(env.port, '0.0.0.0', () => console.log(`UAV backend listening on :${env.port}`));

  server.requestTimeout = 0;
  server.headersTimeout = 65000;
  const shutdown = () => server.close(() => pool.end().finally(() => process.exit(0)));
  process.on('SIGINT', shutdown);
  process.on('SIGTERM', shutdown);
}

main().catch((e) => { console.error('Startup failed:', e.message); process.exit(1); });

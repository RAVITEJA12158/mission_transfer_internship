const { Pool, types } = require('pg');
const env = require('./env');

types.setTypeParser(20, (v) => parseInt(v, 10));

const pool = new Pool({ connectionString: env.databaseUrl, max: 10 });
pool.on('error', (err) => console.error('Unexpected PostgreSQL pool error:', err.message));

async function withTransaction(fn) {
  const client = await pool.connect();
  try {
    await client.query('BEGIN');
    const result = await fn(client);
    await client.query('COMMIT');
    return result;
  } catch (err) {
    try { await client.query('ROLLBACK'); } catch (_) {  }
    throw err;
  } finally {
    client.release();
  }
}

module.exports = { pool, withTransaction };

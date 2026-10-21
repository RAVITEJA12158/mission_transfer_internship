require('dotenv').config();
const path = require('path');

function required(name) {
  const v = process.env[name];
  if (!v) throw new Error(`Missing required environment variable ${name}`);
  return v;
}

const apiKey = required('API_KEY');
if (apiKey.length < 16) throw new Error('API_KEY must be at least 16 characters');

const logStorageDir = path.resolve(process.env.LOG_STORAGE_DIR || './storage/flight-logs');
const port = Number(process.env.PORT || '5000');
const maxLogMb = Number(process.env.MAX_LOG_MB || '512');

if (!Number.isInteger(port) || port < 1 || port > 65535) throw new Error('PORT must be an integer from 1 to 65535');
if (!Number.isInteger(maxLogMb) || maxLogMb < 1) throw new Error('MAX_LOG_MB must be a positive integer');

module.exports = {
  port,
  databaseUrl: required('DATABASE_URL'),
  apiKey,
  logStorageDir,
  tmpDir: path.join(logStorageDir, '.tmp'),
  maxLogBytes: maxLogMb * 1024 * 1024,
  corsOrigins: process.env.CORS_ORIGIN ? process.env.CORS_ORIGIN.split(',').map((s) => s.trim()) : null,
};

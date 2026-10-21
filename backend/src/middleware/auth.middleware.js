const crypto = require('crypto');
const env = require('../config/env');

const expected = crypto.createHash('sha256').update(env.apiKey).digest();

module.exports = function requireApiKey(req, res, next) {
  const provided = crypto.createHash('sha256').update(req.get('x-api-key') || '').digest();
  if (crypto.timingSafeEqual(provided, expected)) return next();
  return res.status(401).json({ error: 'unauthorized', message: 'Missing or invalid API key' });
};

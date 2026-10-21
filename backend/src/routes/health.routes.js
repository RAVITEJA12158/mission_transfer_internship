const router = require('express').Router();
const { pool } = require('../config/database');

router.get('/', async (req, res) => {
  try {
    await pool.query('SELECT 1');
    res.json({ status: 'ok', database: 'up', time: new Date().toISOString() });
  } catch (e) {
    res.status(503).json({ status: 'degraded', database: 'down' });
  }
});

module.exports = router;

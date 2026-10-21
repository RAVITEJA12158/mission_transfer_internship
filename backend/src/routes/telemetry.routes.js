const router = require('express').Router();
const c = require('../controllers/telemetry.controller');
const { asyncHandler } = require('../middleware/error.middleware');

router.post('/', asyncHandler(c.ingest));
router.get('/', asyncHandler(c.query));

module.exports = router;

const router = require('express').Router();
const c = require('../controllers/mission.controller');
const { asyncHandler } = require('../middleware/error.middleware');

router.post('/', asyncHandler(c.create));
router.post('/status', asyncHandler(c.updateStatus));
router.get('/', asyncHandler(c.list));
router.get('/:id', asyncHandler(c.get));

module.exports = router;

const crypto = require('crypto');
const router = require('express').Router();
const multer = require('multer');
const env = require('../config/env');
const c = require('../controllers/log.controller');
const { asyncHandler, HttpError } = require('../middleware/error.middleware');

const upload = multer({
  storage: multer.diskStorage({
    destination: (req, file, cb) => cb(null, env.tmpDir),
    filename: (req, file, cb) => cb(null, `${crypto.randomUUID()}.part`),
  }),
  limits: { fileSize: env.maxLogBytes, files: 1 },
  fileFilter: (req, file, cb) => {
    if (!/\.bin$/i.test(file.originalname || '')) {
      return cb(new HttpError(415, 'unsupported_file_type', 'Only .bin flight logs are accepted'));
    }
    return cb(null, true);
  },
});

router.post('/upload', upload.single('file'), asyncHandler(c.upload));
router.get('/', asyncHandler(c.list));
router.get('/:id/download', asyncHandler(c.download));

module.exports = router;

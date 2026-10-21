class HttpError extends Error {
  constructor(status, code, message, details) {
    super(message || code);
    this.status = status;
    this.code = code;
    this.details = details;
  }
}

const asyncHandler = (fn) => (req, res, next) => Promise.resolve(fn(req, res, next)).catch(next);

function notFound(req, res) {
  res.status(404).json({ error: 'not_found', message: `${req.method} ${req.originalUrl} not found` });
}

function errorHandler(err, req, res, next) {
  if (res.headersSent) return next(err);
  if (err instanceof HttpError) {
    return res.status(err.status).json({ error: err.code, message: err.message, details: err.details });
  }

  if (err && (err.message === 'Request aborted' || err.code === 'ECONNRESET' || req.aborted)) {
    console.warn(`Upload aborted by client: ${req.method} ${req.originalUrl}`);
    return undefined;
  }
  if (err && err.name === 'MulterError') {
    const status = err.code === 'LIMIT_FILE_SIZE' ? 413 : 400;
    return res.status(status).json({ error: 'upload_error', message: err.message });
  }
  if (err && err.type === 'entity.too.large') {
    return res.status(413).json({ error: 'payload_too_large', message: 'Request body too large' });
  }
  if (err && err.type === 'entity.parse.failed') {
    return res.status(400).json({ error: 'invalid_json', message: 'Request body is not valid JSON' });
  }

  if (err && ['22P02', '22007', '22008', '22003'].includes(err.code)) {
    return res.status(400).json({ error: 'invalid_input', message: 'A value had an invalid format' });
  }
  console.error('Unhandled error:', err);
  return res.status(500).json({ error: 'internal_error', message: 'Internal server error' });
}

module.exports = { HttpError, asyncHandler, notFound, errorHandler };

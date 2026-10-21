const svc = require('../services/log.service');
const { HttpError } = require('../middleware/error.middleware');

exports.upload = async (req, res) => {
  const file = req.file;
  try {
    if (!file) throw new HttpError(400, 'file_required', 'Multipart field "file" is required');
    const r = await svc.storeUploadedLog(file, req.body);
    res.status(r.duplicate ? 200 : 201).json(r);
  } finally {

    if (file) await svc.safeUnlink(file.path);
  }
};
exports.list = async (req, res) => res.json(await svc.listLogs(req.query));
exports.download = async (req, res, next) => {
  const { abs, filename } = await svc.getDownload(req.params.id);
  res.download(abs, filename, (err) => { if (err && !res.headersSent) next(err); });
};

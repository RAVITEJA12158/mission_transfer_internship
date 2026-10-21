const svc = require('../services/telemetry.service');

exports.ingest = async (req, res) => {
  const r = await svc.ingest(req.body);
  res.status(r.duplicate ? 200 : 201).json(r);
};
exports.query = async (req, res) => res.json(await svc.query(req.query));

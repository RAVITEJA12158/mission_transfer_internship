const svc = require('../services/mission.service');

exports.create = async (req, res) => {
  const r = await svc.createMission(req.body);
  res.status(r.duplicate ? 200 : 201).json(r);
};
exports.list = async (req, res) => res.json(await svc.listMissions(req.query));
exports.get = async (req, res) => res.json(await svc.getMission(req.params.id));
exports.updateStatus = async (req, res) => res.json(await svc.updateStatus(req.body));

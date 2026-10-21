"""HTTP client for the workstation backend with transient/permanent error classification."""
import logging
import os

import requests
from requests_toolbelt.multipart.encoder import MultipartEncoder

log = logging.getLogger("api")

class TransientError(Exception):
    """Worth retrying later (network down, 5xx, auth misconfig, dependency not stored yet)."""

    def __init__(self, msg, network=False):
        super().__init__(msg)
        self.network = network

class PermanentError(Exception):
    """The backend rejected the payload; retrying the same request will not help."""

_TRANSIENT_STATUS = {401, 403, 408, 409, 425, 429}

class ApiClient:
    def __init__(self, cfg):
        self.cfg = cfg
        self.base = cfg.backend_url
        self.session = requests.Session()
        self.session.headers.update({"X-API-Key": cfg.api_key})

    def _request(self, method, path, **kwargs):
        kwargs.setdefault("timeout", self.cfg.http_timeout)
        try:
            resp = self.session.request(method, self.base + path, **kwargs)
        except (requests.ConnectionError, requests.Timeout) as exc:
            raise TransientError(f"network error: {exc.__class__.__name__}", network=True) from exc
        except requests.RequestException as exc:
            raise TransientError(f"request error: {exc}") from exc
        if resp.ok:
            try:
                return resp.json()
            except ValueError as exc:
                raise TransientError("backend returned non-JSON success response") from exc
        snippet = resp.text[:300].replace("\n", " ")
        msg = f"HTTP {resp.status_code}: {snippet}"
        if resp.status_code in _TRANSIENT_STATUS or resp.status_code >= 500:
            raise TransientError(msg)
        raise PermanentError(msg)

    def health(self):
        return self._request("GET", "/health")

    def post_mission(self, payload):
        return self._request("POST", "/api/missions", json=payload)

    def post_mission_status(self, payload):
        return self._request("POST", "/api/missions/status", json=payload)

    def post_telemetry(self, payload):
        return self._request("POST", "/api/telemetry", json=payload)

    def upload_log(self, file_path, meta):
        if not os.path.isfile(file_path):
            raise PermanentError(f"local log file missing: {file_path}")
        fields = {
            "aircraft_id": str(meta["aircraft_id"]),
            "checksum_sha256": meta["checksum_sha256"],
            "file_size_bytes": str(meta["file_size_bytes"]),
            "filename": meta["filename"],
        }
        for key in ("mission_hash", "flight_id", "log_number"):
            if meta.get(key) is not None:
                fields[key] = str(meta[key])
        with open(file_path, "rb") as fh:
            fields["file"] = (meta["filename"], fh, "application/octet-stream")
            encoder = MultipartEncoder(fields=fields)
            return self._request("POST", "/api/logs/upload", data=encoder,
                                 headers={"Content-Type": encoder.content_type},
                                 timeout=(10, 300))

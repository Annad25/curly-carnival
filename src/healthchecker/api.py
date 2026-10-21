"""Minimal HTTP API so schedulers, CI, chat-ops bots or other agents can trigger checks.

  GET  /healthz                         -> {"ok": true}
  POST /check  {"url": "https://..."}   -> full JSON report
  Authorization: Bearer $HEALTH_API_TOKEN   (required)

Only hosts listed in the config (or HEALTH_ALLOWED_HOSTS) can be checked, so the
API cannot be abused as an open proxy / SSRF gadget against internal networks.
"""
from __future__ import annotations

import asyncio
import hmac
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

from .config import load_config, site_urls
from .runner import run_health_check


def serve(host: str, port: int, config_path: str | None):
    token = os.environ.get("HEALTH_API_TOKEN")
    if not token:
        raise SystemExit("HEALTH_API_TOKEN must be set to run the API")
    allowed = {urlparse(s["url"]).netloc for s in site_urls(config_path)}
    allowed |= {h.strip() for h in os.environ.get("HEALTH_ALLOWED_HOSTS", "").split(",") if h.strip()}

    class H(BaseHTTPRequestHandler):
        def _json(self, code, obj):
            body = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path == "/healthz":
                return self._json(200, {"ok": True})
            self._json(404, {"error": "not found"})

        def do_POST(self):
            auth = self.headers.get("Authorization", "")
            if not hmac.compare_digest(auth, f"Bearer {token}"):
                return self._json(401, {"error": "unauthorized"})
            if self.path != "/check":
                return self._json(404, {"error": "not found"})
            try:
                req = json.loads(self.rfile.read(min(int(self.headers.get("Content-Length", 0)), 10_000)))
                url = req["url"]
            except (ValueError, KeyError, TypeError):
                return self._json(400, {"error": "body must be {\"url\": ...}"})
            u = urlparse(url)
            if u.scheme not in ("http", "https") or u.netloc not in allowed:
                return self._json(403, {"error": f"host not in allowlist: {u.netloc}"})
            cfg = load_config(config_path, url, {"llm": {"enabled": bool(req.get("llm", True))}})
            report = asyncio.run(run_health_check(url, cfg))
            report.pop("request_log", None)
            self._json(200, report)

    print(f"health API on http://{host}:{port}  allowlist={sorted(allowed)}")
    ThreadingHTTPServer((host, port), H).serve_forever()

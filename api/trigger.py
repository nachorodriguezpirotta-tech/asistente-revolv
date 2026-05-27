"""
GET /api/trigger?t=<token>

Endpoint para que cron-job.org dispare el workflow scan-incremental de GHA.
Booster cuando el cron */5 de GHA skipea (latencia 1h+).

Token: HMAC(DASHBOARD_SECRET, "trigger")[:16]. Reusa el secret que ya está
sincronizado entre GH Actions y Vercel — sin env var nueva.
"""

import hmac
import hashlib
import json
import os
import secrets
import urllib.request
import urllib.error
from http.server import BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs


_env_secret = os.environ.get("DASHBOARD_SECRET", "").strip()
if _env_secret:
    DASHBOARD_SECRET = _env_secret
else:
    DASHBOARD_SECRET = "ephemeral-" + secrets.token_urlsafe(24)

GITHUB_OWNER = os.environ.get("GITHUB_OWNER", "nachorodriguezpirotta-tech")
GITHUB_REPO = os.environ.get("GITHUB_REPO", "asistente-revolv")
GITHUB_BRANCH = os.environ.get("GITHUB_BRANCH", "main")
GITHUB_PAT = os.environ.get("GITHUB_PAT", "")
WORKFLOW_FILE = os.environ.get("TRIGGER_WORKFLOW", "scan-incremental.yml")


def _make_token(name: str) -> str:
    return hmac.new(
        DASHBOARD_SECRET.encode(),
        name.lower().encode(),
        hashlib.sha256,
    ).hexdigest()[:16]


def _check_token(name: str, token: str) -> bool:
    if not name or not token:
        return False
    return hmac.compare_digest(_make_token(name), token)


def _dispatch_workflow():
    if not GITHUB_PAT:
        return False, "GITHUB_PAT no configurado"
    url = (f"https://api.github.com/repos/{GITHUB_OWNER}/{GITHUB_REPO}"
           f"/actions/workflows/{WORKFLOW_FILE}/dispatches")
    body = json.dumps({"ref": GITHUB_BRANCH}).encode()
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("Authorization", f"Bearer {GITHUB_PAT}")
    req.add_header("X-GitHub-Api-Version", "2022-11-28")
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            if resp.status in (200, 201, 204):
                return True, f"triggered {WORKFLOW_FILE}"
            return False, f"GitHub API status {resp.status}"
    except urllib.error.HTTPError as e:
        return False, f"HTTP {e.code}: {e.read()[:200].decode('utf-8', 'replace')}"
    except Exception as e:
        return False, f"{type(e).__name__}: {str(e)[:200]}"


def _json(handler, payload, status=200):
    body = json.dumps(payload).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("Cache-Control", "no-store")
    handler.send_header("Access-Control-Allow-Origin", "*")
    handler.end_headers()
    handler.wfile.write(body)


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        try:
            # Vercel Cron envía header x-vercel-cron automáticamente.
            # Si viene de Vercel Cron, no necesita token externo.
            is_vercel_cron = self.headers.get("x-vercel-cron") is not None
            if not is_vercel_cron:
                params = parse_qs(urlparse(self.path).query)
                token = (params.get("t", [""])[0] or "").strip()
                if not _check_token("TRIGGER", token):
                    return _json(self, {"error": "unauthorized"}, status=401)
            ok, msg = _dispatch_workflow()
            if ok:
                return _json(self, {"ok": True, "msg": msg})
            return _json(self, {"ok": False, "error": msg}, status=502)
        except Exception as e:
            return _json(self, {"error": str(e)[:200]}, status=500)

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        self.end_headers()

    def log_message(self, *a, **kw):
        pass

"""
GET /api/trigger?secret=XXX

Endpoint para que un servicio externo (cron-job.org) dispare el workflow
scan-incremental de GitHub Actions. Es un "booster" — GHA cron de */5 es
inconfiable (gaps de 1h+), así que cron-job.org pingea esto cada 1-2 min
y garantiza latencia baja para detectar videos nuevos.

Auth: secret en query string (?secret=). Comparado contra TRIGGER_SECRET
env var. Si no matchea → 401.

Trigger: workflow_dispatch via GitHub API usando GITHUB_PAT (ya configurado
para otras cosas en Vercel env vars).

Idempotente: si scan-incremental ya está corriendo, el concurrency group
'scan-drive' del workflow garantiza que no se dispare en paralelo.
"""

import json
import os
import sys
import traceback
import hmac
import urllib.request
import urllib.error
from http.server import BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

try:
    from _shared import json_response, GITHUB_OWNER, GITHUB_REPO, GITHUB_BRANCH, GITHUB_PAT
    _IMPORT_ERROR = None
except Exception as _e:
    _IMPORT_ERROR = f"{type(_e).__name__}: {_e}\n{traceback.format_exc()}"


TRIGGER_SECRET = os.environ.get("TRIGGER_SECRET", "").strip()
WORKFLOW_FILE = os.environ.get("TRIGGER_WORKFLOW", "scan-incremental.yml")


def _dispatch_workflow() -> tuple[bool, str]:
    """Llama a workflow_dispatch via GitHub API. Devuelve (ok, message)."""
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
            # workflow_dispatch returns 204 No Content on success
            if resp.status in (200, 201, 204):
                return True, f"triggered {WORKFLOW_FILE}"
            return False, f"GitHub API status {resp.status}"
    except urllib.error.HTTPError as e:
        return False, f"HTTP {e.code}: {e.read()[:200].decode('utf-8', 'replace')}"
    except Exception as e:
        return False, f"{type(e).__name__}: {str(e)[:200]}"


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        try:
            if _IMPORT_ERROR:
                return json_response(self, {"error": "import", "detail": _IMPORT_ERROR}, status=500)

            params = parse_qs(urlparse(self.path).query)
            secret = (params.get("secret", [""])[0] or "").strip()

            if not TRIGGER_SECRET:
                return json_response(self, {"error": "TRIGGER_SECRET no configurado en server"}, status=500)
            if not secret or not hmac.compare_digest(secret, TRIGGER_SECRET):
                return json_response(self, {"error": "unauthorized"}, status=401)

            ok, msg = _dispatch_workflow()
            if ok:
                return json_response(self, {"ok": True, "msg": msg})
            return json_response(self, {"ok": False, "error": msg}, status=502)

        except Exception as e:
            return json_response(self, {"error": str(e)[:200], "trace": traceback.format_exc()[:500]}, status=500)

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        self.end_headers()

    def log_message(self, *a, **kw): pass

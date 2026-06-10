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

# View de la Mesa (Win Securities) — repo aparte, token propio
WINVIEW_REPO = os.environ.get("WINVIEW_REPO", "win-view-mesa")
WINVIEW_WORKFLOW = os.environ.get("WINVIEW_WORKFLOW", "win-view.yml")
WINVIEW_GH_PAT = os.environ.get("WINVIEW_GH_PAT", "")


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


def _dispatch_one(workflow_file, repo=None, pat=None):
    pat = pat or GITHUB_PAT
    if not pat:
        return False, "GITHUB_PAT no configurado"
    url = (f"https://api.github.com/repos/{GITHUB_OWNER}/{repo or GITHUB_REPO}"
           f"/actions/workflows/{workflow_file}/dispatches")
    body = json.dumps({"ref": GITHUB_BRANCH}).encode()
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("Authorization", f"Bearer {pat}")
    req.add_header("X-GitHub-Api-Version", "2022-11-28")
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            if resp.status in (200, 201, 204):
                return True, f"triggered {workflow_file}"
            return False, f"GitHub API status {resp.status}"
    except urllib.error.HTTPError as e:
        return False, f"HTTP {e.code}: {e.read()[:200].decode('utf-8', 'replace')}"
    except Exception as e:
        return False, f"{type(e).__name__}: {str(e)[:200]}"


def _dispatch_workflow(force_winview=False):
    # Siempre disparar el scan incremental (rápido, Drive Changes API).
    ok, msg = _dispatch_one(WORKFLOW_FILE)
    # Cada ~6 min disparar TAMBIÉN el audit-recover (backup que NO depende de
    # Drive Changes — usa files.list directo, detecta lo que el incremental
    # pierde, ej. editados en subcarpetas profundas tipo pack mayo/Editados).
    # El cron de GHA del audit es inconfiable (corre cada 1-2h), así que lo
    # disparamos nosotros. Bug 09/jun: editados de Lili en pack mayo/Editados
    # no se detectaban por días. Usamos el minuto del reloj para espaciar.
    try:
        from datetime import datetime, timezone
        minute = datetime.now(timezone.utc).minute
        # in (0,1) tolera que el ping caiga en minutos pares o impares:
        # como cron-job.org pinguea siempre con la misma paridad, esto
        # dispara el audit ~cada 6 min sin doble-disparo.
        if minute % 6 in (0, 1):
            ok2, msg2 = _dispatch_one("audit-recover.yml")
            msg = f"{msg} + {msg2}"
        # Scan COMPLETO 1×/hora (minuto 4-5): es la única red que recorre las
        # carpetas DIRECTO (por parent), inmune al lag del índice de búsqueda
        # de Drive. Bug 09/jun Lili pack mayo: los editados que subía Rami no
        # aparecían ni en Changes API ni en files.list global por 30-60 min
        # (indexación), así que ni el incremental ni el audit los veían. El
        # cron de GHA del scan completo es errático (gaps de 1.5-2h) — esto
        # lo hace confiable. Duplicados: cubiertos por lock + Turso dedupe.
        if minute in (4, 5):
            ok3, msg3 = _dispatch_one("scan.yml")
            msg = f"{msg} + {msg3}"
        # View de la Mesa (Win Securities): mail L/M/V ~12:00 ART. Disparo
        # puntual 11:59 ART (14:59 UTC) — el cron de GHA en repos privados se
        # atrasa horas; este endpoint pinguea cada minuto y es puntual.
        # Ventana 14:59-15:01 por si el ping saltea un minuto: los duplicados
        # los mata el guard anti-duplicado del workflow win-view (skip si ya
        # hubo corrida exitosa/en curso hoy).
        now = datetime.now(timezone.utc)
        in_window = (now.weekday() in (0, 2, 4)
                     and ((now.hour == 14 and now.minute == 59)
                          or (now.hour == 15 and now.minute <= 1)))
        if force_winview or in_window:
            ok4, msg4 = _dispatch_one(WINVIEW_WORKFLOW, repo=WINVIEW_REPO,
                                      pat=WINVIEW_GH_PAT or GITHUB_PAT)
            msg = f"{msg} + winview:{msg4}"
        # Reminders de editores atrasados: 1×/día PUNTUAL a las 18:00 UTC
        # (15:00 ART). El cron de GHA atrasa horas; esto los hace salir a
        # horario y SEPARADOS del daily summary de la mañana (pedido 10/jun).
        # reminders.py tiene throttle por editor → no duplica con el cron.
        hour = datetime.now(timezone.utc).hour
        if hour == 18 and minute in (0, 1):
            ok4, msg4 = _dispatch_one("reminders.yml")
            msg = f"{msg} + {msg4}"
    except Exception:
        pass
    return ok, msg


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
            params = parse_qs(urlparse(self.path).query)
            if not is_vercel_cron:
                token = (params.get("t", [""])[0] or "").strip()
                if not _check_token("TRIGGER", token):
                    return _json(self, {"error": "unauthorized"}, status=401)
            # ?winview=1 fuerza el disparo del View de la Mesa (para tests)
            force_winview = (params.get("winview", ["0"])[0] or "0") == "1"
            ok, msg = _dispatch_workflow(force_winview=force_winview)
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

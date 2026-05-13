"""
POST /api/push?action=subscribe — guarda una suscripción de push notifications
POST /api/push?action=unsubscribe — borra una suscripción
GET  /api/push?action=vapid-key   — devuelve la public key VAPID (público, sin auth)

Body para subscribe:
  {
    editor: "Rami" o null (admin),
    t: <token>,
    subscription: { endpoint, keys: { p256dh, auth } }
  }
"""

import json
import os
import sys
import traceback
from http.server import BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

try:
    from _shared import check_token, json_response, with_db
    _IMPORT_ERROR = None
except Exception as _e:
    _IMPORT_ERROR = f"{type(_e).__name__}: {_e}\n{traceback.format_exc()}"

# Public key hardcodeada (no es secreto, va al frontend)
VAPID_PUBLIC_KEY = "BMtsyOvrEP8Il9DTF4ssU_SdAVOBQT9xZGrtJjPfKSeuSewpCUItgVo07j7DWwFKnlkQUQfpfAByA6bBkMqB1C8"


class handler(BaseHTTPRequestHandler):

    def do_GET(self):
        params = parse_qs(urlparse(self.path).query)
        action = params.get("action", [""])[0]
        if action == "vapid-key":
            return json_response(self, {"key": VAPID_PUBLIC_KEY})
        return json_response(self, {"error": "action inválida"}, status=400)

    def do_POST(self):
        try:
            if _IMPORT_ERROR:
                return json_response(self, {"error": "import", "detail": _IMPORT_ERROR}, status=500)
            length = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(length).decode("utf-8") if length > 0 else "{}"
            body = json.loads(raw)
            params = parse_qs(urlparse(self.path).query)
            action = params.get("action", [""])[0] or body.get("action", "")

            token = (body.get("t") or "").strip()
            editor = (body.get("editor") or "").strip() or None
            is_admin = body.get("admin") == 1

            # Auth
            if is_admin:
                if not check_token("ADMIN", token):
                    return json_response(self, {"error": "unauthorized admin"}, status=401)
                editor = None  # admin guarda sub sin editor
            else:
                if not editor or not check_token(editor, token):
                    return json_response(self, {"error": "unauthorized"}, status=401)

            if action == "subscribe":
                sub = body.get("subscription") or {}
                endpoint = sub.get("endpoint")
                keys = sub.get("keys") or {}
                p256dh = keys.get("p256dh")
                auth = keys.get("auth")
                if not endpoint or not p256dh or not auth:
                    return json_response(self, {"error": "subscription incompleta"}, status=400)

                def op(conn):
                    from datetime import datetime
                    now = datetime.now().isoformat(timespec="seconds")
                    conn.execute("""
                        INSERT INTO push_subscriptions (editor, endpoint, p256dh, auth, created_at)
                        VALUES (?, ?, ?, ?, ?)
                        ON CONFLICT(endpoint) DO UPDATE SET
                            editor=excluded.editor, p256dh=excluded.p256dh, auth=excluded.auth,
                            last_used_at=excluded.created_at, failed_count=0
                    """, (editor, endpoint, p256dh, auth, now))

                with_db(op, message=f"push: subscribe {editor or 'admin'}")
                return json_response(self, {"ok": True, "editor": editor})

            if action == "unsubscribe":
                endpoint = body.get("endpoint")
                if not endpoint:
                    return json_response(self, {"error": "falta endpoint"}, status=400)

                def op2(conn):
                    conn.execute("DELETE FROM push_subscriptions WHERE endpoint = ?", (endpoint,))

                with_db(op2, message=f"push: unsubscribe {editor or 'admin'}")
                return json_response(self, {"ok": True})

            if action == "test":
                # Manda un push de prueba (admin only)
                if not is_admin:
                    return json_response(self, {"error": "test solo admin"}, status=403)
                # Como esto requiere VAPID_PRIVATE_KEY que NO está en Vercel,
                # solo registra que se quiso probar
                return json_response(self, {"ok": True, "info": "test no implementado en Vercel — el push real se manda desde workflows GitHub Actions"})

            return json_response(self, {"error": f"action inválida: {action}"}, status=400)
        except Exception as e:
            return json_response(self, {"error": str(e)[:200], "trace": traceback.format_exc()[:1500]}, status=500)

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def log_message(self, *a, **kw): pass

"""
Envío manual del resumen diario.

POST /api/daily_summary?admin=1&t=ADMIN_TOKEN&target=editor_X
  → Manda el resumen al editor X

POST /api/daily_summary?admin=1&t=ADMIN_TOKEN&target=all
  → Manda a todos los editores (los que tienen receives_daily_summary=1)

POST /api/daily_summary?admin=1&t=ADMIN_TOKEN&target=admin
  → Manda al admin (vos)

Solo accesible con admin token.
"""

import json
import os
import sys
import traceback
from http.server import BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
# El daily_summary.py está en root del repo, no en api/
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    from _shared import check_token, json_response
    _IMPORT_ERROR = None
except Exception as _e:
    _IMPORT_ERROR = f"{type(_e).__name__}: {_e}\n{traceback.format_exc()}"


class handler(BaseHTTPRequestHandler):

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_POST(self):
        try:
            if _IMPORT_ERROR:
                return json_response(self, {"error": _IMPORT_ERROR}, status=500)
            params = parse_qs(urlparse(self.path).query)
            token = (params.get("t", [""])[0] or "").strip()
            admin = params.get("admin", [""])[0] == "1"
            target = (params.get("target", [""])[0] or "").strip()
            if not (admin and check_token("ADMIN", token)):
                return json_response(self, {"error": "admin required"}, status=401)
            if not target:
                return json_response(self, {"error": "target requerido (admin|all|<editor_name>)"}, status=400)

            from daily_summary import send_to_admin, send_to_editor, send_to_all_editors

            if target == "admin":
                result = send_to_admin()
                return json_response(self, {"ok": True, "result": result})
            if target == "all":
                results = send_to_all_editors(include_admin=False)
                sent = sum(1 for r in results if r.get("ok"))
                skipped = sum(1 for r in results if "skipped" in r)
                return json_response(self, {"ok": True, "sent": sent, "skipped": skipped, "results": results})
            # target = nombre de un editor específico
            result = send_to_editor(target)
            return json_response(self, {"ok": True, "result": result})
        except Exception as e:
            return json_response(self, {"error": str(e)[:200], "trace": traceback.format_exc()[:1500]}, status=500)

    def log_message(self, *a, **kw):
        pass

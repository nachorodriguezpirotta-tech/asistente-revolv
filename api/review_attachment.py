"""
Sirve attachments (imágenes) de una review.

GET /api/review_attachment?id=N&t=ADMIN_TOKEN
  → admin puede ver cualquier attachment
GET /api/review_attachment?id=N&cliente=X&t=CLIENT_TOKEN
  → cliente puede ver SUS PROPIOS attachments
"""

import os
import sys
import traceback
from http.server import BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    from _shared import check_token, check_client_token, json_response, read_db
    _IMPORT_ERROR = None
except Exception as _e:
    _IMPORT_ERROR = f"{type(_e).__name__}: {_e}\n{traceback.format_exc()}"


class handler(BaseHTTPRequestHandler):
    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self):
        try:
            if _IMPORT_ERROR:
                return json_response(self, {"error": _IMPORT_ERROR}, status=500)
            params = parse_qs(urlparse(self.path).query)
            token = (params.get("t", [""])[0] or "").strip()
            admin = params.get("admin", [""])[0] == "1"
            cliente = (params.get("cliente", [""])[0] or "").strip()

            try:
                att_id = int(params.get("id", [""])[0])
            except Exception:
                return json_response(self, {"error": "id inválido"}, status=400)

            # Cargar attachment + cliente del review padre para auth
            def _q(conn):
                row = conn.execute("""
                    SELECT a.id, a.review_id, a.filename, a.mime_type, a.blob, a.size_bytes,
                           r.cliente as review_cliente
                    FROM client_review_attachments a
                    LEFT JOIN client_reviews r ON r.id = a.review_id
                    WHERE a.id = ?
                """, (att_id,)).fetchone()
                return dict(row) if row else None
            row = read_db(_q)
            if not row:
                return json_response(self, {"error": "not found"}, status=404)

            # Auth: admin O cliente del review
            ok = False
            if admin and check_token("ADMIN", token):
                ok = True
            elif cliente and row.get("review_cliente") == cliente and check_client_token(cliente, token):
                ok = True
            if not ok:
                return json_response(self, {"error": "unauthorized"}, status=401)

            blob = row["blob"]
            mime = row["mime_type"] or "image/jpeg"
            self.send_response(200)
            self.send_header("Content-Type", mime)
            self.send_header("Content-Length", str(len(blob)))
            self.send_header("Cache-Control", "private, max-age=600")
            self.send_header("Content-Disposition", f'inline; filename="{row.get("filename") or "attachment"}"')
            self.end_headers()
            self.wfile.write(blob)
        except Exception as e:
            return json_response(self, {"error": str(e)[:200], "trace": traceback.format_exc()[:1500]}, status=500)

    def log_message(self, *a, **kw):
        pass

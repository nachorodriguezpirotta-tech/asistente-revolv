"""
Endpoint para el panel del cliente: lista TODOS los videos editados que
el cron del asistente trackea para ese cliente.

Auth: token de cliente (mismo `make_client_token(cliente)` que el resto
del flujo de revisiones — coincide con el token que va en los mails).

GET /api/client_videos?cliente=X&t=TOKEN
  → { ok: true, cliente: "X", items: [...] }

Cada item:
  - file_id, file_name, cliente
  - first_seen_at (cuándo lo detectó el cron)
  - created_time (cuándo se subió a Drive)
  - editor (best effort — del task asociado)
  - review_id (NULL si no hay revisión activa/histórica)
  - review_status (NULL | 'revision_requested' | 'resolved')
"""

import json
import os
import sys
import traceback
from http.server import BaseHTTPRequestHandler
from urllib.parse import parse_qs, urlparse

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

_IMPORT_ERROR = None
try:
    from api._shared import check_client_token, json_response, read_db
except Exception as e:
    _IMPORT_ERROR = f"{type(e).__name__}: {e}"


def _build(conn, cliente: str):
    rows = conn.execute(
        """
        SELECT
            kef.file_id, kef.cliente, kef.name AS file_name,
            kef.created_time, kef.first_seen_at, kef.subfolder_name,
            (SELECT t.editor FROM tasks t
              WHERE t.completed_by_file_id = kef.file_id
              ORDER BY t.completed_at DESC LIMIT 1) AS editor,
            (SELECT cr.id FROM client_reviews cr
              WHERE cr.video_file_id = kef.file_id
              ORDER BY cr.id DESC LIMIT 1) AS review_id,
            (SELECT cr.status FROM client_reviews cr
              WHERE cr.video_file_id = kef.file_id
              ORDER BY cr.id DESC LIMIT 1) AS review_status,
            (SELECT cr.created_at FROM client_reviews cr
              WHERE cr.video_file_id = kef.file_id
              ORDER BY cr.id DESC LIMIT 1) AS review_created_at
        FROM known_edited_files kef
        WHERE TRIM(LOWER(kef.cliente)) = TRIM(LOWER(?))
        ORDER BY kef.first_seen_at DESC
        LIMIT 500
        """,
        (cliente,),
    ).fetchall()
    return [dict(r) for r in rows]


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        try:
            if _IMPORT_ERROR:
                return json_response(self, {"error": _IMPORT_ERROR}, status=500)
            params = parse_qs(urlparse(self.path).query)
            cliente = (params.get("cliente", [""])[0] or "").strip()
            token = (params.get("t", [""])[0] or "").strip()
            if not cliente:
                return json_response(self, {"error": "cliente requerido"}, status=400)
            if not check_client_token(cliente, token):
                return json_response(self, {"error": "unauthorized"}, status=401)
            items = read_db(lambda c: _build(c, cliente))
            return json_response(
                self,
                {"ok": True, "cliente": cliente, "items": items, "count": len(items)},
            )
        except Exception as e:
            return json_response(
                self,
                {"error": str(e)[:200], "trace": traceback.format_exc()[:1500]},
                status=500,
            )

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

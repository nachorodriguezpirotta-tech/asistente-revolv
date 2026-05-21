"""
Lista de revisiones — vista admin y editor.

GET /api/reviews?admin=1&t=TOKEN
  → admin ve TODAS las reviews (con stats)

GET /api/reviews?editor=Rami&t=TOKEN
  → editor ve solo SUS reviews (status revision_requested)
"""

import os
import sys
import traceback
from http.server import BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

try:
    from _shared import check_token, json_response, read_db
    _IMPORT_ERROR = None
except Exception as _e:
    _IMPORT_ERROR = f"{type(_e).__name__}: {_e}\n{traceback.format_exc()}"


def _build_admin(conn):
    rows = conn.execute("""
        SELECT r.id, r.cliente, r.video_file_id, r.video_file_name, r.editor, r.status,
               r.notes, r.created_at, r.responded_at, r.resolved_at,
               (SELECT COUNT(*) FROM client_review_attachments a WHERE a.review_id = r.id) as attachments_count
        FROM client_reviews r
        ORDER BY r.id DESC
        LIMIT 200
    """).fetchall()
    items = []
    for r in rows:
        d = dict(r)
        if d.get("attachments_count", 0) > 0:
            # Listar metadata de cada attachment para que la UI muestre thumbs
            atts = conn.execute(
                "SELECT id, filename, mime_type FROM client_review_attachments WHERE review_id=? ORDER BY id",
                (r["id"],)
            ).fetchall()
            d["attachments"] = [dict(a) for a in atts]
        items.append(d)

    # Stats
    stats = conn.execute("""
        SELECT status, COUNT(*) as n FROM client_reviews GROUP BY status
    """).fetchall()
    by_status = {r["status"]: r["n"] for r in stats}

    # Tiempo promedio de respuesta (responded_at - created_at) para los respondidos
    pendientes = by_status.get("pending", 0)
    aprobadas = by_status.get("approved", 0)
    revision = by_status.get("revision_requested", 0)
    resueltas = by_status.get("resolved", 0)

    return {
        "ok": True,
        "items": items,
        "stats": {
            "pending": pendientes,
            "approved": aprobadas,
            "revision_requested": revision,
            "resolved": resueltas,
            "total": sum(by_status.values()),
        },
    }


def _build_editor(conn, editor: str):
    rows = conn.execute("""
        SELECT id, cliente, video_file_id, video_file_name, status,
               notes, created_at, responded_at, resolved_at
        FROM client_reviews
        WHERE editor=?
        ORDER BY id DESC LIMIT 100
    """, (editor,)).fetchall()
    items = [dict(r) for r in rows]
    open_count = sum(1 for r in items if r["status"] == "revision_requested")
    return {
        "ok": True,
        "items": items,
        "open_count": open_count,
        "editor": editor,
    }


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        try:
            if _IMPORT_ERROR:
                return json_response(self, {"error": _IMPORT_ERROR}, status=500)
            params = parse_qs(urlparse(self.path).query)
            token = (params.get("t", [""])[0] or "").strip()
            admin = params.get("admin", [""])[0] == "1"
            editor = (params.get("editor", [""])[0] or "").strip()

            if admin and check_token("ADMIN", token):
                data = read_db(_build_admin)
                return json_response(self, data)
            if editor and check_token(editor, token):
                data = read_db(lambda c: _build_editor(c, editor))
                return json_response(self, data)
            return json_response(self, {"error": "unauthorized"}, status=401)
        except Exception as e:
            return json_response(self, {"error": str(e)[:200], "trace": traceback.format_exc()[:1500]}, status=500)

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def log_message(self, *a, **kw): pass

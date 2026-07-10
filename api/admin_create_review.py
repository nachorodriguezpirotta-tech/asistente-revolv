"""
Crea/actualiza una review de cliente desde el admin, usando with_db (coexiste
con el bot que pushea tracker.db — a diferencia de una edición manual + git push
que el bot pisa).

Sirve para recuperar correcciones que el portal registró pero que no llegaron
al asistente (ej. por GITHUB_PAT vencido en el POST original).

GET /api/admin_create_review?cliente=X&file_id=Y&file_name=Z&editor=E
    &status=revision_requested&notes=...&admin=1&t=ADMIN_TOKEN

Idempotente: si ya existe una review de ese file_id con el mismo status, no
duplica. status default = revision_requested.
"""

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
    from api._shared import check_token, json_response, with_db, read_db
except Exception as e:
    _IMPORT_ERROR = f"{type(e).__name__}: {e}"


VALID_STATUS = ("revision_requested", "approved", "resolved", "pending")


def _exists(conn, file_id, status):
    row = conn.execute(
        "SELECT id FROM client_reviews WHERE video_file_id=? AND status=? LIMIT 1",
        (file_id, status),
    ).fetchone()
    return row["id"] if row else None


def _make_insert(cliente, file_id, file_name, editor, status, notes, notify):
    def _do(conn):
        notified = "NULL" if notify else "datetime('now')"
        conn.execute(
            f"""
            INSERT INTO client_reviews
                (cliente, video_file_id, video_file_name, editor, status, notes,
                 created_at, responded_at, resolved_at, notified_at)
            VALUES (?, ?, ?, ?, ?, ?, datetime('now'), datetime('now'),
                    {"datetime('now')" if status == "resolved" else "NULL"},
                    {notified})
            """,
            (cliente, file_id, file_name, editor or None, status, notes),
        )
        return None
    return _do


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        try:
            if _IMPORT_ERROR:
                return json_response(self, {"error": _IMPORT_ERROR}, status=500)
            p = parse_qs(urlparse(self.path).query)
            cliente = (p.get("cliente", [""])[0] or "").strip()
            file_id = (p.get("file_id", [""])[0] or "").strip()
            file_name = (p.get("file_name", [""])[0] or "").strip()
            editor = (p.get("editor", [""])[0] or "").strip()
            status = (p.get("status", ["revision_requested"])[0] or "").strip()
            notes = (p.get("notes", [""])[0] or "").strip()
            notify = p.get("notify", ["0"])[0] == "1"
            admin = p.get("admin", [""])[0] == "1"
            token = (p.get("t", [""])[0] or "").strip()

            if not admin or not check_token("ADMIN", token):
                return json_response(self, {"error": "unauthorized"}, status=401)
            if not cliente or not file_id:
                return json_response(
                    self, {"error": "cliente y file_id requeridos"}, status=400
                )
            if status not in VALID_STATUS:
                return json_response(
                    self, {"error": f"status inválido (usar {VALID_STATUS})"}, status=400
                )

            existing = read_db(lambda c: _exists(c, file_id, status))
            if existing:
                return json_response(
                    self,
                    {"ok": True, "already_exists": True, "review_id": existing},
                )

            with_db(
                _make_insert(cliente, file_id, file_name, editor, status, notes, notify),
                message=f"admin_create_review {status}: {cliente} [skip ci]",
            )
            return json_response(
                self,
                {"ok": True, "created": True, "cliente": cliente,
                 "status": status, "editor": editor},
            )
        except Exception as e:
            return json_response(
                self,
                {"error": str(e)[:200], "trace": traceback.format_exc()[:1500]},
                status=500,
            )

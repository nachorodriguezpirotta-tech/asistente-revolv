"""
Endpoint admin de migración one-shot: marca como 'approved' todos los
videos editados que NO tienen review activa todavía.

Pensado para correr UNA sola vez cuando se quiere "resetear" el estado:
todos los videos históricos quedan como aprobados, y desde ese momento
los nuevos editados que detecte el cron aparecen como 'Por revisar'
hasta que el cliente toque algo.

Idempotente: si ya existe una review para (cliente, file_id), no la toca.

GET /api/bulk_approve_existing?admin=1&t=ADMIN_TOKEN[&dry_run=1]
  → { ok, approved: N, dry_run: bool, sample: [...] }
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


def _preview(conn):
    rows = conn.execute("""
        SELECT kef.cliente, kef.file_id, kef.name
        FROM known_edited_files kef
        WHERE NOT EXISTS (
            SELECT 1 FROM client_reviews cr
            WHERE TRIM(LOWER(cr.cliente)) = TRIM(LOWER(kef.cliente))
              AND COALESCE(cr.video_file_id,'') = COALESCE(kef.file_id,'')
        )
        LIMIT 10
    """).fetchall()
    return [dict(r) for r in rows]


def _count(conn):
    row = conn.execute("""
        SELECT COUNT(*) AS n FROM known_edited_files kef
        WHERE NOT EXISTS (
            SELECT 1 FROM client_reviews cr
            WHERE TRIM(LOWER(cr.cliente)) = TRIM(LOWER(kef.cliente))
              AND COALESCE(cr.video_file_id,'') = COALESCE(kef.file_id,'')
        )
    """).fetchone()
    return row[0] if row else 0


def _make_apply(limit: int):
    def _apply(conn):
        conn.execute("""
            INSERT INTO client_reviews
                (cliente, video_file_id, video_file_name, editor, status, notes,
                 created_at, responded_at, resolved_at)
            SELECT
                kef.cliente,
                kef.file_id,
                kef.name,
                NULL,
                'approved',
                '(aprobado en migración inicial — todos los videos previos)',
                datetime('now'),
                datetime('now'),
                datetime('now')
            FROM known_edited_files kef
            WHERE NOT EXISTS (
                SELECT 1 FROM client_reviews cr
                WHERE TRIM(LOWER(cr.cliente)) = TRIM(LOWER(kef.cliente))
                  AND COALESCE(cr.video_file_id,'') = COALESCE(kef.file_id,'')
            )
            LIMIT ?
        """, (limit,))
        return None
    return _apply


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        try:
            if _IMPORT_ERROR:
                return json_response(self, {"error": _IMPORT_ERROR}, status=500)
            params = parse_qs(urlparse(self.path).query)
            admin = params.get("admin", [""])[0] == "1"
            token = (params.get("t", [""])[0] or "").strip()
            dry_run = params.get("dry_run", [""])[0] == "1"
            try:
                limit = int(params.get("limit", ["100000"])[0])
            except ValueError:
                limit = 100000

            if not admin or not check_token("ADMIN", token):
                return json_response(self, {"error": "unauthorized"}, status=401)

            count = read_db(_count)
            sample = read_db(_preview) if count else []
            if dry_run:
                return json_response(
                    self,
                    {
                        "ok": True,
                        "dry_run": True,
                        "would_approve": count,
                        "sample": sample,
                    },
                )

            applied = min(count, limit)
            with_db(
                _make_apply(limit),
                message=f"bulk approve: {applied} videos a estado approved [skip ci]",
            )
            return json_response(
                self,
                {
                    "ok": True,
                    "dry_run": False,
                    "approved": applied,
                    "remaining": max(0, count - applied),
                    "sample": sample,
                },
            )
        except Exception as e:
            return json_response(
                self,
                {"error": str(e)[:200], "trace": traceback.format_exc()[:1500]},
                status=500,
            )

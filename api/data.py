"""
GET /api/data?editor=<nombre>&t=<token>
GET /api/data?admin=1&t=<admin_token>  → vista global (Ignacio)

Devuelve JSON con los pendientes:
  Si editor: solo los suyos.
  Si admin: agrupados por editor.
"""

from http.server import BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

from _shared import (
    check_token, read_db, json_response, EDITORS, make_token,
    DASHBOARD_SECRET,
)


def get_editor_data(conn, editor: str) -> dict:
    rows = conn.execute(
        """SELECT id, cliente, file_name, detected_at
           FROM tasks
           WHERE editor = ? AND status = 'pending'
           ORDER BY cliente""",
        (editor,),
    ).fetchall()
    return {
        "editor": editor,
        "pendientes": [
            {
                "id": r["id"],
                "cliente": r["cliente"].strip(),
                "detected_at": r["detected_at"],
            }
            for r in rows
        ],
    }


def get_all_data(conn) -> dict:
    rows = conn.execute(
        """SELECT id, editor, cliente, detected_at
           FROM tasks
           WHERE status = 'pending'
           ORDER BY editor, cliente"""
    ).fetchall()
    by_editor = {}
    for r in rows:
        ed = r["editor"] or "— sin editor —"
        by_editor.setdefault(ed, []).append({
            "id": r["id"],
            "cliente": r["cliente"].strip(),
            "detected_at": r["detected_at"],
        })

    # Stats
    closed_total = conn.execute("SELECT COUNT(*) FROM tasks WHERE status='done'").fetchone()[0]
    return {
        "by_editor": by_editor,
        "stats": {
            "pendientes": sum(len(v) for v in by_editor.values()),
            "editores": len(by_editor),
            "cerradas_total": closed_total,
        },
    }


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        params = parse_qs(urlparse(self.path).query)
        editor = (params.get("editor", [""])[0] or "").strip()
        admin = params.get("admin", [""])[0]
        token = (params.get("t", [""])[0] or "").strip()

        if admin == "1":
            # Token admin = hash de "ADMIN" con la secret
            from _shared import make_token
            if not check_token("ADMIN", token):
                return json_response(self, {"error": "unauthorized"}, status=401)
            try:
                data = read_db(get_all_data)
                return json_response(self, data)
            except Exception as e:
                return json_response(self, {"error": str(e)[:200]}, status=500)

        if not editor:
            return json_response(self, {"error": "missing editor param"}, status=400)
        if not check_token(editor, token):
            return json_response(self, {"error": "unauthorized"}, status=401)

        try:
            data = read_db(lambda conn: get_editor_data(conn, editor))
            return json_response(self, data)
        except Exception as e:
            return json_response(self, {"error": str(e)[:200]}, status=500)

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def log_message(self, *args, **kwargs):
        pass

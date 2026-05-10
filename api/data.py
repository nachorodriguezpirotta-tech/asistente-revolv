"""
GET /api/data?editor=<nombre>&t=<token>
GET /api/data?admin=1&t=<admin_token>  → vista global (Ignacio)

Devuelve JSON con los pendientes:
  Si editor: solo los suyos.
  Si admin: agrupados por editor.
"""

import json
import os
import sys
import traceback
from http.server import BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

# Asegurar que podemos importar _shared.py del mismo directorio
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

try:
    from _shared import (
        check_token, read_db, json_response, EDITORS, make_token,
        DASHBOARD_SECRET, GITHUB_PAT,
    )
    _IMPORT_ERROR = None
except Exception as _e:
    _IMPORT_ERROR = f"{type(_e).__name__}: {_e}\n{traceback.format_exc()}"


def get_editor_data(conn, editor: str) -> dict:
    # Agrupar por cliente: 1 entry por cliente, con conteo de tasks asociadas
    rows = conn.execute(
        """SELECT cliente, MIN(id) as id, COUNT(*) as count, MIN(detected_at) as oldest
           FROM tasks
           WHERE editor = ? AND status = 'pending'
           GROUP BY TRIM(cliente)
           ORDER BY TRIM(cliente)""",
        (editor,),
    ).fetchall()
    return {
        "editor": editor,
        "pendientes": [
            {
                "id": r["id"],  # id de la task más vieja (para referencia)
                "cliente": r["cliente"].strip(),
                "count": r["count"],
                "detected_at": r["oldest"],
            }
            for r in rows
        ],
    }


def get_all_data(conn) -> dict:
    # Agrupar por cliente+editor: 1 entry por combinación
    rows = conn.execute(
        """SELECT editor, TRIM(cliente) as cliente, MIN(id) as id,
                  COUNT(*) as count, MIN(detected_at) as oldest
           FROM tasks
           WHERE status = 'pending'
           GROUP BY editor, TRIM(cliente)
           ORDER BY editor, cliente"""
    ).fetchall()
    by_editor = {}
    for r in rows:
        ed = r["editor"] or "— sin editor —"
        by_editor.setdefault(ed, []).append({
            "id": r["id"],
            "cliente": r["cliente"],
            "count": r["count"],
            "detected_at": r["oldest"],
        })

    # Generar links únicos por editor (cualquier editor que aparezca acá tiene su link)
    editor_links = {}
    for ed in by_editor.keys():
        if ed.startswith("—"):  # sin editor → no link
            continue
        editor_links[ed] = f"?editor={ed}&t={make_token(ed)}"

    # Stats
    closed_total = conn.execute("SELECT COUNT(*) FROM tasks WHERE status='done'").fetchone()[0]
    return {
        "by_editor": by_editor,
        "editor_links": editor_links,
        "stats": {
            "pendientes": sum(len(v) for v in by_editor.values()),
            "editores": len(by_editor),
            "cerradas_total": closed_total,
        },
    }


class handler(BaseHTTPRequestHandler):
    def _safe_json(self, data, status=200):
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if _IMPORT_ERROR is not None:
            return self._safe_json({"error": "import error", "detail": _IMPORT_ERROR}, status=500)

        try:
            return self._do_get_inner()
        except Exception as e:
            return self._safe_json({
                "error": f"{type(e).__name__}: {e}",
                "trace": traceback.format_exc()[:1500],
                "github_pat_set": bool(GITHUB_PAT),
            }, status=500)

    def _do_get_inner(self):
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

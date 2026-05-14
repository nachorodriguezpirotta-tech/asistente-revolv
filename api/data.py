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


def _get_client_folder_map(conn) -> dict:
    """Devuelve {cliente_normalizado: folder_id} de la tabla clients."""
    rows = conn.execute("SELECT cliente, folder_id FROM clients").fetchall()
    return {r["cliente"].strip().lower(): r["folder_id"] for r in rows}


def get_all_clients(conn) -> dict:
    """Devuelve lista de TODOS los clientes conocidos del sistema con su folder_id.
    Fuente: tabla clients + tasks + known_files + known_edited_files.
    Útil para autocomplete en el dashboard.
    """
    universe = {}  # cliente → folder_id

    # 1. Tabla clients (con folder_id confirmado)
    for r in conn.execute("SELECT cliente, folder_id FROM clients").fetchall():
        if r["cliente"]:
            universe[r["cliente"].strip()] = r["folder_id"]

    # 2. Tasks (puede haber clientes sin folder_id confirmado)
    for r in conn.execute("SELECT DISTINCT TRIM(cliente) as c FROM tasks WHERE cliente IS NOT NULL AND cliente != ''").fetchall():
        if r["c"] and r["c"] not in universe:
            universe[r["c"]] = None

    # 3. known_files / known_edited_files (histórico)
    for table in ("known_files", "known_edited_files"):
        try:
            for r in conn.execute(f"SELECT DISTINCT TRIM(cliente) as c FROM {table}").fetchall():
                if r["c"] and r["c"] not in universe:
                    universe[r["c"]] = None
        except Exception:
            pass

    clients = [{"cliente": name, "folder_id": fid} for name, fid in universe.items()]
    clients.sort(key=lambda x: x["cliente"].lower())
    return {"clients": clients}


def get_editor_data(conn, editor: str) -> dict:
    # 1 entry por cliente con la suma de pending_count (videos pendientes)
    # Urgentes primero (MAX(urgent)=1)
    rows = conn.execute(
        """SELECT cliente, MIN(id) as id, SUM(COALESCE(pending_count, 1)) as videos,
                  MIN(detected_at) as oldest,
                  MAX(COALESCE(urgent, 0)) as urgent,
                  MAX(COALESCE(note, '')) as note
           FROM tasks
           WHERE editor = ? AND status = 'pending'
           GROUP BY TRIM(cliente)
           ORDER BY MAX(COALESCE(urgent, 0)) DESC, TRIM(cliente)""",
        (editor,),
    ).fetchall()

    folder_map = _get_client_folder_map(conn)

    # Progresos del editor (múltiples labels, ej. "Básicos" y "Avanzados")
    prog_rows = conn.execute(
        "SELECT label, current, total FROM editor_progress WHERE editor = ? ORDER BY label",
        (editor,),
    ).fetchall()
    progresses = [
        {"label": r["label"], "current": r["current"], "total": r["total"]}
        for r in prog_rows
    ]

    return {
        "editor": editor,
        "pendientes": [
            {
                "id": r["id"],
                "cliente": r["cliente"].strip(),
                "videos": r["videos"] or 1,
                "detected_at": r["oldest"],
                "drive_folder_id": folder_map.get(r["cliente"].strip().lower()),
                "urgent": bool(r["urgent"]) if "urgent" in r.keys() else False,
                "note": r["note"] if "note" in r.keys() else None,
            }
            for r in rows
        ],
        "progresses": progresses,
    }


def get_all_data(conn) -> dict:
    rows = conn.execute(
        """SELECT editor, TRIM(cliente) as cliente, MIN(id) as id,
                  SUM(COALESCE(pending_count, 1)) as videos, MIN(detected_at) as oldest,
                  MAX(COALESCE(urgent, 0)) as urgent,
                  MAX(COALESCE(note, '')) as note
           FROM tasks
           WHERE status = 'pending'
           GROUP BY editor, TRIM(cliente)
           ORDER BY editor, MAX(COALESCE(urgent, 0)) DESC, cliente"""
    ).fetchall()
    folder_map = _get_client_folder_map(conn)
    by_editor = {}
    for r in rows:
        ed = r["editor"] or "— sin editor —"
        by_editor.setdefault(ed, []).append({
            "id": r["id"],
            "cliente": r["cliente"],
            "videos": r["videos"] or 1,
            "detected_at": r["oldest"],
            "drive_folder_id": folder_map.get(r["cliente"].strip().lower()),
            "urgent": bool(r["urgent"]) if "urgent" in r.keys() else False,
            "note": r["note"] if "note" in r.keys() else None,
        })

    # Conteo de carpetas Drive pendientes de aprobación (para badge en dashboard)
    pending_folders_count = 0
    try:
        pending_folders_count = conn.execute(
            "SELECT COUNT(*) FROM pending_drive_folders WHERE status='pending'"
        ).fetchone()[0]
    except Exception:
        pass

    # Asegurar que TODOS los editores ACTIVOS aparezcan, aunque no tengan pendientes.
    # Lee de cfg_editors (DB) que es la fuente de verdad runtime.
    editors_on_vacation = set()
    try:
        ed_rows = conn.execute("SELECT name, COALESCE(on_vacation, 0) as on_vacation FROM cfg_editors WHERE active=1").fetchall()
        for r in ed_rows:
            ed_name = r["name"]
            if ed_name and ed_name not in by_editor:
                by_editor[ed_name] = []
            if r["on_vacation"]:
                editors_on_vacation.add(ed_name)
    except Exception:
        # Fallback al hardcoded si la tabla no existe
        try:
            from aliases import EDITORS_LIST
            for ed in EDITORS_LIST:
                if ed not in by_editor:
                    by_editor[ed] = []
        except Exception:
            pass

    # Generar links únicos por editor (cualquier editor que aparezca acá tiene su link)
    editor_links = {}
    for ed in by_editor.keys():
        if ed.startswith("—"):  # sin editor → no link
            continue
        editor_links[ed] = f"?editor={ed}&t={make_token(ed)}"

    # Progresses por editor (cada editor puede tener varios labels)
    prog_rows = conn.execute(
        "SELECT editor, label, current, total FROM editor_progress ORDER BY editor, label"
    ).fetchall()
    editor_progresses = {}
    for r in prog_rows:
        editor_progresses.setdefault(r["editor"], []).append({
            "label": r["label"], "current": r["current"], "total": r["total"]
        })

    # Stats
    closed_total = conn.execute("SELECT COUNT(*) FROM tasks WHERE status='done'").fetchone()[0]
    return {
        "by_editor": by_editor,
        "editor_links": editor_links,
        "editor_progresses": editor_progresses,
        "pending_folders_count": pending_folders_count,
        "editors_on_vacation": sorted(editors_on_vacation),
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
        list_clients = params.get("list_clients", [""])[0]

        if admin == "1":
            from _shared import make_token
            if not check_token("ADMIN", token):
                return json_response(self, {"error": "unauthorized"}, status=401)
            try:
                if list_clients == "1":
                    data = read_db(get_all_clients)
                else:
                    data = read_db(get_all_data)
                return json_response(self, data)
            except Exception as e:
                return json_response(self, {"error": str(e)[:200]}, status=500)

        if not editor:
            return json_response(self, {"error": "missing editor param"}, status=400)
        if not check_token(editor, token):
            return json_response(self, {"error": "unauthorized"}, status=401)

        try:
            if list_clients == "1":
                data = read_db(get_all_clients)
            else:
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

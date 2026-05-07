"""
POST   /api/task        (body: {editor, t, cliente})         → crear pendiente
DELETE /api/task?id=N&editor=E&t=TOKEN                       → borrar pendiente

Solo permite borrar tasks que pertenezcan al editor del token (o admin).
"""

import json
import os
import sys
import time as _t
import traceback
from http.server import BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

# Asegurar que podemos importar _shared.py del mismo directorio
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

try:
    from _shared import check_token, with_db, json_response, now_iso
    _IMPORT_ERROR = None
except Exception as _e:
    _IMPORT_ERROR = f"{type(_e).__name__}: {_e}\n{traceback.format_exc()}"


class handler(BaseHTTPRequestHandler):

    def do_POST(self):
        try:
            length = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(length).decode("utf-8") if length > 0 else "{}"
            body = json.loads(raw)
        except Exception as e:
            return json_response(self, {"error": f"body inválido: {e}"}, status=400)

        editor = (body.get("editor") or "").strip()
        token = (body.get("t") or "").strip()
        cliente = (body.get("cliente") or "").strip()
        is_admin = body.get("admin") == 1

        if is_admin:
            from _shared import check_token as _ct
            if not _ct("ADMIN", token):
                return json_response(self, {"error": "unauthorized"}, status=401)
            target_editor = (body.get("target_editor") or "").strip()
            if not target_editor or not cliente:
                return json_response(self, {"error": "Faltan target_editor o cliente"}, status=400)
            editor = target_editor
        else:
            if not editor or not cliente:
                return json_response(self, {"error": "Faltan editor o cliente"}, status=400)
            if not check_token(editor, token):
                return json_response(self, {"error": "unauthorized"}, status=401)

        pseudo_id = f"manual:{editor.lower()}:{cliente.lower().replace(' ', '_')}:{int(_t.time() * 1000000)}"

        def op(conn):
            existing = conn.execute(
                "SELECT id FROM tasks WHERE cliente = ? AND editor = ? AND status = 'pending'",
                (cliente, editor),
            ).fetchone()
            if existing:
                raise ValueError("duplicado")
            conn.execute(
                """INSERT INTO tasks (cliente, editor, file_id, file_name, detected_at, status, mail_sent_at)
                   VALUES (?, ?, ?, ?, ?, 'pending', ?)""",
                (cliente, editor, pseudo_id, "(pendiente cargado manualmente)", now_iso(), now_iso()),
            )

        try:
            with_db(op, message=f"manual: agregada {cliente} / {editor}")
            return json_response(self, {"ok": True, "cliente": cliente, "editor": editor})
        except ValueError as e:
            if "duplicado" in str(e):
                return json_response(self, {"error": f"Ya hay un pendiente de '{cliente}'"}, status=409)
            return json_response(self, {"error": str(e)[:200]}, status=500)
        except Exception as e:
            return json_response(self, {"error": str(e)[:200]}, status=500)

    def do_DELETE(self):
        params = parse_qs(urlparse(self.path).query)
        try:
            task_id = int(params.get("id", ["0"])[0])
        except ValueError:
            return json_response(self, {"error": "id inválido"}, status=400)
        editor = (params.get("editor", [""])[0] or "").strip()
        token = (params.get("t", [""])[0] or "").strip()
        is_admin = params.get("admin", [""])[0] == "1"

        if is_admin:
            if not check_token("ADMIN", token):
                return json_response(self, {"error": "unauthorized"}, status=401)
        else:
            if not editor or not check_token(editor, token):
                return json_response(self, {"error": "unauthorized"}, status=401)

        # Verificar que la task pertenece al editor (a menos que sea admin)
        captured = {"cliente": None, "editor": None}

        def op(conn):
            row = conn.execute("SELECT id, cliente, editor FROM tasks WHERE id = ?", (task_id,)).fetchone()
            if not row:
                raise ValueError("notfound")
            if not is_admin and row["editor"] != editor:
                raise ValueError("forbidden")
            captured["cliente"] = row["cliente"]
            captured["editor"] = row["editor"]
            conn.execute("DELETE FROM tasks WHERE id = ?", (task_id,))

        try:
            with_db(op, message=f"manual: borrada task #{task_id}")
            return json_response(self, {"ok": True, "task_id": task_id, **captured})
        except ValueError as e:
            err = str(e)
            if err == "notfound":
                return json_response(self, {"error": f"task #{task_id} no existe"}, status=404)
            if err == "forbidden":
                return json_response(self, {"error": "No podés borrar tareas de otro editor"}, status=403)
            return json_response(self, {"error": err[:200]}, status=500)
        except Exception as e:
            return json_response(self, {"error": str(e)[:200]}, status=500)

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "POST, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def log_message(self, *args, **kwargs):
        pass

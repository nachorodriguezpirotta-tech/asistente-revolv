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


def _normalize(s: str) -> str:
    import unicodedata
    s = unicodedata.normalize("NFD", s)
    s = "".join(c for c in s if unicodedata.category(c) != "Mn")
    return " ".join(s.lower().split())


def resolve_nickname(conn, cliente_input: str, editor: str) -> str:
    """
    Resuelve un apodo al nombre real del cliente. Estrategia:
      1. Diccionario estático de apodos (delfi → Delfina Orange Power)
      2. Match exacto en tasks del editor
      3. Match parcial (contains/startswith) en clientes conocidos del editor
      4. Match por prefijo común (≥3 chars) en tokens
    """
    if not cliente_input:
        return cliente_input

    # 1. Diccionario estático
    try:
        from aliases import resolve_nickname_static
        nick = resolve_nickname_static(cliente_input)
        if nick != cliente_input:
            return nick
    except Exception:
        pass

    norm = _normalize(cliente_input)

    # Buscar entre clientes conocidos del editor (en tasks)
    rows = conn.execute(
        "SELECT DISTINCT TRIM(cliente) as cliente FROM tasks WHERE editor = ?",
        (editor,),
    ).fetchall()
    known = {r["cliente"] for r in rows if r["cliente"]}

    # 2. Match exacto
    for k in known:
        if _normalize(k) == norm:
            return k

    # 3. Match parcial: cliente conocido contiene el apodo
    contains = [k for k in known if norm in _normalize(k)]
    if len(contains) == 1:
        return contains[0]

    starts = [k for k in known if _normalize(k).startswith(norm)]
    if len(starts) == 1:
        return starts[0]

    # 4. Prefijo común con algún token
    fuzzy = []
    for k in known:
        for token in _normalize(k).split():
            if len(token) >= 3 and (token.startswith(norm) or norm.startswith(token)) and min(len(token), len(norm)) >= 3:
                fuzzy.append(k)
                break
    if len(set(fuzzy)) == 1:
        return fuzzy[0]

    return cliente_input


def _set_pending_count_op(conn, cliente, editor, count):
    """Setea pending_count para una task pending de cliente+editor."""
    if editor:
        rows = conn.execute(
            "UPDATE tasks SET pending_count=? WHERE TRIM(cliente)=? AND editor=? AND status='pending'",
            (count, cliente, editor),
        )
    else:
        rows = conn.execute(
            "UPDATE tasks SET pending_count=? WHERE TRIM(cliente)=? AND status='pending'",
            (count, cliente),
        )
    return rows.rowcount


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

        def op(conn):
            # Resolver apodo: si el usuario escribió 'delfi', buscar el cliente real
            cliente_resuelto = resolve_nickname(conn, cliente, editor)
            existing = conn.execute(
                "SELECT id FROM tasks WHERE TRIM(cliente)=TRIM(?) AND editor=? AND status='pending'",
                (cliente_resuelto, editor),
            ).fetchone()
            if existing:
                raise ValueError("duplicado")
            pseudo_id = f"manual:{editor.lower()}:{cliente_resuelto.lower().replace(' ', '_')}:{int(_t.time() * 1000000)}"
            conn.execute(
                """INSERT INTO tasks (cliente, editor, file_id, file_name, detected_at, status, mail_sent_at, pending_count)
                   VALUES (?, ?, ?, ?, ?, 'pending', ?, 1)""",
                (cliente_resuelto, editor, pseudo_id, "(pendiente cargado manualmente)", now_iso(), now_iso()),
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
        token = (params.get("t", [""])[0] or "").strip()
        editor = (params.get("editor", [""])[0] or "").strip()
        cliente = (params.get("cliente", [""])[0] or "").strip()
        is_admin = params.get("admin", [""])[0] == "1"
        task_id_str = params.get("id", [""])[0]

        if is_admin:
            if not check_token("ADMIN", token):
                return json_response(self, {"error": "unauthorized"}, status=401)
        else:
            if not editor or not check_token(editor, token):
                return json_response(self, {"error": "unauthorized"}, status=401)

        # MODO CLIENTE: borrar TODAS las tasks pending de un cliente (+ editor opcional)
        if cliente:
            target_editor = editor if not is_admin else (editor or None)
            deleted = {"count": 0, "cliente": cliente, "editor": target_editor}

            def op_cliente(conn):
                cli = resolve_nickname(conn, cliente, target_editor) if target_editor else cliente
                deleted["cliente"] = cli
                if target_editor:
                    rows = conn.execute(
                        "DELETE FROM tasks WHERE TRIM(cliente)=TRIM(?) AND editor=? AND status='pending'",
                        (cli, target_editor),
                    )
                else:
                    rows = conn.execute(
                        "DELETE FROM tasks WHERE TRIM(cliente)=TRIM(?) AND status='pending'",
                        (cli,),
                    )
                deleted["count"] = rows.rowcount

            try:
                with_db(op_cliente, message=f"manual: borradas tasks de {cliente}" + (f" / {target_editor}" if target_editor else ""))
                return json_response(self, {"ok": True, **deleted})
            except Exception as e:
                return json_response(self, {"error": str(e)[:200]}, status=500)

        # MODO TASK: borrar una task específica por id (compatibilidad con código viejo)
        try:
            task_id = int(task_id_str)
        except ValueError:
            return json_response(self, {"error": "falta id o cliente"}, status=400)

        captured = {"cliente": None, "editor": None}

        def op_id(conn):
            row = conn.execute("SELECT id, cliente, editor FROM tasks WHERE id = ?", (task_id,)).fetchone()
            if not row:
                raise ValueError("notfound")
            if not is_admin and row["editor"] != editor:
                raise ValueError("forbidden")
            captured["cliente"] = row["cliente"]
            captured["editor"] = row["editor"]
            conn.execute("DELETE FROM tasks WHERE id = ?", (task_id,))

        try:
            with_db(op_id, message=f"manual: borrada task #{task_id}")
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

    def do_PATCH(self):
        """PATCH /api/task → body: {cliente, editor, count, t, [admin]}
        Setea el pending_count de una task pendiente."""
        try:
            length = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(length).decode("utf-8") if length > 0 else "{}"
            body = json.loads(raw)
        except Exception as e:
            return json_response(self, {"error": f"body inválido: {e}"}, status=400)

        token = (body.get("t") or "").strip()
        editor = (body.get("editor") or "").strip()
        cliente = (body.get("cliente") or "").strip()
        is_admin = body.get("admin") == 1
        try:
            count = int(body.get("count", 1))
        except (TypeError, ValueError):
            return json_response(self, {"error": "count debe ser número"}, status=400)
        if count < 0:
            return json_response(self, {"error": "count debe ser >= 0"}, status=400)

        if is_admin:
            if not check_token("ADMIN", token):
                return json_response(self, {"error": "unauthorized"}, status=401)
        else:
            if not editor or not check_token(editor, token):
                return json_response(self, {"error": "unauthorized"}, status=401)
        if not cliente:
            return json_response(self, {"error": "falta cliente"}, status=400)

        target_editor = editor if (not is_admin or editor) else None
        updated = {"count": 0}

        def op(conn):
            cliente_resuelto = resolve_nickname(conn, cliente, target_editor) if target_editor else cliente
            updated["count"] = _set_pending_count_op(conn, cliente_resuelto, target_editor, count)

        try:
            with_db(op, message=f"manual: count={count} para {cliente}" + (f" / {target_editor}" if target_editor else ""))
            return json_response(self, {"ok": True, "cliente": cliente, "editor": target_editor, "count": count, "affected": updated["count"]})
        except Exception as e:
            return json_response(self, {"error": str(e)[:200]}, status=500)

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "POST, PATCH, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def log_message(self, *args, **kwargs):
        pass

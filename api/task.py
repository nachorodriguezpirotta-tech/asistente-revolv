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
      1. Apodo registrado en cfg_nicknames (con prioridad editor-específico)
      2. Fuzzy match contra TODOS los clientes conocidos del sistema
         (tasks pending, clients, known_files, known_edited_files).
         Si hay UN match único → usar. Si hay >1 match, priorizar:
            a) cliente del MISMO editor
            b) cliente con tasks pending (más reciente)
         Si todo empata, devolver original (el user decide).
    """
    if not cliente_input:
        return cliente_input

    # 1. Diccionario configurado (DB con fallback hardcoded)
    try:
        from aliases import resolve_nickname_static
        nick = resolve_nickname_static(cliente_input, editor=editor)
        if nick != cliente_input:
            return nick
    except Exception:
        pass

    norm = _normalize(cliente_input)
    if len(norm) < 3:
        return cliente_input  # demasiado corto para fuzzy match seguro

    # 2. Construir universo de clientes conocidos
    universe = {}  # cliente_real → metadata {has_pending, same_editor, in_drive}

    # 2a. Clientes con tasks (cualquier editor)
    for r in conn.execute("SELECT DISTINCT TRIM(cliente) as c, editor, status FROM tasks").fetchall():
        if not r["c"]: continue
        ent = universe.setdefault(r["c"], {"has_pending": False, "same_editor": False, "in_drive": False})
        if r["status"] == "pending":
            ent["has_pending"] = True
        if editor and r["editor"] and _normalize(r["editor"]) == _normalize(editor):
            ent["same_editor"] = True

    # 2b. Clientes en tabla clients (carpetas Drive conocidas)
    for r in conn.execute("SELECT DISTINCT cliente FROM clients").fetchall():
        if not r["cliente"]: continue
        ent = universe.setdefault(r["cliente"].strip(), {"has_pending": False, "same_editor": False, "in_drive": False})
        ent["in_drive"] = True

    # 2c. Clientes en known_files / known_edited_files
    for table in ("known_files", "known_edited_files"):
        try:
            for r in conn.execute(f"SELECT DISTINCT cliente FROM {table}").fetchall():
                if r["cliente"]:
                    universe.setdefault(r["cliente"].strip(), {"has_pending": False, "same_editor": False, "in_drive": False})
        except Exception:
            pass

    # 3. Buscar match exacto
    for k in universe:
        if _normalize(k) == norm:
            return k

    # 4. Match parcial: token >=3 chars en común
    candidates = []  # (cliente, metadata, match_strength)
    for k, meta in universe.items():
        k_norm = _normalize(k)
        strength = 0
        # contiene como substring
        if norm in k_norm:
            strength = 100
        # algún token del cliente coincide o tiene prefijo común con el input
        else:
            for token in k_norm.split():
                if len(token) >= 3:
                    # match exacto de token
                    if token == norm:
                        strength = max(strength, 90)
                    # token empieza con input (o vice versa) — prefijo de 4+ chars
                    elif token.startswith(norm) and len(norm) >= 3:
                        strength = max(strength, 80)
                    elif norm.startswith(token) and len(token) >= 3:
                        strength = max(strength, 70)
                    # prefijo común de 4+ chars
                    elif len(token) >= 4 and len(norm) >= 4 and token[:4] == norm[:4]:
                        strength = max(strength, 60)
        if strength > 0:
            candidates.append((k, meta, strength))

    if not candidates:
        return cliente_input

    # 5. Decidir el ganador
    # Prioridad: mismo editor + has_pending > strength
    def score(c):
        k, meta, strength = c
        return (
            meta["same_editor"] and meta["has_pending"],
            meta["same_editor"],
            meta["has_pending"],
            strength,
        )
    candidates.sort(key=score, reverse=True)

    # Si el top es claramente mejor que el segundo (en score tuple), devolverlo.
    # Si hay empate en el top score, devolver original (ambiguo).
    if len(candidates) == 1:
        return candidates[0][0]
    top_score = score(candidates[0])
    second_score = score(candidates[1])
    if top_score != second_score:
        return candidates[0][0]

    # Empate → ambiguo, no resolver (el user puede ser más específico)
    return cliente_input


def _set_pending_count_op(conn, cliente, editor, count):
    """Setea pending_count para una task pending de cliente+editor Y MARCA count_locked=1
    para que el scan automático no lo sobrescriba."""
    if editor:
        rows = conn.execute(
            "UPDATE tasks SET pending_count=?, count_locked=1 WHERE TRIM(cliente)=TRIM(?) AND editor=? AND status='pending'",
            (count, cliente, editor),
        )
    else:
        rows = conn.execute(
            "UPDATE tasks SET pending_count=?, count_locked=1 WHERE TRIM(cliente)=TRIM(?) AND status='pending'",
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
            # count_locked=1 desde el principio: significa "esto es manual, el scan
            # automático NO debe sobreescribir count NI crear duplicado para otro editor".
            conn.execute(
                """INSERT INTO tasks (cliente, editor, file_id, file_name, detected_at, status, mail_sent_at, pending_count, count_locked)
                   VALUES (?, ?, ?, ?, ?, 'pending', ?, 1, 1)""",
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
            # "— sin editor —" es el label que el dashboard pone para tasks con
            # editor=NULL en la DB. Si viene ese string, lo tratamos como "sin editor".
            is_no_editor_placeholder = bool(editor and editor.startswith("—"))
            if is_no_editor_placeholder:
                editor = ""
            target_editor = editor if not is_admin else (editor or None)
            deleted = {"count": 0, "cliente": cliente, "editor": target_editor}

            def op_cliente(conn):
                from datetime import datetime, timedelta
                if target_editor:
                    # Editor específico → borrar solo de ese editor
                    rows = conn.execute(
                        "DELETE FROM tasks WHERE TRIM(cliente)=TRIM(?) AND editor=? AND status='pending'",
                        (cliente, target_editor),
                    )
                elif is_no_editor_placeholder or (editor == "" and not is_admin):
                    # "Sin editor" → borrar las tasks con editor=NULL o ''
                    rows = conn.execute(
                        "DELETE FROM tasks WHERE TRIM(cliente)=TRIM(?) AND (editor IS NULL OR editor='') AND status='pending'",
                        (cliente,),
                    )
                else:
                    # Admin sin editor especificado: borrar TODAS las tasks del cliente
                    rows = conn.execute(
                        "DELETE FROM tasks WHERE TRIM(cliente)=TRIM(?) AND status='pending'",
                        (cliente,),
                    )
                count_deleted = rows.rowcount
                cli = cliente
                # Si no encontró nada, fallback al resolved (por si vino apodo)
                if count_deleted == 0 and target_editor:
                    resolved = resolve_nickname(conn, cliente, target_editor)
                    if resolved != cliente:
                        rows = conn.execute(
                            "DELETE FROM tasks WHERE TRIM(cliente)=TRIM(?) AND editor=? AND status='pending'",
                            (resolved, target_editor),
                        )
                        count_deleted = rows.rowcount
                        cli = resolved
                deleted["cliente"] = cli
                deleted["count"] = count_deleted
                # Bloquear re-creación automática del cliente por 24 horas
                # Bloquear AMBOS nombres por si vino apodo
                blocked_until = (datetime.now() + timedelta(hours=24)).isoformat(timespec="seconds")
                conn.execute("""
                    INSERT INTO client_blocks (cliente, editor, blocked_until)
                    VALUES (TRIM(?), ?, ?)
                    ON CONFLICT(cliente, editor) DO UPDATE SET blocked_until=excluded.blocked_until
                """, (cli, target_editor or "", blocked_until))
                if cli != cliente:
                    conn.execute("""
                        INSERT INTO client_blocks (cliente, editor, blocked_until)
                        VALUES (TRIM(?), ?, ?)
                        ON CONFLICT(cliente, editor) DO UPDATE SET blocked_until=excluded.blocked_until
                    """, (cliente, target_editor or "", blocked_until))

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
        """PATCH /api/task → 2 modos:
           - Modo "task": body {cliente, editor, count, t, [admin]} → setea pending_count
           - Modo "progress": body {progress: 1, editor, current, total, t, [admin]} → setea editor_progress
        """
        try:
            length = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(length).decode("utf-8") if length > 0 else "{}"
            body = json.loads(raw)
        except Exception as e:
            return json_response(self, {"error": f"body inválido: {e}"}, status=400)

        token = (body.get("t") or "").strip()
        editor = (body.get("editor") or "").strip()
        is_admin = body.get("admin") == 1

        # MODO PROGRESS: editar editor_progress (current/total del pack)
        if body.get("progress") == 1:
            if is_admin:
                if not check_token("ADMIN", token):
                    return json_response(self, {"error": "unauthorized"}, status=401)
            else:
                if not editor or not check_token(editor, token):
                    return json_response(self, {"error": "unauthorized"}, status=401)
            if not editor:
                return json_response(self, {"error": "falta editor"}, status=400)
            label = (body.get("label") or "Básicos").strip()
            try:
                current = int(body.get("current", 0))
                total = int(body.get("total", 0))
            except (TypeError, ValueError):
                return json_response(self, {"error": "current/total deben ser números"}, status=400)
            if current < 0 or total < 0:
                return json_response(self, {"error": "valores >= 0"}, status=400)

            def op_prog(conn):
                conn.execute("""
                    INSERT INTO editor_progress (editor, label, current, total, updated_at)
                    VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(editor, label) DO UPDATE SET
                        current=excluded.current,
                        total=excluded.total,
                        updated_at=excluded.updated_at
                """, (editor, label, current, total, now_iso()))

            try:
                with_db(op_prog, message=f"manual: progress {editor}/{label} = {current}/{total}")
                return json_response(self, {"ok": True, "editor": editor, "label": label, "current": current, "total": total})
            except Exception as e:
                return json_response(self, {"error": str(e)[:200]}, status=500)

        # MODO SET_NOTE: agregar/editar nota de una task pending
        if body.get("action") == "set_note":
            if not is_admin or not check_token("ADMIN", token):
                return json_response(self, {"error": "unauthorized (admin)"}, status=401)
            cliente_n = (body.get("cliente") or "").strip()
            editor_n = (body.get("editor") or "").strip()
            note = body.get("note", "")
            note = note.strip() if note else None
            if not cliente_n:
                return json_response(self, {"error": "falta cliente"}, status=400)

            def op_note(conn):
                if editor_n:
                    conn.execute(
                        "UPDATE tasks SET note=? WHERE TRIM(cliente)=TRIM(?) AND editor=? AND status='pending'",
                        (note, cliente_n, editor_n),
                    )
                else:
                    conn.execute(
                        "UPDATE tasks SET note=? WHERE TRIM(cliente)=TRIM(?) AND status='pending'",
                        (note, cliente_n),
                    )

            try:
                with_db(op_note, message=f"manual: note {cliente_n}")
                return json_response(self, {"ok": True, "cliente": cliente_n, "note": note})
            except Exception as e:
                return json_response(self, {"error": str(e)[:200]}, status=500)

        # MODO SET_URGENT: marcar/desmarcar urgente
        if body.get("action") == "set_urgent":
            if not is_admin or not check_token("ADMIN", token):
                return json_response(self, {"error": "unauthorized (admin)"}, status=401)
            cliente_u = (body.get("cliente") or "").strip()
            editor_u = (body.get("editor") or "").strip()
            urgent = 1 if body.get("urgent") else 0
            if not cliente_u:
                return json_response(self, {"error": "falta cliente"}, status=400)

            def op_urgent(conn):
                if editor_u:
                    conn.execute(
                        "UPDATE tasks SET urgent=? WHERE TRIM(cliente)=TRIM(?) AND editor=? AND status='pending'",
                        (urgent, cliente_u, editor_u),
                    )
                else:
                    conn.execute(
                        "UPDATE tasks SET urgent=? WHERE TRIM(cliente)=TRIM(?) AND status='pending'",
                        (urgent, cliente_u),
                    )

            try:
                with_db(op_urgent, message=f"manual: urgent={urgent} {cliente_u}")
                return json_response(self, {"ok": True, "cliente": cliente_u, "urgent": bool(urgent)})
            except Exception as e:
                return json_response(self, {"error": str(e)[:200]}, status=500)

        # MODO REASSIGN: cambiar editor de una task pending
        if body.get("action") == "reassign":
            if not is_admin or not check_token("ADMIN", token):
                return json_response(self, {"error": "unauthorized (admin)"}, status=401)
            cliente_r = (body.get("cliente") or "").strip()
            current_editor = (body.get("current_editor") or "").strip()
            new_editor = (body.get("new_editor") or "").strip()
            if not cliente_r or not new_editor:
                return json_response(self, {"error": "falta cliente o new_editor"}, status=400)

            # Detectar el placeholder "sin editor" del UI. El backend
            # (api/data.py) reemplaza editor NULL/'' por "— sin editor —"
            # para mostrar. El UI nos manda ese mismo string. Lo tratamos
            # como "filtrar por NULL o vacío" en el WHERE.
            # Bug 21/may: reassign "sin editor" → Fran no se guardaba porque
            # WHERE editor='— sin editor —' no matcheaba con editor=''.
            is_no_editor_placeholder = (
                not current_editor or
                current_editor.startswith("—") or
                "sin editor" in current_editor.lower()
            )

            updated_count = {"n": 0}
            def op_reassign(conn):
                # Verificar que NO exista ya una task del nuevo editor con el mismo cliente
                existing = conn.execute(
                    "SELECT id FROM tasks WHERE TRIM(cliente)=TRIM(?) AND editor=? AND status='pending'",
                    (cliente_r, new_editor),
                ).fetchone()
                if existing:
                    raise ValueError("ya_existe")
                # Reasignar
                if is_no_editor_placeholder:
                    # Match tasks con editor NULL o '' (string vacío)
                    r = conn.execute(
                        "UPDATE tasks SET editor=? WHERE TRIM(cliente)=TRIM(?) "
                        "AND (editor IS NULL OR editor='') AND status='pending'",
                        (new_editor, cliente_r),
                    )
                else:
                    r = conn.execute(
                        "UPDATE tasks SET editor=? WHERE TRIM(cliente)=TRIM(?) AND editor=? AND status='pending'",
                        (new_editor, cliente_r, current_editor),
                    )
                updated_count["n"] = r.rowcount
                if updated_count["n"] == 0:
                    # No actualizamos nada — el cliente no existe con el editor
                    # actual. Avisamos al frontend.
                    raise ValueError("not_found")
                # IMPORTANTE: persistir el cambio como override permanente para
                # que los próximos archivos de este cliente vayan al nuevo
                # editor. Antes el cambio era SOLO de las tasks actuales —
                # próximos scans creaban nueva task con el editor del Sheet
                # original, pisando lo que el user reasignó. Bug 29/may.
                from datetime import datetime as _dt
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS cfg_client_editor (
                        cliente TEXT PRIMARY KEY,
                        editor TEXT NOT NULL,
                        updated_at TEXT
                    )
                """)
                conn.execute("""
                    INSERT INTO cfg_client_editor (cliente, editor, updated_at)
                    VALUES (?, ?, ?)
                    ON CONFLICT(cliente) DO UPDATE SET editor=excluded.editor, updated_at=excluded.updated_at
                """, (cliente_r, new_editor, _dt.now().isoformat(timespec='seconds')))

            try:
                with_db(op_reassign, message=f"manual: reasignar {cliente_r}: {current_editor} → {new_editor}")
                return json_response(self, {"ok": True, "cliente": cliente_r, "from": current_editor, "to": new_editor, "affected": updated_count["n"]})
            except ValueError as e:
                if "ya_existe" in str(e):
                    return json_response(self, {"error": f"{new_editor} ya tiene pending de {cliente_r}"}, status=409)
                if "not_found" in str(e):
                    return json_response(self, {"error": f"No se encontró task pending de {cliente_r}"}, status=404)
                return json_response(self, {"error": str(e)[:200]}, status=500)
            except Exception as e:
                return json_response(self, {"error": str(e)[:200]}, status=500)

        # MODO TASK: editar pending_count
        cliente = (body.get("cliente") or "").strip()
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
            # En PATCH NO resolvemos apodo: la task ya existe con el nombre como está.
            # Si no se encuentra con el nombre tal cual, fallback al resolved (por si vino apodo).
            n = _set_pending_count_op(conn, cliente, target_editor, count)
            if n == 0 and target_editor:
                resolved = resolve_nickname(conn, cliente, target_editor)
                if resolved != cliente:
                    n = _set_pending_count_op(conn, resolved, target_editor, count)
            updated["count"] = n

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

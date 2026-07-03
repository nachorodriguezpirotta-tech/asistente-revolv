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
# ... y tasks_store/tracker de la raíz del repo
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

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

    # 2a. Clientes con tasks (desde TURSO — fuente de verdad, jul/2026)
    try:
        import tasks_store
        _task_rows = tasks_store.query("SELECT DISTINCT TRIM(cliente) as c, editor, status FROM tasks")
    except Exception:
        _task_rows = [dict(r) for r in conn.execute(
            "SELECT DISTINCT TRIM(cliente) as c, editor, status FROM tasks").fetchall()] if conn else []
    for r in _task_rows:
        if not r["c"]: continue
        ent = universe.setdefault(r["c"], {"has_pending": False, "same_editor": False, "in_drive": False})
        if r["status"] == "pending":
            ent["has_pending"] = True
        if editor and r["editor"] and _normalize(r["editor"]) == _normalize(editor):
            ent["same_editor"] = True

    # 2b. Clientes en tabla clients (carpetas Drive conocidas)
    for r in (conn.execute("SELECT DISTINCT cliente FROM clients").fetchall() if conn else []):
        if not r["cliente"]: continue
        ent = universe.setdefault(r["cliente"].strip(), {"has_pending": False, "same_editor": False, "in_drive": False})
        ent["in_drive"] = True

    # 2c. Clientes en known_files / known_edited_files
    for table in ("known_files", "known_edited_files"):
        try:
            if not conn:
                break
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


def _set_pending_count_op(cliente, editor, count):
    """Setea pending_count (count_locked=1) en TURSO. Instantáneo y transaccional."""
    import tasks_store
    if editor:
        r = tasks_store.execute(
            "UPDATE tasks SET pending_count=?, count_locked=1 WHERE TRIM(cliente)=TRIM(?) AND editor=? AND status='pending'",
            (count, cliente, editor))
    else:
        r = tasks_store.execute(
            "UPDATE tasks SET pending_count=?, count_locked=1 WHERE TRIM(cliente)=TRIM(?) AND status='pending'",
            (count, cliente))
    return r.get("affected") or 0


def _bundle_conn():
    """Conn sqlite de SOLO LECTURA para tablas frías (clients/known_files/cfg)
    usadas por resolve_nickname. En Vercel abre la copia BUNDLEADA del deploy
    (stale pero instantánea, sin bajar 6MB) — suficiente para resolver apodos.
    Las tasks NO se leen de acá (van directo a Turso)."""
    try:
        import tracker
        return tracker.get_conn()
    except Exception:
        return None


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

        # AGREGAR pendiente manual — directo a TURSO (jul/2026): antes with_db
        # (bajar+subir 6MB, 5-8s y a veces se pisaba). Ahora ~300ms transaccional.
        import tasks_store
        try:
            conn = _bundle_conn()
            try:
                cliente_resuelto = resolve_nickname(conn, cliente, editor)
            finally:
                if conn:
                    conn.close()
            existing = tasks_store.query(
                "SELECT id FROM tasks WHERE TRIM(cliente)=TRIM(?) AND editor=? AND status='pending' LIMIT 1",
                (cliente_resuelto, editor))
            if existing:
                return json_response(self, {"error": f"Ya hay un pendiente de '{cliente}'"}, status=409)
            pseudo_id = f"manual:{editor.lower()}:{cliente_resuelto.lower().replace(' ', '_')}:{int(_t.time() * 1000000)}"
            tasks_store.execute(
                "INSERT INTO tasks (cliente, editor, file_id, file_name, detected_at, status, mail_sent_at, pending_count, count_locked) "
                "VALUES (?, ?, ?, ?, ?, 'pending', ?, 1, 1)",
                (cliente_resuelto, editor, pseudo_id, "(pendiente cargado manualmente)", now_iso(), now_iso()))
            return json_response(self, {"ok": True, "cliente": cliente_resuelto, "editor": editor})
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

        import tasks_store

        # MODO CLIENTE: borrar TODAS las pending del cliente (+ editor opcional).
        # TURSO directo (jul/2026): transaccional, sin verify, instantáneo.
        if cliente:
            is_no_editor_placeholder = bool(editor and editor.startswith("—"))
            if is_no_editor_placeholder:
                editor = ""
            target_editor = editor if not is_admin else (editor or None)
            try:
                from datetime import datetime, timedelta
                if target_editor:
                    r = tasks_store.execute(
                        "DELETE FROM tasks WHERE TRIM(cliente)=TRIM(?) AND editor=? AND status='pending'",
                        (cliente, target_editor))
                elif is_no_editor_placeholder or (editor == "" and not is_admin):
                    r = tasks_store.execute(
                        "DELETE FROM tasks WHERE TRIM(cliente)=TRIM(?) AND (editor IS NULL OR editor='') AND status='pending'",
                        (cliente,))
                else:
                    r = tasks_store.execute(
                        "DELETE FROM tasks WHERE TRIM(cliente)=TRIM(?) AND status='pending'",
                        (cliente,))
                count_deleted = r.get("affected") or 0
                cli = cliente
                if count_deleted == 0 and target_editor:
                    conn = _bundle_conn()
                    try:
                        resolved = resolve_nickname(conn, cliente, target_editor)
                    finally:
                        if conn:
                            conn.close()
                    if resolved != cliente:
                        r = tasks_store.execute(
                            "DELETE FROM tasks WHERE TRIM(cliente)=TRIM(?) AND editor=? AND status='pending'",
                            (resolved, target_editor))
                        count_deleted = r.get("affected") or 0
                        cli = resolved
                # Bloqueo CORTO (2 días): evita que el scan re-cree la tarjeta ya,
                # pero los crudos NUEVOS posteriores vuelven a detectarse solos.
                # (El bloqueo de 10 años mataba clientes para siempre — 30/jun.)
                # Los crudos actuales ya están claimeados en known_files, no se
                # re-procesan. Para apagar un cliente PARA SIEMPRE: Archivar.
                blocked_until = (datetime.now() + timedelta(days=2)).isoformat(timespec="seconds")
                stmts = []
                for nombre in {cli, cliente}:
                    stmts.append((
                        "INSERT INTO client_blocks (cliente, editor, blocked_until) VALUES (TRIM(?), '', ?) "
                        "ON CONFLICT(cliente, editor) DO UPDATE SET blocked_until=excluded.blocked_until",
                        (nombre, blocked_until)))
                tasks_store.execute_many(stmts)
                return json_response(self, {"ok": True, "count": count_deleted,
                                            "cliente": cli, "editor": target_editor})
            except Exception as e:
                return json_response(self, {"error": str(e)[:200]}, status=500)

        # MODO TASK: borrar por id
        try:
            task_id = int(task_id_str)
        except ValueError:
            return json_response(self, {"error": "falta id o cliente"}, status=400)
        try:
            rows = tasks_store.query("SELECT id, cliente, editor FROM tasks WHERE id = ?", (task_id,))
            if not rows:
                return json_response(self, {"error": f"task #{task_id} no existe"}, status=404)
            row = rows[0]
            if not is_admin and row["editor"] != editor:
                return json_response(self, {"error": "No podés borrar tareas de otro editor"}, status=403)
            tasks_store.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
            return json_response(self, {"ok": True, "task_id": task_id,
                                        "cliente": row["cliente"], "editor": row["editor"]})
        except Exception as e:
            return json_response(self, {"error": str(e)[:200]}, status=500)

    def do_PATCH(self):
        """PATCH /api/task — todas las mutaciones van a TURSO (jul/2026):
        transaccionales, ~300ms, sin verify (no hay pisadas posibles)."""
        try:
            length = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(length).decode("utf-8") if length > 0 else "{}"
            body = json.loads(raw)
        except Exception as e:
            return json_response(self, {"error": f"body inválido: {e}"}, status=400)

        token = (body.get("t") or "").strip()
        editor = (body.get("editor") or "").strip()
        is_admin = body.get("admin") == 1
        import tasks_store

        # MODO BATCH: varios counts en UN request a Turso
        if isinstance(body.get("batch"), list):
            if is_admin:
                if not check_token("ADMIN", token):
                    return json_response(self, {"error": "unauthorized"}, status=401)
            else:
                if not editor or not check_token(editor, token):
                    return json_response(self, {"error": "unauthorized"}, status=401)
            changes = []
            for it in body["batch"]:
                if not isinstance(it, dict):
                    continue
                c = (it.get("cliente") or "").strip()
                e = (it.get("editor") or "").strip()
                if not is_admin:
                    e = editor
                e = e or None
                try:
                    cnt = int(it.get("count"))
                except (TypeError, ValueError):
                    continue
                if not c or cnt < 0:
                    continue
                changes.append((c, e, cnt))
            if not changes:
                return json_response(self, {"error": "batch vacío"}, status=400)
            try:
                total = 0
                for (c, e, cnt) in changes:
                    n = _set_pending_count_op(c, e, cnt)
                    if n == 0 and e:
                        conn = _bundle_conn()
                        try:
                            resolved = resolve_nickname(conn, c, e)
                        finally:
                            if conn:
                                conn.close()
                        if resolved != c:
                            n = _set_pending_count_op(resolved, e, cnt)
                    total += n
                return json_response(self, {"ok": True, "applied": total,
                                            "clientes": len(changes)})
            except Exception as e:
                return json_response(self, {"error": str(e)[:200]}, status=500)

        # MODO PROGRESS
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
            try:
                tasks_store.execute(
                    "INSERT INTO editor_progress (editor, label, current, total, updated_at) VALUES (?, ?, ?, ?, ?) "
                    "ON CONFLICT(editor, label) DO UPDATE SET current=excluded.current, "
                    "total=excluded.total, updated_at=excluded.updated_at",
                    (editor, label, current, total, now_iso()))
                return json_response(self, {"ok": True, "editor": editor, "label": label,
                                            "current": current, "total": total})
            except Exception as e:
                return json_response(self, {"error": str(e)[:200]}, status=500)

        # MODO SET_PRIORITY
        if body.get("action") == "set_priority":
            target_editor_p = (body.get("editor") or "").strip()
            order = body.get("order")
            if not target_editor_p or not isinstance(order, list):
                return json_response(self, {"error": "falta editor u order (lista)"}, status=400)
            if is_admin:
                if not check_token("ADMIN", token):
                    return json_response(self, {"error": "unauthorized"}, status=401)
            else:
                if not check_token(target_editor_p, token):
                    return json_response(self, {"error": "unauthorized"}, status=401)
            clean = [str(c).strip() for c in order if str(c).strip()][:200]
            try:
                stmts = [("DELETE FROM cfg_delivery_priority WHERE editor=?", (target_editor_p,))]
                for i, cli in enumerate(clean):
                    stmts.append((
                        "INSERT INTO cfg_delivery_priority (editor, cliente, priority, updated_at) VALUES (?,?,?,?)",
                        (target_editor_p, cli, i, now_iso())))
                tasks_store.execute_many(stmts)
                return json_response(self, {"ok": True, "editor": target_editor_p, "orden": len(clean)})
            except Exception as e:
                return json_response(self, {"error": str(e)[:200]}, status=500)

        # MODO SET_NOTE
        if body.get("action") == "set_note":
            if not is_admin or not check_token("ADMIN", token):
                return json_response(self, {"error": "unauthorized (admin)"}, status=401)
            cliente_n = (body.get("cliente") or "").strip()
            editor_n = (body.get("editor") or "").strip()
            note = body.get("note", "")
            note = note.strip() if note else None
            if not cliente_n:
                return json_response(self, {"error": "falta cliente"}, status=400)
            try:
                if editor_n:
                    tasks_store.execute(
                        "UPDATE tasks SET note=? WHERE TRIM(cliente)=TRIM(?) AND editor=? AND status='pending'",
                        (note, cliente_n, editor_n))
                else:
                    tasks_store.execute(
                        "UPDATE tasks SET note=? WHERE TRIM(cliente)=TRIM(?) AND status='pending'",
                        (note, cliente_n))
                return json_response(self, {"ok": True, "cliente": cliente_n, "note": note})
            except Exception as e:
                return json_response(self, {"error": str(e)[:200]}, status=500)

        # MODO SET_URGENT
        if body.get("action") == "set_urgent":
            if not is_admin or not check_token("ADMIN", token):
                return json_response(self, {"error": "unauthorized (admin)"}, status=401)
            cliente_u = (body.get("cliente") or "").strip()
            editor_u = (body.get("editor") or "").strip()
            urgent = 1 if body.get("urgent") else 0
            if not cliente_u:
                return json_response(self, {"error": "falta cliente"}, status=400)
            try:
                if editor_u:
                    tasks_store.execute(
                        "UPDATE tasks SET urgent=? WHERE TRIM(cliente)=TRIM(?) AND editor=? AND status='pending'",
                        (urgent, cliente_u, editor_u))
                else:
                    tasks_store.execute(
                        "UPDATE tasks SET urgent=? WHERE TRIM(cliente)=TRIM(?) AND status='pending'",
                        (urgent, cliente_u))
                return json_response(self, {"ok": True, "cliente": cliente_u, "urgent": bool(urgent)})
            except Exception as e:
                return json_response(self, {"error": str(e)[:200]}, status=500)

        # MODO REASSIGN
        if body.get("action") == "reassign":
            if not is_admin or not check_token("ADMIN", token):
                return json_response(self, {"error": "unauthorized (admin)"}, status=401)
            cliente_r = (body.get("cliente") or "").strip()
            current_editor = (body.get("current_editor") or "").strip()
            new_editor = (body.get("new_editor") or "").strip()
            if not cliente_r or not new_editor:
                return json_response(self, {"error": "falta cliente o new_editor"}, status=400)
            is_no_editor_placeholder = (
                not current_editor or
                current_editor.startswith("—") or
                "sin editor" in current_editor.lower())
            try:
                existing = tasks_store.query(
                    "SELECT id FROM tasks WHERE TRIM(cliente)=TRIM(?) AND editor=? AND status='pending' LIMIT 1",
                    (cliente_r, new_editor))
                if existing:
                    return json_response(self, {"error": f"{new_editor} ya tiene pending de {cliente_r}"}, status=409)
                if is_no_editor_placeholder:
                    r = tasks_store.execute(
                        "UPDATE tasks SET editor=? WHERE TRIM(cliente)=TRIM(?) "
                        "AND (editor IS NULL OR editor='') AND status='pending'",
                        (new_editor, cliente_r))
                else:
                    r = tasks_store.execute(
                        "UPDATE tasks SET editor=? WHERE TRIM(cliente)=TRIM(?) AND editor=? AND status='pending'",
                        (new_editor, cliente_r, current_editor))
                n = r.get("affected") or 0
                if n == 0:
                    return json_response(self, {"error": f"No se encontró task pending de {cliente_r}"}, status=404)
                # Override permanente: próximos archivos del cliente van al nuevo
                # editor (cfg_client_editor es tabla caliente en Turso, el scan
                # la ve vía espejo). Bug 29/may.
                tasks_store.execute(
                    "INSERT INTO cfg_client_editor (cliente, editor, updated_at) VALUES (?, ?, ?) "
                    "ON CONFLICT(cliente) DO UPDATE SET editor=excluded.editor, updated_at=excluded.updated_at",
                    (cliente_r, new_editor, now_iso()))
                return json_response(self, {"ok": True, "cliente": cliente_r,
                                            "from": current_editor, "to": new_editor, "affected": n})
            except Exception as e:
                return json_response(self, {"error": str(e)[:200]}, status=500)

        # MODO TASK: editar pending_count (single)
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
        try:
            n = _set_pending_count_op(cliente, target_editor, count)
            if n == 0 and target_editor:
                conn = _bundle_conn()
                try:
                    resolved = resolve_nickname(conn, cliente, target_editor)
                finally:
                    if conn:
                        conn.close()
                if resolved != cliente:
                    n = _set_pending_count_op(resolved, target_editor, count)
            return json_response(self, {"ok": True, "cliente": cliente, "editor": target_editor,
                                        "count": count, "affected": n})
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

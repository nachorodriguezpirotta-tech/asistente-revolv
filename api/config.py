"""
GET /api/config?admin=1&t=<admin_token>
  → Devuelve todas las tablas de config (editores, nicknames, aliases, delivery_folders)

POST/PATCH/DELETE /api/config
  body JSON con campos:
    - section: 'editor' | 'nickname' | 'alias' | 'delivery'
    - action: 'create' | 'update' | 'delete'
    - data: { campos según sección }
    - admin: 1
    - t: <admin_token>

Todas las operaciones requieren token admin.
"""

import json
import os
import sys
import traceback
from http.server import BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

try:
    from _shared import check_token, json_response, with_db, GITHUB_PAT
    _IMPORT_ERROR = None
except Exception as _e:
    _IMPORT_ERROR = f"{type(_e).__name__}: {_e}\n{traceback.format_exc()}"


def _get_all_config(conn):
    """Devuelve {editors, nicknames, aliases, delivery_folders, pending_folders}."""
    editors = [dict(r) for r in conn.execute(
        "SELECT name, email, receives_daily_summary, receives_notifications, on_vacation, active FROM cfg_editors ORDER BY name"
    ).fetchall()]
    nicknames = [dict(r) for r in conn.execute(
        "SELECT id, nickname, cliente_real, editor FROM cfg_nicknames ORDER BY nickname"
    ).fetchall()]
    aliases = [dict(r) for r in conn.execute(
        "SELECT id, drive_name, cliente_real FROM cfg_aliases ORDER BY drive_name"
    ).fetchall()]
    delivery = [dict(r) for r in conn.execute(
        "SELECT id, cliente, folder_id, description FROM cfg_delivery_folders ORDER BY cliente"
    ).fetchall()]
    client_emails = []
    try:
        client_emails = [dict(r) for r in conn.execute(
            "SELECT cliente, email, display_name, notifications_enabled FROM cfg_clients ORDER BY cliente"
        ).fetchall()]
    except Exception:
        pass
    # Lista de TODOS los clientes que conocemos (para dropdown en UI).
    # Evita typos al agregar mails — Ignacio elige de la lista en vez de tipear.
    available_clients = []
    try:
        rows = conn.execute("""
            SELECT DISTINCT TRIM(cliente) AS cliente FROM (
                SELECT cliente FROM clients
                UNION SELECT cliente FROM tasks
                UNION SELECT cliente FROM known_files
                UNION SELECT cliente FROM known_edited_files
            )
            WHERE cliente IS NOT NULL AND TRIM(cliente) != ''
            ORDER BY TRIM(cliente)
        """).fetchall()
        available_clients = [r["cliente"] for r in rows]
    except Exception:
        pass
    pending_folders = []
    try:
        pending_folders = [dict(r) for r in conn.execute(
            "SELECT folder_id, folder_name, detected_at FROM pending_drive_folders WHERE status='pending' ORDER BY detected_at DESC"
        ).fetchall()]
    except Exception:
        pass
    # Mail log (últimos 100)
    mail_log = []
    try:
        mail_log = [dict(r) for r in conn.execute(
            "SELECT sent_at, to_email, subject, kind, cliente, editor, success FROM mail_log ORDER BY sent_at DESC LIMIT 100"
        ).fetchall()]
    except Exception:
        pass

    # Asignación editor por cliente — combina cfg_client_editor (override DB)
    # + el Sheet. Para cada cliente conocido, devuelve editor actual + flag
    # si es override o viene del Sheet. Pedido Nacho 28/may.
    client_editors = []
    # Asegurar tabla de overrides (idempotente)
    try:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS cfg_client_editor (
                cliente TEXT PRIMARY KEY,
                editor TEXT NOT NULL,
                updated_at TEXT
            )
        """)
    except Exception:
        pass
    # Cargar overrides
    overrides = {}
    try:
        overrides = {r["cliente"]: r["editor"] for r in conn.execute(
            "SELECT cliente, editor FROM cfg_client_editor"
        ).fetchall()}
    except Exception:
        pass

    # Cargar archivados (clientes que el user borró desde el dashboard).
    # Filtramos client_editors después del grouping — si el canonical O
    # cualquier alias está archivado, todo el grupo se oculta.
    archived_clients = set()
    archived_list = []
    try:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS cfg_archived_clients (
                cliente TEXT PRIMARY KEY,
                archived_at TEXT
            )
        """)
        for r in conn.execute("SELECT cliente FROM cfg_archived_clients").fetchall():
            archived_clients.add(r["cliente"])
        archived_list = [
            {"cliente": r["cliente"], "archived_at": r["archived_at"]}
            for r in conn.execute(
                "SELECT cliente, archived_at FROM cfg_archived_clients ORDER BY archived_at DESC"
            ).fetchall()
        ]
    except Exception:
        pass
    # Editor por cliente — desde tabla cfg_excel_clients (sincronizada desde
    # el Sheet vía GHA/script local). Es la fuente de verdad: tiene TODOS los
    # clientes del Excel con su editor asignado, no solo los que tienen task.
    sheet_editors = {}  # {normalized_name: (cliente_canonical, editor)}
    import unicodedata
    def _norm(s):
        s = unicodedata.normalize("NFD", s or "")
        s = "".join(c for c in s if unicodedata.category(c) != "Mn")
        return " ".join(s.lower().split())
    def _tokens(s):
        # tokens significativos (≥3 chars, sin stopwords) para fuzzy subset match
        STOP = {"de","del","la","el","los","las","y","e","o","u","a","con","sin","para","por"}
        return [t for t in _norm(s).replace("/", " ").replace("-", " ").split()
                if len(t) >= 3 and t not in STOP]
    try:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS cfg_excel_clients (
                cliente TEXT PRIMARY KEY,
                editor TEXT,
                row_in_sheet INTEGER,
                synced_at TEXT
            )
        """)
        rows = conn.execute(
            "SELECT cliente, editor FROM cfg_excel_clients WHERE editor IS NOT NULL AND editor != ''"
        ).fetchall()
        for r in rows:
            n = _norm(r["cliente"])
            if n:
                sheet_editors[n] = (r["cliente"], r["editor"])
    except Exception:
        pass

    # Build response: cada cliente conocido de Drive/tasks/etc se matchea
    # contra el Excel — exact normalized → exact alias → fuzzy subset.
    try:
        # Pre-build token sets del Excel
        excel_tokens = {}  # excel_cliente → set(tokens)
        for k, (excel_cli, _) in sheet_editors.items():
            excel_tokens[excel_cli] = set(_tokens(excel_cli))

        for cli in available_clients:
            ovr = overrides.get(cli)
            if ovr:
                client_editors.append({"cliente": cli, "editor": ovr, "source": "override"})
                continue
            n = _norm(cli)
            # 1) Match exacto normalizado
            hit = sheet_editors.get(n)
            if hit:
                client_editors.append({"cliente": cli, "editor": hit[1], "source": "sheet"})
                continue
            # 2) Fuzzy subset: si tokens del cli son subset (o superset) de algún excel cliente
            cli_t = set(_tokens(cli))
            if cli_t:
                best = None
                best_overlap = 0
                for excel_cli, et in excel_tokens.items():
                    if not et:
                        continue
                    # subset match en cualquier dirección
                    if cli_t <= et or et <= cli_t:
                        overlap = len(cli_t & et)
                        if overlap > best_overlap:
                            best_overlap = overlap
                            best = excel_cli
                if best:
                    editor = sheet_editors[_norm(best)][1]
                    client_editors.append({"cliente": cli, "editor": editor, "source": "sheet"})
                    continue
            client_editors.append({"cliente": cli, "editor": None, "source": "ninguno"})

        # Agregar clientes que están en Excel pero NO en available_clients
        # (clientes en Excel sin folder de Drive todavía). Esos se ven igual.
        avail_norm = {_norm(c) for c in available_clients}
        for k, (excel_cli, editor) in sheet_editors.items():
            if k not in avail_norm:
                cli_t = set(_tokens(excel_cli))
                matched = False
                for c in available_clients:
                    ct = set(_tokens(c))
                    if cli_t and ct and (cli_t <= ct or ct <= cli_t):
                        matched = True
                        break
                if not matched:
                    client_editors.append({"cliente": excel_cli, "editor": editor, "source": "sheet"})
    except Exception:
        pass

    # AGRUPACIÓN: combinar variantes del mismo cliente en una sola fila.
    # Reglas (conservadoras para evitar falsos positivos):
    #   1. mismo nombre normalizado → grupo
    #   2. shorter (≥2 tokens significativos) tokens ⊆ longer tokens → grupo
    #   3. shorter (1 token) es prefix exacto del longer (>= 4 chars) → grupo
    # No agrupa: "Darien" + "Jorge y Darien" (no prefix); "Amir" + "Daniel" (no prefix).
    try:
        def _prefix_match(a, b):
            # ¿"a" es prefix exacto de "b" considerando palabras completas?
            na, nb = _norm(a), _norm(b)
            if not na or not nb or len(na) < 4:
                return False
            if nb == na:
                return True
            # b debe empezar con a seguido de espacio (palabra completa)
            return nb.startswith(na + " ")

        # Build grupos
        groups = []  # list of {canonical, members:[{cli,editor,source}], editor, source}
        consumed = set()  # indices already grouped

        def _add_to_group(group, member):
            group["members"].append(member)
            # Preferir editor de override > sheet > ninguno
            pri = {"override": 3, "sheet": 2, "ninguno": 1}
            if pri.get(member["source"], 0) > pri.get(group["source"], 0):
                group["editor"] = member["editor"]
                group["source"] = member["source"]
            # Preferir el canonical más largo
            if len(member["cliente"]) > len(group["canonical"]):
                group["canonical"] = member["cliente"]

        # Sort by length DESC para que los más completos se agrupen primero
        indexed = list(enumerate(client_editors))
        indexed.sort(key=lambda x: -len(x[1]["cliente"]))

        for idx, ce in indexed:
            if idx in consumed:
                continue
            # Crear grupo nuevo con este como base
            group = {
                "canonical": ce["cliente"],
                "editor": ce["editor"],
                "source": ce["source"],
                "members": [ce],
            }
            consumed.add(idx)
            cli_t = set(_tokens(ce["cliente"]))
            # Buscar todos los que matchean
            for idx2, ce2 in indexed:
                if idx2 in consumed:
                    continue
                cli2_t = set(_tokens(ce2["cliente"]))
                merge = False
                # Regla 1: exact normalized
                if _norm(ce["cliente"]) == _norm(ce2["cliente"]):
                    merge = True
                # Regla 2: shorter (≥2 tokens) ⊆ longer
                elif cli_t and cli2_t:
                    if len(cli2_t) <= len(cli_t) and len(cli2_t) >= 2 and cli2_t <= cli_t:
                        merge = True
                    elif len(cli_t) <= len(cli2_t) and len(cli_t) >= 2 and cli_t <= cli2_t:
                        merge = True
                # Regla 3: shorter (1 token) es prefix exacto
                if not merge:
                    if _prefix_match(ce2["cliente"], ce["cliente"]) or _prefix_match(ce["cliente"], ce2["cliente"]):
                        merge = True
                if merge:
                    _add_to_group(group, ce2)
                    consumed.add(idx2)
            groups.append(group)

        # Reemplazar client_editors con la versión agrupada.
        # Filtrar grupos donde el canonical O cualquier alias está archivado.
        client_editors = []
        for g in sorted(groups, key=lambda x: x["canonical"].lower()):
            all_names = [m["cliente"] for m in g["members"]]
            if any(n in archived_clients for n in all_names):
                continue  # grupo archivado, no mostrar
            aliases = [n for n in all_names if n != g["canonical"]]
            client_editors.append({
                "cliente": g["canonical"],
                "editor": g["editor"],
                "source": g["source"],
                "aliases": aliases,
            })
    except Exception:
        pass

    return {
        "editors": editors,
        "nicknames": nicknames,
        "aliases": aliases,
        "delivery_folders": delivery,
        "pending_folders": pending_folders,
        "mail_log": mail_log,
        "client_emails": client_emails,
        "available_clients": available_clients,
        "client_editors": client_editors,
        "archived_clients": archived_list,
    }


class handler(BaseHTTPRequestHandler):

    def _auth(self, token):
        return check_token("ADMIN", token)

    def do_GET(self):
        try:
            if _IMPORT_ERROR:
                return json_response(self, {"error": "import", "detail": _IMPORT_ERROR}, status=500)
            params = parse_qs(urlparse(self.path).query)
            if params.get("admin", [""])[0] != "1":
                return json_response(self, {"error": "admin required"}, status=401)
            token = (params.get("t", [""])[0] or "").strip()
            if not self._auth(token):
                return json_response(self, {"error": "unauthorized"}, status=401)

            from _shared import read_db
            data = read_db(_get_all_config)
            return json_response(self, {"ok": True, **data})
        except Exception as e:
            return json_response(self, {"error": str(e)[:200], "trace": traceback.format_exc()[:1500]}, status=500)

    def _read_body(self):
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length).decode("utf-8") if length > 0 else "{}"
        return json.loads(raw)

    def do_POST(self):
        return self._handle_mutation()

    def do_PATCH(self):
        return self._handle_mutation()

    def do_DELETE(self):
        return self._handle_mutation()

    def _handle_mutation(self):
        try:
            if _IMPORT_ERROR:
                return json_response(self, {"error": "import", "detail": _IMPORT_ERROR}, status=500)
            try:
                body = self._read_body()
            except Exception as e:
                return json_response(self, {"error": f"body inválido: {e}"}, status=400)

            if body.get("admin") != 1:
                return json_response(self, {"error": "admin required"}, status=401)
            token = (body.get("t") or "").strip()
            if not self._auth(token):
                return json_response(self, {"error": "unauthorized"}, status=401)

            section = (body.get("section") or "").strip().lower()
            action = (body.get("action") or "").strip().lower()
            data = body.get("data") or {}

            if section not in ("editor", "nickname", "alias", "delivery", "pending_folder", "client_email", "client_editor", "archive_client"):
                return json_response(self, {"error": f"section inválida: {section}"}, status=400)
            if action not in ("create", "update", "delete"):
                return json_response(self, {"error": f"action inválida: {action}"}, status=400)

            result = {}

            def op(conn):
                if section == "editor":
                    self._op_editor(conn, action, data, result)
                elif section == "nickname":
                    self._op_nickname(conn, action, data, result)
                elif section == "alias":
                    self._op_alias(conn, action, data, result)
                elif section == "delivery":
                    self._op_delivery(conn, action, data, result)
                elif section == "pending_folder":
                    self._op_pending_folder(conn, action, data, result)
                elif section == "client_email":
                    self._op_client_email(conn, action, data, result)
                elif section == "client_editor":
                    self._op_client_editor(conn, action, data, result)
                elif section == "archive_client":
                    self._op_archive_client(conn, action, data, result)

            # verify: confirma que el cambio PERSISTIÓ tras el push (no fue
            # pisado por un scan concurrente). with_db reintenta si falla.
            def _verify(conn):
                try:
                    if action == "delete":
                        return True  # borrados: no re-verificamos
                    if section == "client_email":
                        cli = (data.get("cliente") or "").strip()
                        email = (data.get("email") or "").strip().lower()
                        r = conn.execute(
                            "SELECT email FROM cfg_clients WHERE TRIM(cliente)=TRIM(?)", (cli,)
                        ).fetchone()
                        return bool(r and (r["email"] or "").lower() == email)
                    if section == "client_editor":
                        cli = (data.get("cliente") or "").strip()
                        ed = (data.get("editor") or "").strip()
                        r = conn.execute(
                            "SELECT editor FROM cfg_client_editor WHERE TRIM(cliente)=TRIM(?)", (cli,)
                        ).fetchone()
                        return bool(r and r["editor"] == ed)
                    if section == "editor":
                        nm = (data.get("name") or "").strip()
                        r = conn.execute("SELECT 1 FROM cfg_editors WHERE name=?", (nm,)).fetchone()
                        return bool(r)
                    if section == "pending_folder":
                        # Caso Mónica Vozmediano 30/jun: la aprobación pusheó pero un
                        # scan concurrente la pisó → el cliente NUNCA entró a `clients`
                        # → sin tarjeta, sin link, sin tasks automáticas. Verificar que
                        # la decisión Y el alta en clients persistieron; si no, with_db
                        # re-ejecuta la operación completa.
                        fid = (data.get("folder_id") or "").strip()
                        dec = (data.get("decision") or "").strip()
                        r = conn.execute(
                            "SELECT status FROM pending_drive_folders WHERE folder_id=?",
                            (fid,)).fetchone()
                        if not r or r["status"] != dec:
                            return False
                        if dec == "approved":
                            return conn.execute(
                                "SELECT 1 FROM clients WHERE folder_id=?", (fid,)
                            ).fetchone() is not None
                        return True
                except Exception:
                    return True  # si la verificación falla, no bloquear
                return True

            with_db(op, message=f"config: {action} {section}", verify=_verify)
            return json_response(self, {"ok": True, "section": section, "action": action, **result})
        except ValueError as e:
            return json_response(self, {"error": str(e)[:200]}, status=400)
        except Exception as e:
            return json_response(self, {"error": str(e)[:200], "trace": traceback.format_exc()[:1500]}, status=500)

    def _op_editor(self, conn, action, data, result):
        from datetime import datetime
        now = datetime.now().isoformat(timespec="seconds")
        name = (data.get("name") or "").strip()
        if not name:
            raise ValueError("falta name")

        if action == "delete":
            conn.execute("DELETE FROM cfg_editors WHERE name = ?", (name,))
            result["deleted"] = name
            return

        email = (data.get("email") or "").strip() or None
        receives = 1 if data.get("receives_daily_summary") else 0
        receives_notif = 1 if data.get("receives_notifications") else 0
        on_vacation = 1 if data.get("on_vacation") else 0
        active = 1 if data.get("active", True) else 0

        if action == "create":
            existing = conn.execute("SELECT 1 FROM cfg_editors WHERE name = ?", (name,)).fetchone()
            if existing:
                raise ValueError(f"editor '{name}' ya existe")
            conn.execute("""
                INSERT INTO cfg_editors (name, email, receives_daily_summary, receives_notifications, on_vacation, active, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (name, email, receives, receives_notif, on_vacation, active, now, now))
            result["created"] = name
        else:  # update
            conn.execute("""
                UPDATE cfg_editors SET email=?, receives_daily_summary=?, receives_notifications=?,
                    on_vacation=?, active=?, updated_at=?
                WHERE name=?
            """, (email, receives, receives_notif, on_vacation, active, now, name))
            result["updated"] = name

    def _op_nickname(self, conn, action, data, result):
        from datetime import datetime
        now = datetime.now().isoformat(timespec="seconds")

        if action == "delete":
            row_id = data.get("id")
            if not row_id:
                raise ValueError("falta id")
            conn.execute("DELETE FROM cfg_nicknames WHERE id = ?", (row_id,))
            result["deleted_id"] = row_id
            return

        nick = (data.get("nickname") or "").strip().lower()
        real = (data.get("cliente_real") or "").strip()
        editor = (data.get("editor") or "").strip() or None
        if not nick or not real:
            raise ValueError("faltan nickname y cliente_real")

        if action == "create":
            cur = conn.execute("""
                INSERT INTO cfg_nicknames (nickname, cliente_real, editor, created_at)
                VALUES (?, ?, ?, ?)
            """, (nick, real, editor, now))
            result["created_id"] = cur.lastrowid
        else:  # update
            row_id = data.get("id")
            if not row_id:
                raise ValueError("falta id para update")
            conn.execute("""
                UPDATE cfg_nicknames SET nickname=?, cliente_real=?, editor=? WHERE id=?
            """, (nick, real, editor, row_id))
            result["updated_id"] = row_id

    def _op_alias(self, conn, action, data, result):
        from datetime import datetime
        now = datetime.now().isoformat(timespec="seconds")

        if action == "delete":
            row_id = data.get("id")
            if not row_id:
                raise ValueError("falta id")
            conn.execute("DELETE FROM cfg_aliases WHERE id = ?", (row_id,))
            result["deleted_id"] = row_id
            return

        drive_name = (data.get("drive_name") or "").strip().lower()
        real = (data.get("cliente_real") or "").strip()
        if not drive_name or not real:
            raise ValueError("faltan drive_name y cliente_real")

        if action == "create":
            cur = conn.execute("""
                INSERT INTO cfg_aliases (drive_name, cliente_real, created_at) VALUES (?, ?, ?)
            """, (drive_name, real, now))
            result["created_id"] = cur.lastrowid
        else:
            row_id = data.get("id")
            if not row_id:
                raise ValueError("falta id para update")
            conn.execute("UPDATE cfg_aliases SET drive_name=?, cliente_real=? WHERE id=?",
                         (drive_name, real, row_id))
            result["updated_id"] = row_id

    def _op_delivery(self, conn, action, data, result):
        from datetime import datetime
        now = datetime.now().isoformat(timespec="seconds")

        if action == "delete":
            row_id = data.get("id")
            if not row_id:
                raise ValueError("falta id")
            conn.execute("DELETE FROM cfg_delivery_folders WHERE id = ?", (row_id,))
            result["deleted_id"] = row_id
            return

        cliente = (data.get("cliente") or "").strip()
        folder_id = (data.get("folder_id") or "").strip()
        description = (data.get("description") or "").strip() or None
        if not cliente or not folder_id:
            raise ValueError("faltan cliente y folder_id")

        if action == "create":
            cur = conn.execute("""
                INSERT INTO cfg_delivery_folders (cliente, folder_id, description, created_at)
                VALUES (?, ?, ?, ?)
            """, (cliente, folder_id, description, now))
            result["created_id"] = cur.lastrowid
        else:
            row_id = data.get("id")
            if not row_id:
                raise ValueError("falta id para update")
            conn.execute("""
                UPDATE cfg_delivery_folders SET cliente=?, folder_id=?, description=? WHERE id=?
            """, (cliente, folder_id, description, row_id))
            result["updated_id"] = row_id

    def _op_client_email(self, conn, action, data, result):
        """Maneja CRUD de cfg_clients (mails de clientes para notificar entregas)."""
        from datetime import datetime
        now = datetime.now().isoformat(timespec="seconds")
        cliente = (data.get("cliente") or "").strip()
        if not cliente:
            raise ValueError("falta cliente")

        # Auto-crear tabla por las dudas (idempotente; si la migration no corrió)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS cfg_clients (
                cliente TEXT PRIMARY KEY,
                email TEXT NOT NULL,
                display_name TEXT,
                notifications_enabled INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
        """)

        if action == "delete":
            import tasks_store
            tasks_store.execute("DELETE FROM cfg_clients WHERE TRIM(cliente)=TRIM(?)", (cliente,))
            result["deleted"] = cliente
            return

        email = (data.get("email") or "").strip().lower()
        if not email:
            raise ValueError("falta email")
        if "@" not in email or "." not in email.split("@", 1)[-1]:
            raise ValueError("email inválido")
        display = (data.get("display_name") or "").strip() or None
        enabled = 1 if data.get("notifications_enabled", True) else 0

        if action == "create":
            existing = conn.execute("SELECT 1 FROM cfg_clients WHERE TRIM(cliente)=TRIM(?)", (cliente,)).fetchone()
            if existing:
                raise ValueError(f"cliente '{cliente}' ya tiene mail configurado (usar update)")
            # cfg_clients vive en TURSO (08/jul): el mail de Román se agregó vía
            # with_db y un push concurrente lo PISÓ. Transaccional = no se pierde.
            import tasks_store
            tasks_store.execute(
                "INSERT INTO cfg_clients (cliente, email, display_name, notifications_enabled, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (cliente, email, display, enabled, now, now))
            result["created"] = cliente
        else:  # update
            import tasks_store
            tasks_store.execute(
                "UPDATE cfg_clients SET email=?, display_name=?, notifications_enabled=?, updated_at=? "
                "WHERE TRIM(cliente)=TRIM(?)",
                (email, display, enabled, now, cliente))
            result["updated"] = cliente

    def _op_archive_client(self, conn, action, data, result):
        """Archivar/desarchivar cliente del dashboard. Recibe `clientes` (lista)
        para archivar TODOS los nombres del grupo (canonical + aliases) y que
        no reaparezca después del fuzzy grouping."""
        from datetime import datetime
        now = datetime.now().isoformat(timespec="seconds")

        conn.execute("""
            CREATE TABLE IF NOT EXISTS cfg_archived_clients (
                cliente TEXT PRIMARY KEY,
                archived_at TEXT
            )
        """)

        # Acepta `cliente` (string) o `clientes` (lista) — para archivar el
        # grupo completo (canonical + aliases) en un solo request.
        names = []
        if data.get("clientes"):
            names = [str(c).strip() for c in data["clientes"] if str(c).strip()]
        elif data.get("cliente"):
            names = [str(data["cliente"]).strip()]
        if not names:
            raise ValueError("falta cliente o clientes")

        if action == "delete":
            # Desarchivar
            for n in names:
                conn.execute("DELETE FROM cfg_archived_clients WHERE TRIM(cliente)=TRIM(?)", (n,))
            result["unarchived"] = names
        else:
            # archive (create/update)
            for n in names:
                conn.execute("""
                    INSERT INTO cfg_archived_clients (cliente, archived_at)
                    VALUES (?, ?)
                    ON CONFLICT(cliente) DO UPDATE SET archived_at=excluded.archived_at
                """, (n, now))
            # Borrar las tasks PENDING del cliente (match normalizado por
            # acentos/case) para que desaparezca del dashboard al instante.
            # Las 'done' se conservan (historial de stats).
            import unicodedata
            def _normx(s):
                s = unicodedata.normalize("NFD", s or "")
                s = "".join(ch for ch in s if unicodedata.category(ch) != "Mn")
                return " ".join(s.lower().split())
            targets = {_normx(n) for n in names}
            killed = 0
            import tasks_store
            for row in tasks_store.query("SELECT id, cliente FROM tasks WHERE status='pending'"):
                if _normx(row["cliente"]) in targets:
                    tasks_store.execute("DELETE FROM tasks WHERE id=?", (row["id"],))
                    killed += 1
            result["archived"] = names
            result["tasks_borradas"] = killed

    def _op_client_editor(self, conn, action, data, result):
        """Maneja override de editor por cliente (cfg_client_editor).
        action='update' setea/cambia el editor. action='delete' quita el
        override (vuelve al Sheet).
        """
        from datetime import datetime
        now = datetime.now().isoformat(timespec="seconds")
        cliente = (data.get("cliente") or "").strip()
        if not cliente:
            raise ValueError("falta cliente")

        conn.execute("""
            CREATE TABLE IF NOT EXISTS cfg_client_editor (
                cliente TEXT PRIMARY KEY,
                editor TEXT NOT NULL,
                updated_at TEXT
            )
        """)

        if action == "delete":
            conn.execute("DELETE FROM cfg_client_editor WHERE TRIM(cliente)=TRIM(?)", (cliente,))
            result["deleted"] = cliente
            return

        editor = (data.get("editor") or "").strip()
        if not editor:
            raise ValueError("falta editor (usá action=delete para quitar el override)")

        conn.execute("""
            INSERT INTO cfg_client_editor (cliente, editor, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(cliente) DO UPDATE SET editor=excluded.editor, updated_at=excluded.updated_at
        """, (cliente, editor, now))
        result["updated" if action == "update" else "created"] = cliente
        result["editor"] = editor

    def _op_pending_folder(self, conn, action, data, result):
        """action: 'update' con data.decision='approved'|'rejected', data.folder_id, opcional editor."""
        from datetime import datetime
        now = datetime.now().isoformat(timespec="seconds")
        folder_id = (data.get("folder_id") or "").strip()
        if not folder_id:
            raise ValueError("falta folder_id")
        if action != "update":
            raise ValueError("solo action=update soportada en pending_folder")
        decision = (data.get("decision") or "").strip()
        if decision not in ("approved", "rejected"):
            raise ValueError("decision debe ser approved o rejected")
        editor = (data.get("editor") or "").strip() or None
        row = conn.execute("SELECT folder_name FROM pending_drive_folders WHERE folder_id = ?", (folder_id,)).fetchone()
        if not row:
            raise ValueError("folder no existe")
        conn.execute("""
            UPDATE pending_drive_folders SET status = ?, decided_at = ?, decided_editor = ?
            WHERE folder_id = ?
        """, (decision, now, editor, folder_id))
        if decision == "approved":
            # Agregar a clients para que aparezca con link
            conn.execute("""
                INSERT INTO clients (folder_id, cliente, raw_folder_id, last_scan_at)
                VALUES (?, ?, NULL, ?)
                ON CONFLICT(folder_id) DO UPDATE SET cliente=excluded.cliente, last_scan_at=excluded.last_scan_at
            """, (folder_id, row["folder_name"], now))
            # Si hay editor, crear task pending count=1 con count_locked=1
            if editor:
                import tasks_store
                tasks_store.execute(
                    "INSERT INTO tasks (cliente, editor, file_id, file_name, detected_at, status, mail_sent_at, pending_count, count_locked) "
                    "VALUES (?, ?, ?, '(cliente agregado desde detección)', ?, 'pending', ?, 1, 1)",
                    (row["folder_name"], editor, f"approval:{folder_id}", now, now))
        result["decision"] = decision
        result["folder_id"] = folder_id

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, PATCH, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def log_message(self, *args, **kwargs):
        pass

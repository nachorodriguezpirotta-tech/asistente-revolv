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
    return {
        "editors": editors,
        "nicknames": nicknames,
        "aliases": aliases,
        "delivery_folders": delivery,
        "pending_folders": pending_folders,
        "mail_log": mail_log,
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

            if section not in ("editor", "nickname", "alias", "delivery", "pending_folder"):
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

            with_db(op, message=f"config: {action} {section}")
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
                conn.execute("""
                    INSERT INTO tasks (cliente, editor, file_id, file_name, detected_at, status, mail_sent_at, pending_count, count_locked)
                    VALUES (?, ?, ?, '(cliente agregado desde detección)', ?, 'pending', ?, 1, 1)
                """, (row["folder_name"], editor, f"approval:{folder_id}", now, now))
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

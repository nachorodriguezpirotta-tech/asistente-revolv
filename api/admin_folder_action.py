"""
Marca manual de carpetas desde el explorador del admin:

- action=edited ("✓ correcciones"): la carpeta contiene videos editados.
  Inserta sus videos en known_edited_files (is_baseline=1, silencioso, sin
  mails) y guarda el override. Los videos aparecen en el panel del cliente.

- action=raw ("🚫 crudos"): la carpeta es de crudos / no necesita revisión.
  NO borra filas de known_edited_files (si las borrara, el scan re-detectaría
  los archivos como "editados nuevos" y mandaría mails duplicados al cliente).
  En cambio guarda el override y normaliza el folder_id de las filas vivas;
  client_videos filtra todo folder marcado raw → desaparecen del panel.

- action=clear: borra el override (la carpeta vuelve al comportamiento
  automático del clasificador).

GET /api/admin_folder_action?cliente=X&folder_id=Y&action=edited|raw|clear
    &admin=1&t=ADMIN_TOKEN
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
    from api._shared import check_token, json_response, with_db
    from drive_client import _list_files
except Exception as e:
    _IMPORT_ERROR = f"{type(e).__name__}: {e}"


def _ensure_table(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS folder_overrides (
            folder_id  TEXT PRIMARY KEY,
            cliente    TEXT NOT NULL,
            kind       TEXT NOT NULL CHECK (kind IN ('edited','raw')),
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
    """)


def _make_apply(action, cliente, folder_id, files):
    video_files = [
        f for f in files
        if (f.get("mimeType") or "").startswith("video/")
        or (f.get("name") or "").lower().endswith(
            (".mp4", ".mov", ".m4v", ".avi", ".mkv", ".webm")
        )
    ]

    def _apply(conn):
        _ensure_table(conn)
        result = {"inserted": 0, "updated_folder_id": 0}
        if action == "clear":
            conn.execute(
                "DELETE FROM folder_overrides WHERE folder_id = ?", (folder_id,)
            )
            return result
        conn.execute(
            """
            INSERT INTO folder_overrides (folder_id, cliente, kind, created_at)
            VALUES (?, ?, ?, datetime('now'))
            ON CONFLICT(folder_id) DO UPDATE SET kind = excluded.kind,
                cliente = excluded.cliente, created_at = datetime('now')
            """,
            (folder_id, cliente, action),
        )
        # Normalizar folder_id de las filas existentes (algunas vienen del
        # Changes API con otro parent) para que el filtro raw las cubra.
        for f in video_files:
            cur = conn.execute(
                "UPDATE known_edited_files SET folder_id = ? WHERE file_id = ?",
                (folder_id, f.get("id")),
            )
            result["updated_folder_id"] += cur.rowcount or 0
        if action == "edited":
            for f in video_files:
                cur = conn.execute(
                    """
                    INSERT OR IGNORE INTO known_edited_files
                        (file_id, cliente, folder_id, name, size, created_time,
                         first_seen_at, is_baseline, closed_task_id)
                    VALUES (?, ?, ?, ?, ?, ?, datetime('now'), 1, NULL)
                    """,
                    (
                        f.get("id"),
                        cliente,
                        folder_id,
                        f.get("name") or "",
                        f.get("size"),
                        f.get("createdTime"),
                    ),
                )
                result["inserted"] += cur.rowcount or 0
        return result

    return _apply


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        try:
            if _IMPORT_ERROR:
                return json_response(self, {"error": _IMPORT_ERROR}, status=500)
            params = parse_qs(urlparse(self.path).query)
            cliente = (params.get("cliente", [""])[0] or "").strip()
            folder_id = (params.get("folder_id", [""])[0] or "").strip()
            action = (params.get("action", [""])[0] or "").strip()
            admin = params.get("admin", [""])[0] == "1"
            token = (params.get("t", [""])[0] or "").strip()

            if not admin or not check_token("ADMIN", token):
                return json_response(self, {"error": "unauthorized"}, status=401)
            if not folder_id or action not in ("edited", "raw", "clear"):
                return json_response(
                    self,
                    {"error": "folder_id y action=edited|raw|clear requeridos"},
                    status=400,
                )
            if action != "clear" and not cliente:
                return json_response(self, {"error": "cliente requerido"}, status=400)

            files = [] if action == "clear" else _list_files(folder_id, only_videos=False)

            holder = {}
            def _wrapped(conn):
                holder["result"] = _make_apply(action, cliente, folder_id, files)(conn)
                return None
            with_db(
                _wrapped,
                message=f"folder override {action}: {cliente or folder_id} [skip ci]",
            )
            return json_response(self, {
                "ok": True,
                "action": action,
                "folder_id": folder_id,
                "files_in_folder": len(files),
                **holder.get("result", {}),
            })
        except Exception as e:
            return json_response(
                self,
                {"error": str(e)[:200], "trace": traceback.format_exc()[:1500]},
                status=500,
            )

"""
Explorador de Drive para el admin: lista el contenido EXACTO de una carpeta
(subcarpetas + archivos) tal como está en Drive, con metadata útil:
- kind: editado/crudo/ambiguo (classify por nombre)
- editor: nombre del editor si el owner del archivo es un editor conocido
- in_panel: si el archivo está en known_edited_files (o sea, visible en el
  panel del cliente)

GET /api/admin_browse_folder?cliente=X&admin=1&t=ADMIN_TOKEN[&folder_id=Y]
  Sin folder_id → resuelve la carpeta raíz del cliente por nombre.
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
    from api._shared import check_token, json_response, read_db, make_client_token
    from drive_client import (
        find_folder_by_name,
        list_root_folders,
        _list_files,
        _list_subfolders,
        _is_video,
    )
    from classifier import classify, identify_editor_by_owner
except Exception as e:
    _IMPORT_ERROR = f"{type(e).__name__}: {e}"


def _in_panel_ids(conn, file_ids):
    if not file_ids:
        return set()
    placeholders = ",".join("?" for _ in file_ids)
    rows = conn.execute(
        f"SELECT file_id FROM known_edited_files WHERE file_id IN ({placeholders})",
        tuple(file_ids),
    ).fetchall()
    return {r["file_id"] for r in rows}


def _review_status_map(conn, file_ids):
    """Último status de client_reviews por file_id (id más alto gana)."""
    if not file_ids:
        return {}
    placeholders = ",".join("?" for _ in file_ids)
    rows = conn.execute(
        f"""
        SELECT video_file_id, status FROM client_reviews
        WHERE video_file_id IN ({placeholders})
        ORDER BY id ASC
        """,
        tuple(file_ids),
    ).fetchall()
    return {r["video_file_id"]: r["status"] for r in rows}


def _overrides_for(conn, folder_ids):
    """Devuelve {folder_id: kind} de folder_overrides para los ids dados."""
    if not folder_ids:
        return {}
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='folder_overrides'"
    ).fetchone()
    if not row:
        return {}
    placeholders = ",".join("?" for _ in folder_ids)
    rows = conn.execute(
        f"SELECT folder_id, kind FROM folder_overrides WHERE folder_id IN ({placeholders})",
        tuple(folder_ids),
    ).fetchall()
    return {r["folder_id"]: r["kind"] for r in rows}


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        try:
            if _IMPORT_ERROR:
                return json_response(self, {"error": _IMPORT_ERROR}, status=500)
            params = parse_qs(urlparse(self.path).query)
            cliente = (params.get("cliente", [""])[0] or "").strip()
            folder_id = (params.get("folder_id", [""])[0] or "").strip()
            admin = params.get("admin", [""])[0] == "1"
            token = (params.get("t", [""])[0] or "").strip()

            if not admin or not check_token("ADMIN", token):
                return json_response(self, {"error": "unauthorized"}, status=401)
            if not cliente and not folder_id:
                return json_response(
                    self, {"error": "cliente o folder_id requerido"}, status=400
                )

            folder_name = None
            if not folder_id:
                all_root = list_root_folders()
                folder = find_folder_by_name(cliente, all_root)
                if folder:
                    folder_id = folder["id"]
                    folder_name = folder.get("name")
                else:
                    # Fallback: el scan ya guardó el folder_id en la tabla clients
                    # (cubre clientes cuyo nombre no matchea la carpeta por fuzzy).
                    def _from_db(conn):
                        row = conn.execute(
                            """
                            SELECT folder_id FROM clients
                            WHERE TRIM(LOWER(cliente)) = TRIM(LOWER(?))
                            ORDER BY last_scan_at DESC NULLS LAST LIMIT 1
                            """,
                            (cliente,),
                        ).fetchone()
                        return row["folder_id"] if row else None
                    folder_id = read_db(_from_db)
                    folder_name = cliente
                    if not folder_id:
                        return json_response(
                            self,
                            {"error": f"No encontré carpeta de '{cliente}' en Drive"},
                            status=404,
                        )

            subfolders = _list_subfolders(folder_id)
            files = _list_files(folder_id, only_videos=False)

            file_ids = [f.get("id") for f in files if f.get("id")]
            all_folder_ids = [folder_id] + [s.get("id") for s in subfolders if s.get("id")]

            def _read(conn):
                return (
                    _in_panel_ids(conn, file_ids),
                    _overrides_for(conn, all_folder_ids),
                    _review_status_map(conn, file_ids),
                )
            in_panel, overrides, review_status = read_db(_read)

            out_files = []
            for f in files:
                mime = f.get("mimeType") or ""
                name = f.get("name") or ""
                is_vid = _is_video(name, mime)
                kind = None
                editor = None
                if is_vid:
                    sig = classify(f, parent_name=folder_name or cliente,
                                   cliente_name=cliente or folder_name)
                    kind = "editado" if sig is True else ("crudo" if sig is False else "ambiguo")
                    editor = identify_editor_by_owner(f)
                    if editor:
                        kind = "editado"  # owner editor = editado, override total
                owners = [
                    (o.get("emailAddress") or "").lower()
                    for o in (f.get("owners") or [])
                ]
                out_files.append({
                    "id": f.get("id"),
                    "name": name,
                    "mime": mime,
                    "size": f.get("size"),
                    "created_time": f.get("createdTime"),
                    "is_video": is_vid,
                    "kind": kind,
                    "editor": editor,
                    "owner": owners[0] if owners else None,
                    "in_panel": f.get("id") in in_panel,
                    "review_status": review_status.get(f.get("id")),
                })

            return json_response(self, {
                "ok": True,
                "folder_id": folder_id,
                "folder_name": folder_name,
                "folder_override": overrides.get(folder_id),
                "client_token": make_client_token(cliente) if cliente else None,
                "subfolders": [
                    {
                        "id": s.get("id"),
                        "name": s.get("name"),
                        "override": overrides.get(s.get("id")),
                    }
                    for s in subfolders
                ],
                "files": out_files,
            })
        except Exception as e:
            return json_response(
                self,
                {"error": str(e)[:200], "trace": traceback.format_exc()[:1500]},
                status=500,
            )

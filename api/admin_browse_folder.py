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
    from api._shared import check_token, json_response, read_db
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
                if not folder:
                    return json_response(
                        self,
                        {"error": f"No encontré carpeta de '{cliente}' en Drive"},
                        status=404,
                    )
                folder_id = folder["id"]
                folder_name = folder.get("name")

            subfolders = _list_subfolders(folder_id)
            files = _list_files(folder_id, only_videos=False)

            file_ids = [f.get("id") for f in files if f.get("id")]
            in_panel = read_db(lambda c: _in_panel_ids(c, file_ids))

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
                })

            return json_response(self, {
                "ok": True,
                "folder_id": folder_id,
                "folder_name": folder_name,
                "subfolders": [
                    {"id": s.get("id"), "name": s.get("name")} for s in subfolders
                ],
                "files": out_files,
            })
        except Exception as e:
            return json_response(
                self,
                {"error": str(e)[:200], "trace": traceback.format_exc()[:1500]},
                status=500,
            )

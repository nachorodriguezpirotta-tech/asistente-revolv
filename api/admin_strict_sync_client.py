"""
Strict sync: deja en `known_edited_files` SOLO los archivos cuyo owner sea un
editor conocido. Si el owner es el cliente (o cualquier mail no registrado en
cfg_editors), lo borra. Esta es la regla "los editados son los que sube el
editor al Drive — los crudos no van", aplicada de forma estricta.

GET /api/admin_strict_sync_client?cliente=X&admin=1&t=ADMIN_TOKEN
   → dry_run por defecto. Pasá &confirm=1 para borrar de verdad.
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
    from api._shared import check_token, json_response, with_db, read_db
    from drive_client import (
        find_folder_by_name,
        find_raw_subfolder,
        list_root_folders,
        _list_files,
        _list_subfolders,
        _is_raw_subfolder_name,
    )
    from classifier import identify_editor_by_owner, _get_owner_emails
except Exception as e:
    _IMPORT_ERROR = f"{type(e).__name__}: {e}"


def _db_ids(conn, cliente: str):
    rows = conn.execute(
        """
        SELECT file_id, name FROM known_edited_files
        WHERE TRIM(LOWER(cliente)) = TRIM(LOWER(?))
        """,
        (cliente,),
    ).fetchall()
    return {r["file_id"]: r["name"] for r in rows}


def _walk_client(client_folder_id, raw_folder_id, client_folder_name, max_depth=3):
    """
    Camina recursivamente la carpeta del cliente, EXCLUYENDO /Material/ y sus
    subcarpetas hijas con nombres de "raw" (Crudos, etc). Devuelve TODOS los
    archivos que encuentre, con su owner y carpeta padre.
    """
    out = []
    visited = set()

    def _walk(folder_id, name_for_log, depth):
        if depth > max_depth:
            return
        if folder_id in visited:
            return
        visited.add(folder_id)
        # Archivos del folder
        try:
            files = _list_files(folder_id, only_videos=False)
        except Exception:
            files = []
        for f in files:
            out.append({
                "id": f.get("id"),
                "name": f.get("name"),
                "mime": f.get("mimeType"),
                "parent_name": name_for_log,
                "owners": [
                    (o.get("emailAddress") or "").lower()
                    for o in (f.get("owners") or [])
                ],
                "last_mod": (f.get("lastModifyingUser") or {}).get("emailAddress", "").lower(),
            })
        # Recurse subfolders
        try:
            subs = _list_subfolders(folder_id)
        except Exception:
            subs = []
        for s in subs:
            if raw_folder_id and s["id"] == raw_folder_id:
                continue
            if _is_raw_subfolder_name(s.get("name") or ""):
                continue
            _walk(s["id"], s.get("name") or "", depth + 1)

    _walk(client_folder_id, client_folder_name, 0)
    return out


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        try:
            if _IMPORT_ERROR:
                return json_response(self, {"error": _IMPORT_ERROR}, status=500)
            params = parse_qs(urlparse(self.path).query)
            cliente = (params.get("cliente", [""])[0] or "").strip()
            admin = params.get("admin", [""])[0] == "1"
            token = (params.get("t", [""])[0] or "").strip()
            confirm = params.get("confirm", [""])[0] == "1"

            if not cliente:
                return json_response(self, {"error": "cliente requerido"}, status=400)
            if not admin or not check_token("ADMIN", token):
                return json_response(self, {"error": "unauthorized"}, status=401)

            # 1) Encontrar carpeta del cliente en Drive
            all_root = list_root_folders()
            folder = find_folder_by_name(cliente, all_root)
            if not folder:
                return json_response(
                    self,
                    {"error": f"No encontré carpeta de '{cliente}' en Drive"},
                    status=404,
                )
            client_folder_id = folder["id"]
            client_folder_name = folder.get("name") or cliente
            raw_folder = find_raw_subfolder(client_folder_id)
            raw_folder_id = raw_folder["id"] if raw_folder else None

            # 2) Walk recursivo (sin /Material/) → todos los archivos
            drive_files = _walk_client(
                client_folder_id, raw_folder_id, client_folder_name
            )

            # 3) Aplicar la regla: editor knownsoby owner
            keep_ids = set()
            file_info_by_id = {}
            for f in drive_files:
                editor = identify_editor_by_owner({
                    "owners": [{"emailAddress": em} for em in f["owners"]],
                    "lastModifyingUser": {"emailAddress": f["last_mod"]},
                })
                f["editor_match"] = editor
                file_info_by_id[f["id"]] = f
                if editor is not None:
                    keep_ids.add(f["id"])

            # 4) Comparar con DB
            db_map = read_db(lambda c: _db_ids(c, cliente))
            to_delete = []
            kept_in_db = []
            for fid, name in db_map.items():
                if fid in keep_ids:
                    kept_in_db.append({"id": fid, "name": name})
                else:
                    info = file_info_by_id.get(fid)
                    to_delete.append({
                        "id": fid,
                        "name": name,
                        "reason": (
                            "no_en_drive"
                            if info is None
                            else f"owner_no_es_editor (owners={','.join(info['owners']) or '?'}, lastmod={info['last_mod'] or '?'})"
                        ),
                    })

            # 5) Total que Drive considera editado (para comparar)
            drive_editor_count = len(keep_ids)
            drive_no_editor = len(drive_files) - drive_editor_count

            response = {
                "ok": True,
                "cliente": cliente,
                "matched_drive_folder": client_folder_name,
                "drive_total_files_examined": len(drive_files),
                "drive_files_with_editor_owner": drive_editor_count,
                "drive_files_without_editor_owner": drive_no_editor,
                "db_total": len(db_map),
                "would_keep_in_db": len(kept_in_db),
                "would_delete_from_db": len(to_delete),
                "preview_delete": to_delete[:30],
                "preview_keep": kept_in_db[:30],
                "dry_run": not confirm,
            }

            if confirm and to_delete:
                file_ids = [d["id"] for d in to_delete]
                def _do(conn):
                    placeholders = ",".join("?" for _ in file_ids)
                    conn.execute(
                        f"DELETE FROM known_edited_files WHERE file_id IN ({placeholders})",
                        tuple(file_ids),
                    )
                    return None
                with_db(
                    _do,
                    message=f"strict sync: -{len(file_ids)} de {cliente} [skip ci]",
                )
                response["deleted"] = len(file_ids)

            return json_response(self, response)
        except Exception as e:
            return json_response(
                self,
                {"error": str(e)[:200], "trace": traceback.format_exc()[:1500]},
                status=500,
            )

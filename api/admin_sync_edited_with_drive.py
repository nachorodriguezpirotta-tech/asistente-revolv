"""
Sincroniza known_edited_files contra la realidad de Drive para un cliente.

Caso de uso: el scan SOLO inserta a known_edited_files, nunca borra. Si
movemos archivos a /Material/ o los borramos en Drive, las entradas viejas
se quedan en la DB. Este endpoint consulta Drive y borra de la DB todo
file_id que ya NO esté listado como "editado" en Drive (por list_edited_files,
que usa la misma lógica que el scan).

GET /api/admin_sync_edited_with_drive?cliente=X&admin=1&t=ADMIN_TOKEN
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
        list_edited_files,
        list_root_folders,
    )
except Exception as e:
    _IMPORT_ERROR = f"{type(e).__name__}: {e}"


def _existing_ids(conn, cliente: str):
    rows = conn.execute(
        """
        SELECT file_id, name FROM known_edited_files
        WHERE TRIM(LOWER(cliente)) = TRIM(LOWER(?))
        """,
        (cliente,),
    ).fetchall()
    return [(r["file_id"], r["name"]) for r in rows]


def _make_apply(file_ids_to_delete, files_to_add, renames, cliente):
    """Borra los huérfanos, agrega los que faltan y refresca nombres viejos,
    en una sola transacción. Los agregados van con is_baseline=1 para que NO
    disparen mails ni cierren tasks — es un backfill silencioso."""
    def _apply(conn):
        if file_ids_to_delete:
            placeholders = ",".join("?" for _ in file_ids_to_delete)
            conn.execute(
                f"DELETE FROM known_edited_files WHERE file_id IN ({placeholders})",
                tuple(file_ids_to_delete),
            )
        for f in files_to_add:
            conn.execute(
                """
                INSERT OR IGNORE INTO known_edited_files
                    (file_id, cliente, folder_id, name, size, created_time,
                     first_seen_at, is_baseline, closed_task_id)
                VALUES (?, ?, ?, ?, ?, ?, datetime('now'), 1, NULL)
                """,
                (
                    f.get("id"),
                    cliente,
                    f.get("_parent_id") or "",
                    f.get("name") or "",
                    f.get("size"),
                    f.get("createdTime"),
                ),
            )
        for fid, new_name in renames:
            conn.execute(
                "UPDATE known_edited_files SET name = ? WHERE file_id = ?",
                (new_name, fid),
            )
        return None
    return _apply


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

            # 1) Encontrar la carpeta del cliente en Drive
            all_root = list_root_folders()
            folder = find_folder_by_name(cliente, all_root)
            if folder:
                client_folder_id = folder["id"]
                client_folder_name = folder.get("name") or cliente
            else:
                # Fallback: el folder_id ya guardado por el scan (tabla clients)
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
                client_folder_id = read_db(_from_db)
                client_folder_name = cliente
                if not client_folder_id:
                    return json_response(
                        self,
                        {"error": f"No encontré carpeta de '{cliente}' en Drive"},
                        status=404,
                    )

            # 2) Detectar /Material/ (si tiene)
            raw_folder = find_raw_subfolder(client_folder_id)
            raw_folder_id = raw_folder["id"] if raw_folder else None

            # 3) Listar archivos editados según Drive (realidad actual)
            drive_edited = list_edited_files(
                client_folder_id, raw_folder_id, client_folder_name
            )
            drive_ids = {f["id"] for f in drive_edited}

            # 4) Comparar contra DB — dos direcciones:
            #    - to_delete: en DB pero ya no en Drive (movidos/borrados)
            #    - to_add:    en Drive pero faltan en DB (el scan se los perdió)
            db_rows = read_db(lambda c: _existing_ids(c, cliente))
            db_ids = {fid for (fid, _) in db_rows}
            to_delete = [(fid, name) for (fid, name) in db_rows if fid not in drive_ids]
            survivors = [(fid, name) for (fid, name) in db_rows if fid in drive_ids]
            to_add = [f for f in drive_edited if f.get("id") not in db_ids]
            # Renombrados en Drive → refrescar el nombre en DB
            drive_name_by_id = {f.get("id"): (f.get("name") or "") for f in drive_edited}
            to_rename = [
                (fid, drive_name_by_id[fid])
                for (fid, name) in survivors
                if drive_name_by_id.get(fid) and drive_name_by_id[fid] != name
            ]

            response = {
                "ok": True,
                "cliente": cliente,
                "matched_folder_in_drive": client_folder_name,
                "matched_folder_id": client_folder_id,
                "has_material_folder": raw_folder_id is not None,
                "in_drive_now": len(drive_ids),
                "in_db_now": len(db_rows),
                "to_delete": len(to_delete),
                "to_add": len(to_add),
                "to_rename": len(to_rename),
                "survivors": len(survivors),
                "preview_delete": [name for (_, name) in to_delete[:30]],
                "preview_add": [f.get("name") for f in to_add[:30]],
                "preview_rename": [n for (_, n) in to_rename[:30]],
                "survivors_sample": [name for (_, name) in survivors[:30]],
                "dry_run": not confirm,
            }

            if confirm and (to_delete or to_add or to_rename):
                file_ids = [fid for (fid, _) in to_delete]
                with_db(
                    _make_apply(file_ids, to_add, to_rename, cliente),
                    message=f"sync drive: -{len(file_ids)} +{len(to_add)} ~{len(to_rename)} de {cliente} [skip ci]",
                )
                response["deleted"] = len(file_ids)
                response["added"] = len(to_add)
                response["renamed"] = len(to_rename)

            return json_response(self, response)
        except Exception as e:
            return json_response(
                self,
                {"error": str(e)[:200], "trace": traceback.format_exc()[:1500]},
                status=500,
            )

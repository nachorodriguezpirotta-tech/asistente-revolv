"""
Audit raw de Drive vs DB para un cliente. Lista CADA archivo en root de la
carpeta del cliente con su clasificación (editado / crudo / ambiguo), su
presencia en DB, y la razón por la que el classifier lo cuenta o no.

GET /api/admin_drive_audit?cliente=X&admin=1&t=ADMIN_TOKEN
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
        find_raw_subfolder,
        list_root_folders,
        _list_files,
        _list_subfolders,
    )
    from classifier import classify
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


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        try:
            if _IMPORT_ERROR:
                return json_response(self, {"error": _IMPORT_ERROR}, status=500)
            params = parse_qs(urlparse(self.path).query)
            cliente = (params.get("cliente", [""])[0] or "").strip()
            admin = params.get("admin", [""])[0] == "1"
            token = (params.get("t", [""])[0] or "").strip()

            if not cliente:
                return json_response(self, {"error": "cliente requerido"}, status=400)
            if not admin or not check_token("ADMIN", token):
                return json_response(self, {"error": "unauthorized"}, status=401)

            all_root = list_root_folders()
            folder = find_folder_by_name(cliente, all_root)
            if not folder:
                return json_response(
                    self, {"error": f"No encontré carpeta de '{cliente}'"}, status=404
                )
            client_folder_id = folder["id"]
            client_folder_name = folder.get("name") or cliente

            raw_folder = find_raw_subfolder(client_folder_id)
            raw_folder_id = raw_folder["id"] if raw_folder else None

            # Listar TODOS los archivos en root del cliente (sin filtrar por video)
            all_files = _list_files(client_folder_id, only_videos=False)
            # Listar subcarpetas (para mostrar también lo que haya adentro)
            subfolders = _list_subfolders(client_folder_id)

            db_map = read_db(lambda c: _db_ids(c, cliente))

            # Clasificar cada archivo
            files_info = []
            for f in all_files:
                sig = classify(
                    f,
                    parent_name=client_folder_name,
                    cliente_name=client_folder_name,
                )
                # sig: True=editado / False=crudo / None=ambiguo
                if sig is True:
                    kind = "EDITADO_seguro"
                elif sig is False:
                    kind = "CRUDO_seguro"
                else:
                    kind = "AMBIGUO"
                files_info.append({
                    "id": f.get("id"),
                    "name": f.get("name"),
                    "size": f.get("size"),
                    "mime": f.get("mimeType"),
                    "kind": kind,
                    "in_db": f.get("id") in db_map,
                })

            # Detectar IDs en DB que ya NO están en Drive root
            drive_root_ids = {f.get("id") for f in all_files}
            db_orphan = [
                {"id": fid, "name": name}
                for fid, name in db_map.items()
                if fid not in drive_root_ids
            ]

            # Si hay subfolder Material/, mostrar sus hijos también
            material_sample = []
            if raw_folder_id:
                try:
                    mat_files = _list_files(raw_folder_id, only_videos=False)
                    material_sample = [
                        {"name": f.get("name"), "id": f.get("id")}
                        for f in mat_files[:30]
                    ]
                except Exception:
                    pass

            # NUEVO: recursar en cada subcarpeta (que no sea Material) y traer
            # también esos archivos con su clasificación.
            subfolder_contents = []
            all_drive_files_recursive_ids = set(drive_root_ids)
            for sub in subfolders:
                if raw_folder_id and sub["id"] == raw_folder_id:
                    continue
                from drive_client import _is_raw_subfolder_name
                if _is_raw_subfolder_name(sub.get("name") or ""):
                    continue
                try:
                    sub_files = _list_files(sub["id"], only_videos=False)
                    sub_info = []
                    for f in sub_files:
                        sig = classify(
                            f,
                            parent_name=sub.get("name"),
                            cliente_name=client_folder_name,
                        )
                        if sig is True:
                            kind = "EDITADO_seguro"
                        elif sig is False:
                            kind = "CRUDO_seguro"
                        else:
                            kind = "AMBIGUO"
                        all_drive_files_recursive_ids.add(f.get("id"))
                        sub_info.append({
                            "id": f.get("id"),
                            "name": f.get("name"),
                            "kind": kind,
                            "in_db": f.get("id") in db_map,
                        })
                    subfolder_contents.append({
                        "subfolder_name": sub.get("name"),
                        "subfolder_id": sub.get("id"),
                        "files": sub_info,
                        "n_files": len(sub_info),
                    })
                except Exception as e:
                    subfolder_contents.append({
                        "subfolder_name": sub.get("name"),
                        "error": str(e)[:200],
                    })

            # Recalcular huérfanos contra TODO (root + subfolders no-raw)
            db_orphan_real = [
                {"id": fid, "name": name}
                for fid, name in db_map.items()
                if fid not in all_drive_files_recursive_ids
            ]

            return json_response(self, {
                "ok": True,
                "cliente_query": cliente,
                "matched_drive_folder": client_folder_name,
                "matched_drive_folder_id": client_folder_id,
                "has_material_folder": raw_folder_id is not None,
                "drive_root_total_files": len(all_files),
                "drive_root_subfolders": [{"id": s.get("id"), "name": s.get("name")} for s in subfolders],
                "files_in_drive_root": files_info,
                "subfolder_contents": subfolder_contents,
                "db_orphans_vs_root_only": db_orphan,
                "db_orphans_vs_recursive": db_orphan_real,
                "material_sample": material_sample,
            })
        except Exception as e:
            return json_response(
                self,
                {"error": str(e)[:200], "trace": traceback.format_exc()[:1500]},
                status=500,
            )

"""
Limpieza one-shot de archivos crudos colados en known_edited_files.

Caso de uso: el clasificador del scan NO chequea owner para archivos en root
del cliente, así que cuando suben crudos a la carpeta del cliente (en lugar
de /Material/), los marca como editados. Después si los movemos a /Material/,
el scan NO los saca de known_edited_files (no hay DELETE en el código actual).

Este endpoint borra esos falsos editados para un cliente, usando heurística
de nombres de archivo de cámara (Sony C####.MP4, Canon MVI_, Nikon DSC_,
DJI/GoPro, Panasonic P####, Apple Double ._files, .DS_Store).

GET /api/admin_cleanup_raw_files?cliente=X&admin=1&t=ADMIN_TOKEN
  → dry_run por defecto. Pasá &confirm=1 para borrar de verdad.
"""

import os
import re
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
except Exception as e:
    _IMPORT_ERROR = f"{type(e).__name__}: {e}"


# Misma heurística que el portal (src/lib/client-canonical.ts)
CAMERA_RAW_PATTERNS = [
    re.compile(r"^C\d{4,6}(?:\.[A-Z0-9]+)?$", re.I),
    re.compile(r"^MVI[_-]?\d{3,6}", re.I),
    re.compile(r"^DSC[_-]?\d{3,6}", re.I),
    re.compile(r"^IMG[_-]?\d{3,6}", re.I),
    re.compile(r"^DJI[_-]?\d{4,8}", re.I),
    re.compile(r"^GO?PR\d{3,8}", re.I),
    re.compile(r"^G[HX]\d{6,10}", re.I),
    re.compile(r"^P\d{7,10}(?:\.[A-Z0-9]+)?$", re.I),
    re.compile(r"^00\d{4,6}\.M(P4|TS|XF)", re.I),
    re.compile(r"^A\d{3,6}[A-Z]?\d{3,6}", re.I),
]


def is_camera_raw(name: str) -> bool:
    if not name:
        return False
    raw = name.strip()
    if not raw:
        return False
    if raw.startswith("._"):
        return True
    if raw in (".DS_Store", "Thumbs.db"):
        return True
    base = re.sub(r"\.[^.]+$", "", raw)
    if not base:
        return False
    for rx in CAMERA_RAW_PATTERNS:
        if rx.match(base):
            return True
    return False


def _scan(conn, cliente: str):
    rows = conn.execute(
        """
        SELECT file_id, cliente, name, first_seen_at
        FROM known_edited_files
        WHERE TRIM(LOWER(cliente)) = TRIM(LOWER(?))
        """,
        (cliente,),
    ).fetchall()
    matches = []
    survivors_sample = []
    for r in rows:
        name = (r["name"] or "")
        if is_camera_raw(name):
            matches.append({"file_id": r["file_id"], "name": name})
        else:
            if len(survivors_sample) < 30:
                survivors_sample.append(name)
    return {
        "total": len(rows),
        "matches": matches,
        "survivors_sample": survivors_sample,
    }


def _make_delete(file_ids):
    def _delete(conn):
        placeholders = ",".join("?" for _ in file_ids)
        conn.execute(
            f"DELETE FROM known_edited_files WHERE file_id IN ({placeholders})",
            tuple(file_ids),
        )
        return None
    return _delete


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

            scan_result = read_db(lambda c: _scan(c, cliente))
            matches = scan_result["matches"]
            response = {
                "ok": True,
                "cliente": cliente,
                "total_in_known_edited": scan_result["total"],
                "would_delete": len(matches),
                "survivors": scan_result["total"] - len(matches),
                "preview_delete": [m["name"] for m in matches[:30]],
                "survivors_sample": scan_result["survivors_sample"],
                "dry_run": not confirm,
            }

            if confirm and matches:
                file_ids = [m["file_id"] for m in matches]
                with_db(
                    _make_delete(file_ids),
                    message=f"cleanup raw files: -{len(file_ids)} de {cliente} [skip ci]",
                )
                response["deleted"] = len(file_ids)

            return json_response(self, response)
        except Exception as e:
            return json_response(
                self,
                {"error": str(e)[:200], "trace": traceback.format_exc()[:1500]},
                status=500,
            )

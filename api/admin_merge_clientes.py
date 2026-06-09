"""
Fusiona dos entradas de cliente en la DB del asistente.

Caso de uso: el asistente registró el mismo cliente con variantes distintas
del nombre (con/sin tilde, casing, whitespace) — quedaron como 2+ entradas
separadas. Este endpoint los unifica renombrando `from` → `to` en todas las
tablas que tienen columna `cliente`.

GET /api/admin_merge_clientes?from=Jose+Social+Pulse+Media&to=José+Social+Pulse+Media
   &admin=1&t=ADMIN_TOKEN[&confirm=1]
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
except Exception as e:
    _IMPORT_ERROR = f"{type(e).__name__}: {e}"


# Tablas con columna `cliente` que renombramos.
TABLES_WITH_CLIENTE = [
    "known_files",
    "known_edited_files",
    "tasks",
    "client_reviews",
    "clients",
    "mail_log",
]


def _scan(conn, from_c: str, to_c: str):
    report = {}
    for tbl in TABLES_WITH_CLIENTE:
        try:
            from_n = conn.execute(
                f"SELECT COUNT(*) FROM {tbl} WHERE cliente = ?", (from_c,)
            ).fetchone()[0]
            to_n = conn.execute(
                f"SELECT COUNT(*) FROM {tbl} WHERE cliente = ?", (to_c,)
            ).fetchone()[0]
            report[tbl] = {"from": from_n, "to": to_n, "exists": True}
        except Exception as e:
            report[tbl] = {"error": str(e)[:100], "exists": False}
    return report


def _make_rename(from_c: str, to_c: str):
    def _do(conn):
        for tbl in TABLES_WITH_CLIENTE:
            try:
                conn.execute(
                    f"UPDATE {tbl} SET cliente = ? WHERE cliente = ?",
                    (to_c, from_c),
                )
            except Exception:
                # Tabla puede no existir en versiones viejas — ignorar
                pass
        return None
    return _do


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        try:
            if _IMPORT_ERROR:
                return json_response(self, {"error": _IMPORT_ERROR}, status=500)
            params = parse_qs(urlparse(self.path).query)
            from_c = (params.get("from", [""])[0] or "").strip()
            to_c = (params.get("to", [""])[0] or "").strip()
            admin = params.get("admin", [""])[0] == "1"
            token = (params.get("t", [""])[0] or "").strip()
            confirm = params.get("confirm", [""])[0] == "1"

            if not from_c or not to_c:
                return json_response(
                    self,
                    {"error": "params 'from' y 'to' requeridos"},
                    status=400,
                )
            if from_c == to_c:
                return json_response(
                    self, {"error": "from y to son iguales"}, status=400
                )
            if not admin or not check_token("ADMIN", token):
                return json_response(self, {"error": "unauthorized"}, status=401)

            pre = read_db(lambda c: _scan(c, from_c, to_c))

            response = {
                "ok": True,
                "from_cliente": from_c,
                "to_cliente": to_c,
                "pre": pre,
                "dry_run": not confirm,
            }

            if confirm:
                with_db(
                    _make_rename(from_c, to_c),
                    message=f"merge cliente: '{from_c}' → '{to_c}' [skip ci]",
                )
                post = read_db(lambda c: _scan(c, from_c, to_c))
                response["post"] = post

            return json_response(self, response)
        except Exception as e:
            return json_response(
                self,
                {"error": str(e)[:200], "trace": traceback.format_exc()[:1500]},
                status=500,
            )

"""
GET /api/calendar?admin=1&t=<token>&month=YYYY-MM

Devuelve actividad diaria del mes:
  days: { 'YYYY-MM-DD': { crudos: N, editados: M } }
"""

import json
import os
import sys
import traceback
from http.server import BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

try:
    from _shared import check_token, json_response, read_db
    _IMPORT_ERROR = None
except Exception as _e:
    _IMPORT_ERROR = f"{type(_e).__name__}: {_e}\n{traceback.format_exc()}"


def _build_month(conn, month_str: str):
    """month_str = 'YYYY-MM'. Devuelve días con crudos/editados/entregas.

    Notas importantes:
    - first_seen_at está guardado en UTC. El usuario está en Argentina (UTC-3,
      sin DST). Aplicamos `-3 hours` en SQLite para que los uploads de la
      noche BA caigan en el día correcto. Bug reportado por Ignacio 21/may:
      "hoy dice 11 editados pero subí menos" — los extras eran de ayer
      después de las 21hs BA (que en UTC ya es el día siguiente).

    - Filtramos archivos AppleDouble (name LIKE '._%'). Bug del 12/may:
      Jose Social Pulse Media salía con 354 editados en un día, pero 346
      eran archivos basura `._C21XX.MP4` (metadata de macOS).
    """
    # Validar formato
    try:
        datetime.strptime(month_str, "%Y-%m")
    except Exception:
        raise ValueError("month inválido (formato YYYY-MM)")

    days = {}

    DAY_EXPR = "substr(datetime(first_seen_at, '-3 hours'), 1, 10)"

    # Crudos por día (timezone-aware + sin ._ files)
    rows = conn.execute(f"""
        SELECT {DAY_EXPR} as day, COUNT(*) as n
        FROM known_files
        WHERE {DAY_EXPR} LIKE ? || '%'
          AND is_baseline = 0
          AND name NOT LIKE '._%'
        GROUP BY day
    """, (month_str,)).fetchall()
    for r in rows:
        days.setdefault(r["day"], {"crudos": 0, "editados": 0})["crudos"] = r["n"]

    # Editados por día
    rows = conn.execute(f"""
        SELECT {DAY_EXPR} as day, COUNT(*) as n
        FROM known_edited_files
        WHERE {DAY_EXPR} LIKE ? || '%'
          AND is_baseline = 0
          AND name NOT LIKE '._%'
        GROUP BY day
    """, (month_str,)).fetchall()
    for r in rows:
        days.setdefault(r["day"], {"crudos": 0, "editados": 0})["editados"] = r["n"]

    return {
        "ok": True,
        "month": month_str,
        "days": days,
    }


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        try:
            if _IMPORT_ERROR:
                return json_response(self, {"error": "import", "detail": _IMPORT_ERROR}, status=500)
            params = parse_qs(urlparse(self.path).query)
            if params.get("admin", [""])[0] != "1":
                return json_response(self, {"error": "admin required"}, status=401)
            token = (params.get("t", [""])[0] or "").strip()
            if not check_token("ADMIN", token):
                return json_response(self, {"error": "unauthorized"}, status=401)
            month = (params.get("month", [""])[0] or "").strip()
            if not month:
                month = datetime.now().strftime("%Y-%m")
            data = read_db(lambda conn: _build_month(conn, month))
            return json_response(self, data)
        except ValueError as e:
            return json_response(self, {"error": str(e)}, status=400)
        except Exception as e:
            return json_response(self, {"error": str(e)[:200], "trace": traceback.format_exc()[:1000]}, status=500)

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def log_message(self, *a, **kw): pass

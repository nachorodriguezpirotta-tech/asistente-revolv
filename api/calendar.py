"""
GET /api/calendar?admin=1&t=<token>&month=YYYY-MM

Devuelve actividad diaria del mes:
  days: { 'YYYY-MM-DD': { crudos: N, editados: M } }
"""

import json
import os
import re
import sys
import traceback
from collections import defaultdict
from http.server import BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

try:
    from _shared import check_token, json_response, read_db
    _IMPORT_ERROR = None
except Exception as _e:
    _IMPORT_ERROR = f"{type(_e).__name__}: {_e}\n{traceback.format_exc()}"


# ─── Heurísticas para limpiar conteos ───────────────────────────────────────

# Patrones obvios de raw de cámara (no son "editados" reales aunque estén
# fuera de /Material/). Si el cliente sube los crudos a la carpeta principal
# en vez de a /Material/, no queremos contarlos como entregas.
_CAMERA_PATTERNS = [
    r'^C\d{4}\.mp4$',           # Sony XAVC / FX (C2119.MP4, C2126.MP4)
    r'^B\d{4}\.mp4$',           # Blackmagic
    r'^A\d{4}\.mp4$',           # Atomos
    r'^IMG_\d+\.(mov|mp4|m4v)$',# iPhone / iPad
    r'^DSC_?\d+\.(mov|mp4)$',   # Sony Alpha / Nikon
    r'^MVI_\d+\.(mov|mp4)$',    # Canon
    r'^MAH\d+\.(mp4|mts)$',     # Panasonic
    r'^VID_\d+\.(mp4|3gp)$',    # Android
    r'^GH\d{4,}\.(mp4|mov)$',   # GoPro Hero
    r'^GX\d{4,}\.(mp4|mov)$',   # GoPro
    r'^00\d+\.mts$',            # AVCHD
    r'^P\d{7}\.(mp4|mov)$',     # Panasonic GH series
]
_CAMERA_RE = re.compile("|".join(_CAMERA_PATTERNS), re.IGNORECASE)

def _is_camera_raw_name(name: str) -> bool:
    """¿El nombre del archivo es claramente raw de cámara? (no edición)."""
    if not name:
        return False
    return bool(_CAMERA_RE.match(name.strip()))


# Umbral de bulk import: si un cliente sube N+ archivos en el mismo segundo,
# lo tratamos como UN evento (no N entregas separadas). Típicamente cuando
# alguien arrastra una carpeta entera a Drive web.
_BULK_THRESHOLD = 10


def _count_with_heuristics(rows, *, filter_camera_names: bool) -> dict:
    """Aplica las heurísticas de limpieza:
      1. (Opcional) Filtra nombres obvios de raw de cámara — solo en editados
         (en crudos NO se filtra, ahí los nombres de cámara son lo esperado).
      2. Colapsa bulk imports: si un cliente sube ≥ _BULK_THRESHOLD archivos
         en el MISMO segundo, se cuenta como 1 evento del día.

    Retorna {day: count}.
    """
    # Paso 1: filtrar camera names si corresponde
    if filter_camera_names:
        rows = [r for r in rows if not _is_camera_raw_name(r["name"] or "")]

    # Paso 2: bucket por (cliente, first_seen_at) para detectar bulk
    buckets = defaultdict(list)  # (cliente, ts) -> [row, ...]
    for r in rows:
        key = (r["cliente"] or "", r["first_seen_at"] or "")
        buckets[key].append(r)

    # Paso 3: contar por día. Bulk → 1 entrada del día (del primer archivo).
    count_by_day = defaultdict(int)
    for (cliente, ts), files in buckets.items():
        if len(files) >= _BULK_THRESHOLD:
            # Bulk import — 1 evento solamente
            count_by_day[files[0]["day"]] += 1
        else:
            # Normal — cuenta cada archivo
            for f in files:
                count_by_day[f["day"]] += 1
    return dict(count_by_day)


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

    - Filtramos nombres obvios de raw de cámara en EDITADOS (no en crudos).
      Bug del 11/may: Jose subió 173 archivos C2126.MP4 etc. (Sony FX) a
      la carpeta cliente fuera de /Material/ y se contaron como editados.

    - Colapsamos bulk imports (≥10 archivos del mismo cliente en el mismo
      segundo → 1 evento). Caso típico de cliente arrastrando una carpeta
      entera a Drive web.
    """
    # Validar formato
    try:
        datetime.strptime(month_str, "%Y-%m")
    except Exception:
        raise ValueError("month inválido (formato YYYY-MM)")

    DAY_EXPR = "substr(datetime(first_seen_at, '-3 hours'), 1, 10)"

    # Crudos: leemos las filas y aplicamos heurísticas en Python
    crudos_rows = conn.execute(f"""
        SELECT file_id, cliente, name, first_seen_at,
               {DAY_EXPR} as day
        FROM known_files
        WHERE {DAY_EXPR} LIKE ? || '%'
          AND is_baseline = 0
          AND name NOT LIKE '._%'
    """, (month_str,)).fetchall()
    crudos_by_day = _count_with_heuristics(crudos_rows, filter_camera_names=False)

    # Editados: igual + filtro de nombres de cámara
    editados_rows = conn.execute(f"""
        SELECT file_id, cliente, name, first_seen_at,
               {DAY_EXPR} as day
        FROM known_edited_files
        WHERE {DAY_EXPR} LIKE ? || '%'
          AND is_baseline = 0
          AND name NOT LIKE '._%'
    """, (month_str,)).fetchall()
    editados_by_day = _count_with_heuristics(editados_rows, filter_camera_names=True)

    days = {}
    for day, n in crudos_by_day.items():
        days.setdefault(day, {"crudos": 0, "editados": 0})["crudos"] = n
    for day, n in editados_by_day.items():
        days.setdefault(day, {"crudos": 0, "editados": 0})["editados"] = n

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

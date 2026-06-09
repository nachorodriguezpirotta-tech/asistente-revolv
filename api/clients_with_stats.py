"""
Endpoint admin para el panel maestro de Revolv: lista TODOS los clientes
con stats (videos editados, pendientes, corregidos) y link directo al
panel de cada uno.

Auth: ADMIN token (make_token("ADMIN")).

GET /api/clients_with_stats?admin=1&t=ADMIN_TOKEN
  → { ok, clientes: [{name, videos, pending, requested, resolved, link}] }
"""

import os
import sys
import traceback
import hmac
import hashlib
import urllib.parse
from http.server import BaseHTTPRequestHandler
from urllib.parse import parse_qs, urlparse

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

_IMPORT_ERROR = None
try:
    from api._shared import check_token, json_response, read_db, DASHBOARD_SECRET
except Exception as e:
    _IMPORT_ERROR = f"{type(e).__name__}: {e}"


PORTAL_URL = "https://revolv-portal.vercel.app"


def _client_token(cliente: str) -> str:
    """Mismo formato que make_client_token del _shared."""
    return hmac.new(
        DASHBOARD_SECRET.encode(),
        f"client:{cliente.lower().strip()}".encode(),
        hashlib.sha256,
    ).hexdigest()[:16]


def _build(conn):
    rows = conn.execute(
        """
        WITH stats AS (
            SELECT
                kef.cliente,
                COUNT(*) AS videos,
                MAX(kef.first_seen_at) AS last_seen
            FROM known_edited_files kef
            GROUP BY kef.cliente
            HAVING videos > 0
        ),
        review_stats AS (
            SELECT
                cliente,
                SUM(CASE WHEN status = 'revision_requested' THEN 1 ELSE 0 END) AS requested,
                SUM(CASE WHEN status = 'resolved' THEN 1 ELSE 0 END) AS resolved
            FROM client_reviews
            GROUP BY cliente
        )
        SELECT
            s.cliente,
            s.videos,
            s.last_seen,
            COALESCE(rs.requested, 0) AS requested,
            COALESCE(rs.resolved, 0) AS resolved
        FROM stats s
        LEFT JOIN review_stats rs ON TRIM(LOWER(s.cliente)) = TRIM(LOWER(rs.cliente))
        ORDER BY s.last_seen DESC NULLS LAST, s.cliente
        """
    ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        c = d["cliente"]
        t = _client_token(c)
        safe = urllib.parse.quote(c, safe="")
        d["link"] = f"{PORTAL_URL}/c/{safe}?t={t}"
        d["pending"] = max(0, d["videos"] - d["requested"] - d["resolved"])
        out.append(d)
    return out


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        try:
            if _IMPORT_ERROR:
                return json_response(self, {"error": _IMPORT_ERROR}, status=500)
            params = parse_qs(urlparse(self.path).query)
            admin = params.get("admin", [""])[0] == "1"
            token = (params.get("t", [""])[0] or "").strip()
            if not admin or not check_token("ADMIN", token):
                return json_response(self, {"error": "unauthorized"}, status=401)
            data = read_db(_build)
            totals = {
                "clientes": len(data),
                "videos": sum(c["videos"] for c in data),
                "requested": sum(c["requested"] for c in data),
                "resolved": sum(c["resolved"] for c in data),
            }
            return json_response(
                self,
                {"ok": True, "totals": totals, "clientes": data},
            )
        except Exception as e:
            return json_response(
                self,
                {"error": str(e)[:200], "trace": traceback.format_exc()[:1500]},
                status=500,
            )

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

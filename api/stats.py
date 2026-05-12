"""
GET /api/stats?admin=1&t=<admin_token>

Métricas de productividad por editor:
  - pending_videos: videos pendientes (suma de pending_count)
  - pending_clientes: clientes con tareas pendientes
  - delivered_week: tareas cerradas en últimos 7 días
  - delivered_month: tareas cerradas en últimos 30 días
  - avg_turnaround_hours: tiempo promedio detectado → entregado (últimos 30 días)
  - oldest_pending_days: días desde la pending más vieja (0 si no hay pending)
  - health: "ok" | "warning" | "critical" según oldest_pending_days
"""

import json
import os
import sys
import traceback
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

try:
    from _shared import (
        check_token, read_db, json_response, EDITORS, make_token,
        DASHBOARD_SECRET, GITHUB_PAT,
    )
    _IMPORT_ERROR = None
except Exception as _e:
    _IMPORT_ERROR = f"{type(_e).__name__}: {_e}\n{traceback.format_exc()}"


def _parse_iso(s):
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "").split(".")[0])
    except Exception:
        return None


def get_editor_stats(conn, editor: str, now: datetime) -> dict:
    week_ago = (now - timedelta(days=7)).isoformat(timespec="seconds")
    month_ago = (now - timedelta(days=30)).isoformat(timespec="seconds")

    # Pendientes
    pending_videos = conn.execute(
        "SELECT COALESCE(SUM(COALESCE(pending_count, 1)), 0) FROM tasks WHERE editor = ? AND status = 'pending'",
        (editor,),
    ).fetchone()[0]
    pending_clientes = conn.execute(
        "SELECT COUNT(DISTINCT TRIM(cliente)) FROM tasks WHERE editor = ? AND status = 'pending'",
        (editor,),
    ).fetchone()[0]

    # Entregados última semana / mes
    delivered_week = conn.execute(
        "SELECT COUNT(*) FROM tasks WHERE editor = ? AND status = 'done' AND completed_at >= ?",
        (editor, week_ago),
    ).fetchone()[0]
    delivered_month = conn.execute(
        "SELECT COUNT(*) FROM tasks WHERE editor = ? AND status = 'done' AND completed_at >= ?",
        (editor, month_ago),
    ).fetchone()[0]

    # Tiempo promedio detected_at → completed_at (últimos 30 días)
    rows = conn.execute(
        """SELECT detected_at, completed_at FROM tasks
           WHERE editor = ? AND status = 'done' AND completed_at >= ?
             AND detected_at IS NOT NULL AND completed_at IS NOT NULL""",
        (editor, month_ago),
    ).fetchall()
    turnarounds = []
    for r in rows:
        det = _parse_iso(r["detected_at"])
        comp = _parse_iso(r["completed_at"])
        if det and comp and comp > det:
            turnarounds.append((comp - det).total_seconds() / 3600)  # horas
    avg_turnaround_hours = round(sum(turnarounds) / len(turnarounds), 1) if turnarounds else None

    # Oldest pending → cuántos días lleva
    row = conn.execute(
        "SELECT MIN(detected_at) FROM tasks WHERE editor = ? AND status = 'pending'",
        (editor,),
    ).fetchone()
    oldest = _parse_iso(row[0]) if row[0] else None
    oldest_pending_days = round((now - oldest).total_seconds() / 86400, 1) if oldest else 0

    # Health indicator
    if oldest_pending_days >= 7:
        health = "critical"
    elif oldest_pending_days >= 3:
        health = "warning"
    else:
        health = "ok"

    return {
        "editor": editor,
        "pending_videos": int(pending_videos),
        "pending_clientes": int(pending_clientes),
        "delivered_week": int(delivered_week),
        "delivered_month": int(delivered_month),
        "avg_turnaround_hours": avg_turnaround_hours,
        "oldest_pending_days": oldest_pending_days,
        "health": health,
    }


def _build_stats(conn):
    now = datetime.now()
    stats_per_editor = [get_editor_stats(conn, editor, now) for editor in EDITORS]
    total_pending_videos = sum(s["pending_videos"] for s in stats_per_editor)
    total_pending_clientes = sum(s["pending_clientes"] for s in stats_per_editor)
    total_delivered_week = sum(s["delivered_week"] for s in stats_per_editor)
    total_delivered_month = sum(s["delivered_month"] for s in stats_per_editor)
    top_delivered_week = sorted(stats_per_editor, key=lambda x: -x["delivered_week"])[:3]
    critical_editors = [s for s in stats_per_editor if s["health"] == "critical"]
    return {
        "ok": True,
        "now": now.isoformat(timespec="seconds"),
        "by_editor": stats_per_editor,
        "totals": {
            "pending_videos": total_pending_videos,
            "pending_clientes": total_pending_clientes,
            "delivered_week": total_delivered_week,
            "delivered_month": total_delivered_month,
        },
        "top_delivered_week": [s["editor"] for s in top_delivered_week if s["delivered_week"] > 0],
        "critical_editors": [s["editor"] for s in critical_editors],
    }


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        try:
            if _IMPORT_ERROR:
                return json_response(self, {"error": "import", "detail": _IMPORT_ERROR}, status=500)

            params = parse_qs(urlparse(self.path).query)
            admin = params.get("admin", [""])[0] == "1"
            token = (params.get("t", [""])[0] or "").strip()

            if not admin:
                return json_response(self, {"error": "admin token required"}, status=401)
            if not check_token("ADMIN", token):
                return json_response(self, {"error": "unauthorized"}, status=401)

            data = read_db(_build_stats)
            return json_response(self, data)
        except Exception as e:
            return json_response(self, {
                "error": str(e),
                "traceback": traceback.format_exc()[:1500],
            }, status=500)

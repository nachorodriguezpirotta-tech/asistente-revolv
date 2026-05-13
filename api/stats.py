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


def get_editor_pending_detail(conn, editor: str) -> list:
    """Lista detallada de los pending del editor: cliente, count, días esperando, file_name."""
    rows = conn.execute(
        """SELECT TRIM(cliente) as cliente,
                  SUM(COALESCE(pending_count, 1)) as videos,
                  MIN(detected_at) as detected_at,
                  MIN(id) as id,
                  GROUP_CONCAT(file_name, ' | ') as files
           FROM tasks
           WHERE editor = ? AND status = 'pending'
           GROUP BY TRIM(cliente)
           ORDER BY detected_at ASC""",
        (editor,),
    ).fetchall()
    now = datetime.now()
    out = []
    for r in rows:
        det = _parse_iso(r["detected_at"])
        days = round((now - det).total_seconds() / 86400, 1) if det else 0
        files = (r["files"] or "")[:200]  # cortar largo
        out.append({
            "id": r["id"],
            "cliente": r["cliente"],
            "videos": int(r["videos"]),
            "days_waiting": days,
            "first_file": files.split(" | ")[0] if files else "",
        })
    return out


def get_client_stats(conn, cliente: str, now: datetime) -> dict:
    """Métricas de un cliente individual."""
    week_ago = (now - timedelta(days=7)).isoformat(timespec="seconds")
    month_ago = (now - timedelta(days=30)).isoformat(timespec="seconds")
    quarter_ago = (now - timedelta(days=90)).isoformat(timespec="seconds")

    # Crudos subidos por cliente (de known_files)
    crudos_week = conn.execute(
        "SELECT COUNT(*) FROM known_files WHERE TRIM(cliente) = ? AND first_seen_at >= ? AND is_baseline = 0",
        (cliente, week_ago),
    ).fetchone()[0]
    crudos_month = conn.execute(
        "SELECT COUNT(*) FROM known_files WHERE TRIM(cliente) = ? AND first_seen_at >= ? AND is_baseline = 0",
        (cliente, month_ago),
    ).fetchone()[0]

    # Editados entregados (de known_edited_files)
    editados_week = conn.execute(
        "SELECT COUNT(*) FROM known_edited_files WHERE TRIM(cliente) = ? AND first_seen_at >= ? AND is_baseline = 0",
        (cliente, week_ago),
    ).fetchone()[0]
    editados_month = conn.execute(
        "SELECT COUNT(*) FROM known_edited_files WHERE TRIM(cliente) = ? AND first_seen_at >= ? AND is_baseline = 0",
        (cliente, month_ago),
    ).fetchone()[0]

    # Pendiente actual
    pending = conn.execute(
        "SELECT COALESCE(SUM(COALESCE(pending_count, 1)), 0), MIN(editor) FROM tasks WHERE TRIM(cliente) = ? AND status = 'pending'",
        (cliente,),
    ).fetchone()
    pending_videos = int(pending[0] or 0)
    editor = pending[1]

    # Último crudo subido + último editado entregado
    last_crudo = conn.execute(
        "SELECT MAX(first_seen_at) FROM known_files WHERE TRIM(cliente) = ?", (cliente,),
    ).fetchone()[0]
    last_editado = conn.execute(
        "SELECT MAX(first_seen_at) FROM known_edited_files WHERE TRIM(cliente) = ?", (cliente,),
    ).fetchone()[0]

    days_since_crudo = None
    if last_crudo:
        d = _parse_iso(last_crudo)
        if d:
            days_since_crudo = round((now - d).total_seconds() / 86400, 1)

    days_since_editado = None
    if last_editado:
        d = _parse_iso(last_editado)
        if d:
            days_since_editado = round((now - d).total_seconds() / 86400, 1)

    # Health: ghost si >60 días sin crudos, activo si subió en últimos 30 días
    if days_since_crudo is None:
        status = "unknown"
    elif days_since_crudo > 60:
        status = "ghost"  # candidato a churn
    elif days_since_crudo <= 7:
        status = "hot"  # subiendo activamente
    elif days_since_crudo <= 30:
        status = "active"
    else:
        status = "cold"

    return {
        "cliente": cliente,
        "editor": editor,
        "crudos_week": int(crudos_week),
        "crudos_month": int(crudos_month),
        "editados_week": int(editados_week),
        "editados_month": int(editados_month),
        "pending_videos": pending_videos,
        "days_since_crudo": days_since_crudo,
        "days_since_editado": days_since_editado,
        "status": status,
    }


def _build_stats(conn):
    now = datetime.now()
    # === EDITORES === — usar lista activa desde cfg_editors (DB), no hardcoded
    try:
        rows = conn.execute("SELECT name FROM cfg_editors WHERE active=1 ORDER BY name").fetchall()
        editors_active = [r["name"] for r in rows]
        if not editors_active:
            editors_active = EDITORS  # fallback
    except Exception:
        editors_active = EDITORS
    stats_per_editor = [get_editor_stats(conn, editor, now) for editor in editors_active]
    pending_detail = {ed: get_editor_pending_detail(conn, ed) for ed in editors_active}

    total_pending_videos = sum(s["pending_videos"] for s in stats_per_editor)
    total_pending_clientes = sum(s["pending_clientes"] for s in stats_per_editor)
    total_delivered_week = sum(s["delivered_week"] for s in stats_per_editor)
    total_delivered_month = sum(s["delivered_month"] for s in stats_per_editor)
    top_delivered_week = sorted(stats_per_editor, key=lambda x: -x["delivered_week"])[:3]
    critical_editors = [s for s in stats_per_editor if s["health"] == "critical"]

    # === CLIENTES ===
    # Tomamos TODOS los clientes que aparecen en known_files o known_edited_files
    client_rows = conn.execute(
        """SELECT DISTINCT TRIM(cliente) as cliente FROM (
              SELECT cliente FROM known_files
              UNION SELECT cliente FROM known_edited_files
              UNION SELECT cliente FROM tasks WHERE status='pending'
           ) WHERE cliente IS NOT NULL AND cliente != ''"""
    ).fetchall()
    clients_stats = [get_client_stats(conn, r["cliente"], now) for r in client_rows]

    # Ordenamientos útiles
    top_active = sorted(clients_stats, key=lambda x: -x["crudos_month"])[:10]
    ghost_clients = [c for c in clients_stats if c["status"] == "ghost"][:20]
    hot_clients = [c for c in clients_stats if c["status"] == "hot"]

    return {
        "ok": True,
        "now": now.isoformat(timespec="seconds"),
        "by_editor": stats_per_editor,
        "pending_detail": pending_detail,
        "totals": {
            "pending_videos": total_pending_videos,
            "pending_clientes": total_pending_clientes,
            "delivered_week": total_delivered_week,
            "delivered_month": total_delivered_month,
            "clientes_activos": len([c for c in clients_stats if c["status"] in ("hot", "active")]),
            "clientes_ghost": len(ghost_clients),
        },
        "top_delivered_week": [s["editor"] for s in top_delivered_week if s["delivered_week"] > 0],
        "critical_editors": [s["editor"] for s in critical_editors],
        "clients": clients_stats,
        "top_active_clients": top_active,
        "ghost_clients": ghost_clients,
        "hot_clients_count": len(hot_clients),
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

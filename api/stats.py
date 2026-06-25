"""
GET /api/stats?admin=1&t=<admin_token>

Métricas de productividad por editor:
  - pending_videos: videos pendientes (suma de pending_count)
  - pending_clientes: clientes con tareas pendientes
  - delivered_week: VIDEOS entregados en últimos 7 días (no clientes)
  - delivered_month: VIDEOS entregados en últimos 30 días
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



from delivered_count import _delivered_by_editor, _delivery_events


def get_editor_stats(conn, editor: str, now: datetime, delivered_w=None, delivered_m=None) -> dict:
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

    # Entregados última semana / mes.
    # FIX 05/jun: antes contaba tasks 'done' (= 1 por CLIENTE completado), no
    # videos. Si Santi entregaba 20 videos de Duna era 1 task done → contaba 1.
    # Ahora cuenta VIDEOS entregados desde mail_log (completion mails únicos por
    # dedupe_key). Cada completion mail = 1 video editado entregado por el editor.
    # COALESCE para no colapsar mails viejos con dedupe_key vacío.
    # Excluir correcciones: una corrección NO es un video nuevo entregado, es
    # re-trabajo del mismo video. Se excluyen los 🔧 (correcciones detectadas)
    # y los nombres con "correc" (correcciones que entraron como entrega normal
    # pero el archivo se llama "video 5 correccion"). FIX 05/jun: Duna daba 28
    # con correcciones, pero Santi entregó 20 videos reales.
    # Entregas REALES (known_edited_files, todos incl. baseline, dedup
    # correcciones, por created_time). Si no vienen precomputados, calcular.
    if delivered_w is None:
        delivered_w = _delivered_by_editor(conn, week_ago).get(editor, 0)
    if delivered_m is None:
        delivered_m = _delivered_by_editor(conn, month_ago).get(editor, 0)
    delivered_week = delivered_w
    delivered_month = delivered_m

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


def get_daily_aggregates(conn, days: int = 14) -> dict:
    """Devuelve agregados diarios para gráficos: por día, entregas por editor + crudos recibidos."""
    now = datetime.now()
    days_list = [(now - timedelta(days=i)).date().isoformat() for i in range(days-1, -1, -1)]
    days_set = set(days_list)

    # Entregas por día por editor — desde los completion mails (1 mail = 1 video).
    deliveries_by_day = {d: {} for d in days_list}
    editors_set = set()
    for (ed, ts) in _delivery_events(conn, (now - timedelta(days=days)).isoformat(timespec="seconds")):
        day = (ts or "")[:10]
        if day in deliveries_by_day:
            deliveries_by_day[day][ed] = deliveries_by_day[day].get(ed, 0) + 1
            editors_set.add(ed)

    # Crudos recibidos por día
    rows = conn.execute("""
        SELECT substr(first_seen_at, 1, 10) as day, COUNT(*) as n
        FROM known_files WHERE first_seen_at >= ? AND is_baseline=0
        GROUP BY day
    """, ((now - timedelta(days=days)).isoformat(timespec="seconds"),)).fetchall()
    crudos_by_day = {d: 0 for d in days_list}
    for r in rows:
        if r["day"] in crudos_by_day:
            crudos_by_day[r["day"]] = r["n"]

    return {
        "days": days_list,
        "editors": sorted(editors_set),
        "deliveries_by_day": deliveries_by_day,
        "crudos_by_day": crudos_by_day,
    }


def _build_sla_data(conn):
    """SLA = horas entre detected_at y completed_at en tasks 'done'.
    Devuelve matrix por (editor, cliente) con avg/median/p90/count y un
    resumen por editor y por cliente.

    Solo cuenta tasks con file_name != '(pendiente cargado manualmente)' porque
    las manuales no tienen detected_at significativo."""
    rows = conn.execute("""
        SELECT cliente, editor, detected_at, completed_at, file_name
        FROM tasks
        WHERE status='done'
          AND completed_at IS NOT NULL
          AND detected_at IS NOT NULL
          AND editor IS NOT NULL
          AND TRIM(editor) != ''
          AND file_name NOT LIKE '%manualmente%'
    """).fetchall()

    def _hours_between(det, comp):
        try:
            d = datetime.fromisoformat((det or "").replace("Z", "+00:00"))
            c = datetime.fromisoformat((comp or "").replace("Z", "+00:00"))
            if d.tzinfo and not c.tzinfo:
                c = c.replace(tzinfo=d.tzinfo)
            if c.tzinfo and not d.tzinfo:
                d = d.replace(tzinfo=c.tzinfo)
            delta = (c - d).total_seconds() / 3600.0
            return delta if delta > 0 else None
        except Exception:
            return None

    # Acumular horas por (editor, cliente)
    by_pair = {}    # {(editor, cliente): [hours,...]}
    by_editor = {}  # {editor: [hours,...]}
    by_client = {}  # {cliente: [hours,...]}
    for r in rows:
        h = _hours_between(r["detected_at"], r["completed_at"])
        if h is None or h > 24 * 60:  # filtrar outliers > 60 días
            continue
        key = (r["editor"], r["cliente"])
        by_pair.setdefault(key, []).append(h)
        by_editor.setdefault(r["editor"], []).append(h)
        by_client.setdefault(r["cliente"], []).append(h)

    def _stats(arr):
        if not arr:
            return None
        s = sorted(arr)
        n = len(s)
        return {
            "count": n,
            "avg_h": round(sum(s) / n, 1),
            "median_h": round(s[n // 2], 1),
            "p90_h": round(s[min(n - 1, int(n * 0.9))], 1),
            "min_h": round(s[0], 1),
            "max_h": round(s[-1], 1),
        }

    matrix = []
    for (editor, cliente), arr in by_pair.items():
        st = _stats(arr)
        if st and st["count"] >= 1:
            matrix.append({"editor": editor, "cliente": cliente, **st})
    # Orden: editor asc, count desc
    matrix.sort(key=lambda x: (x["editor"], -x["count"]))

    editor_summary = [{"editor": e, **(_stats(a) or {})} for e, a in by_editor.items()]
    editor_summary.sort(key=lambda x: x.get("avg_h", 0))

    client_summary = [{"cliente": c, **(_stats(a) or {})} for c, a in by_client.items()]
    # Solo clientes con >=3 entregas (datos significativos)
    client_summary = [c for c in client_summary if c.get("count", 0) >= 3]
    client_summary.sort(key=lambda x: -x.get("avg_h", 0))  # los más lentos primero

    return {
        "matrix": matrix,
        "by_editor": editor_summary,
        "by_client_slowest": client_summary[:20],
    }


def _build_productivity_hours(conn):
    """Distribución de entregas por hora del día (0-23h), por editor.
    Identifica si un editor labura de día/noche/fin de semana.

    Fuente: hora (sent_at) de los COMPLETION MAILS de los últimos 60 días —
    cada mail = una entrega del editor. Hora real del aviso. Pedido Ignacio
    18/jun (antes usaba completed_at de tasks done, menos fiel)."""
    since = (datetime.now() - timedelta(days=60)).isoformat(timespec="seconds")
    events = _delivery_events(conn, since)
    rows = [{"editor": e, "completed_at": ts} for (e, ts) in events]

    from collections import Counter
    by_editor_hour = {}  # {editor: [0]*24}
    by_editor_dow = {}   # {editor: [0]*7}  Mon=0..Sun=6
    for r in rows:
        try:
            t = datetime.fromisoformat((r["completed_at"] or "").replace("Z", "+00:00"))
        except Exception:
            continue
        ed = r["editor"]
        by_editor_hour.setdefault(ed, [0] * 24)[t.hour] += 1
        by_editor_dow.setdefault(ed, [0] * 7)[t.weekday()] += 1

    result = []
    for ed, hours in by_editor_hour.items():
        total = sum(hours)
        if total == 0:
            continue
        # Detectar "peak hours" — top 3 horas más activas
        sorted_hours = sorted(enumerate(hours), key=lambda x: -x[1])
        peaks = [h for h, c in sorted_hours[:3] if c > 0]
        # Pattern: night (22-5), morning (6-11), afternoon (12-17), evening (18-21)
        buckets = {
            "night": sum(hours[h] for h in [22, 23, 0, 1, 2, 3, 4, 5]),
            "morning": sum(hours[h] for h in [6, 7, 8, 9, 10, 11]),
            "afternoon": sum(hours[h] for h in [12, 13, 14, 15, 16, 17]),
            "evening": sum(hours[h] for h in [18, 19, 20, 21]),
        }
        dominant = max(buckets, key=lambda k: buckets[k])
        # Weekend ratio
        dow = by_editor_dow.get(ed, [0] * 7)
        weekend = dow[5] + dow[6]
        weekend_pct = round(weekend * 100 / total, 1) if total else 0
        result.append({
            "editor": ed,
            "total": total,
            "hours": hours,
            "dow": dow,
            "peak_hours": peaks,
            "buckets": buckets,
            "dominant_period": dominant,
            "weekend_pct": weekend_pct,
        })

    result.sort(key=lambda x: -x["total"])
    return result


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
    _dw = _delivered_by_editor(conn, (now - timedelta(days=7)).isoformat(timespec="seconds"))
    _dm = _delivered_by_editor(conn, (now - timedelta(days=30)).isoformat(timespec="seconds"))
    stats_per_editor = [get_editor_stats(conn, editor, now, _dw.get(editor, 0), _dm.get(editor, 0)) for editor in editors_active]
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

    # Agregados diarios para gráficos
    daily = get_daily_aggregates(conn, days=14)

    # SLA + horarios — info nueva
    sla = _build_sla_data(conn)
    productivity = _build_productivity_hours(conn)

    return {
        "ok": True,
        "now": now.isoformat(timespec="seconds"),
        "by_editor": stats_per_editor,
        "pending_detail": pending_detail,
        "daily": daily,
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
        "sla": sla,
        "productivity": productivity,
    }


def _build_editor_self_stats(conn, editor: str):
    """Stats personales para un editor (vista 'Mis stats')."""
    now = datetime.now()
    my_stats = get_editor_stats(conn, editor, now)
    my_pending = get_editor_pending_detail(conn, editor)

    # Ranking semanal: comparar contra otros editores activos
    week_ago = (now - timedelta(days=7)).isoformat(timespec="seconds")
    _dw_all = _delivered_by_editor(conn, week_ago)
    leaderboard = [{"editor": e, "delivered": n} for e, n in
                   sorted(_dw_all.items(), key=lambda x: -x[1]) if n > 0]
    rank = None
    for i, ed in enumerate(leaderboard, 1):
        if ed["editor"] == editor:
            rank = i
            break

    # Histograma últimos 14 días (solo del editor)
    days_list = [(now - timedelta(days=i)).date().isoformat() for i in range(13, -1, -1)]
    rows = conn.execute("""
        SELECT substr(completed_at, 1, 10) as day, COUNT(*) as n
        FROM tasks WHERE status='done' AND editor=? AND completed_at >= ?
        GROUP BY day
    """, (editor, (now - timedelta(days=14)).isoformat(timespec="seconds"))).fetchall()
    by_day = {d: 0 for d in days_list}
    for r in rows:
        if r["day"] in by_day:
            by_day[r["day"]] = r["n"]

    return {
        "ok": True,
        "editor": editor,
        "stats": my_stats,
        "pending_detail": my_pending,
        "leaderboard": leaderboard[:5],
        "rank": rank,
        "total_editors": len(leaderboard),
        "daily": {"days": days_list, "by_day": by_day},
    }


def _build_client_detail(conn, cliente: str):
    """Detalle histórico de un cliente: crudos por mes, editados por mes,
    tiempo promedio, lista de últimos archivos, editores que entregaron."""
    now = datetime.now()
    # Crudos por mes (últimos 6 meses)
    crudos_by_month = {}
    rows = conn.execute("""
        SELECT substr(first_seen_at, 1, 7) as mes, COUNT(*) as n
        FROM known_files WHERE TRIM(cliente)=TRIM(?) AND is_baseline=0
        GROUP BY mes ORDER BY mes DESC LIMIT 6
    """, (cliente,)).fetchall()
    for r in rows:
        crudos_by_month[r["mes"]] = r["n"]
    # Editados por mes
    editados_by_month = {}
    rows = conn.execute("""
        SELECT substr(first_seen_at, 1, 7) as mes, COUNT(*) as n
        FROM known_edited_files WHERE TRIM(cliente)=TRIM(?) AND is_baseline=0
        GROUP BY mes ORDER BY mes DESC LIMIT 6
    """, (cliente,)).fetchall()
    for r in rows:
        editados_by_month[r["mes"]] = r["n"]

    # Últimos 10 archivos de cada tipo
    last_crudos = [dict(r) for r in conn.execute("""
        SELECT name, first_seen_at FROM known_files
        WHERE TRIM(cliente)=TRIM(?) AND is_baseline=0
        ORDER BY first_seen_at DESC LIMIT 10
    """, (cliente,)).fetchall()]
    last_editados = [dict(r) for r in conn.execute("""
        SELECT name, first_seen_at FROM known_edited_files
        WHERE TRIM(cliente)=TRIM(?) AND is_baseline=0
        ORDER BY first_seen_at DESC LIMIT 10
    """, (cliente,)).fetchall()]

    # Editores que entregaron (count de tasks done)
    editors_history = [dict(r) for r in conn.execute("""
        SELECT editor, COUNT(*) as n FROM tasks
        WHERE TRIM(cliente)=TRIM(?) AND status='done' AND editor IS NOT NULL
        GROUP BY editor ORDER BY n DESC
    """, (cliente,)).fetchall()]

    # Tiempo promedio entrega (detected_at → completed_at)
    rows = conn.execute("""
        SELECT detected_at, completed_at FROM tasks
        WHERE TRIM(cliente)=TRIM(?) AND status='done'
          AND detected_at IS NOT NULL AND completed_at IS NOT NULL
    """, (cliente,)).fetchall()
    turnarounds = []
    for r in rows:
        det = _parse_iso(r["detected_at"])
        comp = _parse_iso(r["completed_at"])
        if det and comp and comp > det:
            turnarounds.append((comp - det).total_seconds() / 3600)
    avg_turnaround_hours = round(sum(turnarounds) / len(turnarounds), 1) if turnarounds else None

    # Editor actual asignado (de pending o último task)
    current = conn.execute("""
        SELECT editor FROM tasks WHERE TRIM(cliente)=TRIM(?)
        ORDER BY (status='pending') DESC, id DESC LIMIT 1
    """, (cliente,)).fetchone()
    current_editor = current["editor"] if current else None

    # Pending actual
    pending = conn.execute(
        "SELECT COALESCE(SUM(COALESCE(pending_count, 1)), 0) FROM tasks WHERE TRIM(cliente)=TRIM(?) AND status='pending'",
        (cliente,),
    ).fetchone()[0]

    # Folder ID
    fid_row = conn.execute("SELECT folder_id FROM clients WHERE TRIM(cliente)=TRIM(?)", (cliente,)).fetchone()
    drive_folder_id = fid_row["folder_id"] if fid_row else None

    return {
        "ok": True,
        "cliente": cliente,
        "current_editor": current_editor,
        "pending_videos": int(pending or 0),
        "drive_folder_id": drive_folder_id,
        "crudos_by_month": crudos_by_month,
        "editados_by_month": editados_by_month,
        "last_crudos": last_crudos,
        "last_editados": last_editados,
        "editors_history": editors_history,
        "avg_turnaround_hours": avg_turnaround_hours,
        "total_crudos": sum(crudos_by_month.values()),
        "total_editados": sum(editados_by_month.values()),
    }


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        try:
            if _IMPORT_ERROR:
                return json_response(self, {"error": "import", "detail": _IMPORT_ERROR}, status=500)

            params = parse_qs(urlparse(self.path).query)
            admin = params.get("admin", [""])[0] == "1"
            editor = (params.get("editor", [""])[0] or "").strip()
            token = (params.get("t", [""])[0] or "").strip()
            cliente_detail = (params.get("client_detail", [""])[0] or "").strip()

            if admin and check_token("ADMIN", token):
                if cliente_detail:
                    data = read_db(lambda conn: _build_client_detail(conn, cliente_detail))
                    return json_response(self, data)
                data = read_db(_build_stats)
                return json_response(self, data)

            if editor and check_token(editor, token):
                data = read_db(lambda conn: _build_editor_self_stats(conn, editor))
                return json_response(self, data)

            return json_response(self, {"error": "unauthorized"}, status=401)
        except Exception as e:
            return json_response(self, {
                "error": str(e),
                "traceback": traceback.format_exc()[:1500],
            }, status=500)

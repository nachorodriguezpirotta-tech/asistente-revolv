"""
GET /api/data?editor=<nombre>&t=<token>
GET /api/data?admin=1&t=<admin_token>  → vista global (Ignacio)

Devuelve JSON con los pendientes:
  Si editor: solo los suyos.
  Si admin: agrupados por editor.
"""

import json
import os
import sys
import traceback
from http.server import BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

# Asegurar que podemos importar _shared.py del mismo directorio
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

try:
    from _shared import (
        check_token, read_db, json_response, EDITORS, make_token,
        DASHBOARD_SECRET, GITHUB_PAT,
    )
    _IMPORT_ERROR = None
except Exception as _e:
    _IMPORT_ERROR = f"{type(_e).__name__}: {_e}\n{traceback.format_exc()}"


def _manual_adjust_map(conn) -> dict:
    """{(editor_lower, cliente_lower): videos_a_restar} para tasks pending MANUALES
    (count_locked=1) cuyas entregas reales (mails) ya descontaron parte/todo el
    count. Reconciliación EN MEMORIA (no muta DB) para que el display sea correcto
    al instante y robusto: aunque el cierre en DB se haya perdido por un push
    concurrente de tracker.db, el restante se re-deriva de mail_log (durable)."""
    out = {}
    try:
        from tracker import delivered_against_task
        tasks = conn.execute(
            "SELECT TRIM(cliente) AS c, TRIM(COALESCE(editor,'')) AS e, "
            "COALESCE(pending_count,1) AS pc, detected_at "
            "FROM tasks WHERE status='pending' AND COALESCE(count_locked,0)=1"
        ).fetchall()
        for t in tasks:
            d = delivered_against_task(conn, t["c"], t["e"], t["detected_at"])
            if d > 0:
                k = (t["e"].lower(), t["c"].lower())
                out[k] = out.get(k, 0) + min(t["pc"] or 1, d)
    except Exception:
        pass
    return out


def _get_client_folder_map(conn) -> dict:
    """Devuelve {cliente_normalizado: folder_id} de la tabla clients."""
    rows = conn.execute("SELECT cliente, folder_id FROM clients").fetchall()
    return {r["cliente"].strip().lower(): r["folder_id"] for r in rows}


def _archived_norm_set(conn) -> set:
    """Set normalizado de clientes archivados — sus tasks no se muestran."""
    import unicodedata
    def norm(s):
        s = unicodedata.normalize("NFD", s or "")
        s = "".join(c for c in s if unicodedata.category(c) != "Mn")
        return " ".join(s.lower().split())
    try:
        rows = conn.execute("SELECT cliente FROM cfg_archived_clients").fetchall()
    except Exception:
        return set()
    return {norm(r["cliente"]) for r in rows}, norm


def _priority_sort(conn, editor, items, norm):
    """Aplica el orden de entrega manual (cfg_delivery_priority) si existe.
    Urgentes SIEMPRE primero; dentro de cada grupo: prioridad asignada
    (menor = primero), después los sin prioridad alfabético. Opcional:
    sin prioridades guardadas, el orden queda como estaba."""
    try:
        rows = conn.execute(
            "SELECT cliente, priority FROM cfg_delivery_priority WHERE editor=?",
            (editor,)).fetchall()
    except Exception:
        rows = []
    if not rows:
        return items
    prio = {norm(r["cliente"]): r["priority"] for r in rows}
    BIG = 10 ** 9
    return sorted(items, key=lambda it: (
        0 if it.get("urgent") else 1,
        prio.get(norm(it.get("cliente") or ""), BIG),
        norm(it.get("cliente") or ""),
    ))


def get_all_clients(conn) -> dict:
    """Devuelve lista de TODOS los clientes conocidos del sistema con su folder_id.
    Fuente: tabla clients + tasks + known_files + known_edited_files.
    Útil para autocomplete en el dashboard.
    """
    universe = {}  # cliente → folder_id

    # 1. Tabla clients (con folder_id confirmado)
    for r in conn.execute("SELECT cliente, folder_id FROM clients").fetchall():
        if r["cliente"]:
            universe[r["cliente"].strip()] = r["folder_id"]

    # 2. Tasks (puede haber clientes sin folder_id confirmado)
    for r in conn.execute("SELECT DISTINCT TRIM(cliente) as c FROM tasks WHERE cliente IS NOT NULL AND cliente != ''").fetchall():
        if r["c"] and r["c"] not in universe:
            universe[r["c"]] = None

    # 3. known_files / known_edited_files (histórico)
    for table in ("known_files", "known_edited_files"):
        try:
            for r in conn.execute(f"SELECT DISTINCT TRIM(cliente) as c FROM {table}").fetchall():
                if r["c"] and r["c"] not in universe:
                    universe[r["c"]] = None
        except Exception:
            pass

    clients = [{"cliente": name, "folder_id": fid} for name, fid in universe.items()]
    clients.sort(key=lambda x: x["cliente"].lower())
    return {"clients": clients}


def get_editor_data(conn, editor: str) -> dict:
    # Incluir variantes/apodos del editor (Sheet usa 'Adri', dashboard 'Adrian').
    # Bug Luis/Adri 17/jun: pendientes con apodo no le aparecían al editor.
    try:
        from tracker import canonical_editor
        _eds = [r["name"] for r in conn.execute("SELECT name FROM cfg_editors WHERE active=1").fetchall()]
        _target = canonical_editor(editor, _eds).strip().lower()
        _distinct = [r["editor"] for r in conn.execute(
            "SELECT DISTINCT editor FROM tasks WHERE status='pending' AND editor IS NOT NULL").fetchall()]
        _variants = [e for e in _distinct if canonical_editor(e, _eds).strip().lower() == _target]
        if editor not in _variants:
            _variants.append(editor)
    except Exception:
        _variants = [editor]
    _ph = ",".join("?" * len(_variants))
    # 1 entry por cliente con la suma de pending_count (videos pendientes)
    rows = conn.execute(
        f"""SELECT cliente, MIN(id) as id, SUM(COALESCE(pending_count, 1)) as videos,
                  MIN(detected_at) as oldest,
                  MAX(COALESCE(urgent, 0)) as urgent,
                  MAX(COALESCE(note, '')) as note
           FROM tasks
           WHERE editor IN ({_ph}) AND status = 'pending'
           GROUP BY TRIM(cliente)
           ORDER BY MAX(COALESCE(urgent, 0)) DESC, TRIM(cliente)""",
        _variants,
    ).fetchall()

    folder_map = _get_client_folder_map(conn)

    # Progresos del editor (múltiples labels, ej. "Básicos" y "Avanzados")
    prog_rows = conn.execute(
        "SELECT label, current, total FROM editor_progress WHERE editor = ? ORDER BY label",
        (editor,),
    ).fetchall()
    progresses = [
        {"label": r["label"], "current": r["current"], "total": r["total"]}
        for r in prog_rows
    ]

    # Mini-stats personales del editor
    from datetime import datetime, timedelta
    now = datetime.now()
    week_ago = (now - timedelta(days=7)).isoformat(timespec="seconds")
    # Entregas REALES de la semana (known_edited_files, no tasks-done que
    # contaba clientes). Unificado con /api/stats vía delivered_count.
    try:
        from delivered_count import _delivered_by_editor
        delivered_week = _delivered_by_editor(conn, week_ago).get(editor, 0)
    except Exception:
        delivered_week = conn.execute(
            "SELECT COUNT(*) FROM tasks WHERE editor=? AND status='done' AND completed_at >= ?",
            (editor, week_ago),
        ).fetchone()[0]
    urgent_count = conn.execute(
        "SELECT COUNT(*) FROM tasks WHERE editor=? AND status='pending' AND COALESCE(urgent, 0) = 1",
        (editor,),
    ).fetchone()[0]
    _arch, _norm = _archived_norm_set(conn)
    rows = [r for r in rows if _norm(r["cliente"]) not in _arch]
    # Reconciliación manual: restar entregas reales (mails) de las tasks manual ya
    # cubiertas. Suma el ajuste de todas las variantes de apodo del editor.
    _adj = _manual_adjust_map(conn)
    pendientes_list = []
    for r in rows:
        cli = r["cliente"].strip()
        restar = sum(_adj.get((v.strip().lower(), cli.lower()), 0) for v in _variants)
        videos = max(0, (r["videos"] or 1) - restar)
        if videos <= 0:
            continue  # ya entregado → no mostrar
        pendientes_list.append({
            "id": r["id"],
            "cliente": cli,
            "videos": videos,
            "detected_at": r["oldest"],
            "drive_folder_id": folder_map.get(cli.lower()),
            "urgent": bool(r["urgent"]) if "urgent" in r.keys() else False,
            "note": r["note"] if "note" in r.keys() else None,
        })
    pendientes_list = _priority_sort(conn, editor, pendientes_list, _norm)
    return {
        "editor": editor,
        "pendientes": pendientes_list,
        "progresses": progresses,
        "stats": {
            "pending_total": sum(p["videos"] for p in pendientes_list),
            "pending_clientes": len(pendientes_list),
            "delivered_week": int(delivered_week),
            "urgent_count": int(urgent_count),
        },
    }


def get_all_data(conn) -> dict:
    rows = conn.execute(
        """SELECT editor, TRIM(cliente) as cliente, MIN(id) as id,
                  SUM(COALESCE(pending_count, 1)) as videos, MIN(detected_at) as oldest,
                  MAX(COALESCE(urgent, 0)) as urgent,
                  MAX(COALESCE(note, '')) as note
           FROM tasks
           WHERE status = 'pending'
           GROUP BY editor, TRIM(cliente)
           ORDER BY editor, MAX(COALESCE(urgent, 0)) DESC, cliente"""
    ).fetchall()
    folder_map = _get_client_folder_map(conn)
    _arch, _norm = _archived_norm_set(conn)
    _adj = _manual_adjust_map(conn)
    by_editor = {}
    for r in rows:
        if _norm(r["cliente"]) in _arch:
            continue
        ed = r["editor"] or "— sin editor —"
        cli = r["cliente"]
        restar = _adj.get(((r["editor"] or "").strip().lower(), cli.strip().lower()), 0)
        videos = max(0, (r["videos"] or 1) - restar)
        if videos <= 0:
            continue  # ya entregado → no mostrar
        by_editor.setdefault(ed, []).append({
            "id": r["id"],
            "cliente": cli,
            "videos": videos,
            "detected_at": r["oldest"],
            "drive_folder_id": folder_map.get(cli.strip().lower()),
            "urgent": bool(r["urgent"]) if "urgent" in r.keys() else False,
            "note": r["note"] if "note" in r.keys() else None,
        })

    # Orden de entrega manual por editor (opcional)
    for ed in list(by_editor.keys()):
        by_editor[ed] = _priority_sort(conn, ed, by_editor[ed], _norm)

    # Conteo de carpetas Drive pendientes de aprobación (para badge en dashboard)
    pending_folders_count = 0
    try:
        pending_folders_count = conn.execute(
            "SELECT COUNT(*) FROM pending_drive_folders WHERE status='pending'"
        ).fetchone()[0]
    except Exception:
        pass

    # Asegurar que TODOS los editores ACTIVOS aparezcan, aunque no tengan pendientes.
    # Lee de cfg_editors (DB) que es la fuente de verdad runtime.
    editors_on_vacation = set()
    try:
        ed_rows = conn.execute("SELECT name, COALESCE(on_vacation, 0) as on_vacation FROM cfg_editors WHERE active=1").fetchall()
        for r in ed_rows:
            ed_name = r["name"]
            if ed_name and ed_name not in by_editor:
                by_editor[ed_name] = []
            if r["on_vacation"]:
                editors_on_vacation.add(ed_name)
    except Exception:
        # Fallback al hardcoded si la tabla no existe
        try:
            from aliases import EDITORS_LIST
            for ed in EDITORS_LIST:
                if ed not in by_editor:
                    by_editor[ed] = []
        except Exception:
            pass

    # Generar links únicos por editor (cualquier editor que aparezca acá tiene su link)
    editor_links = {}
    for ed in by_editor.keys():
        if ed.startswith("—"):  # sin editor → no link
            continue
        editor_links[ed] = f"?editor={ed}&t={make_token(ed)}"

    # Progresses por editor (cada editor puede tener varios labels)
    prog_rows = conn.execute(
        "SELECT editor, label, current, total FROM editor_progress ORDER BY editor, label"
    ).fetchall()
    editor_progresses = {}
    for r in prog_rows:
        editor_progresses.setdefault(r["editor"], []).append({
            "label": r["label"], "current": r["current"], "total": r["total"]
        })

    # Stats — resumen del estado actual para banner arriba del dashboard
    from datetime import datetime, timedelta
    now = datetime.now()
    week_ago = (now - timedelta(days=7)).isoformat(timespec="seconds")
    closed_total = conn.execute("SELECT COUNT(*) FROM tasks WHERE status='done'").fetchone()[0]
    try:
        from delivered_count import _delivered_by_editor as _dbe
        from datetime import datetime as _dt, timedelta as _td
        delivered_week = sum(_dbe(conn, (_dt.now()-_td(days=7)).isoformat(timespec="seconds")).values())
    except Exception:
        delivered_week = conn.execute(
        "SELECT COUNT(*) FROM tasks WHERE status='done' AND completed_at >= ?",
        (week_ago,)
    ).fetchone()[0]
    # Totales derivados de by_editor (ya reconciliado con entregas reales), para
    # que el banner coincida con lo que se ve en las tarjetas.
    _items = [it for lst in by_editor.values() for it in lst]
    pending_total = sum(it["videos"] for it in _items)
    pending_clientes = len({it["cliente"].strip().lower() for it in _items})
    urgent_count = conn.execute(
        "SELECT COUNT(*) FROM tasks WHERE status='pending' AND COALESCE(urgent, 0) = 1"
    ).fetchone()[0]
    # Sparkline por editor: entregas últimos 7 días (lista de 7 enteros, hoy al final)
    editor_sparkline = {}
    try:
        from datetime import timedelta
        days = [(now - timedelta(days=i)).date().isoformat() for i in range(6, -1, -1)]
        week_back = (now - timedelta(days=7)).isoformat(timespec="seconds")
        spk_rows = conn.execute("""
            SELECT editor, substr(completed_at, 1, 10) as day, COUNT(*) as n
            FROM tasks WHERE status='done' AND completed_at >= ? AND editor IS NOT NULL
            GROUP BY editor, day
        """, (week_back,)).fetchall()
        # Inicializar con ceros
        for ed in by_editor.keys():
            editor_sparkline[ed] = [0] * len(days)
        # Llenar
        for r in spk_rows:
            ed = r["editor"]
            if ed in editor_sparkline:
                try:
                    idx = days.index(r["day"])
                    editor_sparkline[ed][idx] = r["n"]
                except ValueError:
                    pass
    except Exception:
        pass

    return {
        "by_editor": by_editor,
        "editor_links": editor_links,
        "editor_progresses": editor_progresses,
        "pending_folders_count": pending_folders_count,
        "editors_on_vacation": sorted(editors_on_vacation),
        "editor_sparkline": editor_sparkline,
        "stats": {
            "pendientes": sum(len(v) for v in by_editor.values()),
            "editores": len(by_editor),
            "cerradas_total": closed_total,
            "pending_total": int(pending_total),
            "pending_clientes": int(pending_clientes),
            "delivered_week": int(delivered_week),
            "urgent_count": int(urgent_count),
        },
    }


class handler(BaseHTTPRequestHandler):
    def _safe_json(self, data, status=200):
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if _IMPORT_ERROR is not None:
            return self._safe_json({"error": "import error", "detail": _IMPORT_ERROR}, status=500)

        try:
            return self._do_get_inner()
        except Exception as e:
            return self._safe_json({
                "error": f"{type(e).__name__}: {e}",
                "trace": traceback.format_exc()[:1500],
                "github_pat_set": bool(GITHUB_PAT),
            }, status=500)

    def _do_get_inner(self):
        params = parse_qs(urlparse(self.path).query)
        editor = (params.get("editor", [""])[0] or "").strip()
        admin = params.get("admin", [""])[0]
        token = (params.get("t", [""])[0] or "").strip()
        list_clients = params.get("list_clients", [""])[0]

        if admin == "1":
            from _shared import make_token
            if not check_token("ADMIN", token):
                return json_response(self, {"error": "unauthorized"}, status=401)
            try:
                if list_clients == "1":
                    data = read_db(get_all_clients)
                else:
                    data = read_db(get_all_data)
                return json_response(self, data)
            except Exception as e:
                return json_response(self, {"error": str(e)[:200]}, status=500)

        if not editor:
            return json_response(self, {"error": "missing editor param"}, status=400)
        if not check_token(editor, token):
            return json_response(self, {"error": "unauthorized"}, status=401)

        try:
            if list_clients == "1":
                data = read_db(get_all_clients)
            else:
                data = read_db(lambda conn: get_editor_data(conn, editor))
            return json_response(self, data)
        except Exception as e:
            return json_response(self, {"error": str(e)[:200]}, status=500)

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def log_message(self, *args, **kwargs):
        pass

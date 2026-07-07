"""
Scan — corre periódicamente. Detecta:
  1. Crudos nuevos (en /Material/ o estructuras alternativas) → crea tareas pendientes
  2. Editados nuevos → cierra tareas (delegado al closer)

Uso:
    python3 scan.py            # un scan
    python3 scan.py --notify   # crea tareas Y manda mails
"""

import os
import sys

# === KILL SWITCH ===
# Si existe el archivo .scan_disabled en el repo, el scan no ejecuta nada.
# Sirve para parar de emergencia sin tocar el cron ni el workflow.
_HERE = os.path.dirname(os.path.abspath(__file__))
if os.path.exists(os.path.join(_HERE, ".scan_disabled")):
    print("🛑 KILL SWITCH activo (.scan_disabled existe en repo). Scan deshabilitado.")
    print("   Para reactivar: borrar el archivo .scan_disabled y commitear.")
    sys.exit(0)

import argparse

from drive_client import (
    discover_client_folders, list_material_files,
    find_folder_by_name, list_crudos_anywhere,
    _list_root_items_with_shortcuts, estimate_pending_videos,
)
from tracker import (
    init_db, upsert_client, add_known_file, claim_file, is_file_known,
    create_task, list_pending_tasks, stats, get_conn,
    has_pending_for_client_editor, has_manual_pending_for_client,
    find_similar_pending_client, upsert_pending_drive_folder,
    increment_pending_count, set_pending_count, is_client_blocked,
    mark_pending_task_for_renotification,
)
from sheets_client import read_packs, get_editor_for_client
from aliases import resolve_alias


def _clients_with_pending(conn):
    try:
        import tasks_store
        rows = tasks_store.query("SELECT DISTINCT cliente FROM tasks WHERE status='pending'")
        return {(r["cliente"] or "").strip() for r in rows}
    except Exception:
        rows = conn.execute("SELECT DISTINCT cliente FROM tasks WHERE status='pending'").fetchall()
        return {r[0].strip() for r in rows}


def _clients_already_baselined(conn):
    """Clientes que ya tienen entradas en known_files (entonces no hace falta baseline)."""
    rows = conn.execute("SELECT DISTINCT cliente FROM known_files").fetchall()
    return {r[0].strip() for r in rows}


def _is_file_too_old(created_time_str, max_age_days=3):
    """Si createdTime es más viejo que max_age_days (default 1 día), asumimos
    que es histórico: el archivo existía hace tiempo y recién lo descubrimos
    hoy por algún motivo (carpeta no scaneada antes, archivo movido/renombrado,
    backfill de raw_folder_id, etc.). Tratarlo como baseline para no mandar
    mail de 'crudo/editado nuevo' por algo del pasado.

    Historia del umbral: 3d → 1d (pedido 'no quiero mails viejos') → 3d de
    nuevo (06/jul, caso Ismafeten): subió el VIERNES a la tarde, el sistema lo
    procesó el DOMINGO (finde + lag) → con 1d entró como baseline silencioso →
    ni mail ni tarjeta. 1 día no sobrevive un fin de semana. Los backfills
    históricos reales (Fer Trips, Sandy) eran de 8+ días — 3d los sigue
    cubriendo sin comerse material legítimo del finde."""
    if not created_time_str:
        return False
    try:
        from datetime import datetime, timedelta, timezone
        # Drive usa "2026-05-06T11:41:04.366Z" o sin milis "2026-05-06T11:41:04Z"
        s = created_time_str.replace("Z", "+00:00")
        # Sacar fracción de segundo si la hay
        if "." in s and "+" in s:
            base, tz = s.rsplit("+", 1)
            base = base.split(".")[0]
            s = base + "+" + tz
        ct = datetime.fromisoformat(s)
        age = datetime.now(timezone.utc) - ct
        return age > timedelta(days=max_age_days)
    except Exception:
        return False


def _process_standard_client(c, packs):
    """Procesa un cliente con estructura /Material/ standard.
    Devuelve (new_tasks_list, sin_editor_list, had_new_file).
    Diseñada para correr en ThreadPoolExecutor (cada llamada es independiente)."""
    cliente_real = resolve_alias(c.cliente)
    # ¿La carpeta fue RENOMBRADA en Drive? (mismo folder_id, nombre distinto al
    # registrado). Migrar el cliente entero al nombre nuevo ANTES del upsert —
    # si no, queda partido en dos (caso Pedro→Román Pedroza 07/jul).
    try:
        from tracker import rename_client_for_folder
        _conn_rn = get_conn()
        _prev = _conn_rn.execute(
            "SELECT cliente FROM clients WHERE folder_id=?", (c.folder_id,)).fetchone()
        _conn_rn.close()
        if _prev and (_prev["cliente"] or "").strip() and _prev["cliente"].strip() != cliente_real.strip():
            rename_client_for_folder(c.folder_id, _prev["cliente"], cliente_real)
    except Exception as _e:
        print(f"   ⚠️ chequeo de renombre {cliente_real}: {_e}")
    upsert_client(c.folder_id, cliente_real, c.raw_folder_id)
    files = list_material_files(c.raw_folder_id)

    local_new_tasks = []
    local_sin_editor = []
    had_new_file = False

    for f in files:
        if is_file_known(f["id"]):
            continue
        # OVERRIDE: si el owner/lastModifying del archivo es un editor conocido,
        # NO es crudo — es un editado mal ubicado. Skipear este flow.
        # Bug 28/may Egdylu: Rami subió yt.dejar.mp4 a /Material/, scan.py lo
        # clasificó como crudo + asignó a Fran (editor del Sheet) por error.
        try:
            from classifier import identify_editor_by_owner
            if identify_editor_by_owner(f):
                print(f"  🎯 [scan] owner es editor → skip como crudo: {f['name'][:50]}")
                continue
        except Exception:
            pass
        size = int(f["size"]) if f.get("size") else None
        # Archivo viejo (createdTime > 3 días atrás) → tratar como baseline,
        # NO crear task ni mandar mail. Evita falsos positivos de archivos
        # descubiertos tarde (caso Fer Trips IMG_0792.MOV).
        is_old_file = _is_file_too_old(f.get("createdTime"))
        claimed = claim_file(
            file_id=f["id"], cliente=cliente_real, folder_id=c.raw_folder_id,
            name=f["name"], size=size, created_time=f.get("createdTime"),
            is_baseline=is_old_file,
            subfolder_name=f.get("_subfolder_name"),
        )
        if not claimed:
            continue
        if is_old_file:
            # Archivo viejo: queda registrado en known_files (no se vuelve a procesar)
            # pero NO crea task ni manda mail
            continue
        had_new_file = True
        # Detectar duplicado por apodo/nombre similar: si ya hay pending de "Cisco"
        # y el scan detecta "Cisco Amengual", son la misma persona → no duplicar.
        similar = find_similar_pending_client(cliente_real)
        if similar:
            continue
        # Reglas por subcarpeta/nombre (ej. Egdylu: 'reel'→Fran, 'yt'→Rami)
        # antes del default del Sheet. Pedido Ignacio 10/jun.
        from tracker import resolve_editor_rules as _rer
        editor = (_rer(cliente_real, f.get("_subfolder_name"), f["name"],
                       (f.get("videoMediaMetadata") or {}).get("durationMillis"))
                  or get_editor_for_client(cliente_real, packs))
        # NO duplicar si ya hay manual pending para ESTE editor específico
        # (clientes multi-editor como Roger Marti: Fran/Youtube + Valen/Reels).
        if has_manual_pending_for_client(cliente_real, editor):
            continue
        if has_pending_for_client_editor(cliente_real, editor):
            # Re-notificar (debounce 6h) si llegaron crudos nuevos pero
            # ya había pending. Antes esto se skipeaba silenciosamente.
            renotif_id = mark_pending_task_for_renotification(
                cliente_real, editor, f["id"], f["name"]
            )
            if renotif_id:
                local_new_tasks.append({"cliente": cliente_real, "editor": editor,
                                         "file": f["name"], "renotif": True})
            continue
        if is_client_blocked(cliente_real, editor):
            continue
        if not editor:
            local_sin_editor.append((cliente_real, f["name"]))
        create_task(cliente_real, editor, f["id"], f["name"])
        local_new_tasks.append({"cliente": cliente_real, "editor": editor, "file": f["name"]})

    # Re-estimar pending_count
    if had_new_file or has_pending_for_client_editor(cliente_real, None):
        editor_for_client = get_editor_for_client(cliente_real, packs)
        if has_pending_for_client_editor(cliente_real, editor_for_client):
            estimated = estimate_pending_videos(c.raw_folder_id, c.folder_id)
            if estimated > 0:
                set_pending_count(cliente_real, editor_for_client, estimated)

    return local_new_tasks, local_sin_editor, had_new_file


def run(notify: bool = False):
    print("🔍 SCAN — detectando crudos nuevos\n")
    init_db()

    # === FASE 1: clientes con /Material/ (lógica original) — PARALELIZADO ===
    clients_standard = discover_client_folders()
    print(f"   {len(clients_standard)} clientes con estructura /Material/ standard.")

    print("📋 Leyendo Sheet para mapeo cliente→editor...")
    packs = read_packs()
    print(f"   {len(packs)} packs en el Sheet.\n")

    new_tasks = []
    sin_editor = []

    # ThreadPoolExecutor: paraleliza las llamadas a Drive API (I/O bound).
    # 15 workers = ~15x speedup vs secuencial. SQLite usa WAL mode + timeout para
    # tolerar writes concurrentes. Google services se crean por-thread (thread-safe).
    from concurrent.futures import ThreadPoolExecutor, as_completed
    print(f"⚡ Procesando en paralelo (15 workers)...")
    with ThreadPoolExecutor(max_workers=15) as ex:
        futures = [ex.submit(_process_standard_client, c, packs) for c in clients_standard]
        for fut in as_completed(futures):
            try:
                local_new, local_sin, _ = fut.result()
                new_tasks.extend(local_new)
                sin_editor.extend(local_sin)
            except Exception as e:
                print(f"   ⚠️  error procesando cliente: {e}")

    # === DETECTAR CARPETAS NUEVAS no conocidas (pendientes de aprobación) ===
    # Si hay carpetas en Mi Unidad que NO son clientes conocidos NI ya decididas → marcar como pendientes
    print("\n🆕 Detectando carpetas nuevas en Drive...")
    try:
        from drive_client import _list_root_items_with_shortcuts as _list_root
        all_root = _list_root()
        # Carpetas ya conocidas (en clients + tasks + ya decididas)
        conn = get_conn()
        known_folder_ids = set()
        for r in conn.execute("SELECT folder_id FROM clients").fetchall():
            if r["folder_id"]: known_folder_ids.add(r["folder_id"])
        for r in conn.execute("SELECT folder_id FROM pending_drive_folders").fetchall():
            known_folder_ids.add(r["folder_id"])
        # Clientes en tasks (puede que no tengan folder_id aún)
        known_names = set()
        for r in conn.execute("SELECT DISTINCT TRIM(cliente) as c FROM tasks WHERE cliente IS NOT NULL").fetchall():
            if r["c"]: known_names.add(r["c"].lower())
        conn.close()
        nuevas = 0
        for f in all_root:
            fid = f.get("id")
            fname = (f.get("name") or "").strip()
            if not fid or not fname: continue
            if fid in known_folder_ids: continue
            if fname.lower() in known_names: continue
            # Filtrar nombres "obvios" de no-cliente (config personal, etc.)
            if fname.startswith("."): continue
            upsert_pending_drive_folder(fid, fname)
            nuevas += 1
        if nuevas:
            print(f"   {nuevas} carpetas nuevas marcadas para aprobación.")
    except Exception as e:
        print(f"   ⚠️ Error detectando carpetas nuevas: {e}")

    # === FASE 2: clientes sin /Material/ — incluye conocidos del sistema + del Sheet ===
    print("🔎 Escaneo generalizado — clientes sin /Material/...")
    conn = get_conn()
    pending_clients = _clients_with_pending(conn)
    baselined = _clients_already_baselined(conn)
    rows = conn.execute("SELECT DISTINCT cliente FROM known_edited_files").fetchall()
    closer_known = {r[0].strip() for r in rows}
    standard_names = {c.cliente.strip() for c in clients_standard}
    conn.close()

    # NUEVO: incluir clientes mencionados en el Sheet con editor asignado (activos).
    # Esto captura clientes 100% nuevos: si Ignacio carga una fila en el Sheet
    # con un cliente nuevo y editor, el sistema empieza a watcharlo.
    sheet_clients = {p.cliente.strip() for p in packs if p.cliente.strip() and p.editor}

    # Procesar todos los clientes activos que NO están cubiertos en fase 1
    extra_clients = (pending_clients | closer_known | sheet_clients) - standard_names
    if extra_clients:
        print(f"   {len(extra_clients)} clientes a chequear con scan generalizado.")
        all_root = _list_root_items_with_shortcuts()

        # Threshold: archivos creados hace MENOS de 24hs son "nuevos" en primera corrida
        from datetime import datetime, timezone, timedelta
        recent_threshold = datetime.now(timezone.utc) - timedelta(hours=24)

        def _parse_created(s):
            if not s:
                return None
            try:
                return datetime.fromisoformat(s.replace("Z", "+00:00").split(".")[0] + "+00:00")
            except Exception:
                return None

        def _process_extra_client(cliente_name):
            local_new_tasks = []
            local_sin_editor = []
            folder = find_folder_by_name(cliente_name, all_root)
            if not folder:
                return local_new_tasks, local_sin_editor
            # Guardar folder en clients para que el dashboard muestre link a Drive
            try:
                upsert_client(folder["id"], cliente_name, None)
            except Exception:
                pass
            crudos = list_crudos_anywhere(folder["id"], folder.get("name"))
            if not crudos:
                return local_new_tasks, local_sin_editor

            first_time = cliente_name not in baselined
            for f in crudos:
                if is_file_known(f["id"]):
                    continue
                # OVERRIDE: owner editor → skip (es editado mal ubicado).
                try:
                    from classifier import identify_editor_by_owner
                    if identify_editor_by_owner(f):
                        continue
                except Exception:
                    pass
                size = int(f["size"]) if f.get("size") else None
                created = _parse_created(f.get("createdTime"))
                is_baseline_file = first_time and (not created or created < recent_threshold)
                # Archivo muy viejo (>3 días) → tratar como baseline aunque no sea first_time
                if not is_baseline_file and _is_file_too_old(f.get("createdTime")):
                    is_baseline_file = True
                claimed = claim_file(
                    file_id=f["id"], cliente=cliente_name, folder_id=folder["id"],
                    name=f["name"], size=size, created_time=f.get("createdTime"),
                    is_baseline=is_baseline_file,
                )
                if not claimed:
                    continue
                if is_baseline_file:
                    continue
                # Detectar duplicado por apodo/nombre similar
                if find_similar_pending_client(cliente_name):
                    continue
                from tracker import resolve_editor_rules as _rer2
                editor = (_rer2(cliente_name, None, f["name"],
                                (f.get("videoMediaMetadata") or {}).get("durationMillis"))
                          or get_editor_for_client(cliente_name, packs))
                # NO duplicar si ya hay manual pending para ESTE editor
                if has_manual_pending_for_client(cliente_name, editor):
                    continue
                if has_pending_for_client_editor(cliente_name, editor):
                    continue
                if is_client_blocked(cliente_name, editor):
                    continue
                if not editor:
                    local_sin_editor.append((cliente_name, f["name"]))
                create_task(cliente_name, editor, f["id"], f["name"])
                local_new_tasks.append({"cliente": cliente_name, "editor": editor, "file": f["name"]})
            return local_new_tasks, local_sin_editor

        # Paralelizar Fase 2 también
        with ThreadPoolExecutor(max_workers=15) as ex:
            futures = [ex.submit(_process_extra_client, name) for name in extra_clients]
            for fut in as_completed(futures):
                try:
                    local_new, local_sin = fut.result()
                    new_tasks.extend(local_new)
                    sin_editor.extend(local_sin)
                except Exception as e:
                    print(f"   ⚠️  error procesando cliente extra: {e}")
    else:
        print("   (ninguno)")

    if not new_tasks:
        print("\n✅ Nada nuevo. Todo en orden.")
    else:
        print(f"\n🆕 {len(new_tasks)} archivos nuevos detectados:\n")
        for t in new_tasks:
            ed = t["editor"] or "❌ SIN EDITOR"
            print(f"   • [{t['cliente']}] {t['file']}  → {ed}")

    if sin_editor:
        print(f"\n⚠️  {len(sin_editor)} archivos sin editor en Sheet:")
        for c, fn in sin_editor:
            print(f"   - {c}: {fn}")

    # === RE-ESTIMAR pending_count para clientes con /Material/ — PARALELIZADO ===
    print("\n📊 Re-estimando contadores de videos pendientes...")

    def _refresh_one(c):
        cliente_real = resolve_alias(c.cliente)
        editor_for_client = get_editor_for_client(cliente_real, packs)
        if not has_pending_for_client_editor(cliente_real, editor_for_client):
            return 0
        estimated = estimate_pending_videos(c.raw_folder_id, c.folder_id)
        if estimated > 0:
            set_pending_count(cliente_real, editor_for_client, estimated)
            return 1
        return 0

    refreshed = 0
    with ThreadPoolExecutor(max_workers=15) as ex:
        for r in ex.map(_refresh_one, clients_standard):
            refreshed += r
    if refreshed:
        print(f"   {refreshed} contadores actualizados.")

    # === NOTIFIER DE CRUDOS — ANTES del closer ===
    # CRÍTICO: hay que mandar mails de crudos nuevos ANTES de que el closer
    # pueda cerrar la task con un editado viejo. Caso real (Alberto, 13/05):
    # Alberto subió crudo → task creada count=1 → Benja había entregado editado
    # del crudo PREVIO → closer decrementa → cierra → notifier corre con
    # task ya 'done' → mail del crudo perdido.
    if notify:
        print("\n📧 Notificador de crudos nuevos (antes del closer)...")
        from notifier import run as notify_run
        notify_run(dry_run=False)

    # === CIERRE: detectar editados nuevos y marcar tareas como hechas ===
    print("\n🔄 Buscando editados nuevos para cerrar tareas...")
    from closer import run_closer
    closer_summary = run_closer(verbose=True)
    if closer_summary["tareas_cerradas"] > 0:
        print(f"\n✅ {closer_summary['tareas_cerradas']} tareas cerradas automáticamente.")
    # SIEMPRE procesar cola persistente de mails de cierre (incluye los que fallaron
    # en scans anteriores). El notifier lee de pending_completion_mails Y de los
    # cierres en memoria de este scan.
    if notify:
        print("📧 Procesando cola de mails de cierre...")
        from notifier import send_completion_mails
        sent = send_completion_mails(closer_summary["cierres"])
        if sent:
            print(f"   {sent} mails de cierre enviados.")
        else:
            print(f"   (sin mails pendientes)")

    pendings = list_pending_tasks()
    print(f"\n📊 Total pendientes en DB: {len(pendings)}")
    print(f"   Stats: {stats()}")
    # Notifier de crudos se llama ANTES del closer (línea ~271) para evitar
    # que el closer pise tasks recién creadas. Sigue corriendo solo si notify=True.


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--notify", action="store_true")
    args = p.parse_args()
    run(notify=args.notify)

"""
Scan — corre periódicamente. Detecta:
  1. Crudos nuevos (en /Material/ o estructuras alternativas) → crea tareas pendientes
  2. Editados nuevos → cierra tareas (delegado al closer)

Uso:
    python3 scan.py            # un scan
    python3 scan.py --notify   # crea tareas Y manda mails
"""

import argparse

from drive_client import (
    discover_client_folders, list_material_files,
    find_folder_by_name, list_crudos_anywhere,
    _list_root_items_with_shortcuts,
)
from tracker import (
    init_db, upsert_client, add_known_file, is_file_known,
    create_task, list_pending_tasks, stats, get_conn,
    has_pending_for_client_editor, increment_pending_count,
)
from sheets_client import read_packs, get_editor_for_client


def _clients_with_pending(conn):
    rows = conn.execute("SELECT DISTINCT cliente FROM tasks WHERE status='pending'").fetchall()
    return {r[0].strip() for r in rows}


def _clients_already_baselined(conn):
    """Clientes que ya tienen entradas en known_files (entonces no hace falta baseline)."""
    rows = conn.execute("SELECT DISTINCT cliente FROM known_files").fetchall()
    return {r[0].strip() for r in rows}


def run(notify: bool = False):
    print("🔍 SCAN — detectando crudos nuevos\n")
    init_db()

    # === FASE 1: clientes con /Material/ (lógica original) ===
    clients_standard = discover_client_folders()
    print(f"   {len(clients_standard)} clientes con estructura /Material/ standard.")

    print("📋 Leyendo Sheet para mapeo cliente→editor...")
    packs = read_packs()
    print(f"   {len(packs)} packs en el Sheet.\n")

    new_tasks = []
    sin_editor = []

    for c in clients_standard:
        upsert_client(c.folder_id, c.cliente, c.raw_folder_id)
        files = list_material_files(c.raw_folder_id)
        for f in files:
            if is_file_known(f["id"]):
                continue
            size = int(f["size"]) if f.get("size") else None
            add_known_file(
                file_id=f["id"], cliente=c.cliente, folder_id=c.raw_folder_id,
                name=f["name"], size=size, created_time=f.get("createdTime"),
                is_baseline=False,
            )
            editor = get_editor_for_client(c.cliente, packs)
            # Si ya hay pending del mismo cliente+editor: incrementar el contador, no crear task nueva
            if has_pending_for_client_editor(c.cliente, editor):
                increment_pending_count(c.cliente, editor)
                continue
            if not editor:
                sin_editor.append((c.cliente, f["name"]))
            create_task(c.cliente, editor, f["id"], f["name"])
            new_tasks.append({"cliente": c.cliente, "editor": editor, "file": f["name"]})

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

        for cliente_name in extra_clients:
            folder = find_folder_by_name(cliente_name, all_root)
            if not folder:
                continue
            crudos = list_crudos_anywhere(folder["id"], folder.get("name"))
            if not crudos:
                continue

            first_time = cliente_name not in baselined
            for f in crudos:
                if is_file_known(f["id"]):
                    continue
                size = int(f["size"]) if f.get("size") else None
                created = _parse_created(f.get("createdTime"))

                # En primera corrida, archivos viejos (>24hs) van a baseline,
                # archivos recientes se tratan como nuevos (probablemente recién subidos)
                is_baseline_file = first_time and (not created or created < recent_threshold)

                add_known_file(
                    file_id=f["id"], cliente=cliente_name, folder_id=folder["id"],
                    name=f["name"], size=size, created_time=f.get("createdTime"),
                    is_baseline=is_baseline_file,
                )
                if is_baseline_file:
                    continue  # archivo viejo en primera corrida → baseline silencioso
                # Archivo nuevo → crear tarea (con deduplicación por cliente+editor)
                editor = get_editor_for_client(cliente_name, packs)
                if has_pending_for_client_editor(cliente_name, editor):
                    increment_pending_count(cliente_name, editor)
                    continue
                if not editor:
                    sin_editor.append((cliente_name, f["name"]))
                create_task(cliente_name, editor, f["id"], f["name"])
                new_tasks.append({"cliente": cliente_name, "editor": editor, "file": f["name"]})
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

    # === CIERRE: detectar editados nuevos y marcar tareas como hechas ===
    print("\n🔄 Buscando editados nuevos para cerrar tareas...")
    from closer import run_closer
    closer_summary = run_closer(verbose=True)
    if closer_summary["tareas_cerradas"] > 0:
        print(f"\n✅ {closer_summary['tareas_cerradas']} tareas cerradas automáticamente.")
        if notify and closer_summary["cierres"]:
            print("📧 Mandando mails de cierre...")
            from notifier import send_completion_mails
            sent = send_completion_mails(closer_summary["cierres"])
            print(f"   {sent} mails de cierre enviados.")

    pendings = list_pending_tasks()
    print(f"\n📊 Total pendientes en DB: {len(pendings)}")
    print(f"   Stats: {stats()}")

    if notify:
        print("\n📧 Disparando notificador...")
        from notifier import run as notify_run
        notify_run(dry_run=False)


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--notify", action="store_true")
    args = p.parse_args()
    run(notify=args.notify)

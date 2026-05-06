"""
Scan — corre periódicamente. Detecta archivos nuevos en /Material/ de cada cliente
y crea tareas pendientes con el editor responsable (consultando el Sheet).

Uso:
    python3 scan.py            # un scan manual
    python3 scan.py --notify   # crea tareas Y manda mails (placeholder por ahora)
"""

import argparse
from typing import Optional

from drive_client import discover_client_folders, list_material_files
from tracker import (
    init_db, upsert_client, add_known_file, is_file_known,
    create_task, list_pending_tasks, stats,
)
from sheets_client import read_packs, get_editor_for_client


def run(notify: bool = False):
    print("🔍 SCAN — buscando archivos nuevos en /Material/\n")
    init_db()

    clients = discover_client_folders()
    print(f"   {len(clients)} clientes con carpeta detectados.")

    print("📋 Leyendo Sheet para mapeo cliente→editor...")
    packs = read_packs()
    print(f"   {len(packs)} packs en el Sheet.\n")

    new_tasks = []
    sin_editor = []

    for c in clients:
        upsert_client(c.folder_id, c.cliente, c.raw_folder_id)
        files = list_material_files(c.raw_folder_id)
        for f in files:
            if is_file_known(f["id"]):
                continue
            # Archivo NUEVO. Registrarlo y crear tarea.
            size = int(f["size"]) if f.get("size") else None
            add_known_file(
                file_id=f["id"],
                cliente=c.cliente,
                folder_id=c.raw_folder_id,
                name=f["name"],
                size=size,
                created_time=f.get("createdTime"),
                is_baseline=False,
            )
            editor = get_editor_for_client(c.cliente, packs)
            if not editor:
                sin_editor.append((c.cliente, f["name"]))
            task_id = create_task(c.cliente, editor, f["id"], f["name"])
            new_tasks.append({
                "id": task_id,
                "cliente": c.cliente,
                "editor": editor,
                "file": f["name"],
            })

    if not new_tasks:
        print("✅ Nada nuevo. Todo en orden.")
    else:
        print(f"🆕 {len(new_tasks)} archivos nuevos detectados:\n")
        for t in new_tasks:
            ed = t["editor"] or "❌ SIN EDITOR"
            print(f"   • [{t['cliente']}] {t['file']}  → {ed}")

    if sin_editor:
        print(f"\n⚠️  {len(sin_editor)} archivos sin editor asignado en Sheet:")
        for c, fn in sin_editor:
            print(f"   - {c}: {fn}")

    # CIERRE DE TAREAS: detectar editados nuevos y marcar tareas como hechas
    print("\n🔄 Buscando editados nuevos para cerrar tareas...")
    from closer import run_closer
    closer_summary = run_closer(verbose=True)
    if closer_summary["tareas_cerradas"] > 0:
        print(f"\n✅ {closer_summary['tareas_cerradas']} tareas cerradas automáticamente.")
    if closer_summary["baseline_runs"] > 0:
        print(f"📸 Baseline de editados tomado para {closer_summary['baseline_runs']} clientes (primera vez).")

    pendings = list_pending_tasks()
    print(f"\n📊 Total tareas pendientes en DB: {len(pendings)}")
    print(f"   Stats: {stats()}")

    if notify:
        print("\n📧 Disparando notificador...")
        from notifier import run as notify_run
        notify_run(dry_run=False)


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--notify", action="store_true", help="manda mails al detectar nuevos")
    args = p.parse_args()
    run(notify=args.notify)

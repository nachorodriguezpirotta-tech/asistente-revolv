"""
Baseline — toma el snapshot inicial del estado actual de Drive.

Recorre todas las carpetas de cliente (las que tienen subcarpeta Material/Raw/Crudos),
guarda en la DB local todos los archivos que hay HOY en cada /Material/.
NO genera tareas. NO manda mails. Es el punto cero del sistema.

Después de correr esto, cualquier archivo nuevo que aparezca en /Material/ es tarea pendiente.

Uso:
    python3 baseline.py
"""

from drive_client import discover_client_folders, list_material_files
from tracker import init_db, upsert_client, add_known_file, set_baseline, stats


def run():
    print("📦 BASELINE — snapshot inicial del estado de Drive\n")
    init_db()

    print("🔍 Descubriendo carpetas de cliente (con /Material/ adentro)...")
    clients = discover_client_folders()
    print(f"   {len(clients)} clientes detectados.\n")

    total_files = 0
    for c in clients:
        upsert_client(c.folder_id, c.cliente, c.raw_folder_id)
        files = list_material_files(c.raw_folder_id)
        for f in files:
            size = int(f["size"]) if f.get("size") else None
            add_known_file(
                file_id=f["id"],
                cliente=c.cliente,
                folder_id=c.raw_folder_id,
                name=f["name"],
                size=size,
                created_time=f.get("createdTime"),
                is_baseline=True,
            )
        set_baseline(c.folder_id)
        total_files += len(files)
        if files:
            print(f"   📁 {c.cliente:<35}  {len(files):>3} archivos en /Material/")

    print(f"\n✅ Baseline completo.")
    print(f"   Stats: {stats()}")
    print(f"\n→ A partir de ahora, cualquier archivo NUEVO en /Material/ se considera tarea.")
    print(f"  Corré:  python3 scan.py")


if __name__ == "__main__":
    run()

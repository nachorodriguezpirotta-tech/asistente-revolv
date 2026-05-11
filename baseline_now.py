"""
Baseline retroactivo: marca TODOS los archivos actuales en Drive como 'conocidos'
SIN crear tasks ni mandar mails.

Después de correr este script, solo los archivos que se suban A PARTIR DE AHORA
van a generar mails. Todo lo viejo queda silencioso.
"""

from drive_client import (
    discover_client_folders, list_material_files,
    find_folder_by_name, list_crudos_anywhere, list_edited_files,
    find_raw_subfolder, _list_root_items_with_shortcuts,
)
from tracker import (
    init_db, upsert_client, add_known_file, is_file_known,
    add_known_edited_file, is_edited_known, get_conn,
)
from aliases import resolve_alias, CLIENT_DELIVERY_FOLDERS, _normalize


def run():
    print("📸 BASELINE RETROACTIVO — marcando archivos actuales como conocidos\n")
    init_db()

    clients_standard = discover_client_folders()
    print(f"   {len(clients_standard)} clientes con /Material/.\n")

    crudos_baseline = 0
    editados_baseline = 0

    # FASE 1: clientes con /Material/ - crudos
    for c in clients_standard:
        cliente_real = resolve_alias(c.cliente)
        upsert_client(c.folder_id, cliente_real, c.raw_folder_id)
        files = list_material_files(c.raw_folder_id)
        for f in files:
            if is_file_known(f["id"]):
                continue
            size = int(f["size"]) if f.get("size") else None
            add_known_file(
                file_id=f["id"], cliente=cliente_real, folder_id=c.raw_folder_id,
                name=f["name"], size=size, created_time=f.get("createdTime"),
                is_baseline=True,  # ← BASELINE, no crea task ni mail
            )
            crudos_baseline += 1

        # Editados (todo lo que no está en /Material/)
        editados = list_edited_files(c.folder_id, c.raw_folder_id, client_folder_name=cliente_real)
        for f in editados:
            if is_edited_known(f["id"]):
                continue
            size = int(f["size"]) if f.get("size") else None
            add_known_edited_file(
                file_id=f["id"], cliente=cliente_real, folder_id="(varias)",
                name=f["name"], size=size, created_time=f.get("createdTime"),
                is_baseline=True,
            )
            editados_baseline += 1

    # FASE 2: carpetas de entrega extras (CLIENT_DELIVERY_FOLDERS)
    from drive_client import _list_files
    for cliente, folder_id in CLIENT_DELIVERY_FOLDERS.items():
        try:
            files = _list_files(folder_id, only_videos=True)
            for f in files:
                if is_edited_known(f["id"]):
                    continue
                size = int(f["size"]) if f.get("size") else None
                add_known_edited_file(
                    file_id=f["id"], cliente=cliente, folder_id=folder_id,
                    name=f["name"], size=size, created_time=f.get("createdTime"),
                    is_baseline=True,
                )
                editados_baseline += 1
        except Exception as e:
            print(f"   ⚠️  delivery folder {cliente}: {e}")

    print(f"\n✅ Baseline completo.")
    print(f"   Crudos marcados:    {crudos_baseline}")
    print(f"   Editados marcados:  {editados_baseline}")
    print(f"\n   A partir de ahora SOLO los archivos NUEVOS van a generar mails.")


if __name__ == "__main__":
    run()

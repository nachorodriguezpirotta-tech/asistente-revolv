"""
Closer — detecta editados nuevos en la carpeta del cliente (fuera de /Material/)
y cierra automáticamente la tarea pendiente más vieja del cliente por cada uno.

Lógica:
  1. Para cada cliente con carpeta en Drive:
     a. Listar editados actuales (todo MENOS /Material/, recursivo)
     b. Si es la PRIMERA vez que vemos editados de este cliente → marcar todos como
        baseline (NO cerrar tareas — es estado pre-existente)
     c. Si ya hicimos baseline → los nuevos editados cierran tareas pendientes 1:1
        (la más vieja primero).

Esto se llama desde scan.py después del check de crudos nuevos.
"""

from drive_client import discover_client_folders, list_edited_files
from tracker import (
    upsert_client,
    is_edited_known, add_known_edited_file,
    edited_baseline_done,
    close_oldest_pending, count_pending_for_client,
)


def run_closer(verbose: bool = True) -> dict:
    """
    Corre el closer sobre todos los clientes. Devuelve resumen:
        {'clientes': N, 'nuevos_editados': N, 'tareas_cerradas': N, 'baseline_runs': N}
    """
    clients = discover_client_folders()
    summary = {
        "clientes": len(clients),
        "nuevos_editados": 0,
        "tareas_cerradas": 0,
        "baseline_runs": 0,
        "cierres": [],  # detalle: [(cliente, file_name, task_id_cerrada)]
    }

    for c in clients:
        upsert_client(c.folder_id, c.cliente, c.raw_folder_id)
        editados = list_edited_files(c.folder_id, c.raw_folder_id)
        if not editados:
            continue

        first_time = not edited_baseline_done(c.cliente)

        if first_time:
            # Auto-baseline para este cliente: marcar TODO lo existente como conocido,
            # SIN cerrar tareas pendientes (porque puede ser histórico anterior al sistema)
            for f in editados:
                size = int(f["size"]) if f.get("size") else None
                add_known_edited_file(
                    file_id=f["id"], cliente=c.cliente,
                    folder_id="(varias)",  # los editados pueden estar en distintas subcarpetas
                    name=f["name"], size=size,
                    created_time=f.get("createdTime"),
                    is_baseline=True,
                )
            summary["baseline_runs"] += 1
            if verbose:
                print(f"  📸 [baseline] {c.cliente}: {len(editados)} editados marcados como conocidos")
            continue

        # Ya hicimos baseline. Detectar nuevos editados.
        for f in editados:
            if is_edited_known(f["id"]):
                continue
            # Editado nuevo → cerrar tarea pendiente más vieja (si existe)
            closed_task_id = None
            if count_pending_for_client(c.cliente) > 0:
                closed_task_id = close_oldest_pending(c.cliente, completed_by_file_id=f["id"])
                if closed_task_id:
                    summary["tareas_cerradas"] += 1
                    summary["cierres"].append((c.cliente, f["name"], closed_task_id))
            size = int(f["size"]) if f.get("size") else None
            add_known_edited_file(
                file_id=f["id"], cliente=c.cliente,
                folder_id="(varias)",
                name=f["name"], size=size,
                created_time=f.get("createdTime"),
                is_baseline=False,
                closed_task_id=closed_task_id,
            )
            summary["nuevos_editados"] += 1
            if verbose:
                action = f"cerró task #{closed_task_id}" if closed_task_id else "sin pending para cerrar"
                print(f"  ✅ [{c.cliente}] editado nuevo: {f['name']} → {action}")

    return summary


if __name__ == "__main__":
    print("🔄 CLOSER — detectando editados nuevos y cerrando tareas\n")
    summary = run_closer()
    print("\n📊 Resumen:")
    print(f"   Clientes scaneados:   {summary['clientes']}")
    print(f"   Baseline (1ra vez):   {summary['baseline_runs']} clientes")
    print(f"   Editados nuevos:      {summary['nuevos_editados']}")
    print(f"   Tareas cerradas:      {summary['tareas_cerradas']}")

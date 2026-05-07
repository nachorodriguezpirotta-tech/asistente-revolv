"""
Closer — detecta editados nuevos en la carpeta del cliente y cierra tareas pendientes.

Lógica nueva (más robusta):
  - Itera sobre TODOS los clientes con tareas pendientes (no solo los que tienen /Material/).
  - Para cada uno: busca su carpeta en Drive por nombre.
  - Lista editados (todo lo que NO está en una subcarpeta de crudos).
  - Si NO hay baseline previo: marca como conocidos los archivos cuyo createdTime
    sea ANTERIOR al detected_at de la tarea pendiente más vieja. Los archivos con
    createdTime POSTERIOR son "nuevos" → cierran tareas (oldest first).
  - Si hay baseline previo: lógica normal (archivo no conocido = nuevo → cierra).
"""

from datetime import datetime
from typing import Optional

from drive_client import (
    find_folder_by_name, find_raw_subfolder,
    list_root_folders, list_edited_files,
)
from tracker import (
    get_conn,
    is_edited_known, add_known_edited_file,
    edited_baseline_done,
    close_oldest_pending, count_pending_for_client,
)


def _parse_iso(s: Optional[str]) -> Optional[datetime]:
    if not s:
        return None
    try:
        # Drive devuelve formato "2026-05-07T10:00:00.000Z"
        return datetime.fromisoformat(s.replace("Z", "+00:00").split(".")[0])
    except Exception:
        try:
            return datetime.fromisoformat(s)
        except Exception:
            return None


def _get_clients_with_pending() -> list[dict]:
    """Devuelve [{cliente, oldest_pending_at}] para todos los clientes con tareas pending."""
    conn = get_conn()
    rows = conn.execute("""
        SELECT cliente, MIN(detected_at) as oldest
        FROM tasks
        WHERE status = 'pending'
        GROUP BY cliente
    """).fetchall()
    conn.close()
    return [{"cliente": r["cliente"], "oldest_pending_at": r["oldest"]} for r in rows]


def run_closer(verbose: bool = True) -> dict:
    """
    Ejecuta el closer. Itera sobre todos los clientes con pending tasks.
    Devuelve resumen del trabajo hecho.
    """
    summary = {
        "clientes_chequeados": 0,
        "carpetas_no_encontradas": [],
        "nuevos_editados": 0,
        "tareas_cerradas": 0,
        "baseline_runs": 0,
        "cierres": [],
    }

    pendings = _get_clients_with_pending()
    if not pendings:
        if verbose:
            print("  (sin clientes con tareas pendientes)")
        return summary

    all_folders = list_root_folders()  # cache una sola vez

    for p in pendings:
        cliente = p["cliente"]
        oldest_pending = _parse_iso(p["oldest_pending_at"])
        summary["clientes_chequeados"] += 1

        folder = find_folder_by_name(cliente, all_folders)
        if not folder:
            summary["carpetas_no_encontradas"].append(cliente)
            if verbose:
                print(f"  ⚠️  [{cliente}] carpeta no encontrada en Drive")
            continue

        # Detectar carpeta de crudos (Material/Raw/Crudos) si existe, para excluirla
        raw = find_raw_subfolder(folder["id"])
        raw_id = raw["id"] if raw else None

        editados = list_edited_files(folder["id"], raw_id)
        if not editados:
            continue

        first_time = not edited_baseline_done(cliente)

        if first_time:
            # Para clientes sin baseline previo, separar archivos viejos vs nuevos
            # según el detected_at de la tarea pendiente más vieja.
            # Archivos creados ANTES de la tarea → baseline (no cierran).
            # Archivos creados DESPUÉS → "nuevos" → cierran tareas pending oldest first.
            baseline_files = []
            new_files = []
            for f in editados:
                f_created = _parse_iso(f.get("createdTime")) or _parse_iso(f.get("modifiedTime"))
                if oldest_pending and f_created and f_created.replace(tzinfo=None) > oldest_pending:
                    new_files.append(f)
                else:
                    baseline_files.append(f)

            # Marcar viejos como baseline
            for f in baseline_files:
                size = int(f["size"]) if f.get("size") else None
                add_known_edited_file(
                    file_id=f["id"], cliente=cliente, folder_id="(varias)",
                    name=f["name"], size=size, created_time=f.get("createdTime"),
                    is_baseline=True,
                )
            summary["baseline_runs"] += 1
            if verbose:
                print(f"  📸 [baseline] {cliente}: {len(baseline_files)} viejos + {len(new_files)} nuevos detectados")

            # Procesar los nuevos (más viejos primero) y cerrar tareas
            new_files.sort(key=lambda f: _parse_iso(f.get("createdTime")) or datetime.min)
            for f in new_files:
                closed_id = None
                if count_pending_for_client(cliente) > 0:
                    closed_id = close_oldest_pending(cliente, completed_by_file_id=f["id"])
                    if closed_id:
                        summary["tareas_cerradas"] += 1
                        summary["cierres"].append((cliente, f["name"], closed_id))
                size = int(f["size"]) if f.get("size") else None
                add_known_edited_file(
                    file_id=f["id"], cliente=cliente, folder_id="(varias)",
                    name=f["name"], size=size, created_time=f.get("createdTime"),
                    is_baseline=False, closed_task_id=closed_id,
                )
                summary["nuevos_editados"] += 1
                if verbose:
                    action = f"cerró task #{closed_id}" if closed_id else "sin pending"
                    print(f"  ✅ [{cliente}] editado nuevo: {f['name']} → {action}")
            continue

        # Cliente con baseline previo: lógica normal
        for f in editados:
            if is_edited_known(f["id"]):
                continue
            closed_id = None
            if count_pending_for_client(cliente) > 0:
                closed_id = close_oldest_pending(cliente, completed_by_file_id=f["id"])
                if closed_id:
                    summary["tareas_cerradas"] += 1
                    summary["cierres"].append((cliente, f["name"], closed_id))
            size = int(f["size"]) if f.get("size") else None
            add_known_edited_file(
                file_id=f["id"], cliente=cliente, folder_id="(varias)",
                name=f["name"], size=size, created_time=f.get("createdTime"),
                is_baseline=False, closed_task_id=closed_id,
            )
            summary["nuevos_editados"] += 1
            if verbose:
                action = f"cerró task #{closed_id}" if closed_id else "sin pending"
                print(f"  ✅ [{cliente}] editado nuevo: {f['name']} → {action}")

    return summary


if __name__ == "__main__":
    print("🔄 CLOSER — detectando editados nuevos y cerrando tareas\n")
    s = run_closer()
    print("\n📊 Resumen:")
    print(f"   Clientes chequeados:  {s['clientes_chequeados']}")
    print(f"   Sin carpeta en Drive: {len(s['carpetas_no_encontradas'])}")
    if s["carpetas_no_encontradas"]:
        for c in s["carpetas_no_encontradas"]:
            print(f"     - {c}")
    print(f"   Baseline runs:        {s['baseline_runs']} clientes")
    print(f"   Editados nuevos:      {s['nuevos_editados']}")
    print(f"   Tareas cerradas:      {s['tareas_cerradas']}")
    if s["cierres"]:
        print()
        for c, fn, tid in s["cierres"]:
            print(f"   ✅ #{tid} {c} ← {fn}")

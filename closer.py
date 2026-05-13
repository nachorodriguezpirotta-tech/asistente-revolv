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
    _list_root_items_with_shortcuts,
)
from tracker import (
    get_conn,
    is_edited_known, add_known_edited_file, claim_edited_file,
    edited_baseline_done,
    close_oldest_pending, count_pending_for_client,
    close_all_pending_for_client, decrement_pending_count,
    enqueue_completion_mail,
)
from aliases import resolve_alias, reverse_alias, CLIENT_DELIVERY_FOLDERS, _normalize as _alias_norm


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
    """Devuelve [{cliente, oldest_pending_at, editor}] para todos los clientes con tareas pending."""
    conn = get_conn()
    rows = conn.execute("""
        SELECT TRIM(cliente) as cliente, MIN(detected_at) as oldest,
               (SELECT editor FROM tasks t2 WHERE TRIM(t2.cliente)=TRIM(tasks.cliente) AND t2.status='pending' LIMIT 1) as editor
        FROM tasks
        WHERE status = 'pending'
        GROUP BY TRIM(cliente)
    """).fetchall()
    conn.close()
    return [{"cliente": r["cliente"], "oldest_pending_at": r["oldest"], "editor": r["editor"]} for r in rows]


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

    all_folders = _list_root_items_with_shortcuts()  # incluye shortcuts

    for p in pendings:
        cliente = p["cliente"]
        oldest_pending = _parse_iso(p["oldest_pending_at"])
        client_editor = p.get("editor") or "—"
        summary["clientes_chequeados"] += 1

        folder = find_folder_by_name(cliente, all_folders)
        if not folder:
            # Probar con aliases inversos: capaz la carpeta de Drive se llama distinto
            for alias_drive_name in reverse_alias(cliente):
                folder = find_folder_by_name(alias_drive_name, all_folders)
                if folder:
                    break
        if not folder:
            summary["carpetas_no_encontradas"].append(cliente)
            if verbose:
                print(f"  ⚠️  [{cliente}] carpeta no encontrada en Drive")
            continue

        # Detectar carpeta de crudos (Material/Raw/Crudos) si existe, para excluirla
        raw = find_raw_subfolder(folder["id"])
        raw_id = raw["id"] if raw else None

        editados = list_edited_files(folder["id"], raw_id, client_folder_name=folder["name"])

        # Si el cliente tiene una carpeta de entregas EXTRA configurada, sumar esos archivos
        delivery_folder_id = None
        for k, v in CLIENT_DELIVERY_FOLDERS.items():
            if _alias_norm(k) == _alias_norm(cliente):
                delivery_folder_id = v
                break
        if delivery_folder_id:
            from drive_client import _list_files
            extra = _list_files(delivery_folder_id, only_videos=True)
            editados.extend(extra)

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

            # Procesar los nuevos (más viejos primero). Cada editado nuevo descuenta 1
            # del contador. Cuando llega a 0, se cierra el cliente.
            new_files.sort(key=lambda f: _parse_iso(f.get("createdTime")) or datetime.min)
            for f in new_files:
                result = decrement_pending_count(cliente, completed_by_file_id=f["id"])
                if result["task_id"] is not None:
                    cierre_data = {
                        "cliente": cliente,
                        "editor": result["editor"] or client_editor,
                        "file_name": f["name"],
                        "file_id": f["id"],
                        # Carpeta donde está el editado (Pack X / Tanda Y / Mayo/Editados / etc.)
                        "edited_folder_id": f.get("_parent_id"),
                        "client_folder_id": folder["id"],
                        "new_count": result["new_count"],
                        "closed": result["closed"],
                    }
                    summary["cierres"].append(cierre_data)
                    # PERSISTIR el cierre para mail con retry. Si crashea o falla el mail,
                    # próximo scan lo retoma.
                    enqueue_completion_mail(
                        task_id=result["task_id"],
                        cliente=cliente,
                        editor=cierre_data["editor"],
                        file_id=f["id"],
                        file_name=f["name"],
                        edited_folder_id=f.get("_parent_id"),
                        client_folder_id=folder["id"],
                        new_count=result["new_count"],
                        closed=result["closed"],
                    )
                    if result["closed"]:
                        summary["tareas_cerradas"] += 1
                size = int(f["size"]) if f.get("size") else None
                add_known_edited_file(
                    file_id=f["id"], cliente=cliente, folder_id="(varias)",
                    name=f["name"], size=size, created_time=f.get("createdTime"),
                    is_baseline=False, closed_task_id=None,
                )
                summary["nuevos_editados"] += 1
                if verbose:
                    if result["task_id"] is None:
                        action = "sin pending"
                    elif result["closed"]:
                        action = f"cerró cliente (count llegó a 0)"
                    else:
                        action = f"descontó 1 → {result['new_count']} restantes"
                    print(f"  ✅ [{cliente}] editado nuevo: {f['name']} → {action}")
            continue

        # Cliente con baseline previo: cada editado nuevo descuenta 1 del contador.
        for f in editados:
            if is_edited_known(f["id"]):
                continue
            # CLAIM ATÓMICO: intentamos marcar el archivo como conocido ANTES de cerrar
            # task / mandar mail. Si otro proceso ya lo claimó (return False), saltamos.
            # Evita mails duplicados cuando dos scans corren concurrentes.
            size = int(f["size"]) if f.get("size") else None
            claimed = claim_edited_file(
                file_id=f["id"], cliente=cliente, folder_id="(varias)",
                name=f["name"], size=size, created_time=f.get("createdTime"),
                is_baseline=False, closed_task_id=None,
            )
            if not claimed:
                continue  # otro workflow ya lo procesó
            result = decrement_pending_count(cliente, completed_by_file_id=f["id"])
            if result["task_id"] is not None:
                cierre_data = {
                    "cliente": cliente,
                    "editor": result["editor"] or client_editor,
                    "file_name": f["name"],
                    "file_id": f["id"],
                    "edited_folder_id": f.get("_parent_id"),
                    "client_folder_id": folder["id"],
                    "new_count": result["new_count"],
                    "closed": result["closed"],
                }
                summary["cierres"].append(cierre_data)
                # PERSISTIR cierre en cola para retry persistente del mail
                enqueue_completion_mail(
                    task_id=result["task_id"],
                    cliente=cliente,
                    editor=cierre_data["editor"],
                    file_id=f["id"],
                    file_name=f["name"],
                    edited_folder_id=f.get("_parent_id"),
                    client_folder_id=folder["id"],
                    new_count=result["new_count"],
                    closed=result["closed"],
                )
                if result["closed"]:
                    summary["tareas_cerradas"] += 1
            summary["nuevos_editados"] += 1
            if verbose:
                if result["task_id"] is None:
                    action = "sin pending"
                elif result["closed"]:
                    action = f"cerró cliente (count llegó a 0)"
                else:
                    action = f"descontó 1 → {result['new_count']} restantes"
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

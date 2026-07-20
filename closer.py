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
    enqueue_completion_mail, upsert_client, is_correction_for_client,
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
    import tasks_store
    rows = tasks_store.query("""
        SELECT TRIM(cliente) as cliente, MIN(detected_at) as oldest,
               (SELECT editor FROM tasks t2 WHERE TRIM(t2.cliente)=TRIM(tasks.cliente) AND t2.status='pending' LIMIT 1) as editor
        FROM tasks
        WHERE status = 'pending'
        GROUP BY TRIM(cliente)
    """)
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

        # Guardar el folder en la tabla clients para que el dashboard tenga el link directo a Drive
        # (incluye clientes sin /Material/ y los cargados manualmente)
        try:
            upsert_client(folder["id"], cliente, raw_id)
        except Exception:
            pass

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

        # Detectar si la pending fue cargada MANUALMENTE (file_id LIKE 'manual:%').
        # En ese caso, los editados existentes son histórico → baseline silencioso,
        # NO mandar mails de cierre por archivos viejos. Solo se va a notificar de
        # entregas POSTERIORES a la corrida actual.
        is_manual_pending = False
        try:
            import tasks_store
            _r = tasks_store.query(
                "SELECT file_id FROM tasks WHERE TRIM(cliente)=TRIM(?) AND status='pending' ORDER BY detected_at ASC LIMIT 1",
                (cliente,))
            row = _r[0] if _r else None
            if row and row["file_id"] and str(row["file_id"]).startswith("manual:"):
                is_manual_pending = True
        except Exception:
            pass

        if first_time:
            # Para clientes sin baseline previo, separar archivos viejos vs nuevos
            # según el detected_at de la tarea pendiente más vieja.
            # Archivos creados ANTES de la tarea → baseline (no cierran).
            # Archivos creados DESPUÉS → "nuevos" → cierran tareas pending oldest first.
            #
            # SI la task pending es MANUAL: tratar TODOS los archivos existentes
            # como baseline. El user sabe lo que está cargando, no le sirve recibir
            # mails por archivos viejos que ya estaban entregados antes.
            baseline_files = []
            new_files = []
            if is_manual_pending:
                # Manual: todo el historial es baseline, no mandar mails retroactivos
                baseline_files = list(editados)
                if verbose:
                    print(f"  📸 [{cliente}] task MANUAL: marcando {len(editados)} editados existentes como baseline (sin mails retroactivos)")
            else:
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
            from classifier import identify_editor_by_owner
            for f in new_files:
                result = decrement_pending_count(cliente, completed_by_file_id=f["id"])
                if result["task_id"] is not None:
                    real_editor = identify_editor_by_owner(f) or result["editor"] or client_editor
                    cierre_data = {
                        "cliente": cliente,
                        "editor": real_editor,
                        "file_name": f["name"],
                        "file_id": f["id"],
                        "edited_folder_id": f.get("_parent_id"),
                        "client_folder_id": folder["id"],
                        "new_count": result["new_count"],
                        "closed": result["closed"],
                    }
                    summary["cierres"].append(cierre_data)
                    enqueue_completion_mail(
                        task_id=result["task_id"],
                        cliente=cliente,
                        editor=real_editor,
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
        from classifier import identify_editor_by_owner
        for f in editados:
            if is_edited_known(f["id"]):
                continue
            size = int(f["size"]) if f.get("size") else None
            # Editado con createdTime VIEJO (>3 días) que recién aparece = histórico
            # re-indexado (cliente reorganizó carpetas, re-share, etc.) — NO es una
            # entrega. Claim como baseline silencioso: sin decrement, sin mail.
            # Caso Iván Juárez 16/jul: Video 17/21/23 (dic-mar) re-aparecieron y
            # cerraron 4 tarjetas de Benja/Rami como entregas falsas.
            from scan import _is_file_too_old
            if _is_file_too_old(f.get("createdTime")):
                claim_edited_file(
                    file_id=f["id"], cliente=cliente, folder_id="(varias)",
                    name=f["name"], size=size, created_time=f.get("createdTime"),
                    is_baseline=True, closed_task_id=None,
                )
                continue
            # CLAIM ATÓMICO: intentamos marcar el archivo como conocido ANTES de cerrar
            # task / mandar mail. Si otro proceso ya lo claimó (return False), saltamos.
            claimed = claim_edited_file(
                file_id=f["id"], cliente=cliente, folder_id="(varias)",
                name=f["name"], size=size, created_time=f.get("createdTime"),
                is_baseline=False, closed_task_id=None,
            )
            if not claimed:
                continue  # otro workflow ya lo procesó
            from classifier import looks_like_client_upload as _cli_up
            if _cli_up(f):
                print(f"  🚫 material del cliente (owner no-editor), no es entrega: {cliente} / {f['name'][:40]}")
                try:
                    _cc = get_conn()
                    _cc.execute("UPDATE known_edited_files SET is_baseline=1 WHERE file_id=?", (f["id"],))
                    _cc.commit(); _cc.close()
                except Exception:
                    pass
                continue

            # ¿Es una CORRECCIÓN de un video previo? Detecta si ya hubo un editado
            # con el mismo número (Video 1 / Reel 5 / etc) del mismo cliente.
            # Si es corrección: NO decrementar pending_count, mandar mail de corrección.
            real_editor = identify_editor_by_owner(f) or client_editor
            if is_correction_for_client(cliente, f["name"], current_file_id=f["id"]):
                if verbose:
                    print(f"  🔧 [{cliente}] corrección detectada: {f['name']} (NO decuenta)")
                enqueue_completion_mail(
                    task_id=None,  # no hay task que decrementar
                    cliente=cliente,
                    editor=real_editor,
                    file_id=f["id"],
                    file_name=f["name"],
                    edited_folder_id=f.get("_parent_id"),
                    client_folder_id=folder["id"],
                    new_count=0,
                    closed=False,
                    is_correction=True,
                )
                summary["nuevos_editados"] += 1
                continue

            result = decrement_pending_count(cliente, completed_by_file_id=f["id"])
            if result["task_id"] is not None:
                # Si identify_editor_by_owner no encontró, usamos el de la task
                real_editor = identify_editor_by_owner(f) or result["editor"] or client_editor
                cierre_data = {
                    "cliente": cliente,
                    "editor": real_editor,
                    "file_name": f["name"],
                    "file_id": f["id"],
                    "edited_folder_id": f.get("_parent_id"),
                    "client_folder_id": folder["id"],
                    "new_count": result["new_count"],
                    "closed": result["closed"],
                }
                summary["cierres"].append(cierre_data)
                enqueue_completion_mail(
                    task_id=result["task_id"],
                    cliente=cliente,
                    editor=real_editor,
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
        for c in s["cierres"]:
            # cierres son dicts con 'cliente', 'file_name', etc — no tuples
            cli = c.get("cliente", "?")
            fn = c.get("file_name", "?")
            new_count = c.get("new_count", 0)
            closed = c.get("closed", False)
            marker = "✅" if closed else "📹"
            print(f"   {marker} {cli} ← {fn} (resta {new_count}, closed={closed})")

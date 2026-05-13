"""
Scan incremental — usa Drive Changes API para procesar SOLO los archivos
que cambiaron desde el último scan. Tarda segundos en vez de minutos.

Uso:
    python3 scan_incremental.py            # un scan incremental
    python3 scan_incremental.py --notify   # crea tareas Y manda mails

Filosofía:
  - La primera vez: guarda el startPageToken de Drive y termina (sin procesar nada).
    El próximo scan será desde ese punto en adelante.
  - Cada scan: pide changes desde el último token, procesa SOLO los archivos
    relevantes (videos en carpetas de cliente conocidas), aplica el mismo
    flujo que scan.py (clasificar crudo/editado, crear task o cerrarla, etc.).
  - Si el token está expirado (Drive borra tokens viejos), se hace fallback
    a un scan completo automáticamente.

Diseñado para correr cada 1-2 min sin gastar muchos recursos.
"""

import os
import sys
import argparse

# KILL SWITCH (igual que scan.py)
_HERE = os.path.dirname(os.path.abspath(__file__))
if os.path.exists(os.path.join(_HERE, ".scan_disabled")):
    print("🛑 KILL SWITCH activo. Scan incremental deshabilitado.")
    sys.exit(0)

from drive_client import (
    get_start_page_token, list_changes_since,
    _is_video, find_raw_subfolder,
)
from tracker import (
    init_db, get_conn, meta_get, meta_set,
    is_file_known, claim_file, create_task, has_pending_for_client_editor,
    has_manual_pending_for_client,
    is_client_blocked, set_pending_count,
    is_edited_known, claim_edited_file, decrement_pending_count,
)
from sheets_client import read_packs, get_editor_for_client
from aliases import resolve_alias, reverse_alias
from classifier import classify

META_KEY_TOKEN = "drive_changes_page_token"


def _build_folder_index() -> tuple[dict, dict]:
    """Devuelve (folder_id_a_cliente_real, raw_folder_id_a_cliente_real).
    Sirve para identificar rápido si un archivo cambiado está en una carpeta
    relevante (sin tener que descubrir TODO Drive de cero)."""
    conn = get_conn()
    rows = conn.execute("SELECT folder_id, cliente, raw_folder_id FROM clients").fetchall()
    conn.close()
    folder_to_client = {}
    raw_to_client = {}
    for r in rows:
        folder_to_client[r["folder_id"]] = r["cliente"]
        if r["raw_folder_id"]:
            raw_to_client[r["raw_folder_id"]] = r["cliente"]
    return folder_to_client, raw_to_client


def _resolve_client_for_file(f: dict, folder_to_client: dict, raw_to_client: dict) -> tuple[str, bool]:
    """Devuelve (cliente_real, is_crudo) o (None, None) si no es relevante.
    is_crudo=True → archivo está en /Material/ del cliente (es crudo)
    is_crudo=False → archivo está en la raíz del cliente o subcarpeta (puede ser editado)
    is_crudo=None → no es de un cliente conocido
    """
    parents = f.get("parents") or []
    for p in parents:
        if p in raw_to_client:
            return raw_to_client[p], True
        if p in folder_to_client:
            return folder_to_client[p], False
    return None, None


def run(notify: bool = False):
    print("⚡ SCAN INCREMENTAL — solo cambios desde el último run")
    init_db()

    # 1) Recuperar el token guardado, o tomar uno nuevo (primera vez)
    token = meta_get(META_KEY_TOKEN)
    if not token:
        new_token = get_start_page_token()
        meta_set(META_KEY_TOKEN, new_token)
        print(f"   📌 Primera vez: token inicial guardado ({new_token[:10]}...).")
        print("   No hay cambios para procesar. Próximo scan empezará desde acá.")
        return

    print(f"   📌 Token actual: {token[:10]}...")

    # 2) Pedir todos los cambios desde el token
    try:
        changes, new_token = list_changes_since(token)
    except Exception as e:
        # Token expirado o inválido → reinicializar
        print(f"   ⚠️  Error al listar cambios ({e}). Reinicializando token.")
        new_token = get_start_page_token()
        meta_set(META_KEY_TOKEN, new_token)
        return

    print(f"   📥 {len(changes)} cambios detectados.")
    meta_set(META_KEY_TOKEN, new_token)  # avanzar el token YA (idempotente)

    if not changes:
        print("   ✅ Nada nuevo.")
        return

    # 3) Cargar índice de carpetas para clasificar rápido
    folder_to_client, raw_to_client = _build_folder_index()

    # 4) Cargar Sheet (1 sola lectura para todos los cambios)
    packs = read_packs()

    new_tasks = []
    cierres = []

    for ch in changes:
        if ch.get("removed"):
            continue  # no nos interesa por ahora
        f = ch.get("file") or {}
        if not f or f.get("trashed"):
            continue
        # Solo videos
        if not _is_video(f.get("name", ""), f.get("mimeType", "")):
            continue

        cliente_real, is_crudo = _resolve_client_for_file(f, folder_to_client, raw_to_client)
        if cliente_real is None:
            # Archivo no está en una carpeta de cliente conocida — ignorar.
            # (El scan completo se encarga de descubrir clientes nuevos cada hora.)
            continue

        # CRUDO en /Material/: crear task + mandar mail
        if is_crudo:
            if is_file_known(f["id"]):
                continue
            size = int(f["size"]) if f.get("size") else None
            # Buscar folder_id (parent que matchea raw_to_client)
            raw_folder_id = next((p for p in (f.get("parents") or []) if p in raw_to_client), None)
            claimed = claim_file(
                file_id=f["id"], cliente=cliente_real,
                folder_id=raw_folder_id or "(?)",
                name=f["name"], size=size, created_time=f.get("createdTime"),
                is_baseline=False,
            )
            if not claimed:
                continue
            # Si admin ya asignó manualmente este cliente a un editor, no duplicar
            if has_manual_pending_for_client(cliente_real):
                continue
            editor = get_editor_for_client(cliente_real, packs)
            if has_pending_for_client_editor(cliente_real, editor):
                continue
            if is_client_blocked(cliente_real, editor):
                continue
            create_task(cliente_real, editor, f["id"], f["name"])
            new_tasks.append({"cliente": cliente_real, "editor": editor, "file": f["name"]})
            continue

        # NO es crudo: clasificar con owner-based (puede ser editado)
        sig = classify(f, parent_name=None)  # parent_name no relevante aquí
        if sig is not True:
            continue  # ambiguo o crudo → ignorar (scan completo se encarga)

        # Es editado → cerrar task pendiente (si hay)
        if is_edited_known(f["id"]):
            continue
        size = int(f["size"]) if f.get("size") else None
        claimed = claim_edited_file(
            file_id=f["id"], cliente=cliente_real, folder_id="(incremental)",
            name=f["name"], size=size, created_time=f.get("createdTime"),
            is_baseline=False, closed_task_id=None,
        )
        if not claimed:
            continue
        result = decrement_pending_count(cliente_real, completed_by_file_id=f["id"])
        if result["task_id"] is not None:
            cierres.append({
                "cliente": cliente_real,
                "editor": result["editor"] or "—",
                "file_name": f["name"],
                "file_id": f["id"],
                "edited_folder_id": (f.get("parents") or [None])[0],
                "new_count": result["new_count"],
                "closed": result["closed"],
            })

    # 5) Reportar y notificar
    if new_tasks:
        print(f"\n🆕 {len(new_tasks)} crudos nuevos:")
        for t in new_tasks:
            print(f"   • [{t['cliente']}] {t['file']} → {t['editor'] or '❌ sin editor'}")
    if cierres:
        print(f"\n✅ {len(cierres)} tareas cerradas por editados:")
        for c in cierres:
            print(f"   • [{c['cliente']}] {c['file_name']} → quedan {c['new_count']}")

    if not new_tasks and not cierres:
        print("   (sin novedades relevantes)")

    if notify:
        if new_tasks or cierres:
            print("\n📧 Disparando notificador...")
        from notifier import run as notify_run, send_completion_mails
        if new_tasks:
            notify_run(dry_run=False)
        if cierres:
            send_completion_mails(cierres)


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--notify", action="store_true")
    args = p.parse_args()
    run(notify=args.notify)

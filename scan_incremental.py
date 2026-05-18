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
from typing import Optional

# KILL SWITCH (igual que scan.py)
_HERE = os.path.dirname(os.path.abspath(__file__))
if os.path.exists(os.path.join(_HERE, ".scan_disabled")):
    print("🛑 KILL SWITCH activo. Scan incremental deshabilitado.")
    sys.exit(0)

from drive_client import (
    get_start_page_token, list_changes_since,
    _is_video, find_raw_subfolder,
)
from scan import _is_file_too_old
from tracker import (
    init_db, get_conn, meta_get, meta_set,
    is_file_known, claim_file, create_task, has_pending_for_client_editor,
    has_manual_pending_for_client, find_similar_pending_client,
    is_client_blocked, set_pending_count,
    is_edited_known, claim_edited_file, decrement_pending_count,
    enqueue_completion_mail,
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


def _resolve_client_for_file(f: dict, folder_to_client: dict, raw_to_client: dict,
                              ancestry_cache: Optional[dict] = None) -> tuple[Optional[str], Optional[bool]]:
    """Devuelve (cliente_real, is_crudo) o (None, None) si no es relevante.
    is_crudo=True → archivo está en /Material/ del cliente (es crudo)
    is_crudo=False → archivo está en una subcarpeta del cliente (raíz o profunda)
    is_crudo=None → no es de un cliente conocido

    Estrategia:
      1. Mira el parent inmediato. Si está en raw_to_client → crudo.
         Si está en folder_to_client → editado.
      2. Si no, sube por la cadena de ancestors (max 6 niveles) buscando un
         folder_id de cliente conocido. Esto detecta archivos en subcarpetas
         profundas como /Cliente/Lina/Tanda 3/video.mp4.
      3. Cache de ancestry para no repetir las calls a Drive API.
    """
    from drive_client import get_service
    if ancestry_cache is None:
        ancestry_cache = {}

    parents = f.get("parents") or []
    if not parents:
        return None, None

    # Nivel 0: parent directo
    for p in parents:
        if p in raw_to_client:
            return raw_to_client[p], True
        if p in folder_to_client:
            return folder_to_client[p], False

    # Subir por la cadena de ancestors
    service = get_service()
    visited = set()
    queue = list(parents)
    depth = 0
    while queue and depth < 6:
        next_queue = []
        for p in queue:
            if p in visited:
                continue
            visited.add(p)
            # Cache hit: ya sabemos la respuesta de este folder
            if p in ancestry_cache:
                cli, is_crudo = ancestry_cache[p]
                if cli is not None:
                    return cli, is_crudo
                continue
            # Si está en folder_to_client / raw_to_client (puede haber sido agregado)
            if p in raw_to_client:
                ancestry_cache[p] = (raw_to_client[p], True)
                return raw_to_client[p], True
            if p in folder_to_client:
                ancestry_cache[p] = (folder_to_client[p], False)
                return folder_to_client[p], False
            # Subir un nivel: pedir parents de este folder
            try:
                meta = service.files().get(fileId=p, fields="parents", supportsAllDrives=True).execute()
                pps = meta.get("parents") or []
                ancestry_cache[p] = (None, None)  # se sobreescribe arriba si matchea más arriba
                next_queue.extend(pps)
            except Exception:
                ancestry_cache[p] = (None, None)
                continue
        queue = next_queue
        depth += 1

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
    ancestry_cache = {}  # cache de folder_id → (cliente, is_crudo) compartido entre changes

    for ch in changes:
        if ch.get("removed"):
            continue  # no nos interesa por ahora
        f = ch.get("file") or {}
        if not f or f.get("trashed"):
            continue
        # Solo videos
        if not _is_video(f.get("name", ""), f.get("mimeType", "")):
            continue

        cliente_real, is_crudo = _resolve_client_for_file(f, folder_to_client, raw_to_client, ancestry_cache)
        if cliente_real is None:
            # Archivo no está en una carpeta de cliente conocida — ignorar.
            # (El scan completo se encarga de descubrir clientes nuevos cada hora.)
            continue

        # CRUDO en /Material/: crear task + mandar mail
        if is_crudo:
            if is_file_known(f["id"]):
                continue
            size = int(f["size"]) if f.get("size") else None
            raw_folder_id = next((p for p in (f.get("parents") or []) if p in raw_to_client), None)
            # Archivo muy viejo (>3d) → baseline, no avisar.
            # Evita falsos positivos con archivos descubiertos tarde.
            is_old = _is_file_too_old(f.get("createdTime"))
            claimed = claim_file(
                file_id=f["id"], cliente=cliente_real,
                folder_id=raw_folder_id or "(?)",
                name=f["name"], size=size, created_time=f.get("createdTime"),
                is_baseline=is_old,
            )
            if not claimed:
                continue
            if is_old:
                # Archivo viejo registrado como baseline, no avisar
                continue
            # Si admin ya asignó manualmente este cliente a un editor, no duplicar
            if has_manual_pending_for_client(cliente_real):
                continue
            # Detectar duplicado por apodo/nombre similar (ej. 'Cisco' vs 'Cisco Amengual')
            if find_similar_pending_client(cliente_real):
                continue
            editor = get_editor_for_client(cliente_real, packs)
            if has_pending_for_client_editor(cliente_real, editor):
                continue
            if is_client_blocked(cliente_real, editor):
                continue
            create_task(cliente_real, editor, f["id"], f["name"])
            new_tasks.append({"cliente": cliente_real, "editor": editor, "file": f["name"]})
            continue

        # NO es crudo según parent: clasificar con owner-based + fuzzy match cliente
        sig = classify(f, parent_name=None, cliente_name=cliente_real)
        if sig is False:
            # Owner matchea el cliente → SÍ es crudo (subió fuera de /Material/)
            if is_file_known(f["id"]):
                continue
            size = int(f["size"]) if f.get("size") else None
            # Archivo viejo (createdTime >3 días) → baseline silencioso.
            # Caso real Sandy: archivos del 9/may detectados el 17/may por
            # cambio de carpeta. NO son nuevos, NO mandar mail.
            is_old = _is_file_too_old(f.get("createdTime"))
            claimed = claim_file(
                file_id=f["id"], cliente=cliente_real,
                folder_id="(incremental-fuera-material)",
                name=f["name"], size=size, created_time=f.get("createdTime"),
                is_baseline=is_old,
            )
            if not claimed:
                continue
            if is_old:
                continue  # archivo viejo, registrado como baseline, sin mail
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
        if sig is not True:
            continue  # ambiguo → ignorar (scan completo se encarga)

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
            # Editor REAL = quien subió el archivo (Drive owner), si es editor conocido.
            # Si no, usar el de la task. Esto refleja la realidad cuando un editor cubre a otro.
            from classifier import identify_editor_by_owner
            real_editor = identify_editor_by_owner(f) or result["editor"] or "—"
            cierre_data = {
                "cliente": cliente_real,
                "editor": real_editor,
                "file_name": f["name"],
                "file_id": f["id"],
                "edited_folder_id": (f.get("parents") or [None])[0],
                "new_count": result["new_count"],
                "closed": result["closed"],
            }
            cierres.append(cierre_data)
            enqueue_completion_mail(
                task_id=result["task_id"],
                cliente=cliente_real,
                editor=real_editor,
                file_id=f["id"],
                file_name=f["name"],
                edited_folder_id=cierre_data["edited_folder_id"],
                client_folder_id=None,
                new_count=result["new_count"],
                closed=result["closed"],
            )

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
        from notifier import run as notify_run, send_completion_mails
        if new_tasks:
            print("\n📧 Disparando notificador (crudos nuevos)...")
            notify_run(dry_run=False)
        # SIEMPRE procesar cola persistente de cierres (incluye retry de fallidos anteriores)
        sent = send_completion_mails(cierres)
        if sent:
            print(f"📧 {sent} mails de cierre enviados.")


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--notify", action="store_true")
    args = p.parse_args()
    run(notify=args.notify)

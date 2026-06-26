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
    is_edited_known, claim_edited_file, decrement_pending_count, reconcile_locked_tasks,
    enqueue_completion_mail, is_correction_for_client,
    get_editor_for_subfolder, _classify_subfolder_type,
    infer_subfolder_editor_from_history, register_subfolder_alert,
    upsert_subfolder_editor, mark_pending_task_for_renotification,
)
from sheets_client import read_packs, get_editor_for_client
from aliases import resolve_alias, reverse_alias
from classifier import classify

META_KEY_TOKEN = "drive_changes_page_token"


def _build_folder_index() -> tuple[dict, dict]:
    """Devuelve (folder_id_a_cliente_real, raw_folder_id_a_cliente_real).
    Sirve para identificar rápido si un archivo cambiado está en una carpeta
    relevante (sin tener que descubrir TODO Drive de cero).

    También incluye CLIENT_DELIVERY_FOLDERS (carpetas extra donde un editor
    entrega editados para un cliente, distintas a la carpeta principal del
    cliente). Bug 21/may: Rami subió "7. lo minimo baño.mp4" a la delivery
    folder de Rafa Elvram y el scan no la pescó porque no estaba mapeada.
    """
    conn = get_conn()
    rows = conn.execute("SELECT folder_id, cliente, raw_folder_id FROM clients").fetchall()
    conn.close()
    folder_to_client = {}
    raw_to_client = {}
    for r in rows:
        folder_to_client[r["folder_id"]] = r["cliente"]
        if r["raw_folder_id"]:
            raw_to_client[r["raw_folder_id"]] = r["cliente"]

    # Agregar delivery folders extra (configuradas en aliases.CLIENT_DELIVERY_FOLDERS
    # o en cfg_delivery_folders en DB). Solo si la cliente ya está mapeada arriba.
    try:
        from aliases import get_delivery_folders_runtime
        delivery = get_delivery_folders_runtime()
        for cliente, folder_id in delivery.items():
            if folder_id and folder_id not in folder_to_client:
                folder_to_client[folder_id] = cliente
    except Exception as e:
        print(f"   ⚠️ no se cargaron delivery folders: {e}")

    return folder_to_client, raw_to_client


def _get_immediate_subfolder_name(f: dict, raw_to_client: dict,
                                   ancestry_cache: Optional[dict] = None) -> Optional[str]:
    """Devuelve el nombre de la subfolder dentro de /Material/ donde está el archivo.
    Si está en /Material/ root (sin subfolder), retorna ''.
    Si no se puede determinar, retorna None.

    Ejemplos para Roger Marti:
      - /Material/Vuori.mov → ''  (root de Material)
      - /Material/Youtube/Claude1.mov → 'Youtube'
      - /Material/Polara 3/IMG_xxx.mov → 'Polara 3'

    Estrategia: sube por ancestors hasta encontrar el raw_folder_id; el folder
    inmediatamente debajo de éste es la subfolder que devolvemos."""
    from drive_client import get_service
    if ancestry_cache is None:
        ancestry_cache = {}

    parents = f.get("parents") or []
    if not parents:
        return None

    # Si el parent directo ES el raw_folder → archivo en root de /Material/
    for p in parents:
        if p in raw_to_client:
            return ''

    # Subir por la cadena de ancestors. El primer folder cuyo padre sea
    # un raw_folder_id es la subfolder que queremos nombrar.
    service = get_service()
    cur_id = parents[0]
    visited = set()
    for _ in range(6):
        if cur_id in visited:
            break
        visited.add(cur_id)
        try:
            meta = service.files().get(fileId=cur_id, fields="id,name,parents",
                                        supportsAllDrives=True).execute()
        except Exception:
            return None
        pps = meta.get("parents") or []
        # Si alguno de mis parents es el raw_folder → este folder es la subfolder
        if any(p in raw_to_client for p in pps):
            return meta.get("name") or ''
        if not pps:
            return None
        cur_id = pps[0]
    return None


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

        # OVERRIDE: si el owner es un editor conocido, ES editado (no importa
        # dónde lo haya puesto). El cliente JAMÁS sube con cuenta del editor.
        # Bug 21/may: Valen subió "reel 13" a /Natalia López/Material/Contenido/
        # Reel 13/ y se clasificó como crudo por estar dentro de /Material/.
        if is_crudo:
            try:
                from classifier import identify_editor_by_owner
                if identify_editor_by_owner(f):
                    print(f"  🎯 owner es editor → override a editado: {f['name'][:40]}")
                    is_crudo = False
            except Exception:
                pass

        # CRUDO en /Material/: crear task + mandar mail
        if is_crudo:
            if is_file_known(f["id"]):
                continue
            size = int(f["size"]) if f.get("size") else None
            raw_folder_id = next((p for p in (f.get("parents") or []) if p in raw_to_client), None)
            # Detectar subfolder ANTES de claimear (para guardarlo en la row)
            subfolder_name = _get_immediate_subfolder_name(f, raw_to_client, ancestry_cache)
            # Archivo muy viejo (>3d) → baseline, no avisar.
            is_old = _is_file_too_old(f.get("createdTime"))
            claimed = claim_file(
                file_id=f["id"], cliente=cliente_real,
                folder_id=raw_folder_id or "(?)",
                name=f["name"], size=size, created_time=f.get("createdTime"),
                is_baseline=is_old,
                subfolder_name=subfolder_name,
            )
            if not claimed:
                continue
            if is_old:
                continue
            # Detectar duplicado por apodo/nombre similar (ej. 'Cisco' vs 'Cisco Amengual')
            if find_similar_pending_client(cliente_real):
                continue
            # Resolver editor con prioridad:
            #   1. cfg_subfolder_editors (override manual configurado)
            #   2. Inferencia automática del histórico (si misma subfolder ya
            #      tiene >=2 entregas todas del mismo editor → usar ese editor
            #      y auto-poblar cfg_subfolder_editors para próxima vez)
            #   3. Sheet (get_editor_for_client) como default
            from tracker import resolve_editor_rules
            editor = resolve_editor_rules(cliente_real, subfolder_name, f["name"],
                                          (f.get("videoMediaMetadata") or {}).get("durationMillis"))
            if not editor and subfolder_name:
                inferred = infer_subfolder_editor_from_history(cliente_real, subfolder_name)
                if inferred:
                    editor = inferred
                    # Persistir la inferencia para que el próximo scan no recalcule
                    upsert_subfolder_editor(cliente_real, subfolder_name, inferred)
                    print(f"   🧠 Inferido del histórico: {cliente_real}/{subfolder_name} → {inferred}")
            if not editor:
                editor = get_editor_for_client(cliente_real, packs)
            # Alerta al admin si la subfolder es "tipo" (Youtube/Reels/Shorts/...)
            # y NO está mapeada en cfg_subfolder_editors. Mandar UN solo mail
            # por (cliente, subfolder).
            if subfolder_name:
                tipo = _classify_subfolder_type(subfolder_name)
                already_mapped = get_editor_for_subfolder(cliente_real, subfolder_name)
                if tipo and not already_mapped:
                    is_new_alert = register_subfolder_alert(
                        cliente=cliente_real, subfolder=subfolder_name,
                        inferred_type=tipo, example_file=f["name"],
                        example_file_id=f["id"], default_editor=editor,
                    )
                    if is_new_alert:
                        # Encolar mail de alerta al admin (envío diferido en notifier)
                        from notifier import enqueue_subfolder_alert
                        enqueue_subfolder_alert(
                            cliente=cliente_real, subfolder=subfolder_name,
                            tipo=tipo, file_name=f["name"], file_id=f["id"],
                            default_editor=editor,
                        )
            # NO duplicar si ya hay task manual pending para ESTE editor específico
            if has_manual_pending_for_client(cliente_real, editor):
                continue
            if has_pending_for_client_editor(cliente_real, editor):
                # Ya hay pending: si el último mail fue hace ≥6h (o nunca),
                # resetear mail_sent_at para que el notifier mande UN aviso
                # nuevo "más material" con este archivo. Debounce evita spam.
                renotif_id = mark_pending_task_for_renotification(
                    cliente_real, editor, f["id"], f["name"]
                )
                if renotif_id:
                    new_tasks.append({"cliente": cliente_real, "editor": editor,
                                       "file": f["name"], "renotif": True})
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
            editor = get_editor_for_client(cliente_real, packs)
            if has_manual_pending_for_client(cliente_real, editor):
                continue
            if has_pending_for_client_editor(cliente_real, editor):
                # Re-notificar si hace ≥6h del último mail (debounce anti-spam)
                renotif_id = mark_pending_task_for_renotification(
                    cliente_real, editor, f["id"], f["name"]
                )
                if renotif_id:
                    new_tasks.append({"cliente": cliente_real, "editor": editor,
                                       "file": f["name"], "renotif": True})
                continue
            if is_client_blocked(cliente_real, editor):
                continue
            create_task(cliente_real, editor, f["id"], f["name"])
            new_tasks.append({"cliente": cliente_real, "editor": editor, "file": f["name"]})
            continue
        if sig is None:
            # AMBIGUO en carpeta de cliente fuera de /Material/. Antes
            # skipeábamos y el scan completo se encargaba — pero el scan
            # completo solo procesa clientes con pending count > 0, así que
            # correcciones / re-entregas con nombres ambiguos ("3.mp4", "v2",
            # etc.) nunca se detectaban. Bug 21/may: Adri subió "3.mp4" para
            # Luis Alberto y no llegó mail.
            #
            # Default permisivo coherente con list_edited_files de closer.py:
            # archivo afuera de /Material/, owner no matchea cliente → asumir
            # que es entrega de un editor (aunque su mail no esté en cfg_editors).
            pass  # tratar como editado (sig=True implícito)
        elif sig is not True:
            continue  # sig is False (crudo) ya manejado arriba en la rama is_crudo

        # Es editado → cerrar task pendiente (si hay)
        if is_edited_known(f["id"]):
            continue
        # Archivo VIEJO (createdTime > 3 días): no mandar mail "X entregó".
        # Probably es un archivo que reaparece por share/permissions/audit
        # de Drive, NO una entrega nueva. Bug 31/may: Rafa subió hace 20 días
        # un archivo a Gaetan por error, y el sistema mandó mail "Rafa entregó"
        # 6 veces porque Drive Changes API lo reportó como modificado.
        # Lo registramos como baseline (conocido) para que no vuelva a procesarse.
        if _is_file_too_old(f.get("createdTime"), max_age_days=3):
            size = int(f["size"]) if f.get("size") else None
            claim_edited_file(
                file_id=f["id"], cliente=cliente_real, folder_id="(viejo-baseline)",
                name=f["name"], size=size, created_time=f.get("createdTime"),
                is_baseline=True, closed_task_id=None,
            )
            print(f"  ⏭️  editado VIEJO (>3 días) registrado como baseline, sin mail: {f['name'][:50]}")
            continue
        # Defensa contra double-process: si OTRO worker ya mandó completion mail
        # para este archivo recientemente (porque pusheó mail_log aunque
        # tracker.db haya quedado desincronizado), no re-procesar. Bug 21/may:
        # V61/V62 Alberto mandados dos veces con counters distintos.
        from mail_client import _sync_mail_log_from_remote
        try:
            _sync_mail_log_from_remote()
        except Exception:
            pass
        from tracker import completion_mail_already_sent
        if completion_mail_already_sent(cliente_real, f["id"], f["name"]):
            print(f"  ⏭️  ya procesado por otro worker (mail_log): {cliente_real} / {f['name'][:40]}")
            continue
        size = int(f["size"]) if f.get("size") else None
        claimed = claim_edited_file(
            file_id=f["id"], cliente=cliente_real, folder_id="(incremental)",
            name=f["name"], size=size, created_time=f.get("createdTime"),
            is_baseline=False, closed_task_id=None,
        )
        if not claimed:
            continue

        # ¿Es CORRECCIÓN de un editado previo del mismo cliente?
        from classifier import identify_editor_by_owner
        if is_correction_for_client(cliente_real, f["name"], current_file_id=f["id"]):
            # Mismo fallback que closer.py: si el mail del owner no matchea editor
            # conocido, usar el editor que el Sheet asigna al cliente. Antes acá
            # caía a "—" siempre, lo que rompía atribución de correcciones de
            # editores no registrados en cfg_editors (caso Adri 21/may con Luis).
            client_editor_fallback = get_editor_for_client(cliente_real, packs)
            real_editor = identify_editor_by_owner(f) or client_editor_fallback or "—"
            enqueue_completion_mail(
                task_id=None,
                cliente=cliente_real,
                editor=real_editor,
                file_id=f["id"],
                file_name=f["name"],
                edited_folder_id=(f.get("parents") or [None])[0],
                client_folder_id=None,
                new_count=0,
                closed=False,
                is_correction=True,
            )
            continue

        result = decrement_pending_count(cliente_real, completed_by_file_id=f["id"])
        if result["task_id"] is not None:
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

    # Red de seguridad DURABLE: cerrar tasks manuales ya entregadas (re-derivado de
    # mail_log). Cubre el caso de un decrement perdido por push concurrente, así
    # reminders/daily/dashboard ven el estado correcto sin depender de ese UPDATE.
    try:
        n_rec = reconcile_locked_tasks()
        if n_rec:
            print(f"♻️  {n_rec} task(s) manual cerradas por entregas reales (reconcile)")
    except Exception as _e:
        print(f"reconcile_locked_tasks: {_e}")

    if notify:
        from notifier import run as notify_run, send_completion_mails
        if new_tasks:
            print("\n📧 Disparando notificador (crudos nuevos)...")
            notify_run(dry_run=False)
        # SIEMPRE procesar cola persistente de cierres (incluye retry de fallidos anteriores)
        sent = send_completion_mails(cierres)
        if sent:
            print(f"📧 {sent} mails de cierre enviados.")
        # Avisos de revisión pedida que el endpoint del portal no logró mandar
        # (notified_at NULL). DURABLE: el scan tiene creds de mail y no muere por
        # timeout HTTP como el endpoint. Cubre el bug de "el editor no se entera
        # cuando el cliente pide una corrección".
        try:
            from notifier import notify_pending_reviews
            nrev = notify_pending_reviews()
            if nrev:
                print(f"📝 {nrev} aviso(s) de revisión pedida enviados (durable).")
        except Exception as _e:
            print(f"notify_pending_reviews: {_e}")


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--notify", action="store_true")
    args = p.parse_args()
    run(notify=args.notify)

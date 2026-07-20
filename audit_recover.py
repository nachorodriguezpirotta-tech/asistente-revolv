"""
SAFETY NET: corre periódicamente, cruza Drive con tracker.db y procesa
cualquier video reciente que el scan_incremental se haya perdido.

Causas conocidas por las que un archivo se puede perder en el incremental:
  - GHA cron throttling (skip de runs de 5min)
  - Drive Changes API eventual-consistency (file no aparece en changes feed)
  - Falla de tracker.db push → próximo run no ve mail_log → dedupe falla
  - Fallo en _resolve_client_for_file por API timeout

Estrategia: NO confiamos en page_token. Vamos directo a files.list filtrando
por modifiedTime > N horas atrás, y procesamos cualquier video que NO esté
en known_files / known_edited_files.

Ventana: últimas 12h por default. Suficiente para atrapar fallas del incremental
sin re-procesar todo. Si un archivo está en DB ya (procesado por incremental),
lo saltea por idempotencia.
"""
import os
import sys
import sqlite3
from datetime import datetime, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
os.chdir(HERE)

from drive_client import get_service
from scan_incremental import _build_folder_index, _resolve_client_for_file, _get_immediate_subfolder_name
from classifier import classify, identify_editor_by_owner
from tracker import (
    init_db, is_file_known, is_edited_known, claim_file, claim_edited_file,
    is_correction_for_client, enqueue_completion_mail, decrement_pending_count,
    create_task, has_pending_for_client_editor, mark_pending_task_for_renotification,
    has_manual_pending_for_client, is_client_blocked, get_conn,
)
from sheets_client import read_packs, get_editor_for_client
from notifier import send_completion_mails, run as notifier_run
from config import TEST_EMAIL


def _format_time_filter(hours_back: int) -> str:
    """ISO UTC para Drive query, hours_back atrás de ahora."""
    cutoff = datetime.utcnow() - timedelta(hours=hours_back)
    return cutoff.strftime("%Y-%m-%dT%H:%M:%S")


def run(hours_back: int = 12, notify: bool = True) -> dict:
    """Recupera archivos perdidos. Retorna stats."""
    init_db()
    svc = get_service()
    folder_to_client, raw_to_client = _build_folder_index()
    packs = read_packs()

    time_filter = _format_time_filter(hours_back)
    print(f"🔍 Audit: buscando videos modificados desde {time_filter} UTC")

    # Pull all video files modified in window
    all_files = []
    page_token = None
    while True:
        kwargs = dict(
            q=f"trashed=false and modifiedTime > '{time_filter}'",
            fields='nextPageToken, files(id, name, mimeType, size, createdTime, modifiedTime, '
                   'parents, owners(emailAddress), lastModifyingUser(emailAddress), videoMediaMetadata(durationMillis))',
            pageSize=500,
            orderBy='modifiedTime desc',
        )
        if page_token:
            kwargs['pageToken'] = page_token
        results = svc.files().list(**kwargs).execute()
        all_files.extend(results.get('files', []))
        page_token = results.get('nextPageToken')
        if not page_token:
            break

    # Filter to videos, exclude macOS junk
    videos = [f for f in all_files
              if (f.get('mimeType','').startswith('video/') or
                  f['name'].lower().endswith(('.mp4','.mov','.m4v')))
              and not f['name'].startswith('._')]

    # Get known ids from DB
    conn = get_conn()
    known_crudo = {r['file_id'] for r in conn.execute("SELECT file_id FROM known_files")}
    known_edit = {r['file_id'] for r in conn.execute("SELECT file_id FROM known_edited_files")}
    conn.close()

    pendientes = [f for f in videos if f['id'] not in known_crudo and f['id'] not in known_edit]
    print(f"📊 {len(videos)} videos en ventana / {len(pendientes)} no procesados aún")

    stats = {
        "videos_ventana": len(videos),
        "pendientes": len(pendientes),
        "crudos_recovered": 0,
        "editados_recovered": 0,
        "correcciones_recovered": 0,
        "sin_cliente": 0,
        "owners_sin_cliente": set(),
    }

    if not pendientes:
        print("✅ Nada que recuperar — todo en orden")
        return stats

    ancestry_cache = {}
    new_tasks = []
    for f in pendientes:
        cliente, is_crudo = _resolve_client_for_file(f, folder_to_client, raw_to_client, ancestry_cache)
        # Normalizar variantes de nombre (cfg_aliases): p.ej. la fila vieja del
        # Sheet 'Pedro' fuzzy-matchea archivos de 'Román Pedroza' → sin esto, el
        # audit creaba known_files/tasks bajo el nombre viejo (08/jul).
        if cliente:
            try:
                cliente = resolve_alias(cliente)
            except Exception:
                pass
        if not cliente:
            stats["sin_cliente"] += 1
            owner = (f.get('owners') or [{}])[0].get('emailAddress','?')
            stats["owners_sin_cliente"].add(owner)
            continue

        # OVERRIDE: si owner es editor conocido, ES editado (mismo razonamiento
        # que scan_incremental.py — el cliente jamás sube con cuenta de editor).
        if is_crudo and identify_editor_by_owner(f):
            print(f"  🎯 owner es editor → override a editado: {f['name'][:40]}")
            is_crudo = False

        size = int(f["size"]) if f.get("size") else None
        parent0 = (f.get('parents') or ['?'])[0]

        if is_crudo:
            if is_file_known(f['id']):
                continue
            subfolder = _get_immediate_subfolder_name(f, raw_to_client, ancestry_cache)
            claimed = claim_file(
                file_id=f["id"], cliente=cliente, folder_id=parent0,
                name=f["name"], size=size, created_time=f.get("createdTime"),
                is_baseline=False, subfolder_name=subfolder,
            )
            if not claimed:
                continue
            stats["crudos_recovered"] += 1
            print(f"  📥 CRUDO recovered: {cliente} / {f['name'][:50]}")

            # Crear/renotificar task — reglas por subcarpeta/nombre primero
            # (ej. Egdylu: 'reel'→Fran, 'yt'→Rami), después Sheet.
            from tracker import resolve_editor_rules as _rer
            editor = (_rer(cliente, subfolder, f["name"],
                           (f.get("videoMediaMetadata") or {}).get("durationMillis"))
                      or get_editor_for_client(cliente, packs) or "—")
            if has_manual_pending_for_client(cliente, editor):
                continue
            if is_client_blocked(cliente, editor):
                continue
            if has_pending_for_client_editor(cliente, editor):
                rid = mark_pending_task_for_renotification(cliente, editor, f["id"], f["name"])
                if rid:
                    new_tasks.append((cliente, editor, f['name']))
            else:
                create_task(cliente, editor, f["id"], f["name"])
                new_tasks.append((cliente, editor, f['name']))
            continue

        # No es crudo: usar classifier
        sig = classify(f, parent_name=None, cliente_name=cliente)
        if sig is False:
            # Owner = cliente → crudo fuera de Material
            if is_file_known(f['id']):
                continue
            # Cliente bloqueado o ARCHIVADO → claimear (no reprocesar) pero NO
            # crear task ni avisar. Bug 12/jun: este path no chequeaba y
            # re-creó la task de Alicia Ramirez (archivada) → aparecía en el
            # daily summary de Benja aunque no estaba en el dashboard.
            if is_client_blocked(cliente, None):
                claim_file(file_id=f["id"], cliente=cliente, folder_id="(audit-fuera-mat)",
                           name=f["name"], size=size, created_time=f.get("createdTime"),
                           is_baseline=True)
                continue
            claim_file(file_id=f["id"], cliente=cliente, folder_id="(audit-fuera-mat)",
                       name=f["name"], size=size, created_time=f.get("createdTime"),
                       is_baseline=False)
            stats["crudos_recovered"] += 1
            print(f"  📥 CRUDO fuera-mat recovered: {cliente} / {f['name'][:50]}")
            from tracker import resolve_editor_rules as _rer3
            editor = (_rer3(cliente, None, f["name"],
                            (f.get("videoMediaMetadata") or {}).get("durationMillis"))
                      or get_editor_for_client(cliente, packs) or "—")
            if has_pending_for_client_editor(cliente, editor):
                mark_pending_task_for_renotification(cliente, editor, f["id"], f["name"])
            else:
                create_task(cliente, editor, f["id"], f["name"])
            new_tasks.append((cliente, editor, f['name']))
            continue

        # sig is True (editado seguro) o sig is None (ambiguo - default permisivo editado)
        if is_edited_known(f['id']):
            continue
        # Archivo VIEJO (>3 días): registrar como baseline, NO mandar mail.
        # Bug 31/may: archivos antiguos reaparecen en Drive Changes feed
        # (share, audit, etc) y se mandaban como entrega nueva.
        from scan import _is_file_too_old
        if _is_file_too_old(f.get("createdTime"), max_age_days=3):
            claim_edited_file(
                file_id=f["id"], cliente=cliente, folder_id="(viejo-audit)",
                name=f["name"], size=size, created_time=f.get("createdTime"),
                is_baseline=True, closed_task_id=None,
            )
            print(f"  ⏭️  editado VIEJO baseline: {cliente} / {f['name'][:40]}")
            continue
        # Defensa contra double-process: si el incremental ya mandó el completion
        # mail para este archivo (mail_log dedupe estable), no re-procesarlo
        # aunque el archivo no figure en known_edited_files (caso típico:
        # incremental no pusheó tracker.db pero sí pusheó mail_log).
        from mail_client import _sync_mail_log_from_remote
        from tracker import completion_mail_already_sent
        try:
            _sync_mail_log_from_remote()
        except Exception:
            pass
        if completion_mail_already_sent(cliente, f["id"], f["name"]):
            print(f"  ⏭️  ya procesado por incremental: {cliente} / {f['name'][:40]}")
            continue
        # Editado viejo re-aparecido (>3 días) = histórico, no entrega (caso Iván 16/jul)
        from scan import _is_file_too_old as _too_old
        if _too_old(f.get("createdTime")):
            claim_edited_file(
                file_id=f["id"], cliente=cliente, folder_id="(audit)",
                name=f["name"], size=size, created_time=f.get("createdTime"),
                is_baseline=True, closed_task_id=None,
            )
            continue
        claimed = claim_edited_file(
            file_id=f["id"], cliente=cliente, folder_id="(audit)",
            name=f["name"], size=size, created_time=f.get("createdTime"),
            is_baseline=False, closed_task_id=None,
        )
        if not claimed:
            continue
        from classifier import looks_like_client_upload as _cli_up
        if _cli_up(f):
            print(f"  🚫 material del cliente (owner no-editor), no es entrega: {cliente} / {f['name'][:40]}")
            continue

        is_correction = is_correction_for_client(cliente, f["name"], current_file_id=f["id"])
        real_editor = identify_editor_by_owner(f) or get_editor_for_client(cliente, packs) or "—"

        if is_correction:
            enqueue_completion_mail(
                task_id=None, cliente=cliente, editor=real_editor,
                file_id=f["id"], file_name=f["name"],
                edited_folder_id=parent0, client_folder_id=None,
                new_count=0, closed=False, is_correction=True,
            )
            stats["correcciones_recovered"] += 1
            print(f"  🔧 CORRECCIÓN recovered: {real_editor} → {cliente} / {f['name'][:50]}")
        else:
            result = decrement_pending_count(cliente, completed_by_file_id=f["id"])
            enqueue_completion_mail(
                task_id=result.get("task_id"), cliente=cliente, editor=real_editor,
                file_id=f["id"], file_name=f["name"],
                edited_folder_id=parent0, client_folder_id=None,
                new_count=result.get("new_count", 0),
                closed=result.get("closed", False), is_correction=False,
            )
            stats["editados_recovered"] += 1
            print(f"  📹 EDITADO recovered: {real_editor} → {cliente} / {f['name'][:50]}")

    # Dispatch notifications
    total_recovered = (stats["crudos_recovered"] + stats["editados_recovered"]
                       + stats["correcciones_recovered"])

    if notify and total_recovered > 0:
        print(f"\n📬 Mandando mails de recovery ({total_recovered} archivos)...")
        # Mails de cierre/corrección
        if stats["editados_recovered"] + stats["correcciones_recovered"] > 0:
            sent = send_completion_mails(recipient=TEST_EMAIL)
            print(f"   📨 completion mails: {sent}")
        # Mails de tareas pendientes (crudos)
        if new_tasks:
            notifier_run()
            print(f"   📨 pending task notifs: {len(new_tasks)} tasks nuevas/renotif")

    print(f"\n📊 Resumen:")
    print(f"   videos en ventana:     {stats['videos_ventana']}")
    print(f"   crudos recovered:      {stats['crudos_recovered']}")
    print(f"   editados recovered:    {stats['editados_recovered']}")
    print(f"   correcciones recovered:{stats['correcciones_recovered']}")
    print(f"   sin cliente (skip):    {stats['sin_cliente']}")
    if stats["owners_sin_cliente"]:
        print(f"   owners no mapeados:    {sorted(stats['owners_sin_cliente'])}")

    return stats


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--hours", type=int, default=12, help="ventana hacia atrás en horas")
    p.add_argument("--no-notify", action="store_true", help="solo procesa, no manda mails")
    args = p.parse_args()
    run(hours_back=args.hours, notify=not args.no_notify)

"""
Audit + recovery: encuentra videos de hoy NO procesados y los procesa.
Útil cuando el scan se atrasa, falla, o el page_token se adelanta sin
procesar algo.
"""
import os, sys
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
os.chdir(ROOT)

import sqlite3
from drive_client import get_service
from scan_incremental import _build_folder_index, _resolve_client_for_file
from classifier import classify, identify_editor_by_owner
from tracker import (
    init_db, is_file_known, is_edited_known, claim_file, claim_edited_file,
    is_correction_for_client, enqueue_completion_mail, decrement_pending_count,
    get_conn,
)
from sheets_client import read_packs, get_editor_for_client
from notifier import send_completion_mails
from config import TEST_EMAIL


def main():
    init_db()
    svc = get_service()
    folder_to_client, raw_to_client = _build_folder_index()
    packs = read_packs()
    print(f"📁 Index: {len(folder_to_client)} folders + {len(raw_to_client)} raws")

    # Videos modificados hoy
    results = svc.files().list(
        q="trashed=false and modifiedTime > '2026-05-21T12:00:00'",
        fields='files(id, name, mimeType, size, createdTime, modifiedTime, parents, owners(emailAddress), lastModifyingUser(emailAddress))',
        pageSize=500,
        orderBy='modifiedTime desc',
    ).execute()
    all_files = results.get('files', [])
    videos = [f for f in all_files
              if (f.get('mimeType','').startswith('video/') or f['name'].lower().endswith(('.mp4','.mov','.m4v')))
              and not f['name'].startswith('._')]
    print(f"🎬 Videos hoy: {len(videos)}")

    # Cuáles no están en DB
    conn = get_conn()
    known_crudo = {r['file_id'] for r in conn.execute("SELECT file_id FROM known_files")}
    known_edit = {r['file_id'] for r in conn.execute("SELECT file_id FROM known_edited_files")}
    conn.close()

    pendientes = [f for f in videos if f['id'] not in known_crudo and f['id'] not in known_edit]
    print(f"🚨 No procesados: {len(pendientes)}\n")

    ancestry_cache = {}
    nuevos_crudos = 0
    nuevos_editados = 0
    skipped_no_client = 0
    cierres = []

    for f in pendientes:
        cliente, is_crudo = _resolve_client_for_file(f, folder_to_client, raw_to_client, ancestry_cache)
        if not cliente:
            skipped_no_client += 1
            owner = (f.get('owners') or [{}])[0].get('emailAddress','?')
            print(f"  ⚠️  SIN CLIENTE: {f['name'][:40]:<40} owner={owner[:30]}")
            continue

        size = int(f["size"]) if f.get("size") else None
        if is_crudo:
            # CRUDO
            if is_file_known(f['id']):
                continue
            claimed = claim_file(
                file_id=f["id"], cliente=cliente,
                folder_id=(f.get('parents') or ['?'])[0],
                name=f["name"], size=size, created_time=f.get("createdTime"),
                is_baseline=False, subfolder_name=None,
            )
            if claimed:
                nuevos_crudos += 1
                print(f"  📥 CRUDO recovered: {cliente} / {f['name'][:40]}")
            continue

        # Editado: clasificar primero
        sig = classify(f, parent_name=None, cliente_name=cliente)
        # Si sig es False (es crudo por classify) → es crudo fuera de Material
        if sig is False:
            if is_file_known(f['id']): continue
            claim_file(file_id=f["id"], cliente=cliente, folder_id="(audit-fuera-mat)",
                       name=f["name"], size=size, created_time=f.get("createdTime"),
                       is_baseline=False)
            nuevos_crudos += 1
            print(f"  📥 CRUDO fuera-material recovered: {cliente} / {f['name'][:40]}")
            continue

        # sig is True (editado seguro) o sig is None (ambiguo - tratamos como editado, default permisivo)
        if is_edited_known(f['id']): continue
        claimed = claim_edited_file(
            file_id=f["id"], cliente=cliente, folder_id="(audit)",
            name=f["name"], size=size, created_time=f.get("createdTime"),
            is_baseline=False, closed_task_id=None,
        )
        if not claimed: continue

        is_correction = is_correction_for_client(cliente, f["name"], current_file_id=f["id"])
        real_editor = identify_editor_by_owner(f) or get_editor_for_client(cliente, packs) or "—"

        if is_correction:
            enqueue_completion_mail(
                task_id=None, cliente=cliente, editor=real_editor,
                file_id=f["id"], file_name=f["name"],
                edited_folder_id=(f.get("parents") or [None])[0],
                client_folder_id=None, new_count=0, closed=False, is_correction=True,
            )
            print(f"  🔧 CORRECCIÓN recovered: {real_editor} → {cliente} / {f['name'][:40]}")
        else:
            result = decrement_pending_count(cliente, completed_by_file_id=f["id"])
            enqueue_completion_mail(
                task_id=result.get("task_id"), cliente=cliente, editor=real_editor,
                file_id=f["id"], file_name=f["name"],
                edited_folder_id=(f.get("parents") or [None])[0],
                client_folder_id=None,
                new_count=result.get("new_count", 0),
                closed=result.get("closed", False), is_correction=False,
            )
            print(f"  📹 EDITADO recovered: {real_editor} → {cliente} / {f['name'][:40]}")
        nuevos_editados += 1

    print(f"\n📊 Resumen:")
    print(f"   crudos recuperados:   {nuevos_crudos}")
    print(f"   editados recuperados: {nuevos_editados}")
    print(f"   sin cliente (skip):   {skipped_no_client}")

    if nuevos_editados > 0:
        print(f"\n📬 Mandando mails encolados...")
        sent = send_completion_mails(recipient=TEST_EMAIL)
        print(f"   Total mails enviados: {sent}")


if __name__ == "__main__":
    main()

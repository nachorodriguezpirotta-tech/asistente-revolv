"""Procesa manualmente el archivo de Rafa Elvram que Rami subió hoy
a la delivery folder extra (no mapeada en folder_to_client hasta el
fix de hoy)."""
import os, sys
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
os.chdir(ROOT)

from drive_client import get_service
from tracker import (
    init_db, is_edited_known, claim_edited_file,
    is_correction_for_client, enqueue_completion_mail,
    decrement_pending_count,
)
from classifier import identify_editor_by_owner
from notifier import send_completion_mails
from sheets_client import read_packs, get_editor_for_client

FILE_ID = None  # busco por nombre y carpeta
CLIENTE = "Rafa Elvram"
DELIVERY_FOLDER = "1PUIRQ80fV9ZffdhDnOz4sUJLCLr6bjDH"

def main():
    init_db()
    svc = get_service()
    # Buscar el archivo
    results = svc.files().list(
        q=f"trashed=false and '{DELIVERY_FOLDER}' in parents and modifiedTime > '2026-05-21T20:00:00'",
        fields='files(id, name, size, createdTime, modifiedTime, parents, owners(emailAddress), lastModifyingUser(emailAddress))',
        pageSize=10,
        orderBy='modifiedTime desc',
    ).execute()
    files = results.get('files', [])
    if not files:
        print(f"❌ No se encontró ningún archivo nuevo")
        return
    f = files[0]
    print(f"📹 Archivo: {f['name']}")
    print(f"   Owner: {[o['emailAddress'] for o in f.get('owners',[])]}")
    print(f"   id: {f['id']}")

    if is_edited_known(f['id']):
        print(f"⚠️  Ya está en known_edited_files. Skipping.")
        return

    size = int(f["size"]) if f.get("size") else None
    claimed = claim_edited_file(
        file_id=f["id"], cliente=CLIENTE, folder_id="(manual-rafa-delivery)",
        name=f["name"], size=size, created_time=f.get("createdTime"),
        is_baseline=False, closed_task_id=None,
    )
    if not claimed:
        print(f"⚠️  No se pudo claimear (otro proceso ya lo procesó)")
        return

    is_correction = is_correction_for_client(CLIENTE, f["name"], current_file_id=f["id"])
    print(f"   is_correction: {is_correction}")
    packs = read_packs()
    real_editor = identify_editor_by_owner(f) or get_editor_for_client(CLIENTE, packs) or "—"
    print(f"   editor: {real_editor}")

    if is_correction:
        enqueue_completion_mail(
            task_id=None, cliente=CLIENTE, editor=real_editor,
            file_id=f["id"], file_name=f["name"],
            edited_folder_id=DELIVERY_FOLDER,
            client_folder_id=None, new_count=0, closed=False, is_correction=True,
        )
        print(f"   ✅ encolado como corrección")
    else:
        result = decrement_pending_count(CLIENTE, completed_by_file_id=f["id"])
        enqueue_completion_mail(
            task_id=result.get("task_id"), cliente=CLIENTE, editor=real_editor,
            file_id=f["id"], file_name=f["name"],
            edited_folder_id=DELIVERY_FOLDER, client_folder_id=None,
            new_count=result.get("new_count", 0),
            closed=result.get("closed", False), is_correction=False,
        )
        print(f"   ✅ encolado como entrega")

    from config import TEST_EMAIL
    sent = send_completion_mails(recipient=TEST_EMAIL)
    print(f"📬 mails enviados: {sent}")


if __name__ == "__main__":
    main()

"""
Procesa manualmente el archivo "3.mp4" que Adrian subió a Luis Alberto
hoy 21/may pero que el scan no detectó por ambiguous-name.

Mimica lo que haría scan_incremental con el fix ya pusheado.
"""

import os, sys
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
os.chdir(ROOT)

from drive_client import get_service
from tracker import (
    init_db, is_edited_known, claim_edited_file,
    is_correction_for_client, enqueue_completion_mail,
)
from classifier import identify_editor_by_owner
from notifier import send_completion_mails
from sheets_client import read_packs, get_editor_for_client

FILE_ID = "1OC_VVF_fmuw76B5p1k3-O_JUOs6bOBXq"
CLIENTE = "Luis Alberto"  # como lo tiene folder_to_client mapped

def main():
    init_db()
    svc = get_service()
    f = svc.files().get(
        fileId=FILE_ID,
        fields="id, name, mimeType, size, createdTime, modifiedTime, parents, owners(emailAddress), lastModifyingUser(emailAddress)"
    ).execute()
    print(f"📹 Archivo: {f['name']}")
    print(f"   Owner: {[o['emailAddress'] for o in f.get('owners',[])]}")
    print(f"   Parent: {f.get('parents')}")

    if is_edited_known(FILE_ID):
        print(f"⚠️  Ya está en known_edited_files. Skipping.")
        return

    size = int(f["size"]) if f.get("size") else None
    claimed = claim_edited_file(
        file_id=FILE_ID, cliente=CLIENTE, folder_id="(manual-adri)",
        name=f["name"], size=size, created_time=f.get("createdTime"),
        is_baseline=False, closed_task_id=None,
    )
    if not claimed:
        print(f"⚠️  No se pudo claimear (otro proceso ya lo procesó)")
        return

    # Es corrección?
    is_correction = is_correction_for_client(CLIENTE, f["name"], current_file_id=FILE_ID)
    print(f"   is_correction: {is_correction}")

    packs = read_packs()
    real_editor = identify_editor_by_owner(f) or get_editor_for_client(CLIENTE, packs) or "—"
    print(f"   real_editor: {real_editor}")

    if is_correction:
        enqueue_completion_mail(
            task_id=None,
            cliente=CLIENTE,
            editor=real_editor,
            file_id=FILE_ID,
            file_name=f["name"],
            edited_folder_id=(f.get("parents") or [None])[0],
            client_folder_id=None,
            new_count=0,
            closed=False,
            is_correction=True,
        )
        print(f"   ✅ encolado como corrección")
    else:
        # NO es corrección — primera entrega de "3.mp4"
        from tracker import decrement_pending_count
        result = decrement_pending_count(CLIENTE, completed_by_file_id=FILE_ID)
        new_count = result.get("new_count", 0)
        closed = result.get("closed", False)
        enqueue_completion_mail(
            task_id=result.get("task_id"),
            cliente=CLIENTE,
            editor=real_editor,
            file_id=FILE_ID,
            file_name=f["name"],
            edited_folder_id=(f.get("parents") or [None])[0],
            client_folder_id=None,
            new_count=new_count,
            closed=closed,
            is_correction=False,
        )
        print(f"   ✅ encolado como entrega (new_count={new_count}, closed={closed})")

    # Mandar
    from config import TEST_EMAIL
    sent = send_completion_mails(recipient=TEST_EMAIL)
    print(f"📬 mails enviados: {sent}")


if __name__ == "__main__":
    main()

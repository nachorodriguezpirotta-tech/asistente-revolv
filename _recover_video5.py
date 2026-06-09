"""One-off: procesa '8. Video 5 junio.mp4' de Lili (pack mayo/Editados) que el
incremental y el audit no vieron por lag del índice de búsqueda de Drive.
Replica el path normal de editado: claim → decrement → completion mail.
Borrar script + workflow después.
"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from tracker import (init_db, is_edited_known, claim_edited_file,
                     decrement_pending_count, enqueue_completion_mail)
from notifier import send_completion_mails
from config import TEST_EMAIL

FILE_ID = None  # se resuelve por nombre en la carpeta
EDITADOS = "14TD81uv0I0zVQEj1C6ugrImIRn4riNYy"  # pack mayo/Editados
CLIENTE = "videos reels Lili Rohe"
EDITOR = "Rami"

init_db()

from drive_client import get_service
svc = get_service()
res = svc.files().list(
    q=f"'{EDITADOS}' in parents and trashed=false",
    fields="files(id,name,size,createdTime)",
    supportsAllDrives=True, includeItemsFromAllDrives=True,
).execute()
f = next((x for x in res.get("files", []) if "Video 5 junio" in x["name"]), None)
if not f:
    print("❌ no encontré el archivo en la carpeta")
    sys.exit(1)
print(f"archivo: {f['name']} ({f['id']})")

if is_edited_known(f["id"]):
    print("ya estaba en DB — nada que hacer")
    sys.exit(0)

claimed = claim_edited_file(
    file_id=f["id"], cliente=CLIENTE, folder_id=EDITADOS,
    name=f["name"], size=int(f["size"]) if f.get("size") else None,
    created_time=f.get("createdTime"), is_baseline=False, closed_task_id=None,
)
print(f"claim: {claimed}")
if not claimed:
    sys.exit(0)

result = decrement_pending_count(CLIENTE, completed_by_file_id=f["id"])
enqueue_completion_mail(
    task_id=result.get("task_id"), cliente=CLIENTE, editor=EDITOR,
    file_id=f["id"], file_name=f["name"],
    edited_folder_id=EDITADOS, client_folder_id=None,
    new_count=result.get("new_count", 0),
    closed=result.get("closed", False), is_correction=False,
)
sent = send_completion_mails(recipient=TEST_EMAIL)
print(f"completion mails enviados: {sent}")

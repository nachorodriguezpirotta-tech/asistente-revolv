"""One-off: notifica al editor (Fran) + admin sobre el material nuevo de Álvaro
Gutiérrez (Video 2/3/4) que se detectó pero no se notificó por el dedupe viejo.
Usa una dedupe_key fresca para no quedar bloqueado. Borrar después.
"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import notifier
from tracker import get_conn
from aliases import get_editor_email_for_notification
from config import TEST_EMAIL
from mail_client import send_mail

CLIENTE = "Álvaro Gutiérrez"
EDITOR = "Fran"
FOLDER = "1uTQgcFudyZIbeQpk8IVmPtmfHS16SguQ"  # Material de Álvaro

conn = get_conn()
rows = conn.execute(
    "SELECT name, size, first_seen_at, subfolder_name FROM known_files "
    "WHERE cliente LIKE '%lvaro%Guti%' AND subfolder_name IN ('Video 2','Video 3','Video 4') "
    "ORDER BY first_seen_at"
).fetchall()
conn.close()

items = [{"task_id": 0, "name": r["name"], "size": r["size"],
          "detected_at": r["first_seen_at"], "subfolder_name": r["subfolder_name"]}
         for r in rows]
print(f"items (clips de Video 2/3/4): {len(items)}")

subject, body_text, body_html = notifier._build_mail(CLIENTE, EDITOR, items, FOLDER)
print(f"subject: {subject}")

editor_email = get_editor_email_for_notification(EDITOR)
dests = [TEST_EMAIL]
if editor_email and editor_email.lower() != TEST_EMAIL.lower():
    dests.append(editor_email)
print(f"destinatarios: {dests}")

for d in dests:
    r = send_mail(to=d, subject=subject, body_text=body_text, body_html=body_html,
                  kind="material", cliente=CLIENTE, editor=EDITOR,
                  dedupe_window_minutes=1200,
                  dedupe_key_override=f"alvaro-video234-recover|{d.lower()}")
    print(f"  -> {d}: {r}")
print("LISTO")

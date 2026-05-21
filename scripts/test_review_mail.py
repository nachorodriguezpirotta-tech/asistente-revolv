"""
Manda un mail de prueba EXACTAMENTE como le llegaría al cliente cuando
se entrega un video, incluyendo los botones de revisión.

Crea un review "de prueba" en la DB (cliente='PRUEBA REVOLV') con un
file_id fake, genera los links REALES (token + endpoint) y manda el mail
a TEST_EMAIL (Ignacio) usando send_client_delivery_mail pero forzando
el destinatario.

Después de mandar, limpia la entry de prueba para no ensuciar la DB.
"""

import os
import sys

# Path al root del repo para imports
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
os.chdir(ROOT)

from tracker import init_db, get_conn, create_client_review, cfg_upsert_client, cfg_delete_client
from notifier import send_client_delivery_mail
from config import TEST_EMAIL


from datetime import datetime
_ts = datetime.now().strftime("%H%M%S")
CLIENTE = "PRUEBA REVOLV"
# file_name con timestamp para que el subject sea único cada vez y el dedupe
# no skipee los tests repetidos. Si dejábamos el nombre fijo, el segundo test
# en <30min se bloqueaba como duplicado.
FILE_NAME = f"Video Prueba {_ts}.mp4"
FILE_ID = f"1FAKE-test-{_ts}"
EDITOR = "Benja"


def main():
    init_db()
    # Setup: agregar cliente de prueba apuntando al mail de Ignacio
    print(f"⚙️  Configurando cliente '{CLIENTE}' → {TEST_EMAIL}")
    cfg_upsert_client(
        cliente=CLIENTE,
        email=TEST_EMAIL,
        display_name="Ignacio",
        notifications_enabled=True,
    )

    # Mandar el mail (eso adentro crea el review pending y los botones)
    print(f"📧 Mandando mail de prueba a {TEST_EMAIL}...")
    ok = send_client_delivery_mail(
        cliente=CLIENTE,
        file_name=FILE_NAME,
        edited_folder_id=None,
        client_folder_id=None,
        file_id=FILE_ID,
        editor=EDITOR,
    )
    if ok:
        print(f"✅ Mail enviado. Revisá tu inbox: {TEST_EMAIL}")
    else:
        print(f"⚠️  send_client_delivery_mail retornó False — algo pasó (cliente sin notif?)")

    # Cleanup OPCIONAL: si quieres conservar el review para probar los botones,
    # NO lo borres. Por default lo dejamos persistir para que los links
    # ✅ Todo perfecto / 📝 Quiero ajustar algo funcionen al click.
    # Setear env var TEST_CLEANUP=1 para limpiar después de enviar.
    if os.environ.get("TEST_CLEANUP", "").strip() == "1":
        conn = get_conn()
        conn.execute("DELETE FROM client_reviews WHERE cliente = ?", (CLIENTE,))
        conn.commit()
        conn.close()
        cfg_delete_client(CLIENTE)
        print(f"🧹 Cleanup OK: cliente y reviews de prueba borrados (TEST_CLEANUP=1)")
    else:
        print(f"💡 Review persistente para que puedas probar los botones del mail.")
        print(f"   Para limpiar después, podés borrar manual:")
        print(f"     - cfg_clients WHERE cliente='{CLIENTE}'")
        print(f"     - client_reviews WHERE cliente='{CLIENTE}'")


if __name__ == "__main__":
    main()

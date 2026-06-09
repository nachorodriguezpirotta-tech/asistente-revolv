"""One-off: manda un mail de prueba de entrega-a-cliente al TEST_EMAIL,
ejercitando el código REAL (send_client_delivery_mail) con un video real,
para verificar que los botones (link al portal + 'todo perfecto') funcionan.

Se dispara por workflow_dispatch (.github/workflows/test-mail.yml) que tiene
los secrets de mail. Borrar script + workflow después de usar.
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import tracker
import notifier

TEST_TO = os.environ.get("TEST_EMAIL") or "nacho.rodriguezpirotta@gmail.com"

# Bypass del gate de config: forzamos que el "cliente de prueba" esté
# notificable y apunte a tu mail. send_client_delivery_mail hace
# `from tracker import cfg_client_should_be_notified` en runtime, así que
# parcheamos el símbolo en el módulo tracker.
tracker.cfg_client_should_be_notified = lambda c: {
    "email": TEST_TO,
    "display_name": "Nacho (PRUEBA)",
}

# Video real (editado de Lili) para que el preview del portal funcione.
FILE_ID = "1lBIeLt1cO9k_sHySqBE6beVIPiohbXpg"

ok = notifier.send_client_delivery_mail(
    cliente="PRUEBA Nacho",
    file_name="Video de PRUEBA.mp4",
    edited_folder_id=None,
    client_folder_id=None,
    file_id=FILE_ID,
    editor="",
)
print("RESULTADO:", "✅ mail enviado a " + TEST_TO if ok else "❌ no se mandó")
sys.exit(0 if ok else 1)

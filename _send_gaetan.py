"""One-off: mandar a Gaetan el delivery mail del Video 17 que no salió por el
bug de matching (config 'Gaetan Jsph' vs carpeta 'Gaetan'). Borrar después."""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from notifier import send_client_delivery_mail
ok = send_client_delivery_mail(
    cliente="Gaetan",
    file_name="Video 17.mp4",
    edited_folder_id=None,
    client_folder_id=None,
    file_id="1uL_4DnTybmjW6-ltGng5s5fQnTWsNX7a",
    editor="Santi",
)
print("RESULTADO:", "✅ mail enviado a Gaetan" if ok else "❌ NO se mandó (revisar)")
sys.exit(0 if ok else 1)

"""
OAuth flow EXCLUSIVO para mandar mails desde una cuenta separada.

Pedir SOLO el scope gmail.send. La cuenta a autorizar es la NUEVA
(asistente.revolv@gmail.com), NO la personal de Ignacio.

Después de correr esto, el token queda en token_mail.json.
El sistema lo usa para mandar mails (vía mail_client.py).
Para Drive/Sheets sigue usando token.json (cuenta personal de Ignacio).
"""

import os
import json

from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from google_auth_oauthlib.flow import InstalledAppFlow

try:
    from config import CLIENT_SECRETS_FILE, BASE_DIR
except ImportError:
    # Vercel: 'config' resuelve a api/config.py (endpoint) → cargar raíz por ruta.
    import os as _os, importlib.util as _ilu
    _p = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "config.py")
    _s = _ilu.spec_from_file_location("_root_config", _p)
    _m = _ilu.module_from_spec(_s)
    _s.loader.exec_module(_m)
    CLIENT_SECRETS_FILE = _m.CLIENT_SECRETS_FILE
    BASE_DIR = _m.BASE_DIR

MAIL_TOKEN_FILE = os.path.join(BASE_DIR, "token_mail.json")
MAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.send"]


def get_mail_credentials():
    """Devuelve credenciales válidas para mandar mails desde la cuenta dedicada."""
    creds = None

    # Cloud (Vercel): leer de env vars
    refresh_token = os.environ.get("MAIL_OAUTH_REFRESH_TOKEN")
    client_id = os.environ.get("MAIL_OAUTH_CLIENT_ID") or os.environ.get("OAUTH_CLIENT_ID")
    client_secret = os.environ.get("MAIL_OAUTH_CLIENT_SECRET") or os.environ.get("OAUTH_CLIENT_SECRET")

    if refresh_token and client_id and client_secret:
        creds = Credentials(
            token=None,
            refresh_token=refresh_token,
            token_uri="https://oauth2.googleapis.com/token",
            client_id=client_id,
            client_secret=client_secret,
            scopes=MAIL_SCOPES,
        )
        creds.refresh(Request())
        return creds

    # Local: leer del archivo
    if os.path.exists(MAIL_TOKEN_FILE):
        with open(MAIL_TOKEN_FILE) as f:
            data = json.load(f)
        creds = Credentials.from_authorized_user_info(data, MAIL_SCOPES)
        if creds.valid:
            return creds
        if creds.expired and creds.refresh_token:
            creds.refresh(Request())
            with open(MAIL_TOKEN_FILE, "w") as f:
                f.write(creds.to_json())
            return creds

    # Primera vez: abrir browser para autorizar
    if not os.path.exists(CLIENT_SECRETS_FILE):
        raise FileNotFoundError(f"Falta {CLIENT_SECRETS_FILE}")

    flow = InstalledAppFlow.from_client_secrets_file(CLIENT_SECRETS_FILE, MAIL_SCOPES)
    creds = flow.run_local_server(port=0)
    with open(MAIL_TOKEN_FILE, "w") as f:
        f.write(creds.to_json())
    print(f"✅ Token guardado en {MAIL_TOKEN_FILE}")
    return creds


if __name__ == "__main__":
    print("📧 AUTORIZACIÓN DE CUENTA DE MAIL DEDICADA")
    print()
    print("⚠️  Va a abrirse el navegador.")
    print("    Iniciá sesión con la CUENTA NUEVA (asistente.revolv@gmail.com)")
    print("    NO con tu cuenta personal.")
    print()
    creds = get_mail_credentials()
    print()
    print("✅ Autorización OK.")
    if os.path.exists(MAIL_TOKEN_FILE):
        with open(MAIL_TOKEN_FILE) as f:
            data = json.load(f)
        print()
        print("📋 Para producción (Vercel + GitHub Secrets), copiá ESTO:")
        print()
        print(f"  MAIL_OAUTH_REFRESH_TOKEN = {data.get('refresh_token')}")
        print()
        print("  (CLIENT_ID y CLIENT_SECRET ya están como secrets, se reusan)")

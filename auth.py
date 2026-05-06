"""
Autorización OAuth de Google.

Modos:
  1. LOCAL primer setup: corre `python3 auth.py`, abre navegador, guarda token.json.
  2. LOCAL recurrente: lee token.json del disco, refresca si vencido.
  3. CLOUD (GitHub Actions): construye credenciales desde env vars
     (OAUTH_REFRESH_TOKEN, OAUTH_CLIENT_ID, OAUTH_CLIENT_SECRET).
     NO necesita token.json en disco.
"""

import os
import json
import logging
from typing import Optional

from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request

from config import CLIENT_SECRETS_FILE, TOKEN_FILE, SCOPES

log = logging.getLogger(__name__)


def _scopes_match(token_scopes, required):
    if not token_scopes:
        return False
    return all(s in set(token_scopes) for s in required)


def _credentials_from_env() -> Optional[Credentials]:
    """Construye Credentials desde env vars (modo cloud)."""
    refresh_token = os.environ.get("OAUTH_REFRESH_TOKEN")
    client_id = os.environ.get("OAUTH_CLIENT_ID")
    client_secret = os.environ.get("OAUTH_CLIENT_SECRET")

    if not (refresh_token and client_id and client_secret):
        return None

    return Credentials(
        token=None,  # access_token se obtiene al primer refresh
        refresh_token=refresh_token,
        token_uri="https://oauth2.googleapis.com/token",
        client_id=client_id,
        client_secret=client_secret,
        scopes=SCOPES,
    )


def get_credentials() -> Credentials:
    """
    Devuelve credenciales válidas. Estrategia:
      1. Si hay env vars OAUTH_* → usar (modo cloud)
      2. Si hay token.json en disco → usar (modo local recurrente)
      3. Si no hay nada → abrir flow OAuth en navegador (modo local primer setup)
    """
    # Modo cloud: env vars
    creds = _credentials_from_env()
    if creds is not None:
        try:
            creds.refresh(Request())
            return creds
        except Exception as e:
            raise RuntimeError(f"Falló refresh con env vars OAuth: {e}") from e

    # Modo local con token.json
    if os.path.exists(TOKEN_FILE):
        try:
            with open(TOKEN_FILE) as f:
                data = json.load(f)
            if _scopes_match(data.get("scopes", []), SCOPES):
                creds = Credentials.from_authorized_user_file(TOKEN_FILE, SCOPES)
                if creds.valid:
                    return creds
                if creds.expired and creds.refresh_token:
                    creds.refresh(Request())
                    with open(TOKEN_FILE, "w") as f:
                        f.write(creds.to_json())
                    return creds
        except Exception as e:
            log.warning(f"No pude usar token.json existente: {e}. Re-autorizando...")

    # Modo local primer setup: abre browser
    if not os.path.exists(CLIENT_SECRETS_FILE):
        raise FileNotFoundError(
            f"No hay credenciales. En cloud, definí OAUTH_REFRESH_TOKEN/CLIENT_ID/CLIENT_SECRET. "
            f"En local, poné {CLIENT_SECRETS_FILE} y corré: python3 auth.py"
        )

    from google_auth_oauthlib.flow import InstalledAppFlow
    flow = InstalledAppFlow.from_client_secrets_file(CLIENT_SECRETS_FILE, SCOPES)
    creds = flow.run_local_server(port=0)
    with open(TOKEN_FILE, "w") as f:
        f.write(creds.to_json())
    print(f"✅ Token guardado en {TOKEN_FILE}")
    return creds


if __name__ == "__main__":
    creds = get_credentials()
    print("✅ Autorización OK. Scopes:")
    for s in creds.scopes or []:
        print(f"  - {s}")
    # Ayudita: imprimir info para configurar GitHub Secrets
    if os.path.exists(TOKEN_FILE):
        with open(TOKEN_FILE) as f:
            data = json.load(f)
        print("\n📋 Para GitHub Actions, copiá ESTOS valores como Secrets:")
        print(f"  OAUTH_REFRESH_TOKEN = {data.get('refresh_token')}")
        print(f"  OAUTH_CLIENT_ID     = {data.get('client_id')}")
        print(f"  OAUTH_CLIENT_SECRET = {data.get('client_secret')}")

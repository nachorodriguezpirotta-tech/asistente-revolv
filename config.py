"""
Asistente Revolv — configuración central.
Lee de env vars con defaults seguros para que funcione local Y en GitHub Actions.
"""

import os

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# OAuth (modo local)
CLIENT_SECRETS_FILE = os.environ.get(
    "CLIENT_SECRETS_FILE",
    os.path.join(BASE_DIR, "client_secrets.json"),
)
TOKEN_FILE = os.environ.get(
    "TOKEN_FILE",
    os.path.join(BASE_DIR, "token.json"),
)

# Scopes requeridos por el sistema.
SCOPES = [
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/spreadsheets.readonly",
    "https://www.googleapis.com/auth/gmail.send",
]

# Sheet de packs
SHEET_ID = os.environ.get(
    "SHEET_ID",
    "1Nkr5P_PDiruONOHwRUsGrt7AKqNO6gTTmrR4YzRB4HY",
)
PACKS_TAB = os.environ.get("PACKS_TAB", "$")
PACKS_HEADER_ROW = int(os.environ.get("PACKS_HEADER_ROW", "4"))

# Extensiones de video válidas (solo formatos de video, NO proyectos de edición)
VIDEO_EXTS = {".mp4", ".mov", ".avi", ".mkv", ".m4v", ".webm", ".mxf"}

# Nombres posibles de la subcarpeta de crudos del cliente
RAW_SUBFOLDER_NAMES = {"material", "raw", "crudos", "material crudo"}

# Mail destino para notificaciones (modo prueba: todo a Ignacio)
TEST_EMAIL = os.environ.get("TEST_EMAIL", "nacho.rodriguezpirotta@gmail.com")

# Path de la DB SQLite
DB_PATH = os.environ.get("DB_PATH", os.path.join(BASE_DIR, "tracker.db"))

"""
Utilidades compartidas para los endpoints de Vercel:
- Auth: tokens por editor
- DB sync: descarga/sube tracker.db de GitHub via API
- Helpers SQLite
"""

import base64
import hashlib
import hmac
import json
import os
import sqlite3
import tempfile
import time
import urllib.error
import urllib.request
from typing import Tuple, Optional, Callable

GITHUB_OWNER = os.environ.get("GITHUB_OWNER", "nachorodriguezpirotta-tech")
GITHUB_REPO = os.environ.get("GITHUB_REPO", "asistente-revolv")
GITHUB_BRANCH = os.environ.get("GITHUB_BRANCH", "main")
GITHUB_PAT = os.environ.get("GITHUB_PAT", "")  # token con permiso repo (SÍ va por env var)

# Secret para firmar tokens HMAC del dashboard. SOLO desde env var — el repo
# es público, no se puede hardcodear. Si la env var no está seteada en Vercel,
# se usa un valor random EFÍMERO por proceso → los tokens generados no
# persisten entre reinicios → dashboard inutilizable, forzando al admin a
# setear DASHBOARD_SECRET en Vercel.
_env_secret = os.environ.get("DASHBOARD_SECRET", "").strip()
if _env_secret:
    DASHBOARD_SECRET = _env_secret
else:
    import secrets as _secrets
    DASHBOARD_SECRET = "ephemeral-" + _secrets.token_urlsafe(24)
    import sys
    print("⚠️  DASHBOARD_SECRET no seteada en env. Usando random efímero — "
          "los tokens no van a funcionar bien hasta setear la env var.",
          file=sys.stderr)

DB_FILE = "tracker.db"

# Editores conocidos. Si quieren agregar uno nuevo, basta con modificar acá
# (o aceptar cualquier nombre — más permisivo, menos seguro).
EDITORS = ["Rami", "Benja", "Fran", "Valen", "Santi", "Agus", "Samu"]


# ────────── AUTH ──────────

def make_token(editor: str) -> str:
    """Token determinístico por editor. URL: ?editor=Rami&t=xxxx"""
    return hmac.new(
        DASHBOARD_SECRET.encode(),
        editor.lower().encode(),
        hashlib.sha256,
    ).hexdigest()[:16]


def check_token(editor: str, token: str) -> bool:
    if not editor or not token:
        return False
    expected = make_token(editor)
    return hmac.compare_digest(expected, token)


# ────────── DB SYNC con GitHub ──────────

def _gh_request(method: str, path: str, body: dict = None) -> dict:
    url = f"https://api.github.com{path}"
    data = json.dumps(body).encode() if body else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Accept", "application/vnd.github+json")
    if GITHUB_PAT:
        req.add_header("Authorization", f"Bearer {GITHUB_PAT}")
    if data:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")
        raise RuntimeError(f"GitHub API {method} {path} → {e.code}: {body[:300]}") from e


def fetch_db() -> Tuple[str, str]:
    """
    Descarga tracker.db del repo. Retorna (path_local_temporal, sha_actual).

    GitHub Contents API solo devuelve content base64 hasta 1MB. Para archivos
    más grandes (la DB pesa ~2MB), hay que usar Accept: application/vnd.github.raw
    que devuelve el archivo binario completo.
    """
    # 1. Obtener sha (metadata)
    meta = _gh_request("GET", f"/repos/{GITHUB_OWNER}/{GITHUB_REPO}/contents/{DB_FILE}?ref={GITHUB_BRANCH}")
    sha = meta["sha"]

    # 2. Descargar contenido crudo (no limitado a 1MB)
    url = f"https://api.github.com/repos/{GITHUB_OWNER}/{GITHUB_REPO}/contents/{DB_FILE}?ref={GITHUB_BRANCH}"
    req = urllib.request.Request(url)
    req.add_header("Accept", "application/vnd.github.raw")
    if GITHUB_PAT:
        req.add_header("Authorization", f"Bearer {GITHUB_PAT}")
    with urllib.request.urlopen(req, timeout=30) as resp:
        raw = resp.read()

    if len(raw) < 1000:
        raise RuntimeError(f"DB descargada parece vacía o truncada ({len(raw)} bytes)")

    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".db")
    tmp.write(raw)
    tmp.close()
    return tmp.name, sha


def push_db(local_path: str, sha: str, message: str) -> dict:
    """Sube tracker.db al repo. Devuelve respuesta de GitHub."""
    with open(local_path, "rb") as f:
        content_b64 = base64.b64encode(f.read()).decode()
    body = {
        "message": message,
        "content": content_b64,
        "sha": sha,
        "branch": GITHUB_BRANCH,
    }
    return _gh_request("PUT", f"/repos/{GITHUB_OWNER}/{GITHUB_REPO}/contents/{DB_FILE}", body)


def with_db(operation, message: str, max_retries: int = 3):
    """
    Wrapper que descarga DB, ejecuta operation(conn), y sube de vuelta.
    Maneja retry si hay conflict de sha (otro pusher modificó entre fetch y push).

    `operation(conn)` debe devolver lo que se quiere retornar al caller (puede ser None).
    """
    last_error = None
    for attempt in range(max_retries):
        local_path, sha = fetch_db()
        try:
            conn = sqlite3.connect(local_path)
            conn.row_factory = sqlite3.Row
            try:
                result = operation(conn)
                conn.commit()
            finally:
                conn.close()

            push_db(local_path, sha, message)
            return result
        except RuntimeError as e:
            err_str = str(e)
            if "409" in err_str or "sha" in err_str.lower():
                # Conflict: alguien más pusheó. Retry desde fetch.
                last_error = e
                time.sleep(0.5 * (attempt + 1))
                continue
            raise
        finally:
            try:
                os.unlink(local_path)
            except Exception:
                pass

    raise RuntimeError(f"Falló tras {max_retries} retries: {last_error}")


def read_db(query_fn):
    """Solo lectura (no necesita push). `query_fn(conn)` devuelve datos."""
    local_path, _ = fetch_db()
    try:
        conn = sqlite3.connect(local_path)
        conn.row_factory = sqlite3.Row
        try:
            return query_fn(conn)
        finally:
            conn.close()
    finally:
        try:
            os.unlink(local_path)
        except Exception:
            pass


# ────────── Helpers HTTP ──────────

def json_response(handler, data: dict, status: int = 200):
    """Envía respuesta JSON desde un BaseHTTPRequestHandler."""
    body = json.dumps(data).encode()
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Access-Control-Allow-Origin", "*")
    handler.send_header("Access-Control-Allow-Methods", "GET, POST, DELETE, OPTIONS")
    handler.send_header("Access-Control-Allow-Headers", "Content-Type")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def now_iso() -> str:
    from datetime import datetime
    return datetime.now().isoformat(timespec="seconds")

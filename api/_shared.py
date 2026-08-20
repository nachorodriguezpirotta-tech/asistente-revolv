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
    # CI (GitHub Actions) sin secret → ABORT ruidosamente.
    # El cron firma tokens de cliente para los mails: sin el secret real esos
    # tokens quedan muertos y los clientes ven "Link inválido" / 401. Es mejor
    # romper el workflow y alertar que mandar mails con tokens fantasma.
    if os.environ.get("GITHUB_ACTIONS") == "true":
        raise RuntimeError(
            "DASHBOARD_SECRET no está seteada en este workflow. "
            "Agregá `DASHBOARD_SECRET: ${{ secrets.DASHBOARD_SECRET }}` "
            "al bloque `env:` del job, sino los tokens de cliente que firme "
            "este cron van a quedar muertos."
        )
    # En runtime serverless (Vercel) solo se VALIDAN tokens — un efímero acá
    # rompe la validación de TODOS los tokens, lo cual es visible y recuperable
    # seteando la env var. No causa el bug silencioso de tokens muertos.
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


def rate_limited(handler_self, who: str, limit: int = 60, window_s: int = 60) -> bool:
    """Si `who` superó el límite, responde 429 y devuelve True (cortar el handler).
    `who` = editor/admin del token → el límite es POR PERSONA, así un editor
    que refresca de más no afecta a los demás ni tumba los scans."""
    try:
        import sys as _s, os as _o
        _r = _o.path.dirname(_o.path.dirname(_o.path.abspath(__file__)))
        if _r not in _s.path:
            _s.path.insert(0, _r)
        import tasks_store
        if not tasks_store.available():
            return False
        if tasks_store.rate_limit_hit(f"rl:{who}", limit=limit, window_s=window_s):
            json_response(handler_self, {
                "error": "Demasiadas peticiones seguidas. Esperá unos segundos y reintentá.",
                "retry_after_s": window_s,
            }, status=429)
            return True
    except Exception:
        pass
    return False


def make_attachment_token(att_id) -> str:
    """Token de ALCANCE MÍNIMO: sirve SOLO para ver ESA imagen adjunta.

    Por qué (27/jul): los mails de "revisión pedida" con fotos incrustaban el
    TOKEN ADMIN en el link de cada imagen — y ese mail va a Ignacio Y al editor
    asignado. O sea, la llave maestra del panel viajaba por correo: quien tuviera
    ese mail (o lo reenviara) entraba a TODO el dashboard. Ahora cada imagen lleva
    su propio token, que no abre nada más.
    """
    return hmac.new(
        DASHBOARD_SECRET.encode(),
        f"att:{att_id}".encode(),
        hashlib.sha256,
    ).hexdigest()[:16]


def check_attachment_token(att_id, token: str) -> bool:
    if not token:
        return False
    return hmac.compare_digest(make_attachment_token(att_id), token)


def make_client_token(cliente: str) -> str:
    """Token determinístico para el cliente (distinto namespace que editores).
    Prefijo 'client:' para que no colisione con 'rami', 'admin', etc.
    URL: /revision?c=Cliente&t=xxxx"""
    return hmac.new(
        DASHBOARD_SECRET.encode(),
        f"client:{cliente.lower().strip()}".encode(),
        hashlib.sha256,
    ).hexdigest()[:16]


def check_client_token(cliente: str, token: str) -> bool:
    if not cliente or not token:
        return False
    expected = make_client_token(cliente)
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


_DB_CACHE = {"path": None, "sha": None, "ts": 0.0}
_FRESH = {"on": False}


def pedir_frescos():
    """La próxima lectura de este proceso salta el cache y re-espeja."""
    _FRESH["on"] = True
_DB_CACHE_TTL = 25  # segundos


def fetch_db() -> Tuple[str, str]:
    """
    Descarga tracker.db del repo. Retorna (path_local_temporal, sha_actual).

    GitHub Contents API solo devuelve content base64 hasta 1MB. Para archivos
    más grandes (la DB pesa ~2MB), hay que usar Accept: application/vnd.github.raw
    que devuelve el archivo binario completo.
    """
    # CACHE (27/jul): cada request bajaba 6.7 MB y gastaba 2 llamadas a la API de
    # GitHub, cuya cuota (5000/h) se COMPARTE con los scans. Un editor refrescando
    # seguido podía agotarla y tumbar el sistema entero (sin detección ni mails).
    # Con 25s de cache, refrescar 20 veces seguidas cuesta 1 descarga, no 20.
    # No afecta la frescura real: las tablas que importan (pendientes, config) se
    # espejan desde Turso en cada llamada, más abajo.
    import time as _t
    _now = _t.time()
    if _FRESH.get("on"):
        # Pedido explícito de datos frescos (viene de una pantalla que acaba de
        # guardar algo): saltar el cache y re-espejar, para que el cambio se vea
        # al instante en vez de esperar hasta 25s.
        _FRESH["on"] = False
    elif (_DB_CACHE["path"] and _now - _DB_CACHE["ts"] < _DB_CACHE_TTL
            and os.path.exists(_DB_CACHE["path"])):
        try:
            _copy = tempfile.NamedTemporaryFile(delete=False, suffix=".db")
            with open(_DB_CACHE["path"], "rb") as _src_f:
                _copy.write(_src_f.read())
            _copy.close()
            _mirror_hot_tables(_copy.name)
            return _copy.name, _DB_CACHE["sha"]
        except Exception:
            pass

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

    # Guardar en cache la copia recién bajada (antes del espejo, para que cada
    # request espeje con datos frescos de Turso sobre la base cacheada).
    try:
        _cache_f = tempfile.NamedTemporaryFile(delete=False, suffix=".cache.db")
        with open(tmp.name, "rb") as _s_f:
            _cache_f.write(_s_f.read())
        _cache_f.close()
        _old = _DB_CACHE.get("path")
        _DB_CACHE.update({"path": _cache_f.name, "sha": sha, "ts": _t.time()})
        if _old and _old != _cache_f.name:
            try:
                os.unlink(_old)
            except Exception:
                pass
    except Exception:
        pass

    _mirror_hot_tables(tmp.name)
    return tmp.name, sha


def _mirror_hot_tables(db_path: str) -> None:
    """Refresca las tablas CALIENTES desde Turso sobre la copia local, para que
    TODOS los SELECT legacy (data/stats/config/reviews) vean el estado real."""
    try:
        import sys as _sys, os as _os
        _root = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
        if _root not in _sys.path:
            _sys.path.insert(0, _root)
        import tasks_store
        if tasks_store.available():
            _mc = sqlite3.connect(db_path)
            try:
                tasks_store.mirror_to_sqlite(_mc)
            finally:
                _mc.close()
    except Exception as _e:
        print(f"   ⚠️ mirror tasks_store: {_e}")


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


def with_db(operation, message: str, max_retries: int = 8, verify=None):
    """
    Wrapper que descarga DB, ejecuta operation(conn), y sube de vuelta.
    Maneja retry si hay conflict de sha (otro pusher modificó entre fetch y push).

    `operation(conn)` debe devolver lo que se quiere retornar al caller.

    `verify(conn) -> bool` (opcional): después de pushear, re-descarga la DB
    del repo y corre verify(conn). Si devuelve False, significa que OTRO push
    (ej. un scan con git rebase) pisó el cambio → reintenta toda la operación.
    Esto garantiza que el guardado del usuario PERSISTE de verdad, no solo que
    el push respondió 200. Pedido Ignacio 05/jun: "todo lo que hago se tiene
    que guardar bien".

    max_retries subido a 8 (era 3) con backoff exponencial para alta
    concurrencia con los scans (cada 2 min).
    """
    import random
    last_error = None
    for attempt in range(max_retries):
        local_path, sha = fetch_db()
        try:
            conn = sqlite3.connect(local_path)
            conn.row_factory = sqlite3.Row
            try:
                result = operation(conn)
                conn.commit()
                # DURABILIDAD (26/jul): empujar las tablas del PANEL a Turso ANTES
                # del push a git. Antes, si el push de tracker.db se pisaba con un
                # scan, el cambio del dashboard se perdía → "borro/cambio algo y no
                # se guarda". Turso es transaccional: lo que se guarda acá queda,
                # aunque git falle. El espejo de fetch_db lo devuelve en la próxima
                # lectura.
                try:
                    import sys as _s, os as _o
                    _r = _o.path.dirname(_o.path.dirname(_o.path.abspath(__file__)))
                    if _r not in _s.path:
                        _s.path.insert(0, _r)
                    import tasks_store as _ts
                    if _ts.available():
                        _ts.push_tables_from_sqlite(conn)
                except Exception as _e:
                    print(f"   ⚠️ durabilidad Turso: {_e}")
            finally:
                conn.close()

            push_db(local_path, sha, message)
        except RuntimeError as e:
            err_str = str(e)
            if "409" in err_str or "422" in err_str or "sha" in err_str.lower():
                # Conflict: alguien más pusheó. Retry desde fetch.
                last_error = e
                time.sleep(min(0.5 * (2 ** attempt) + random.random(), 8))
                continue
            raise
        finally:
            try:
                os.unlink(local_path)
            except Exception:
                pass

        # Push OK. Si hay verify, confirmar que el cambio PERSISTIÓ (no fue
        # pisado por un scan que pusheó justo después con git rebase).
        if verify is None:
            return result
        time.sleep(1.5)  # darle tiempo a que un push concurrente se asiente
        vpath = None
        try:
            vpath, _ = fetch_db()
            vconn = sqlite3.connect(vpath)
            vconn.row_factory = sqlite3.Row
            try:
                ok = bool(verify(vconn))
            finally:
                vconn.close()
        except Exception:
            ok = True  # si la verificación falla por red, asumir OK (ya pusheamos)
        finally:
            if vpath:
                try:
                    os.unlink(vpath)
                except Exception:
                    pass
        if ok:
            return result
        # El cambio fue pisado → reintentar toda la operación
        last_error = RuntimeError("cambio pisado por push concurrente")
        time.sleep(min(0.5 * (2 ** attempt) + random.random(), 8))

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

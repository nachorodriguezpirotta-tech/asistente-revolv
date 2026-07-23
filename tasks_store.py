"""Tablas CALIENTES del dashboard en Turso — fuente de verdad transaccional.

Problema que resuelve (jul/2026): las mutaciones del dashboard (agregar pendiente,
sumar videos, borrar cliente) iban por with_db: bajar tracker.db (6MB) + mutar +
subir + verify → 5-8 segundos por guardado y pérdidas cuando un push concurrente
(scan cada 2 min) pisaba el cambio. Turso es la misma DB que ya usa el dedupe de
mails: transaccional, ~200ms por operación, sin pisadas posibles.

Diseño:
  - ESCRITURAS de tasks / client_blocks / editor_progress / cfg_delivery_priority
    van SIEMPRE acá (fila a fila, atómicas). Ninguna escritura de estas tablas
    debe quedar sobre la conn sqlite — se perdería en el próximo espejo.
  - LECTURAS: el resto del código (68 SELECT dispersos) sigue leyendo la conn
    sqlite de siempre. Para que vean datos frescos, mirror_to_sqlite(conn) pisa
    esas 4 tablas locales con el contenido de Turso:
      * Vercel: _shared.fetch_db() lo llama tras bajar la DB (cada request).
      * GHA: tracker.init_db() lo llama al inicio de cada scan.
  - Si Turso no responde: las lecturas quedan con el espejo anterior (stale pero
    funcional) y las escrituras fallan VISIBLE (pill roja en dashboard / log del
    scan). Nunca escribir en sqlite como fallback: partiría la fuente de verdad.
"""
import os
import json
import time
import urllib.request

HOT_TABLES = ("tasks", "client_blocks", "editor_progress", "cfg_delivery_priority", "cfg_client_editor", "cfg_clients", "cfg_editor_extra_emails")

_SCHEMA = [
    """CREATE TABLE IF NOT EXISTS tasks (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        cliente TEXT NOT NULL,
        editor TEXT,
        file_id TEXT,
        file_name TEXT,
        detected_at TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'pending',
        mail_sent_at TEXT,
        completed_at TEXT,
        completed_by_file_id TEXT,
        pending_count INTEGER NOT NULL DEFAULT 1,
        count_locked INTEGER NOT NULL DEFAULT 0,
        note TEXT,
        urgent INTEGER NOT NULL DEFAULT 0)""",
    "CREATE INDEX IF NOT EXISTS idx_t_status ON tasks(status)",
    "CREATE INDEX IF NOT EXISTS idx_t_cliente ON tasks(cliente)",
    """CREATE TABLE IF NOT EXISTS client_blocks (
        cliente TEXT NOT NULL,
        editor TEXT NOT NULL DEFAULT '',
        blocked_until TEXT NOT NULL,
        PRIMARY KEY (cliente, editor))""",
    """CREATE TABLE IF NOT EXISTS editor_progress (
        editor TEXT NOT NULL,
        label TEXT NOT NULL,
        current INTEGER NOT NULL DEFAULT 0,
        total INTEGER NOT NULL DEFAULT 0,
        updated_at TEXT,
        PRIMARY KEY (editor, label))""",
    """CREATE TABLE IF NOT EXISTS cfg_delivery_priority (
        editor TEXT NOT NULL,
        cliente TEXT NOT NULL,
        priority INTEGER NOT NULL,
        updated_at TEXT,
        PRIMARY KEY (editor, cliente))""",
    """CREATE TABLE IF NOT EXISTS cfg_client_editor (
        cliente TEXT PRIMARY KEY,
        editor TEXT NOT NULL,
        updated_at TEXT)""",
    """CREATE TABLE IF NOT EXISTS cfg_clients (
        cliente TEXT PRIMARY KEY,
        email TEXT NOT NULL,
        display_name TEXT,
        notifications_enabled INTEGER NOT NULL DEFAULT 1,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS cfg_editor_extra_emails (
        email TEXT PRIMARY KEY,
        editor TEXT NOT NULL)""",
]

_SCHEMA_READY = False


def _cfg():
    url = os.environ.get("TURSO_DATABASE_URL", "").strip().replace("libsql://", "https://")
    token = os.environ.get("TURSO_AUTH_TOKEN", "").strip()
    return url, token


def available() -> bool:
    url, token = _cfg()
    return bool(url and token)


def _pipeline(stmts, timeout=12):
    """Ejecuta una lista de (sql, args) en un solo request. Devuelve la lista de
    results crudos de libsql. Lanza en error de red o de SQL."""
    url, token = _cfg()
    if not url or not token:
        raise RuntimeError("Turso no configurado (TURSO_DATABASE_URL/AUTH_TOKEN)")
    reqs = []
    for sql, args in stmts:
        stmt = {"sql": sql}
        if args:
            stmt["args"] = [
                ({"type": "null"} if a is None
                 else {"type": "integer", "value": str(a)} if isinstance(a, int)
                 else {"type": "float", "value": a} if isinstance(a, float)
                 else {"type": "text", "value": str(a)})
                for a in args
            ]
        reqs.append({"type": "execute", "stmt": stmt})
    reqs.append({"type": "close"})
    body = json.dumps({"requests": reqs}).encode()
    req = urllib.request.Request(
        url + "/v2/pipeline", data=body,
        headers={"Authorization": f"Bearer {token}",
                 "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        results = json.load(resp)["results"]
    out = []
    for r in results[:-1]:  # sin el close
        if r["type"] == "error":
            raise RuntimeError(r["error"]["message"])
        out.append(r["response"].get("result") or {})
    return out


def _rows_to_dicts(result):
    cols = [c["name"] for c in result.get("cols", [])]
    rows = []
    for raw in result.get("rows", []):
        vals = []
        for cell in raw:
            v = cell.get("value")
            if cell.get("type") == "integer" and v is not None:
                v = int(v)
            elif cell.get("type") == "float" and v is not None:
                v = float(v)
            elif cell.get("type") == "null":
                v = None
            vals.append(v)
        rows.append(dict(zip(cols, vals)))
    return rows


def ensure_schema():
    global _SCHEMA_READY
    if _SCHEMA_READY:
        return
    _pipeline([(s, None) for s in _SCHEMA])
    _SCHEMA_READY = True


def query(sql, args=None):
    """SELECT → list[dict]."""
    ensure_schema()
    res = _pipeline([(sql, list(args) if args else None)])
    return _rows_to_dicts(res[0])


def execute(sql, args=None):
    """INSERT/UPDATE/DELETE → {'affected': n, 'last_id': id|None}."""
    ensure_schema()
    res = _pipeline([(sql, list(args) if args else None)])[0]
    last = res.get("last_insert_rowid")
    return {"affected": res.get("affected_row_count", 0),
            "last_id": int(last) if last is not None else None}


def execute_many(stmts):
    """Varias (sql, args) en un request (secuencial, misma conexión)."""
    ensure_schema()
    return _pipeline([(s, list(a) if a else None) for s, a in stmts])


def mirror_to_sqlite(conn, tables=HOT_TABLES):
    """Pisa las tablas calientes de la conn sqlite local con el contenido de
    Turso, para que los SELECT legacy vean datos frescos. Best-effort: si Turso
    no responde, deja lo que había (stale) y devuelve False."""
    try:
        ensure_schema()
        res = _pipeline([(f"SELECT * FROM {t}", None) for t in tables], timeout=10)
    except Exception as e:
        print(f"   ⚠️ mirror Turso no disponible ({str(e)[:80]}) — uso copia local")
        return False
    for t, r in zip(tables, res):
        rows = _rows_to_dicts(r)
        try:
            cur = conn.execute(f"SELECT * FROM {t} LIMIT 0")
            local_cols = [d[0] for d in cur.description]
        except Exception:
            # La tabla local no existe (tabla NUEVA, ej. cfg_editor_extra_emails
            # 23/jul: el espejo la salteaba → los scans no veían las cuentas
            # secundarias). Crearla con las columnas de Turso y seguir.
            cols_t = [c["name"] for c in r.get("cols", [])]
            if not cols_t:
                continue
            try:
                conn.execute(f"CREATE TABLE IF NOT EXISTS {t} ({', '.join(c + ' TEXT' for c in cols_t)})")
                local_cols = cols_t
            except Exception:
                continue
        conn.execute(f"DELETE FROM {t}")
        if rows:
            cols = [c for c in rows[0].keys() if c in local_cols]
            ph = ",".join("?" * len(cols))
            collist = ",".join(cols)
            conn.executemany(
                f"INSERT OR REPLACE INTO {t} ({collist}) VALUES ({ph})",
                [[row.get(c) for c in cols] for row in rows])
    conn.commit()
    return True


def seed_from_sqlite(conn, tables=HOT_TABLES):
    """Migración one-shot: copia las tablas calientes del sqlite actual a Turso
    (borra lo que hubiera en Turso). Usar UNA vez al hacer el switch."""
    ensure_schema()
    stmts = []
    for t in tables:
        stmts.append((f"DELETE FROM {t}", None))
        cur = conn.execute(f"SELECT * FROM {t}")
        cols = [d[0] for d in cur.description]
        ph = ",".join("?" * len(cols))
        collist = ",".join(cols)
        for row in cur.fetchall():
            stmts.append((f"INSERT INTO {t} ({collist}) VALUES ({ph})", list(row)))
    _pipeline(stmts, timeout=30)
    return True

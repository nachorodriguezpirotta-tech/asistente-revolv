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

HOT_TABLES = ("tasks", "client_blocks", "editor_progress", "cfg_delivery_priority",
              "cfg_client_editor", "cfg_clients", "cfg_editor_extra_emails",
              # Tablas del PANEL DE CONFIGURACIÓN + correcciones (26/jul): antes
              # iban por with_db (git) y se perdían al pisarse — "borro algo y no
              # se guarda". Chicas (12-79 filas) → replace completo es barato.
              "cfg_editors", "cfg_nicknames", "cfg_aliases", "cfg_delivery_folders",
              "cfg_archived_clients", "pending_drive_folders", "client_reviews")

# Las que el dashboard muta vía with_db y hay que empujar a Turso tras cada
# mutación (las otras ya se escriben directo con tasks_store.execute).
PUSH_AFTER_WITH_DB = ("cfg_editors", "cfg_nicknames", "cfg_aliases",
                      "cfg_delivery_folders", "cfg_archived_clients",
                      "pending_drive_folders", "client_reviews")

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
    """CREATE TABLE IF NOT EXISTS rate_limit (
        k TEXT PRIMARY KEY,
        window_start INTEGER NOT NULL,
        n INTEGER NOT NULL DEFAULT 0)""",
    """CREATE TABLE IF NOT EXISTS client_deletions (
        cliente TEXT PRIMARY KEY,
        deleted_at TEXT NOT NULL)""",
]

_SCHEMA_READY = False


def _cfg_d1():
    """Cloudflare D1 (ago/2026): reemplaza a Turso, que bloqueó las lecturas al
    agotarse la cuota del plan gratis. Mismo SQLite, así que NINGUNA consulta del
    sistema cambió — sólo esta capa de transporte."""
    acc = os.environ.get("CF_ACCOUNT_ID", "").strip()
    token = os.environ.get("CF_API_TOKEN", "").strip()
    db = os.environ.get("CF_D1_DATABASE_ID", "").strip()
    if acc and token and db:
        return (f"https://api.cloudflare.com/client/v4/accounts/{acc}"
                f"/d1/database/{db}/query"), token
    return "", ""


def _cfg():
    """Respaldo: Turso. Se conserva para poder volver atrás sin tocar código."""
    url = os.environ.get("TURSO_DATABASE_URL", "").strip().replace("libsql://", "https://")
    token = os.environ.get("TURSO_AUTH_TOKEN", "").strip()
    return url, token


_BLOCKED_UNTIL = 0.0          # epoch hasta el cual consideramos Turso caído
_BLOCK_COOLDOWN = 300         # 5 min sin reintentar tras un bloqueo


def _note_blocked(msg: str) -> bool:
    """Si el error es un bloqueo de plan/cuota, marcar Turso como NO disponible
    por un rato para que TODO el sistema caiga solo al camino sqlite/git (12/ago:
    Turso free bloqueó las lecturas y el dashboard no dejaba agregar pendientes).
    Devuelve True si era un bloqueo."""
    global _BLOCKED_UNTIL
    m = (msg or "").lower()
    if "blocked" in m or "forbidden" in m or "upgrade your plan" in m or "quota" in m:
        import time as _t
        _BLOCKED_UNTIL = _t.time() + _BLOCK_COOLDOWN
        return True
    return False


def health_ok() -> bool:
    """¿Turso sirve para LEER? (12/ago: el plan bloqueó las lecturas pero seguía
    aceptando escrituras — el peor escenario: escribías y no lo veías, y los datos
    quedaban partidos entre Turso y la copia local). Un ping barato marca el
    bloqueo para que TODO el request use un solo origen, no dos.
    El resultado se cachea vía _BLOCKED_UNTIL (5 min), así no cuesta por llamada."""
    if is_blocked():
        return False
    try:
        _pipeline([("SELECT 1", None)], timeout=8)
        return True
    except Exception as e:
        _note_blocked(str(e))
        return not is_blocked()


def is_blocked() -> bool:
    import time as _t
    return _t.time() < _BLOCKED_UNTIL


def available() -> bool:
    if is_blocked():
        return False
    u1, t1 = _cfg_d1()
    if u1 and t1:
        return True
    url, token = _cfg()
    return bool(url and token)


def _sql_literal(v):
    """Valor SQL inline. D1 topea en 100 parámetros por consulta; cuando se pasa,
    los valores van escritos en el SQL."""
    if v is None:
        return "NULL"
    if isinstance(v, bool):
        return "1" if v else "0"
    if isinstance(v, (int, float)):
        return str(v)
    if isinstance(v, bytes):
        return "X'" + v.hex() + "'"
    return "'" + str(v).replace("'", "''") + "'"


def _pipeline_d1(stmts, timeout=12):
    """Misma firma que el pipeline de libsql, hablando con D1.

    Optimización: si ningún statement lleva parámetros, van TODOS en un solo
    request separados por ';' (D1 devuelve un bloque por consulta) — así el
    espejo de 14 tablas sigue costando 1 sola llamada, como antes."""
    url, token = _cfg_d1()

    def _post(body):
        req = urllib.request.Request(
            url, data=json.dumps(body).encode(),
            headers={"Authorization": f"Bearer {token}",
                     "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            out = json.load(resp)
        if not out.get("success"):
            msg = str(out.get("errors"))[:300]
            _note_blocked(msg)
            raise RuntimeError(msg)
        return out.get("result", [])

    def _conv(block):
        rows = block.get("results") or []
        meta = block.get("meta") or {}
        return {"_d1_rows": rows,
                "affected_row_count": meta.get("changes", 0) or 0,
                "last_insert_rowid": meta.get("last_row_id")}

    sin_args = all(not a for _, a in stmts)
    if sin_args and len(stmts) > 1:
        sql = ";\n".join(s.rstrip().rstrip(";") for s, _ in stmts)
        blocks = _post({"sql": sql})
        if len(blocks) == len(stmts):
            return [_conv(b) for b in blocks]
        # D1 no devolvió un bloque por consulta: caer a una por una
    out = []
    for sql, args in stmts:
        if args:
            vals = list(args)
            if len(vals) > 90:      # límite de parámetros de D1
                inline = sql
                for v in vals:
                    inline = inline.replace("?", _sql_literal(v), 1)
                blocks = _post({"sql": inline})
            else:
                blocks = _post({"sql": sql, "params": [
                    (v if isinstance(v, (int, float)) and not isinstance(v, bool)
                     else None if v is None else str(v)) for v in vals]})
        else:
            blocks = _post({"sql": sql})
        out.append(_conv(blocks[0]) if blocks else {"_d1_rows": [], "affected_row_count": 0, "last_insert_rowid": None})
    return out


def _pipeline(stmts, timeout=12):
    """Ejecuta una lista de (sql, args) en un solo request. Devuelve la lista de
    results crudos. Lanza en error de red o de SQL."""
    u1, t1 = _cfg_d1()
    if u1 and t1:
        return _pipeline_d1(stmts, timeout)
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
            _msg = r["error"]["message"]
            if _note_blocked(_msg):
                print(f"   🚧 Turso BLOQUEADO por plan/cuota → el sistema usa la "
                      f"copia local (sqlite/git) por {_BLOCK_COOLDOWN//60} min")
            raise RuntimeError(_msg)
        out.append(r["response"].get("result") or {})
    return out


def _rows_to_dicts(result):
    if "_d1_rows" in result:          # D1 ya devuelve filas como diccionarios
        return result["_d1_rows"]
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


_LAST_MIRROR = {"ts": 0.0}
MIRROR_MIN_INTERVAL = 20      # segundos entre espejos en la MISMA instancia


def mirror_to_sqlite(conn, tables=HOT_TABLES, force: bool = False):
    """Pisa las tablas calientes de la conn sqlite local con el contenido de
    Turso, para que los SELECT legacy vean datos frescos. Best-effort: si Turso
    no responde, deja lo que había (stale) y devuelve False."""
    # AHORRO (12/ago): el espejo leía las 14 tablas (858 filas) en CADA request y
    # CADA scan → decenas de millones de lecturas/mes, que agotaron la cuota del
    # plan. Dentro de una misma instancia caliente, 20s de gracia entre espejos.
    import time as _t
    if not force and (_t.time() - _LAST_MIRROR["ts"]) < MIRROR_MIN_INTERVAL:
        return True
    _LAST_MIRROR["ts"] = _t.time()
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
        # GUARDARRAIL (12/ago): NUNCA vaciar la copia local con un origen vacío.
        # Si la consulta a Turso devuelve 0 filas por un error transitorio (o un
        # resultado parcial del pipeline), el DELETE borraba la tabla local y el
        # scan pusheaba ese vacío a git → pérdida real de datos. Caso: los 12
        # editores del dashboard quedaron en 2. Ante la duda, NO se borra.
        if not rows:
            try:
                _local_n = conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            except Exception:
                _local_n = 0
            if _local_n:
                print(f"   ⚠️ espejo: {t} vino vacía de Turso pero local tiene "
                      f"{_local_n} filas → NO se borra")
                continue
        # Tablas que el SCAN escribe: espejo por UPSERT (sin DELETE). Si el scan
        # acaba de crear una review/carpeta y todavía no la subió a Turso, un
        # replace la borraría de la copia local → se perdería. Las demás (config
        # pura, que solo escribe el dashboard) sí se reemplazan completas.
        if t not in ("client_reviews", "pending_drive_folders"):
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


def rate_limit_hit(key: str, limit: int = 60, window_s: int = 60) -> bool:
    """Cuenta un uso y devuelve True si SUPERÓ el límite (hay que rechazar).

    Por qué (27/jul): no había ningún tope — un editor refrescando (o una pestaña
    recargando sola) podía agotar la cuota de la API de GitHub, que se COMPARTE
    con los scans, y tumbar el sistema entero (sin detección ni mails hasta el
    reset horario). Contador por ventana en Turso: sirve entre instancias
    serverless (la memoria local no, cada request puede caer en otra máquina).

    FAIL-OPEN: si Turso no responde, NO bloquea (mejor dejar pasar que dejar a
    un editor afuera por un problema nuestro).
    """
    import time as _t
    now = int(_t.time())
    win = now - (now % window_s)
    try:
        ensure_schema()
        res = _pipeline([
            ("INSERT INTO rate_limit (k, window_start, n) VALUES (?, ?, 1) "
             "ON CONFLICT(k) DO UPDATE SET "
             "  n = CASE WHEN rate_limit.window_start = ? THEN rate_limit.n + 1 ELSE 1 END, "
             "  window_start = ?", [key, win, win, win]),
            ("SELECT n FROM rate_limit WHERE k = ?", [key]),
        ], timeout=6)
        rows = _rows_to_dicts(res[1])
        return bool(rows) and int(rows[0]["n"] or 0) > limit
    except Exception:
        return False  # fail-open


def push_tables_from_sqlite(conn, tables=None) -> bool:
    """Empuja el contenido de `tables` de la conn sqlite → Turso (replace completo).
    Se llama al final de with_db: la copia sqlite viene de fetch_db (que YA espejó
    Turso), se le aplicó la mutación, y acá el resultado vuelve a Turso. Así las
    escrituras del panel de configuración dejan de perderse cuando un push de git
    se pisa. Tablas chicas → 1 request. Best-effort: si Turso falla, el dato igual
    quedó en git (no se pierde, solo no gana durabilidad extra)."""
    tables = tables or PUSH_AFTER_WITH_DB
    try:
        ensure_schema()
        stmts = []
        for t in tables:
            try:
                cur = conn.execute(f"SELECT * FROM {t}")
                cols = [d[0] for d in cur.description]
                rows = cur.fetchall()
            except Exception:
                continue  # tabla no existe localmente
            # GUARDARRAIL (12/ago): el replace completo (DELETE+INSERT) solo es
            # seguro si la copia local está COMPLETA. Si tiene menos de la mitad
            # de lo que hay en Turso, es una copia degradada (espejo fallido,
            # bundle viejo): reemplazar destruiría datos buenos. Se hace upsert
            # sin borrar y se avisa.
            _safe_replace = True
            try:
                _remote = query(f"SELECT COUNT(*) AS n FROM {t}")
                _rn = int(_remote[0]["n"]) if _remote else 0
                if _rn and len(rows) * 2 < _rn:
                    _safe_replace = False
                    print(f"   ⚠️ push: {t} local={len(rows)} vs Turso={_rn} — "
                          f"copia incompleta, NO se reemplaza (solo upsert)")
            except Exception:
                pass
            ph = ",".join("?" * len(cols))
            collist = ",".join(cols)
            if _safe_replace:
                stmts.append((f"DELETE FROM {t}", None))
                for row in rows:
                    stmts.append((f"INSERT INTO {t} ({collist}) VALUES ({ph})", list(row)))
            else:
                for row in rows:
                    stmts.append((f"INSERT OR REPLACE INTO {t} ({collist}) VALUES ({ph})", list(row)))
        if stmts:
            _pipeline(stmts, timeout=30)
        return True
    except Exception as e:
        print(f"   ⚠️ push_tables_from_sqlite: {str(e)[:90]}")
        return False


_LAST_UPSERT_SIG = {}


def upsert_tables_from_sqlite(conn, tables) -> bool:
    """Empuja filas de sqlite → Turso con INSERT OR REPLACE (SIN borrar).
    Para el SCAN: escribe lo suyo (reviews nuevas, carpetas detectadas) sin pisar
    lo que el panel de configuración pudo cambiar mientras tanto. El dashboard usa
    push_tables_from_sqlite (replace completo) porque parte de una copia fresca."""
    try:
        ensure_schema()
        stmts = []
        for t in tables:
            try:
                cur = conn.execute(f"SELECT * FROM {t}")
                cols = [d[0] for d in cur.description]
                rows = cur.fetchall()
            except Exception:
                continue
            if not rows:
                continue
            # AHORRO (12/ago): antes se reescribían las ~200 filas en CADA scan
            # (cada 2 min) aunque nada hubiera cambiado → ~4M escrituras/mes de
            # puro derroche. Ahora se sube solo si el contenido cambió.
            import hashlib as _h
            _sig = _h.md5(repr(rows).encode()).hexdigest()
            if _LAST_UPSERT_SIG.get(t) == _sig:
                continue
            _LAST_UPSERT_SIG[t] = _sig
            ph = ",".join("?" * len(cols))
            collist = ",".join(cols)
            for row in rows:
                stmts.append((f"INSERT OR REPLACE INTO {t} ({collist}) VALUES ({ph})", list(row)))
        if stmts:
            _pipeline(stmts, timeout=40)
        return True
    except Exception as e:
        print(f"   ⚠️ upsert_tables_from_sqlite: {str(e)[:90]}")
        return False


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

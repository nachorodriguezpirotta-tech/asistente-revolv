"""Dedupe ATÓMICO de mails en Turso — independiente de git.

Problema que resuelve: tracker.db vive en git (no transaccional). Con 3 procesos
escribiendo a la vez (scan, audit, dashboard), los pushes chocan y el mail_log
se pisa → mails duplicados o reenviados. Esta capa pone el "ya lo mandé" en una
base REAL (Turso, la misma del portal) con un claim atómico por UNIQUE key.

Flujo: antes de mandar un mail, claim_mail() intenta reclamar la clave de dedupe.
  - 'send'        → es nuevo (o fuera de ventana) → MANDAR
  - 'duplicate'   → ya se mandó dentro de la ventana → NO mandar
  - 'unavailable' → Turso no responde → el caller cae al mail_log (git) de respaldo

El claim es 1 solo statement (UPSERT + WHERE de ventana + RETURNING), así que dos
procesos simultáneos NO pueden ambos ganar: Turso serializa las transacciones.

NUNCA bloquea un mail por culpa de Turso: si la base no está, devuelve
'unavailable' y el sistema sigue con las capas viejas (mail_log + Drive appProps).
"""
import os
import json
import time
import urllib.request

_TABLE_READY = False

_CREATE = (
    "CREATE TABLE IF NOT EXISTS sent_mails ("
    "  dedupe_key TEXT PRIMARY KEY,"
    "  sent_at INTEGER NOT NULL,"
    "  kind TEXT, subject TEXT, recipient TEXT,"
    "  confirmed INTEGER NOT NULL DEFAULT 0)"
)

# UPSERT atómico con ventana + ROBO de claims fantasma. Re-reclama si:
#  a) el registro es más viejo que la ventana (renotificación legítima), O
#  b) el claim NUNCA se confirmó (confirmed=0) y tiene >10 min — el proceso que lo
#     reclamó MURIÓ entre el claim y el envío (run cancelado/crash). Sin esto, un
#     claim fantasma bloqueaba el mail toda la ventana (caso 🔧 Daniel 01/jul: el
#     cliente recibió su mail pero el aviso al admin quedó 'dedupe-skip' para
#     siempre). Un envío real tarda segundos; 10 min de gracia no habilita
#     duplicados reales.
# Al ganar el claim (INSERT o UPDATE) confirmed queda 0; el caller confirma con
# confirm_mail() DESPUÉS del envío exitoso.
_CLAIM = (
    "INSERT INTO sent_mails (dedupe_key, sent_at, kind, subject, recipient, confirmed) "
    "VALUES (?, ?, ?, ?, ?, 0) "
    "ON CONFLICT(dedupe_key) DO UPDATE SET sent_at=excluded.sent_at, "
    "  kind=excluded.kind, subject=excluded.subject, recipient=excluded.recipient, "
    "  confirmed=0 "
    "WHERE sent_mails.sent_at < ? "
    "   OR (COALESCE(sent_mails.confirmed, 0) = 0 AND sent_mails.sent_at < ?) "
    "RETURNING dedupe_key"
)


def _cfg():
    url = os.environ.get("TURSO_DATABASE_URL", "").strip()
    url = url.replace("libsql://", "https://")
    token = os.environ.get("TURSO_AUTH_TOKEN", "").strip()
    return url, token


def _exec(url, token, sql, args=None, timeout=8):
    stmt = {"sql": sql}
    if args is not None:
        stmt["args"] = [
            ({"type": "integer", "value": str(a)} if isinstance(a, int)
             else {"type": "text", "value": str(a)})
            for a in args
        ]
    body = json.dumps({
        "requests": [{"type": "execute", "stmt": stmt}, {"type": "close"}]
    }).encode()
    req = urllib.request.Request(
        url + "/v2/pipeline", data=body,
        headers={"Authorization": f"Bearer {token}",
                 "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        r = json.load(resp)["results"][0]
    if r["type"] == "error":
        raise RuntimeError(r["error"]["message"])
    return r["response"].get("result")


def _ensure(url, token):
    global _TABLE_READY
    if not _TABLE_READY:
        _exec(url, token, _CREATE)
        # Migración: tabla vieja sin 'confirmed'. ALTER falla si ya existe → ignorar.
        try:
            _exec(url, token,
                  "ALTER TABLE sent_mails ADD COLUMN confirmed INTEGER NOT NULL DEFAULT 0")
        except Exception:
            pass
        _TABLE_READY = True


def claim_mail(dedupe_key, kind, subject, recipient, window_minutes):
    """Reclama atómicamente el derecho a mandar este mail.
    Devuelve 'send' | 'duplicate' | 'unavailable'."""
    url, token = _cfg()
    if not url or not token or not dedupe_key:
        return "unavailable"
    try:
        _ensure(url, token)
        now = int(time.time())
        cutoff = now - max(int(window_minutes), 0) * 60
        stale = now - 600  # claims sin confirmar >10 min = proceso muerto → robable
        res = _exec(url, token, _CLAIM,
                    [dedupe_key, now, kind or "", (subject or "")[:200],
                     recipient or "", cutoff, stale])
        rows = (res or {}).get("rows", [])
        return "send" if len(rows) > 0 else "duplicate"
    except Exception as e:
        print(f"   ⚠️ Turso dedupe no disponible ({str(e)[:90]}) → uso mail_log")
        return "unavailable"


def confirm_mail(dedupe_key):
    """Marca el claim como CONFIRMADO (el mail salió de verdad). Sin confirmar,
    otro proceso puede robar el claim a los 10 min (asume proceso muerto)."""
    url, token = _cfg()
    if not url or not token or not dedupe_key:
        return
    try:
        _exec(url, token,
              "UPDATE sent_mails SET confirmed=1 WHERE dedupe_key=?", [dedupe_key])
    except Exception:
        pass


def release_mail(dedupe_key):
    """Libera un claim cuando el envío FALLÓ, para permitir el reintento.
    Si no se libera, el reintento vería 'duplicate' y el mail nunca saldría."""
    url, token = _cfg()
    if not url or not token or not dedupe_key:
        return
    try:
        _exec(url, token, "DELETE FROM sent_mails WHERE dedupe_key=?", [dedupe_key])
    except Exception:
        pass

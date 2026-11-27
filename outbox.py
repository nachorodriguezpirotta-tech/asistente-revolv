"""Bandeja de salida ÚNICA de todos los mails del asistente (base rápida D1).

Por qué existe (27/nov/2026). Antes había 4 registros distintos de "ya mandé
esto" (reserva en la base rápida, mail_log en git, marca en la cola de git y una
marca en Drive), cada uno con su clave y su ventana. Medido en 30 días:
  - 170 mails reales y 420 intentos frenados: el sistema reintentaba a ciegas
    porque la marca de "enviado" vivía en git y se perdía al pisarse un push.
  - Las correcciones del MISMO video en la misma semana chocaban en la misma
    clave (nombre del archivo, ventana de 7 días) → la segunda no avisaba.
  - Re-subir el mismo video ("3.mp4", "Copia de 3.mp4", "3.mp4") → 3 avisos.
  - Los mails sin ventana no tenían ninguna protección contra duplicados.

Modelo: cada mail es un EVENTO con identidad propia y un estado.

    enviando ──► enviado
        │
        └──────► fallido ──(reintento)──► enviando ... hasta MAX_INTENTOS

- reservar() es ATÓMICA: dos procesos que intentan el mismo evento a la vez →
  uno manda y el otro recibe 'en-curso'. Nunca los dos.
- 'ya-enviado' significa que el mail SALIÓ (confirmado). El que llama puede
  cerrar su tarea tranquilo: no hay que reintentar.
- Un evento que falla MAX_INTENTOS veces queda 'fallido' y se avisa al admin.
  Nada se descarta en silencio.

Ventana: un evento con `ventana_min=None` se manda UNA vez y nunca más (lo
normal para avisos de entrega, corrección, material). Con ventana, se puede
volver a mandar pasado ese tiempo: lo necesitan los mails cuya clave es el
asunto (ej. "Material nuevo de X" una vez por día).
"""
import time

MAX_INTENTOS = 5
LOCK_SEG = 300          # si un proceso murió a mitad de envío, a los 5 min otro lo retoma

SCHEMA = """CREATE TABLE IF NOT EXISTS outbox (
    evento       TEXT PRIMARY KEY,
    tipo         TEXT,
    destinatario TEXT,
    asunto       TEXT,
    estado       TEXT NOT NULL,
    intentos     INTEGER NOT NULL DEFAULT 0,
    lock_hasta   INTEGER,
    msg_id       TEXT,
    error        TEXT,
    creado       INTEGER,
    actualizado  INTEGER
)"""
INDICE = "CREATE INDEX IF NOT EXISTS idx_outbox_estado ON outbox(estado, actualizado)"

_LISTA = False


def _ts():
    import tasks_store
    return tasks_store


def disponible() -> bool:
    try:
        return _ts().available()
    except Exception:
        return False


def _asegurar():
    global _LISTA
    if _LISTA:
        return
    _ts()._pipeline([(SCHEMA, None), (INDICE, None)])
    _LISTA = True


def reservar(evento, tipo, destinatario, asunto, ventana_min=None) -> str:
    """Pide el derecho a mandar `evento`. Devuelve:
        'mandar'      → te toca a vos: mandalo y después confirmá o marcá falla
        'ya-enviado'  → ya salió (dentro de la ventana, si la hay): no mandes
        'en-curso'    → otro proceso lo está mandando ahora mismo: no mandes
        'abandonado'  → falló MAX_INTENTOS veces: no se reintenta más
    """
    ts = _ts()
    _asegurar()
    ahora = int(time.time())
    r = ts.execute(
        "INSERT OR IGNORE INTO outbox (evento, tipo, destinatario, asunto, estado, intentos, "
        "lock_hasta, creado, actualizado) VALUES (?, ?, ?, ?, 'enviando', 1, ?, ?, ?)",
        (evento, tipo, destinatario, (asunto or "")[:300], ahora + LOCK_SEG, ahora, ahora))
    if r.get("affected") == 1:
        return "mandar"

    filas = ts.query("SELECT estado, intentos, lock_hasta, actualizado FROM outbox WHERE evento=?",
                     (evento,))
    if not filas:
        return "mandar"     # se borró entre medio: raro, mandamos
    f = filas[0]
    estado = f.get("estado")
    intentos = int(f.get("intentos") or 0)
    lock = int(f.get("lock_hasta") or 0)
    actualizado = int(f.get("actualizado") or 0)

    if estado == "enviado":
        if not ventana_min or ahora - actualizado < int(ventana_min) * 60:
            return "ya-enviado"
        # pasó la ventana: se puede volver a mandar (reiniciando los intentos)
        r = ts.execute(
            "UPDATE outbox SET estado='enviando', intentos=1, lock_hasta=?, actualizado=?, "
            "asunto=?, error=NULL WHERE evento=? AND estado='enviado' AND actualizado=?",
            (ahora + LOCK_SEG, ahora, (asunto or "")[:300], evento, actualizado))
        return "mandar" if r.get("affected") == 1 else "en-curso"

    if estado == "enviando" and lock > ahora:
        return "en-curso"
    if intentos >= MAX_INTENTOS:
        return "abandonado"

    # 'fallido', o 'enviando' cuyo proceso murió (lock vencido): lo retomamos,
    # atómico: si dos llegan juntos, solo uno cambia la fila.
    r = ts.execute(
        "UPDATE outbox SET estado='enviando', intentos=intentos+1, lock_hasta=?, actualizado=? "
        "WHERE evento=? AND intentos<? AND (estado='fallido' OR "
        "(estado='enviando' AND COALESCE(lock_hasta,0)<=?))",
        (ahora + LOCK_SEG, ahora, evento, MAX_INTENTOS, ahora))
    return "mandar" if r.get("affected") == 1 else "en-curso"


def confirmar(evento, msg_id, tipo="", destinatario="", asunto=""):
    """Asienta que `evento` SALIÓ. Es lo que impide que se repita, así que se
    reintenta: si esto se perdiera, al vencer la reserva otro proceso lo
    volvería a mandar. Upsert: sirve también si la reserva nunca se pudo
    escribir (bandeja caída al momento de reservar)."""
    ahora = int(time.time())
    ultimo = None
    for intento in range(4):
        try:
            _ts().execute(
                "INSERT INTO outbox (evento, tipo, destinatario, asunto, estado, intentos, "
                "msg_id, creado, actualizado) VALUES (?, ?, ?, ?, 'enviado', 1, ?, ?, ?) "
                "ON CONFLICT(evento) DO UPDATE SET estado='enviado', msg_id=excluded.msg_id, "
                "lock_hasta=NULL, error=NULL, actualizado=excluded.actualizado",
                (evento, tipo, destinatario, (asunto or "")[:300], msg_id, ahora, ahora))
            return True
        except Exception as e:
            ultimo = e
            time.sleep(1.5 * (intento + 1))
    print(f"   ⚠️ no pude asentar el envío de {evento[:60]}: {str(ultimo)[:80]}")
    return False


def fallo(evento, error):
    ahora = int(time.time())
    _ts().execute(
        "UPDATE outbox SET estado='fallido', error=?, lock_hasta=NULL, actualizado=? "
        "WHERE evento=?", (str(error)[:300], ahora, evento))


def registrar_libre(tipo, destinatario, asunto, msg_id):
    """Mails que no tienen identidad de evento (sin protección de duplicado
    pedida): igual quedan registrados, para poder auditar qué salió."""
    import uuid
    ahora = int(time.time())
    try:
        _asegurar()
        _ts().execute(
            "INSERT OR IGNORE INTO outbox (evento, tipo, destinatario, asunto, estado, intentos, "
            "msg_id, creado, actualizado) VALUES (?, ?, ?, ?, 'enviado', 1, ?, ?, ?)",
            (f"libre|{uuid.uuid4().hex}", tipo, destinatario, (asunto or "")[:300],
             msg_id, ahora, ahora))
    except Exception:
        pass


def abandonados_sin_avisar(desde_seg=7 * 86400, quieto_seg=1800):
    """Mails que NO salieron y nadie va a reintentar, sin aviso al admin todavía:
      - fallaron todos sus intentos, o
      - fallaron y quedaron quietos más de 30 min (p. ej. un aviso a 2
        personas donde a una le salió y la tarea se dio por cumplida), o
      - quedaron 'enviando' de un proceso que murió y nadie los retomó.
    """
    ahora = int(time.time())
    try:
        _asegurar()
        return _ts().query(
            "SELECT evento, tipo, destinatario, asunto, estado, intentos, error, actualizado FROM outbox "
            "WHERE actualizado>=? AND COALESCE(error,'') NOT LIKE '%[avisado]%' AND ("
            "  (estado='fallido' AND (intentos>=? OR actualizado<?)) OR "
            "  (estado='enviando' AND COALESCE(lock_hasta,0)<?))",
            (ahora - desde_seg, MAX_INTENTOS, ahora - quieto_seg, ahora - quieto_seg))
    except Exception:
        return []


def marcar_avisado(evento):
    """El admin ya fue avisado: no se lista más ni se reintenta solo."""
    try:
        _ts().execute("UPDATE outbox SET estado='fallido', lock_hasta=NULL, "
                      "intentos=MAX(intentos, ?), "
                      "error=COALESCE(error,'sin respuesta') || ' [avisado]' WHERE evento=?",
                      (MAX_INTENTOS, evento))
    except Exception:
        pass

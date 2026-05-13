"""
Push notifications sender.

Manda Web Push notifications a los browsers suscriptos.
Las suscripciones se guardan en la tabla push_subscriptions de la DB.
Las keys VAPID se leen de env vars: VAPID_PRIVATE_KEY, VAPID_SUBJECT.
"""

import json
import os
from typing import Optional

from tracker import get_conn, now_iso

VAPID_PRIVATE_KEY = os.environ.get("VAPID_PRIVATE_KEY", "")
VAPID_SUBJECT = os.environ.get("VAPID_SUBJECT", "mailto:nacho.rodriguezpirotta@gmail.com")


def _vapid_claims():
    return {"sub": VAPID_SUBJECT}


def list_subscriptions(editor: Optional[str] = None) -> list[dict]:
    """Lista subs activas. Si editor=None → solo admin. Si editor='*' → todas.
    Si editor='Rami' → las de Rami (puede haber múltiples si tiene varios dispositivos)."""
    conn = get_conn()
    if editor == '*':
        rows = conn.execute(
            "SELECT id, editor, endpoint, p256dh, auth FROM push_subscriptions"
        ).fetchall()
    elif editor is None:
        rows = conn.execute(
            "SELECT id, editor, endpoint, p256dh, auth FROM push_subscriptions WHERE editor IS NULL"
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT id, editor, endpoint, p256dh, auth FROM push_subscriptions WHERE editor = ?",
            (editor,)
        ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def save_subscription(editor: Optional[str], endpoint: str, p256dh: str, auth: str) -> int:
    """Guarda o actualiza una suscripción. Devuelve id."""
    conn = get_conn()
    # Si ya existe el endpoint, update (mismo browser que renueva tokens)
    cur = conn.execute("""
        INSERT INTO push_subscriptions (editor, endpoint, p256dh, auth, created_at)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(endpoint) DO UPDATE SET
            editor=excluded.editor, p256dh=excluded.p256dh, auth=excluded.auth,
            last_used_at=excluded.created_at, failed_count=0
    """, (editor, endpoint, p256dh, auth, now_iso()))
    sid = cur.lastrowid
    conn.commit()
    conn.close()
    return sid


def delete_subscription_by_endpoint(endpoint: str):
    conn = get_conn()
    conn.execute("DELETE FROM push_subscriptions WHERE endpoint = ?", (endpoint,))
    conn.commit()
    conn.close()


def send_push(editor: Optional[str], title: str, body: str, url: Optional[str] = None, tag: Optional[str] = None) -> int:
    """Manda push a todas las subs del editor. Devuelve cuántas se enviaron OK.
    editor=None → admin. editor='Rami' → todos los devices de Rami.
    Si una sub falla con 410/404 (Gone), la borra automáticamente.
    """
    if not VAPID_PRIVATE_KEY:
        print("⚠️ VAPID_PRIVATE_KEY no configurado, skip push")
        return 0
    try:
        from pywebpush import webpush, WebPushException
    except ImportError:
        print("⚠️ pywebpush no instalado")
        return 0

    subs = list_subscriptions(editor)
    if not subs:
        return 0

    payload = json.dumps({"title": title, "body": body, "url": url or "/", "tag": tag or "default"})
    sent = 0
    for s in subs:
        try:
            webpush(
                subscription_info={
                    "endpoint": s["endpoint"],
                    "keys": {"p256dh": s["p256dh"], "auth": s["auth"]},
                },
                data=payload,
                vapid_private_key=VAPID_PRIVATE_KEY,
                vapid_claims=_vapid_claims(),
            )
            sent += 1
        except WebPushException as e:
            status = getattr(e.response, "status_code", None) if hasattr(e, "response") else None
            print(f"  push falló: {e} (status={status})")
            # 410/404 = endpoint muerto (browser desinstalado, sub revocada)
            if status in (404, 410):
                print(f"  borrando suscripción muerta: {s['endpoint'][:50]}...")
                delete_subscription_by_endpoint(s["endpoint"])
        except Exception as e:
            print(f"  push error inesperado: {e}")
    return sent

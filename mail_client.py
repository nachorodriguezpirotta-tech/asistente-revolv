"""
Mail client — manda mails desde tu Gmail vía API (scope: gmail.send).

Uso:
    from mail_client import send_mail
    send_mail(to="alguien@mail.com", subject="...", body_text="...", body_html=None)
"""

import base64
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Optional

from googleapiclient.discovery import build

from auth import get_credentials


_service_cache = None


def _get_service():
    """Servicio de Gmail. Prefiere credenciales DEDICADAS para mandar mails
    (cuenta separada asistente.revolv@gmail.com) si están disponibles.
    Si no, usa las credenciales normales (cuenta personal)."""
    global _service_cache
    if _service_cache is None:
        try:
            from auth_mail import get_mail_credentials
            creds = get_mail_credentials()
        except Exception:
            creds = get_credentials()
        _service_cache = build("gmail", "v1", credentials=creds, cache_discovery=False)
    return _service_cache


def already_sent_recently(to: str, subject: str, minutes: int = 30) -> Optional[str]:
    """Busca en Gmail (cuenta del sistema) si ya hay un mail enviado con este
    subject al mismo destinatario en los últimos N minutos. Retorna el msg_id
    del más reciente si existe, None si no.

    Sirve como dedupe entre containers de GHA: si Worker A ya envió, Worker B
    consulta Gmail (source of truth) y skipea. Cubre el caso donde el mail_log
    de tracker.db se pierde por race condition de push concurrente.
    """
    try:
        from datetime import datetime, timedelta
        service = _get_service()
        cutoff = int((datetime.now() - timedelta(minutes=minutes)).timestamp())
        # Escapear comillas en subject — Gmail search syntax
        safe_subject = subject.replace('"', '\\"')
        q = f'in:sent to:{to} subject:"{safe_subject}" after:{cutoff}'
        res = service.users().messages().list(userId="me", q=q, maxResults=1).execute()
        msgs = res.get("messages") or []
        if msgs:
            return msgs[0].get("id")
        return None
    except Exception as e:
        # Si la query falla (cuota, red, etc.), NO bloquear el envío — mejor
        # arriesgar 1 duplicado que no avisar nada.
        print(f"   ⚠️ dedupe check falló (continúa enviando): {e}")
        return None


def send_mail(to: str, subject: str, body_text: str, body_html: Optional[str] = None,
              from_name: str = "Asistente Revolv",
              kind: str = "", cliente: Optional[str] = None, editor: Optional[str] = None,
              dedupe_window_minutes: int = 0) -> str:
    """
    Manda un mail desde la cuenta autorizada.
    Retorna el message_id.
    Registra cada envío (éxito/falla) en mail_log para auditoría.

    Si `dedupe_window_minutes > 0`, ANTES de enviar consulta Gmail si ya se
    mandó un mail con el mismo subject a este `to` en esa ventana. Si sí,
    retorna el msg_id del existente sin enviar otra vez. Útil para evitar
    duplicados causados por race condition entre containers (caso del scan
    que vio el mismo archivo y encoló mail en dos workers paralelos).
    """
    if dedupe_window_minutes > 0:
        existing = already_sent_recently(to, subject, minutes=dedupe_window_minutes)
        if existing:
            print(f"   🔄 dedupe: ya se mandó '{subject[:60]}' a {to} hace <{dedupe_window_minutes}min (msg_id={existing}). SKIP")
            try:
                from tracker import log_mail
                log_mail(to_email=to, subject=subject, kind=kind, cliente=cliente,
                         editor=editor, msg_id=existing, success=True,
                         error="dedupe-skip")
            except Exception:
                pass
            return existing
    msg = MIMEMultipart("alternative")
    msg["To"] = to
    msg["Subject"] = subject
    msg["From"] = from_name

    msg.attach(MIMEText(body_text, "plain", "utf-8"))
    if body_html:
        msg.attach(MIMEText(body_html, "html", "utf-8"))

    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode("ascii")

    try:
        service = _get_service()
        sent = service.users().messages().send(
            userId="me",
            body={"raw": raw},
        ).execute()
        msg_id = sent["id"]
        # Log success
        try:
            from tracker import log_mail
            log_mail(to_email=to, subject=subject, kind=kind, cliente=cliente,
                     editor=editor, msg_id=msg_id, success=True)
        except Exception:
            pass
        return msg_id
    except Exception as e:
        try:
            from tracker import log_mail
            log_mail(to_email=to, subject=subject, kind=kind, cliente=cliente,
                     editor=editor, msg_id=None, success=False, error=str(e)[:300])
        except Exception:
            pass
        raise


if __name__ == "__main__":
    # Test: te manda un mail de prueba
    from config import TEST_EMAIL
    msg_id = send_mail(
        to=TEST_EMAIL,
        subject="🧪 Test Asistente Revolv",
        body_text="Si recibís este mail, el módulo de mail funciona.\n\n— Asistente Revolv",
    )
    print(f"✅ Mail enviado. message_id: {msg_id}")
    print(f"   Revisá tu inbox: {TEST_EMAIL}")

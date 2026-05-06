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
    global _service_cache
    if _service_cache is None:
        creds = get_credentials()
        _service_cache = build("gmail", "v1", credentials=creds, cache_discovery=False)
    return _service_cache


def send_mail(to: str, subject: str, body_text: str, body_html: Optional[str] = None,
              from_name: str = "Asistente Revolv") -> str:
    """
    Manda un mail desde la cuenta autorizada.
    Retorna el message_id.
    """
    msg = MIMEMultipart("alternative")
    msg["To"] = to
    msg["Subject"] = subject
    msg["From"] = from_name

    msg.attach(MIMEText(body_text, "plain", "utf-8"))
    if body_html:
        msg.attach(MIMEText(body_html, "html", "utf-8"))

    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode("ascii")

    service = _get_service()
    sent = service.users().messages().send(
        userId="me",
        body={"raw": raw},
    ).execute()
    return sent["id"]


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

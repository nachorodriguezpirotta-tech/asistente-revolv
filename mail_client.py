"""
Mail client — manda mails desde tu Gmail vía API (scope: gmail.send).

Uso:
    from mail_client import send_mail
    send_mail(to="alguien@mail.com", subject="...", body_text="...", body_html=None)
"""

import base64
import os
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


def _sync_mail_log_from_remote() -> bool:
    """Hace 'git pull --rebase' en el directorio del repo para traer el último
    mail_log de tracker.db pusheado por otros workers de GHA. Sirve como
    sincronización antes de cada envío crítico para evitar duplicados.

    Si no estamos en un repo git (ej. local de desarrollo), retorna False
    silenciosamente.

    BUG FIX 20/may: el path estaba 1 nivel arriba (dirname(dirname(...)) en
    vez de dirname(...)). mail_client.py vive en el ROOT del repo, no en
    subdirectorio, así que el dirname() de su path YA es el repo root.
    """
    import subprocess
    try:
        repo_root = os.path.dirname(os.path.abspath(__file__))
        # Verificar que es repo git
        check = subprocess.run(
            ["git", "-C", repo_root, "rev-parse", "--git-dir"],
            capture_output=True, timeout=5
        )
        if check.returncode != 0:
            return False
        # Stash cualquier cambio local (no debería haber, pero por las dudas)
        subprocess.run(["git", "-C", repo_root, "stash", "--quiet"],
                       capture_output=True, timeout=5)
        # Pull rebase silencioso
        pull = subprocess.run(
            ["git", "-C", repo_root, "pull", "--rebase", "--quiet"],
            capture_output=True, timeout=15
        )
        # Restore stash si había algo
        subprocess.run(["git", "-C", repo_root, "stash", "pop", "--quiet"],
                       capture_output=True, timeout=5)
        return pull.returncode == 0
    except Exception as e:
        print(f"   ⚠️ git pull falló: {e}")
        return False


def _dedupe_key(to: str, subject: str) -> str:
    """Hash determinístico para identificar 'el mismo mail'. Usado en
    UNIQUE constraint de mail_log + dedupe pre-envío."""
    import hashlib
    return hashlib.sha1(f"{to.lower().strip()}||{subject.strip()}".encode()).hexdigest()[:24]


def already_sent_recently(to: str, subject: str, minutes: int = 30) -> Optional[str]:
    """Dedupe via mail_log de tracker.db usando dedupe_key (hash de to+subject).
    Antes de chequear, hace git pull --rebase para traer mail_log de otros
    workers de GHA.

    Retorna msg_id del envío previo si existe, None si no.
    """
    try:
        # Sincronizar con el último estado del repo (puede tener envíos de
        # otros workers). Skipea silencioso si no estamos en GHA.
        _sync_mail_log_from_remote()

        from tracker import check_recent_mail_by_key
        key = _dedupe_key(to, subject)
        return check_recent_mail_by_key(key, minutes=minutes)
    except Exception as e:
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
    # Calcular dedupe_key SIEMPRE (aún si dedupe_window=0, para tener log preciso)
    dkey = _dedupe_key(to, subject)

    if dedupe_window_minutes > 0:
        existing = already_sent_recently(to, subject, minutes=dedupe_window_minutes)
        if existing:
            print(f"   🔄 dedupe: ya se mandó '{subject[:60]}' a {to} hace <{dedupe_window_minutes}min (msg_id={existing}). SKIP")
            try:
                from tracker import log_mail
                log_mail(to_email=to, subject=subject, kind=kind, cliente=cliente,
                         editor=editor, msg_id=existing, success=True,
                         error="dedupe-skip", dedupe_key=dkey)
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
        # Log success con dedupe_key (sirve para próxima vez evitar duplicado)
        try:
            from tracker import log_mail
            log_mail(to_email=to, subject=subject, kind=kind, cliente=cliente,
                     editor=editor, msg_id=msg_id, success=True, dedupe_key=dkey)
        except Exception:
            pass
        return msg_id
    except Exception as e:
        try:
            from tracker import log_mail
            log_mail(to_email=to, subject=subject, kind=kind, cliente=cliente,
                     editor=editor, msg_id=None, success=False, error=str(e)[:300],
                     dedupe_key=dkey)
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

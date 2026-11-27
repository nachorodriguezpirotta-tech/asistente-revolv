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
              dedupe_window_minutes: int = 0,
              dedupe_key_override: Optional[str] = None,
              evento: Optional[str] = None) -> str:
    """Manda un mail. TODO pasa por la bandeja de salida única (outbox.py).

    Identidad del mail (lo que decide si "es el mismo" que uno anterior):
      - `evento`: identidad explícita del suceso, p. ej.
        "entrega|cliente|video 8|2026-11-18|dest". Se manda UNA vez y nunca más.
        Es la forma recomendada.
      - `dedupe_key_override` + `dedupe_window_minutes`: identidad heredada; se
        puede volver a mandar pasada la ventana.
      - solo `dedupe_window_minutes`: la identidad es destinatario + asunto.
      - nada: mail sin protección de duplicado (solo se registra).

    Devuelve el msg_id de Gmail si salió. Si NO se manda porque no corresponde,
    devuelve un texto que termina en "-skip":
      "outbox-sent-skip"      ya había salido (confirmado): el que llama puede
                              dar la tarea por cumplida
      "outbox-busy-skip"      otro proceso lo está mandando en este momento
      "outbox-abandoned-skip" falló todos sus intentos (el admin recibe aviso)
    Si Gmail falla, levanta la excepción (y queda registrada para reintentar).
    """
    import hashlib
    if evento:
        clave, ventana = evento, (dedupe_window_minutes or None)
    elif dedupe_key_override:
        clave = hashlib.sha1(dedupe_key_override.encode()).hexdigest()[:24]
        ventana = dedupe_window_minutes or None
    elif dedupe_window_minutes > 0:
        clave, ventana = _dedupe_key(to, subject), dedupe_window_minutes
    else:
        clave, ventana = None, None

    def _log(msg_id, ok, error=None):
        try:
            from tracker import log_mail
            log_mail(to_email=to, subject=subject, kind=kind, cliente=cliente, editor=editor,
                     msg_id=msg_id, success=ok, error=error, dedupe_key=clave)
        except Exception:
            pass

    usar_outbox = False
    if clave:
        try:
            import outbox
            usar_outbox = outbox.disponible()
        except Exception:
            usar_outbox = False
        if usar_outbox:
            try:
                veredicto = outbox.reservar(clave, kind, to, subject, ventana)
            except Exception as e:
                # La bandeja no respondió: preferimos el respaldo viejo a no mandar.
                print(f"   ⚠️ bandeja de salida no respondió ({str(e)[:80]}): uso el respaldo")
                usar_outbox = False
                veredicto = None
            if veredicto == "ya-enviado":
                print(f"   🔄 ya había salido: '{subject[:60]}' → {to}. No se repite.")
                _log("outbox", True, "dedupe-skip-outbox")
                return "outbox-sent-skip"
            if veredicto == "en-curso":
                print(f"   ⏳ otro proceso está mandando '{subject[:60]}' → {to}. No se repite.")
                return "outbox-busy-skip"
            if veredicto == "abandonado":
                print(f"   🛑 '{subject[:60]}' → {to} falló todos sus intentos; no se reintenta.")
                return "outbox-abandoned-skip"
        if not usar_outbox:
            # RESPALDO (base rápida caída): el chequeo viejo contra mail_log.
            try:
                _sync_mail_log_from_remote()
            except Exception:
                pass
            try:
                from tracker import check_recent_mail_by_key
                existing = check_recent_mail_by_key(clave, minutes=(ventana or 10080))
            except Exception:
                existing = None
            if existing:
                _log(existing, True, "dedupe-skip")
                return "outbox-sent-skip"

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
        sent = service.users().messages().send(userId="me", body={"raw": raw}).execute()
        msg_id = sent["id"]
    except Exception as e:
        if usar_outbox:
            try:
                import outbox
                outbox.fallo(clave, e)
            except Exception:
                pass
        _log(None, False, str(e)[:300])
        raise

    # Salió. Lo primero es dejarlo asentado en la bandeja (lo que evita que se
    # repita); el log de git es secundario y puede perderse sin consecuencias.
    try:
        import outbox
        if clave:
            # también en modo respaldo: así, cuando la bandeja vuelve, ya sabe
            # que este evento salió y no lo repite.
            outbox.confirmar(clave, msg_id, kind, to, subject)
        else:
            outbox.registrar_libre(kind, to, subject, msg_id)
    except Exception:
        pass
    _log(msg_id, True)
    return msg_id


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

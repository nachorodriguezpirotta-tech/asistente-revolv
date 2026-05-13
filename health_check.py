"""
Health check — verifica que el sistema esté corriendo.

Lógica:
  - Mira el último 'first_seen_at' o 'last_scan_at' en la DB
  - Si pasaron más de 90 minutos sin actividad → manda mail de alerta
  - Si no hay nada en la DB, asume primer run → no alerta

Diseñado para correr cada 1 hora vía GitHub Actions.
"""

import os
import sys
from datetime import datetime, timedelta

from config import TEST_EMAIL
from tracker import get_conn
from mail_client import send_mail

ALERT_THRESHOLD_MIN = 90  # > 90 minutos sin actividad = alerta


def _parse_iso(s):
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "").split(".")[0])
    except Exception:
        return None


def run():
    print("🩺 Health check del sistema")
    conn = get_conn()

    # El indicador más confiable: cuándo se vio el último cambio de drive page_token
    # (se actualiza en cada scan incremental).
    row = conn.execute(
        "SELECT value, updated_at FROM meta WHERE key='drive_changes_page_token'"
    ).fetchone()

    last_activity = None
    if row and row["updated_at"]:
        last_activity = _parse_iso(row["updated_at"])

    # Backup: último known_file insertado
    if not last_activity:
        row = conn.execute("SELECT MAX(first_seen_at) as ts FROM known_files").fetchone()
        if row and row["ts"]:
            last_activity = _parse_iso(row["ts"])

    conn.close()

    if not last_activity:
        print("   (Sin actividad registrada, sistema nuevo. Skip alerta.)")
        return

    now = datetime.now()
    minutes_since = (now - last_activity).total_seconds() / 60
    print(f"   Última actividad: {last_activity.isoformat()} ({minutes_since:.0f} min atrás)")

    if minutes_since < ALERT_THRESHOLD_MIN:
        print(f"   ✅ OK (< {ALERT_THRESHOLD_MIN} min)")
        return

    # ALERTA
    hours = minutes_since / 60
    subject = f"🚨 Asistente Revolv: sin actividad hace {hours:.1f} horas"
    text = f"""ALERTA del sistema:

El Asistente Revolv no registra actividad desde hace {hours:.1f} horas.

Última actividad detectada: {last_activity.isoformat()}

Posibles causas:
- Workflows de GitHub Actions fallando (revisar https://github.com/nachorodriguezpirotta-tech/asistente-revolv/actions)
- cron-job.org no dispara
- Credenciales OAuth expiradas
- API de Drive caída

— Asistente Revolv Health Check
"""
    html = f"""<html><body style="font-family:-apple-system,Segoe UI,sans-serif;max-width:600px;color:#222;">
<h2 style="color:#dc2626;">🚨 Sistema sin actividad</h2>
<p>El Asistente Revolv no registra actividad desde hace <strong>{hours:.1f} horas</strong>.</p>
<p>Última actividad detectada: <code>{last_activity.isoformat()}</code></p>
<h3>Posibles causas:</h3>
<ul>
<li>Workflows de GitHub Actions fallando</li>
<li>cron-job.org no dispara</li>
<li>Credenciales OAuth expiradas</li>
<li>API de Drive caída</li>
</ul>
<p><a href="https://github.com/nachorodriguezpirotta-tech/asistente-revolv/actions">Ver Actions</a></p>
</body></html>"""

    try:
        msg_id = send_mail(to=TEST_EMAIL, subject=subject, body_text=text, body_html=html)
        print(f"   📧 Alerta enviada: {msg_id}")
    except Exception as e:
        print(f"   ❌ Falló envío alerta: {e}")
        sys.exit(1)


if __name__ == "__main__":
    run()

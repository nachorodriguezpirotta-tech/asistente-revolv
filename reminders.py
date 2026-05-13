"""
Recordatorios automáticos:
- Mail a editor si tiene pending hace > 5 días sin entregar nada (atraso).
- Para no spam, solo manda 1 vez por semana por editor.

Diseñado para correr 1x/día (mañana, antes del resumen diario).
"""

import os
import sys
from collections import defaultdict
from datetime import datetime, timedelta

from config import TEST_EMAIL
from tracker import get_conn, meta_get, meta_set
from mail_client import send_mail

DAYS_THRESHOLD = 5
META_KEY_LAST_REMINDER = "reminders_last_sent_"  # + editor_name


def _parse_iso(s):
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "").split(".")[0])
    except Exception:
        return None


def run(dry_run: bool = False):
    print("⏰ Recordatorios a editores con atraso > 5 días")
    conn = get_conn()
    now = datetime.now()

    # Editores activos con sus emails
    eds = conn.execute(
        "SELECT name, email FROM cfg_editors WHERE active=1 AND email IS NOT NULL AND email != ''"
    ).fetchall()
    editors_active = {r["name"]: r["email"] for r in eds}

    # Pending por editor con tiempo desde el más viejo
    rows = conn.execute("""
        SELECT editor, MIN(detected_at) as oldest, COUNT(*) as count_clientes,
               SUM(COALESCE(pending_count, 1)) as total_videos
        FROM tasks WHERE status='pending' AND editor IS NOT NULL
        GROUP BY editor
    """).fetchall()
    conn.close()

    to_remind = []
    for r in rows:
        editor = r["editor"]
        if editor not in editors_active:
            continue
        oldest = _parse_iso(r["oldest"])
        if not oldest:
            continue
        days = (now - oldest).total_seconds() / 86400
        if days < DAYS_THRESHOLD:
            continue
        # Throttle: solo recordar 1 vez cada 7 días por editor
        last_sent = meta_get(META_KEY_LAST_REMINDER + editor)
        if last_sent:
            last_dt = _parse_iso(last_sent)
            if last_dt and (now - last_dt).total_seconds() / 86400 < 7:
                print(f"  ⏭ {editor}: skip (último recordatorio hace <7 días)")
                continue
        to_remind.append({
            "editor": editor,
            "email": editors_active[editor],
            "days": round(days, 1),
            "clientes": r["count_clientes"],
            "videos": r["total_videos"],
        })

    if not to_remind:
        print("   ✅ Ningún editor atrasado")
        return

    print(f"   {len(to_remind)} editor(es) atrasados:")
    for r in to_remind:
        print(f"     • {r['editor']}: {r['days']}d, {r['clientes']} clientes, {r['videos']} videos")

    if dry_run:
        print("(dry-run, no se envía nada)")
        return

    for r in to_remind:
        editor = r["editor"]
        videos = r["videos"]
        clientes = r["clientes"]
        days = r["days"]

        subject = f"⏰ {videos} video{'s' if videos != 1 else ''} esperando, hace {days:.0f} días"
        text = f"""Hola {editor},

Tenés {videos} video{'s' if videos != 1 else ''} pendiente{'s' if videos != 1 else ''} de {clientes} cliente{'s' if clientes != 1 else ''}.
El más viejo lleva {days:.0f} días esperando.

Ya están listos para que les des una pasada cuando puedas.

Ver tu dashboard:
https://asistente-revolv.vercel.app/?editor={editor}

— Asistente Revolv
"""
        html = f"""<html><body style="font-family:-apple-system,Segoe UI,sans-serif;max-width:600px;color:#222;line-height:1.5;">
<h2 style="margin-bottom:4px;">⏰ Te faltan {videos} video{'s' if videos != 1 else ''}</h2>
<p>Hola <strong>{editor}</strong>,</p>
<p>Tenés <strong>{videos} video{'s' if videos != 1 else ''}</strong> pendiente{'s' if videos != 1 else ''} de <strong>{clientes} cliente{'s' if clientes != 1 else ''}</strong>.<br>
El más viejo lleva <strong>{days:.0f} días</strong> esperando.</p>
<p>Ya están listos para que les des una pasada cuando puedas 🙌</p>
<p><a href="https://asistente-revolv.vercel.app/?editor={editor}" style="background:#ff4747;color:white;padding:10px 18px;border-radius:6px;text-decoration:none;font-weight:600;">📋 Ver mi dashboard</a></p>
<hr style="border:none;border-top:1px solid #eee;margin:24px 0;">
<p style="color:#888;font-size:12px;">— Asistente Revolv</p>
</body></html>"""

        try:
            msg_id = send_mail(to=r["email"], subject=subject, body_text=text, body_html=html)
            print(f"     ✅ {editor} ({r['email']}): {msg_id}")
            meta_set(META_KEY_LAST_REMINDER + editor, now.isoformat(timespec="seconds"))
        except Exception as e:
            print(f"     ❌ {editor}: {e}")


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()
    run(dry_run=args.dry_run)

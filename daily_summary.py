"""
Daily summary — manda 1 mail por día con el resumen de pendientes,
agrupados por editor.

Se basa en la tabla `tasks` de la DB local: solo cuenta tareas detectadas
por el sistema desde el baseline (no histórico).

Uso:
    python3 daily_summary.py            # manda el mail
    python3 daily_summary.py --dry-run  # imprime lo que mandaría sin enviar
"""

import argparse
from collections import defaultdict
from datetime import datetime

from config import TEST_EMAIL
from tracker import get_conn
from mail_client import send_mail


SPANISH_MONTHS = {
    1: "enero", 2: "febrero", 3: "marzo", 4: "abril",
    5: "mayo", 6: "junio", 7: "julio", 8: "agosto",
    9: "septiembre", 10: "octubre", 11: "noviembre", 12: "diciembre",
}


def get_pending_grouped():
    """
    Devuelve un dict: editor -> [{cliente, videos, oldest_detected}]
    ordenado: editores alfabético, clientes alfabético dentro de cada editor.
    'oldest_detected' es la fecha del crudo más viejo pendiente (para priorizar).
    """
    conn = get_conn()
    rows = conn.execute("""
        SELECT editor, cliente, COUNT(*) as videos, MIN(detected_at) as oldest
        FROM tasks
        WHERE status = 'pending'
        GROUP BY editor, cliente
        ORDER BY editor, cliente
    """).fetchall()
    conn.close()

    grouped = defaultdict(list)
    for r in rows:
        editor = r["editor"] or "— sin editor en Sheet —"
        grouped[editor].append({
            "cliente": r["cliente"].strip(),
            "videos": r["videos"],
            "oldest": r["oldest"],
        })
    return grouped


def _fecha_humana() -> str:
    now = datetime.now()
    return f"{now.day} de {SPANISH_MONTHS[now.month]}"


def build_mail(grouped: dict):
    fecha = _fecha_humana()

    if not grouped:
        subject = f"📋 Resumen diario — {fecha}"
        text = (
            f"Buen día Nacho,\n\n"
            f"Sin pendientes nuevos detectados.\n\n"
            f"— Asistente Revolv"
        )
        html = f"""
        <html><body style="font-family:-apple-system,Segoe UI,sans-serif;max-width:600px;color:#222;">
        <h2>📋 Resumen diario — {fecha}</h2>
        <p>Buen día Nacho,</p>
        <p style="font-size:15px;">No hay pendientes nuevos detectados. ✅</p>
        <hr style="border:none;border-top:1px solid #eee;margin:24px 0;">
        <p style="color:#888;font-size:12px;">— Asistente Revolv</p>
        </body></html>
        """
        return subject, text, html

    subject = f"📋 Pendientes del día — {fecha}"

    # Texto plano (sin números, solo nombres)
    lines_text = [
        "Buen día Nacho,",
        "",
        "Resumen de clientes con pendientes:",
        "",
    ]
    for editor in sorted(grouped.keys(), key=lambda e: -len(grouped[e])):
        clientes = grouped[editor]
        lines_text.append(f"👤 {editor}")
        for c in clientes:
            lines_text.append(f"   • {c['cliente']}")
        lines_text.append("")
    lines_text.append("— Asistente Revolv")
    text = "\n".join(lines_text)

    # HTML
    editor_blocks_html = []
    for editor in sorted(grouped.keys(), key=lambda e: -len(grouped[e])):
        clientes = grouped[editor]
        items = "".join(f'<li>{c["cliente"]}</li>' for c in clientes)
        editor_blocks_html.append(
            f'<div style="margin:18px 0;">'
            f'<h3 style="margin:0 0 6px 0;color:#111;">👤 {editor}</h3>'
            f'<ul style="margin:6px 0 0 4px;padding-left:20px;line-height:1.7;">{items}</ul>'
            f'</div>'
        )

    html = f"""
    <html><body style="font-family:-apple-system,Segoe UI,sans-serif;max-width:640px;color:#222;line-height:1.5;">
    <h2 style="margin-bottom:4px;">📋 Pendientes del día</h2>
    <p style="margin-top:0;color:#555;">{fecha}</p>
    <p>Buen día Nacho,</p>
    <p>Estos son los clientes con pendientes hoy:</p>
    {"".join(editor_blocks_html)}
    <hr style="border:none;border-top:1px solid #eee;margin:24px 0;">
    <p style="color:#888;font-size:12px;">— Asistente Revolv</p>
    </body></html>
    """
    return subject, text, html


def build_mail_para_editor(editor: str, clientes: list):
    """Mail con SOLO los pendientes de UN editor. Para mandarle al editor."""
    fecha = _fecha_humana()
    if not clientes:
        subject = f"📋 Sin pendientes hoy — {fecha}"
        text = f"Hola {editor},\n\nNo tenés pendientes hoy. ✅\n\n— Asistente Revolv"
        html = f"<html><body style='font-family:-apple-system,Segoe UI,sans-serif;max-width:600px;color:#222;'><h2>📋 Sin pendientes hoy — {fecha}</h2><p>Hola {editor},</p><p>No tenés pendientes. ✅</p><hr><p style='color:#888;font-size:12px;'>— Asistente Revolv</p></body></html>"
        return subject, text, html

    subject = f"📋 Tus pendientes del día — {fecha}"
    lines = [f"Hola {editor},", "", "Estos son tus clientes con pendientes hoy:", ""]
    for c in clientes:
        lines.append(f"  • {c['cliente']}")
    lines.append("")
    lines.append("— Asistente Revolv")
    text = "\n".join(lines)

    items = "".join(f"<li>{c['cliente']}</li>" for c in clientes)
    html = f"""
    <html><body style="font-family:-apple-system,Segoe UI,sans-serif;max-width:600px;color:#222;line-height:1.5;">
    <h2 style="margin-bottom:4px;">📋 Tus pendientes</h2>
    <p style="margin-top:0;color:#555;">{fecha}</p>
    <p>Hola {editor},</p>
    <p>Estos son tus clientes con pendientes hoy:</p>
    <ul style="line-height:1.7;">{items}</ul>
    <hr style="border:none;border-top:1px solid #eee;margin:24px 0;">
    <p style="color:#888;font-size:12px;">— Asistente Revolv</p>
    </body></html>
    """
    return subject, text, html


def run(dry_run: bool = False):
    from aliases import (
        get_editor_emails_runtime, get_daily_summary_editors_runtime,
        get_editor_email,
    )
    EDITOR_EMAILS = get_editor_emails_runtime()
    DAILY_SUMMARY_EDITORS = get_daily_summary_editors_runtime()

    grouped = get_pending_grouped()
    subject, text, html = build_mail(grouped)

    print(f"=== Mail ADMIN (Ignacio): {subject}")
    print(text[:200] + "..." if len(text) > 200 else text)
    print("---")

    if dry_run:
        print("(dry-run, no se envía nada)")
    else:
        # 1. Mail completo a Ignacio (admin)
        try:
            msg_id = send_mail(to=TEST_EMAIL, subject=subject, body_text=text, body_html=html)
            print(f"✅ Admin: msg_id={msg_id} → {TEST_EMAIL}")
        except Exception as e:
            print(f"❌ Admin: {e}")

    # 2. Mail individual SOLO a los editores que están en DAILY_SUMMARY_EDITORS
    print(f"\n=== Mails individuales (solo a: {sorted(DAILY_SUMMARY_EDITORS)}) ===")
    from aliases import _normalize as _norm
    for editor, email in EDITOR_EMAILS.items():
        if editor not in DAILY_SUMMARY_EDITORS:
            print(f"   ⏭️  {editor}: skip (no en DAILY_SUMMARY_EDITORS)")
            continue

        editor_clientes = []
        for ed_key, clientes in grouped.items():
            if _norm(ed_key) == _norm(editor):
                editor_clientes = clientes
                break

        ed_subject, ed_text, ed_html = build_mail_para_editor(editor, editor_clientes)
        print(f"   → {editor} ({email}): {len(editor_clientes)} cliente(s)")
        if dry_run:
            print("     (dry-run)")
            continue
        try:
            msg_id = send_mail(to=email, subject=ed_subject, body_text=ed_text, body_html=ed_html)
            print(f"     ✅ msg_id={msg_id}")
        except Exception as e:
            print(f"     ❌ {e}")


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()
    run(dry_run=args.dry_run)

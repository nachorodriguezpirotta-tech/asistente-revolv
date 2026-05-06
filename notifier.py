"""
Notifier — agrupa tareas pendientes (sin mail enviado) por cliente y manda 1 mail por cada uno.

Uso:
    python3 notifier.py            # manda mails para tareas pendientes sin mail
    python3 notifier.py --dry-run  # muestra qué mandaría, sin enviar
"""

import argparse
from collections import defaultdict
from datetime import datetime
from typing import Optional

from config import TEST_EMAIL
from tracker import get_conn, now_iso
from mail_client import send_mail


def _drive_folder_url(folder_id: str) -> str:
    return f"https://drive.google.com/drive/folders/{folder_id}"


def _format_size(bytes_):
    if not bytes_:
        return ""
    n = float(bytes_)
    for unit in ["B", "KB", "MB", "GB"]:
        if n < 1024:
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def _build_mail(cliente: str, editor: Optional[str], items: list[dict], folder_id: Optional[str]) -> tuple[str, str, str]:
    """Devuelve (subject, body_text, body_html)."""
    n = len(items)
    plural = "s" if n != 1 else ""
    subject = f"🎬 Material nuevo de {cliente} ({n} archivo{plural})"

    saludo_editor = f"Editor responsable: {editor}" if editor else "⚠️ No encontré editor asignado en el Sheet"
    files_lines = []
    files_html = []
    for it in items:
        size_str = _format_size(it.get("size"))
        files_lines.append(f"  • {it['name']}" + (f"   ({size_str})" if size_str else ""))
        files_html.append(f"<li><code>{it['name']}</code>" + (f" <span style='color:#888'>· {size_str}</span>" if size_str else "") + "</li>")

    folder_line = ""
    folder_html = ""
    if folder_id:
        url = _drive_folder_url(folder_id)
        folder_line = f"\nCarpeta: {url}"
        folder_html = f'<p>Carpeta: <a href="{url}">{url}</a></p>'

    body_text = f"""Subió material nuevo de {cliente}.
{saludo_editor}

{n} archivo{plural} nuevo{plural}:
{chr(10).join(files_lines)}
{folder_line}

— Asistente Revolv
"""

    body_html = f"""<!DOCTYPE html>
<html>
<body style="font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; color:#222; max-width:600px;">
<h2 style="color:#000; margin-bottom:4px;">🎬 Material nuevo de {cliente}</h2>
<p style="color:#555; margin-top:0;"><strong>{saludo_editor}</strong></p>
<p>Llegaron <strong>{n} archivo{plural} nuevo{plural}</strong> al /Material/ del cliente:</p>
<ul style="line-height:1.6;">
{''.join(files_html)}
</ul>
{folder_html}
<hr style="border:none; border-top:1px solid #eee; margin:24px 0;">
<p style="color:#888; font-size:12px;">— Asistente Revolv</p>
</body>
</html>
"""
    return subject, body_text, body_html


def get_pending_unsent_grouped() -> dict:
    """Devuelve {(cliente, editor): [task_rows]} de tareas pendientes sin mail enviado."""
    conn = get_conn()
    rows = conn.execute("""
        SELECT t.id, t.cliente, t.editor, t.file_id, t.file_name, t.detected_at,
               kf.size, kf.folder_id as raw_folder_id, c.folder_id as client_folder_id
        FROM tasks t
        LEFT JOIN known_files kf ON kf.file_id = t.file_id
        LEFT JOIN clients c ON c.cliente = t.cliente
        WHERE t.status='pending' AND t.mail_sent_at IS NULL
        ORDER BY t.detected_at ASC
    """).fetchall()
    conn.close()

    grouped = defaultdict(list)
    for r in rows:
        grouped[(r["cliente"], r["editor"], r["client_folder_id"])].append({
            "task_id": r["id"],
            "name": r["file_name"],
            "size": r["size"],
            "detected_at": r["detected_at"],
        })
    return grouped


def mark_mail_sent(task_ids: list[int]):
    if not task_ids:
        return
    conn = get_conn()
    placeholders = ",".join("?" * len(task_ids))
    conn.execute(f"UPDATE tasks SET mail_sent_at = ? WHERE id IN ({placeholders})",
                 [now_iso(), *task_ids])
    conn.commit()
    conn.close()


def run(dry_run: bool = False, recipient: Optional[str] = None):
    to = recipient or TEST_EMAIL
    grouped = get_pending_unsent_grouped()

    if not grouped:
        print("✅ No hay tareas pendientes sin notificar.")
        return

    print(f"📧 {len(grouped)} mails a mandar (1 por cliente):\n")

    for (cliente, editor, folder_id), items in grouped.items():
        subject, body_text, body_html = _build_mail(cliente, editor, items, folder_id)
        task_ids = [it["task_id"] for it in items]

        print(f"   → [{cliente}] {len(items)} archivos · editor: {editor or '—'} · destinatario: {to}")
        if dry_run:
            print(f"     (dry-run, no se envía)")
            continue
        try:
            msg_id = send_mail(to=to, subject=subject, body_text=body_text, body_html=body_html)
            mark_mail_sent(task_ids)
            print(f"     ✅ enviado · msg_id={msg_id}")
        except Exception as e:
            print(f"     ❌ error: {e}")

    if dry_run:
        print("\n(dry-run, ningún mail se envió, ningún task se marcó)")


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--dry-run", action="store_true", help="muestra qué mandaría sin enviar")
    p.add_argument("--to", help="override del destinatario (default: TEST_EMAIL)")
    args = p.parse_args()
    run(dry_run=args.dry_run, recipient=args.to)

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
    """
    Devuelve {(cliente, editor): [task_rows]} de tareas pendientes sin mail enviado.

    EXCLUYE las tareas cargadas manualmente (file_name = '(pendiente cargado manualmente)'):
    esas no se notifican individualmente, solo aparecen en el resumen diario.
    """
    conn = get_conn()
    rows = conn.execute("""
        SELECT t.id, t.cliente, t.editor, t.file_id, t.file_name, t.detected_at,
               kf.size, kf.folder_id as raw_folder_id, c.folder_id as client_folder_id
        FROM tasks t
        LEFT JOIN known_files kf ON kf.file_id = t.file_id
        LEFT JOIN clients c ON c.cliente = t.cliente
        WHERE t.status='pending'
          AND t.mail_sent_at IS NULL
          AND t.file_name != '(pendiente cargado manualmente)'
          AND t.file_id NOT LIKE 'manual:%'
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

    from aliases import get_editor_email

    for (cliente, editor, folder_id), items in grouped.items():
        subject, body_text, body_html = _build_mail(cliente, editor, items, folder_id)
        task_ids = [it["task_id"] for it in items]
        editor_email = get_editor_email(editor)
        destinatarios = [to]
        if editor_email and editor_email.lower() != to.lower():
            destinatarios.append(editor_email)

        print(f"   → [{cliente}] {len(items)} archivos · editor: {editor or '—'} · destinatarios: {destinatarios}")
        if dry_run:
            print(f"     (dry-run, no se envía)")
            continue
        any_sent = False
        for dest in destinatarios:
            try:
                msg_id = send_mail(to=dest, subject=subject, body_text=body_text, body_html=body_html)
                print(f"     ✅ enviado a {dest} · msg_id={msg_id}")
                any_sent = True
            except Exception as e:
                print(f"     ❌ falló a {dest}: {e}")
        if any_sent:
            mark_mail_sent(task_ids)

    if dry_run:
        print("\n(dry-run, ningún mail se envió, ningún task se marcó)")


# ─── MAILS DE CIERRE (cuando se entrega un editado) ──────────────────────────

def send_completion_mails(cierres: list, recipient: Optional[str] = None) -> int:
    """
    Manda mails cuando se entrega un video.

    Cada cierre = un editado entregado. El mail varía si quedan más pendientes
    o si completó todo (count llegó a 0).

    `cierres` = [{"cliente", "editor", "file_name", "new_count", "closed"}, ...]
    """
    if not cierres:
        return 0

    to = recipient or TEST_EMAIL
    sent = 0
    for c in cierres:
        cliente = c["cliente"]
        editor = c.get("editor") or "—"
        file_name = c["file_name"]
        file_id = c.get("file_id")
        client_folder_id = c.get("client_folder_id")
        new_count = c.get("new_count", 0)
        closed = c.get("closed", False)

        # Link a la CARPETA del cliente (tenés acceso garantizado vía tu Drive).
        # El link directo al file_id no funciona si el archivo es propiedad del cliente.
        if client_folder_id:
            video_url = f"https://drive.google.com/drive/folders/{client_folder_id}"
            video_label = f"Ver carpeta de {cliente}"
        elif file_id:
            video_url = f"https://drive.google.com/file/d/{file_id}/view"
            video_label = "Ver video en Drive"
        else:
            video_url = None
            video_label = None

        if closed:
            subject = f"✅ {editor} completó {cliente}"
            estado_text = f"{editor} entregó el último video de {cliente}. Cliente cerrado en el dashboard."
            estado_html = f"<p>{editor} entregó el <strong>último video</strong> de <strong>{cliente}</strong>. Cliente cerrado en el dashboard.</p>"
        else:
            plural = "s" if new_count != 1 else ""
            subject = f"📹 {editor} entregó 1 video de {cliente} ({new_count} restante{plural})"
            estado_text = f"{editor} entregó 1 video de {cliente}.\nQuedan {new_count} video{plural} pendiente{plural}."
            estado_html = f"<p>{editor} entregó 1 video de <strong>{cliente}</strong>. Quedan <strong>{new_count} video{plural}</strong> pendiente{plural}.</p>"

        link_text = f"\n📁 {video_label}: {video_url}\n" if video_url else ""
        link_html = f'<p style="margin: 20px 0;"><a href="{video_url}" style="background:#ff4747;color:white;padding:10px 18px;border-radius:6px;text-decoration:none;font-weight:600;">📁 {video_label}</a></p>' if video_url else ""

        text = f"""Buenas,

{estado_text}

Archivo: {file_name}{link_text}

— Asistente Revolv
"""
        html = f"""<!DOCTYPE html>
<html><body style="font-family:-apple-system,Segoe UI,sans-serif;max-width:600px;color:#222;line-height:1.5;">
<h2>{subject}</h2>
{estado_html}
<p style="color:#666;font-size:13px;">Archivo: <code>{file_name}</code></p>
{link_html}
<hr style="border:none;border-top:1px solid #eee;margin:24px 0;">
<p style="color:#888;font-size:12px;">— Asistente Revolv</p>
</body></html>
"""
        try:
            msg_id = send_mail(to=to, subject=subject, body_text=text, body_html=html)
            print(f"  ✅ mail cierre enviado: {editor} → {cliente} (msg_id={msg_id})")
            sent += 1
        except Exception as e:
            print(f"  ❌ falló mail cierre [{cliente}]: {e}")

    return sent


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--dry-run", action="store_true", help="muestra qué mandaría sin enviar")
    p.add_argument("--to", help="override del destinatario (default: TEST_EMAIL)")
    args = p.parse_args()
    run(dry_run=args.dry_run, recipient=args.to)

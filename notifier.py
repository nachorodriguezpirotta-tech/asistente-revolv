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

try:
    from config import TEST_EMAIL
except ImportError:
    # En Vercel, los endpoints (api/review.py etc.) insertan api/ primero en
    # sys.path y 'config' resuelve a api/config.py (el ENDPOINT de config) →
    # ImportError. Bug 11/jun: el aviso de "revisión pedida" NUNCA salía desde
    # Vercel por esto. Cargar el config.py de la RAÍZ por ruta absoluta.
    import os as _os, importlib.util as _ilu
    _p = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "config.py")
    _s = _ilu.spec_from_file_location("_root_config", _p)
    _m = _ilu.module_from_spec(_s)
    _s.loader.exec_module(_m)
    TEST_EMAIL = _m.TEST_EMAIL
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
    """Devuelve (subject, body_text, body_html).

    Si los items tienen subfolder_name no vacío, agrupa por subfolder y
    presenta como 'N videos' (= N subfolders distintas con material).
    Cada subfolder de /Material/ típicamente representa UN video distinto
    para algunos clientes (ej. Alberto Carretero: V58, V60, V61, V63...).
    """
    n = len(items)
    plural_a = "s" if n != 1 else ""

    # Agrupar items por subfolder
    by_sub = {}
    for it in items:
        sub = (it.get("subfolder_name") or "").strip()
        by_sub.setdefault(sub, []).append(it)

    # Subfolders con material (sin contar root '')
    subs_with_material = [s for s in by_sub.keys() if s]
    n_videos = len(subs_with_material)

    if n_videos >= 2:
        # Caso "N videos en M carpetas": resaltarlo en el subject
        subject = f"🎬 {cliente} subió {n_videos} videos ({n} archivo{plural_a})"
        intro_text = f"Subió material nuevo de {cliente}: {n_videos} videos distintos (1 por subcarpeta), {n} archivo{plural_a} en total."
        intro_html = (f"<p>Subió material nuevo: <strong>{n_videos} videos distintos</strong> "
                      f"({n} archivo{plural_a} en total). Cada subcarpeta es un video:</p>")
    else:
        # Caso simple: todo junto
        subject = f"🎬 Material nuevo de {cliente} ({n} archivo{plural_a})"
        intro_text = f"Subió material nuevo de {cliente} ({n} archivo{plural_a})."
        intro_html = f"<p>Llegaron <strong>{n} archivo{plural_a} nuevo{plural_a}</strong> al /Material/ del cliente:</p>"

    saludo_editor = f"Editor responsable: {editor}" if editor else "⚠️ No encontré editor asignado en el Sheet"

    # Texto: agrupar por sub
    text_blocks = []
    html_blocks = []
    # Mostrar subs con material primero, ordenadas alfabéticamente
    for sub in sorted(subs_with_material):
        sub_items = by_sub[sub]
        text_blocks.append(f"\n📁 {sub}  ({len(sub_items)} archivo{'s' if len(sub_items)!=1 else ''})")
        html_blocks.append(f'<h4 style="margin:18px 0 4px;color:#1a4d8a;">📁 {sub}  '
                            f'<span style="color:#888;font-weight:400;font-size:13px;">'
                            f'({len(sub_items)} archivo{"s" if len(sub_items)!=1 else ""})</span></h4><ul style="line-height:1.5;margin:4px 0 12px;">')
        for it in sub_items[:15]:
            size_str = _format_size(it.get("size"))
            text_blocks.append(f"  • {it['name']}" + (f"   ({size_str})" if size_str else ""))
            html_blocks.append(f"<li><code>{it['name']}</code>" + (f" <span style='color:#888'>· {size_str}</span>" if size_str else "") + "</li>")
        if len(sub_items) > 15:
            text_blocks.append(f"  ... y {len(sub_items)-15} archivos más")
            html_blocks.append(f"<li style='color:#888;'>... y {len(sub_items)-15} archivos más</li>")
        html_blocks.append("</ul>")

    # Si hay archivos en root (sin subfolder), listarlos al final como "sueltos"
    root_items = by_sub.get("", [])
    if root_items:
        text_blocks.append(f"\n📂 (sueltos en /Material/ raíz)  ({len(root_items)} archivo{'s' if len(root_items)!=1 else ''})")
        html_blocks.append(f'<h4 style="margin:18px 0 4px;color:#666;">📂 (sueltos en /Material/ raíz)  '
                            f'<span style="color:#888;font-weight:400;font-size:13px;">'
                            f'({len(root_items)} archivo{"s" if len(root_items)!=1 else ""})</span></h4><ul style="line-height:1.5;margin:4px 0 12px;">')
        for it in root_items[:15]:
            size_str = _format_size(it.get("size"))
            text_blocks.append(f"  • {it['name']}" + (f"   ({size_str})" if size_str else ""))
            html_blocks.append(f"<li><code>{it['name']}</code>" + (f" <span style='color:#888'>· {size_str}</span>" if size_str else "") + "</li>")
        if len(root_items) > 15:
            text_blocks.append(f"  ... y {len(root_items)-15} archivos más")
            html_blocks.append(f"<li style='color:#888;'>... y {len(root_items)-15} archivos más</li>")
        html_blocks.append("</ul>")

    folder_line = ""
    folder_html = ""
    if folder_id:
        url = _drive_folder_url(folder_id)
        folder_line = f"\nCarpeta del cliente: {url}"
        folder_html = f'<p style="margin-top:20px;">📁 <a href="{url}">{url}</a></p>'

    body_text = f"""{intro_text}
{saludo_editor}
{"".join(text_blocks)}
{folder_line}

— Asistente Revolv
"""

    body_html = f"""<!DOCTYPE html>
<html>
<body style="font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; color:#222; max-width:600px;">
<h2 style="color:#000; margin-bottom:4px;">🎬 {cliente}</h2>
<p style="color:#555; margin-top:0;"><strong>{saludo_editor}</strong></p>
{intro_html}
{''.join(html_blocks)}
{folder_html}
<hr style="border:none; border-top:1px solid #eee; margin:24px 0;">
<p style="color:#888; font-size:12px;">— Asistente Revolv</p>
</body>
</html>
"""
    return subject, body_text, body_html


def get_pending_unsent_grouped() -> dict:
    """
    Devuelve {(cliente, editor, client_folder_id): [archivos_nuevos]} de
    tareas pendientes sin mail enviado.

    Para cada task pending sin mail, en vez de listar SOLO el file_name de
    la task, hace un JOIN con known_files para listar TODOS los crudos
    recientes del cliente (no baseline). Así si entraron 15 archivos en un
    batch para una task, el mail los lista a todos.

    Estrategia: para una task pending sin mail, buscar crudos del cliente
    con first_seen_at >= (detected_at - margen) Y is_baseline=0. Margen
    chico para no incluir archivos viejos.

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

    grouped = defaultdict(list)
    seen_files_per_group = defaultdict(set)  # evita duplicar mismo file en items
    for r in rows:
        key = (r["cliente"], r["editor"], r["client_folder_id"])
        # Para cada task, sumar TODOS los known_files del cliente no-baseline
        # con first_seen_at >= last 48h. Cap a 30 archivos para no inflar mails.
        from datetime import datetime, timedelta
        cutoff = (datetime.now() - timedelta(hours=48)).isoformat(timespec="seconds")
        files = conn.execute("""
            SELECT file_id, name, size, first_seen_at, subfolder_name
            FROM known_files
            WHERE TRIM(cliente)=TRIM(?)
              AND COALESCE(is_baseline, 0) = 0
              AND first_seen_at >= ?
            ORDER BY first_seen_at DESC
            LIMIT 50
        """, (r["cliente"], cutoff)).fetchall()
        # Si no hay archivos recientes (caso raro), fallback al archivo de la task
        if not files:
            if r["file_id"] not in seen_files_per_group[key]:
                grouped[key].append({
                    "task_id": r["id"], "name": r["file_name"],
                    "size": r["size"], "detected_at": r["detected_at"],
                    "subfolder_name": "",
                })
                seen_files_per_group[key].add(r["file_id"])
        else:
            for fr in files:
                if fr["file_id"] in seen_files_per_group[key]:
                    continue
                grouped[key].append({
                    "task_id": r["id"],  # asociamos al primer task del par
                    "name": fr["name"],
                    "size": fr["size"],
                    "detected_at": fr["first_seen_at"],
                    "subfolder_name": fr["subfolder_name"] or "",
                })
                seen_files_per_group[key].add(fr["file_id"])
    conn.close()
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

    from aliases import get_editor_email_for_notification

    for (cliente, editor, folder_id), items in grouped.items():
        try:
            from tracker import is_client_archived
            if is_client_archived(cliente):
                print(f"   🗄️ {cliente} archivado — skip notificación de material")
                continue
        except Exception:
            pass
        subject, body_text, body_html = _build_mail(cliente, editor, items, folder_id)
        task_ids = [it["task_id"] for it in items]
        editor_email = get_editor_email_for_notification(editor)
        destinatarios = [to]
        if editor_email and editor_email.lower() != to.lower():
            destinatarios.append(editor_email)

        print(f"   → [{cliente}] {len(items)} archivos · editor: {editor or '—'} · destinatarios: {destinatarios}")
        if dry_run:
            print(f"     (dry-run, no se envía)")
            continue
        # Dedupe por (cliente_normalizado, editor) — NO por task_ids.
        # Bug 02/jun Jennifer Díaz: el scan completo re-detecta crudos viejos
        # ("Copia de C0042.mp4" etc) cada hora porque los claims se pierden por
        # concurrencia git (incremental pisa el push del completo). Cada vez
        # crea tasks con IDs nuevos → el dedupe por task_ids fallaba → mail
        # repetido todo el día.
        # Solución: dedupe por cliente+editor normalizado, ventana 20h. Así
        # máximo 1 mail "material nuevo de X" por día, sin importar cuántas
        # veces se re-detecten/re-creen las tasks.
        import unicodedata as _ud
        def _norm_cli(s):
            s = _ud.normalize("NFD", s or "")
            s = "".join(c for c in s if _ud.category(c) != "Mn")
            return " ".join(s.lower().split())
        # FIRMA DE MATERIAL: set ordenado de subcarpetas (= videos) presentes.
        # Bug 08/jun Álvaro Gutiérrez: subió Video 2/3/4 el mismo día y NO se
        # notificó porque el dedupe por cliente+editor (ventana 20h) tapaba todo.
        # Incluyendo las subcarpetas en la clave: material re-detectado (mismas
        # subcarpetas) sigue deduplicándose (bug Jennifer intacto), pero un VIDEO
        # NUEVO (subcarpeta nueva) cambia la firma → se notifica aunque sea el
        # mismo día. Es como lo piensa Ignacio: cada subcarpeta = un video.
        _subs = sorted({(it.get("subfolder_name") or "").strip().lower()
                        for it in items if (it.get("subfolder_name") or "").strip()})
        _mat_sig = "+".join(_subs) if _subs else "root"
        notif_dedupe_key = f"notif-pending|{_norm_cli(cliente)}|{(editor or '').strip().lower()}|{_mat_sig}"

        any_sent = False
        for dest in destinatarios:
            try:
                # ventana 1200 min = 20h → 1 mail por cliente por día
                msg_id = send_mail(to=dest, subject=subject, body_text=body_text, body_html=body_html,
                                   dedupe_window_minutes=1200,
                                   dedupe_key_override=f"{notif_dedupe_key}|{dest.lower()}")
                print(f"     ✅ enviado a {dest} · msg_id={msg_id}")
                any_sent = True
            except Exception as e:
                print(f"     ❌ falló a {dest}: {e}")
        if any_sent:
            mark_mail_sent(task_ids)

        # Mandar push notification además del mail
        try:
            from push_sender import send_push
            push_body = f"{len(items)} archivo{'s' if len(items) != 1 else ''} nuevo{'s' if len(items) != 1 else ''}"
            push_title = f"🎬 {cliente}"
            push_url = f"/?admin=1"
            # A admin
            send_push(editor=None, title=push_title, body=push_body, url=push_url, tag=f"crudo-{cliente}")
            # Al editor
            if editor:
                send_push(editor=editor, title=push_title, body=push_body, url=f"/?editor={editor}", tag=f"crudo-{cliente}")
        except Exception as e:
            print(f"     ⚠️ push: {e}")

    if dry_run:
        print("\n(dry-run, ningún mail se envió, ningún task se marcó)")


# ─── MAILS DE CIERRE (cuando se entrega un editado) ──────────────────────────

def send_completion_mails(cierres: Optional[list] = None, recipient: Optional[str] = None) -> int:
    """
    Manda mails cuando se entrega un video. Lee de la cola persistente
    `pending_completion_mails` (cualquier mail que haya quedado sin enviar
    de scans anteriores) y los manda. Si se pasa una lista `cierres` adicional,
    también se incluye (pero ya debería estar en la cola desde el closer).

    El mail varía si quedan más pendientes o si completó todo (count llegó a 0).
    """
    from tracker import (
        list_pending_completion_mails, mark_completion_mail_failed,
        claim_completion_mail,
    )

    # Leer cola persistente
    queue_items = list_pending_completion_mails(max_age_days=7)
    if not queue_items and not cierres:
        return 0

    to = recipient or TEST_EMAIL
    sent = 0

    # CLAIM ATÓMICO: marcar cada row como "siendo enviada" ANTES de mandar mail.
    # Si otro proceso ya la claimó (claim retorna False), saltarla.
    # Esto previene duplicados cuando dos workflows corren la misma cola.
    items_to_send = []
    for q in queue_items:
        if claim_completion_mail(q["id"]):
            items_to_send.append(q)
        # else: ya claimada por otro, skip silencioso

    # Cierres in-memory que NO están en la cola persistente (no deberían existir
    # con el nuevo flujo, pero por compat)
    if cierres:
        queue_file_ids = {q.get("file_id") for q in queue_items}
        for c in cierres:
            if c.get("file_id") not in queue_file_ids:
                items_to_send.append(c)

    for c in items_to_send:
        cliente = c["cliente"]
        # Cliente ARCHIVADO → no mandar NADA (ni completion ni delivery).
        # El item ya fue claimado, así que no se reintenta — muere acá.
        try:
            from tracker import is_client_archived
            if is_client_archived(cliente):
                print(f"   🗄️ {cliente} archivado — skip completion mail de {c.get('file_name','?')[:40]}")
                continue
        except Exception:
            pass
        editor = c.get("editor") or "—"
        file_name = c["file_name"]
        file_id = c.get("file_id")
        client_folder_id = c.get("client_folder_id")
        edited_folder_id = c.get("edited_folder_id")
        new_count = c.get("new_count", 0)
        closed = c.get("closed", False)
        is_correction = bool(c.get("is_correction"))

        # DEDUPE DEFINITIVO vía Drive appProperties (atómico, no depende de git).
        # Si el archivo de Drive ya está marcado como "mail mandado", saltear.
        # Esto resuelve el bug crónico de mails duplicados: el dedupe por
        # mail_log fallaba cuando el registro se perdía en un push concurrente.
        # Drive es la fuente de verdad compartida. Pedido Ignacio 07/jun.
        if file_id and not is_correction:
            try:
                from drive_client import drive_was_mail_sent
                if drive_was_mail_sent(file_id, "completion"):
                    print(f"  ⏭️  Drive dedupe: mail de {file_name} ya mandado, SKIP")
                    # marcar la cola como enviada para que no se reintente
                    row_id = c.get("id")
                    if isinstance(row_id, int):
                        from tracker import mark_completion_mail_sent
                        try:
                            mark_completion_mail_sent(row_id)
                        except Exception:
                            pass
                    continue
            except Exception:
                pass

        # Link a la CARPETA específica donde está el editado (ej. Mayo/Editados, Pack 1).
        # Tenemos varios fallbacks por si una opción no está disponible:
        if edited_folder_id:
            video_url = f"https://drive.google.com/drive/folders/{edited_folder_id}"
            video_label = "Ver carpeta del editado"
        elif client_folder_id:
            video_url = f"https://drive.google.com/drive/folders/{client_folder_id}"
            video_label = f"Ver carpeta de {cliente}"
        elif file_id:
            video_url = f"https://drive.google.com/file/d/{file_id}/view"
            video_label = "Ver video en Drive"
        else:
            video_url = None
            video_label = None

        if is_correction:
            # Corrección: NO decuenta del pending. Solo informa.
            # Buscar cuándo se entregó el ORIGINAL (mail_log) para mostrarlo —
            # confirma de un vistazo que es el video correcto. Pedido 10/jun.
            original_line_text = ""
            original_line_html = ""
            try:
                import re as _re
                from tracker import get_conn as _gc
                _base = _re.sub(r"\.[a-z0-9]+$", "", (file_name or "").lower().strip())
                _conn = _gc()
                for _m in _conn.execute(
                    "SELECT subject, sent_at FROM mail_log WHERE kind='completion' "
                    "AND success=1 AND TRIM(LOWER(cliente))=TRIM(LOWER(?)) "
                    "ORDER BY sent_at DESC LIMIT 200", (cliente,)):
                    if _base and _base in (_m["subject"] or "").lower() and "🔧" not in (_m["subject"] or ""):
                        _f = _m["sent_at"][:10]
                        _f = f"{_f[8:10]}/{_f[5:7]}"
                        original_line_text = f"\nEntrega original: {_f}."
                        original_line_html = (f"<p style='color:#666;font-size:13px;'>"
                                              f"📅 Entrega original: <strong>{_f}</strong>.</p>")
                        break
                _conn.close()
            except Exception:
                pass
            subject = f"🔧 Corrección: {editor} subió de nuevo {file_name} de {cliente}"
            estado_text = (
                f"{editor} subió una CORRECCIÓN del video {file_name} de {cliente}.\n"
                f"El pending count NO cambia (la corrección no cuenta como entrega nueva)."
                f"{original_line_text}"
            )
            estado_html = (
                f"<p>{editor} subió una <strong>corrección</strong> de "
                f"<code>{file_name}</code> de <strong>{cliente}</strong>.</p>"
                f"{original_line_html}"
                f"<p style='color:#666;font-size:13px;'>El pending count NO cambia. "
                f"La corrección reemplaza el editado previo del mismo video.</p>"
            )
            # Dedupe estable: misma corrección (mismo cliente + mismo stem de archivo)
            # debe deduplicarse aunque el editor se haya detectado distinto en
            # otro worker (bug "Agus" vs "—" reportado por Ignacio 21/may).
            try:
                from tracker import _correction_stem
                _stem = _correction_stem(file_name)
            except Exception:
                _stem = (file_name or "").lower().strip()
            _correction_dedupe_key = f"correction-admin|{cliente.strip().lower()}|{_stem}"
        else:
            # Mail unificado: SIEMPRE el formato "entregó 1 video de X". Incluye
            # el nombre del archivo en el subject para que Ignacio sepa qué subió
            # sin tener que abrir el mail. Antes había un subject separado
            # "✅ completó cliente" cuando era el último video, pero el user pidió
            # eliminarlo porque ya tiene el mail del archivo y le basta con eso.
            if closed:
                rest_text = "0 restantes (cliente cerrado)"
            else:
                plural = "s" if new_count != 1 else ""
                rest_text = f"{new_count} restante{plural}"
            subject = f"📹 {editor} entregó {file_name} de {cliente} ({rest_text})"
            estado_text = f"{editor} entregó 1 video de {cliente}.\n{rest_text}."
            estado_html = f"<p>{editor} entregó 1 video de <strong>{cliente}</strong>. <strong>{rest_text}</strong>.</p>"
            # Dedupe estable por file_id: si dos workers procesan el MISMO archivo
            # (incremental + audit, p.ej.), el counter "(N restantes)" puede ser
            # distinto entre ellos porque cada uno decremental pending. El subject
            # cambia → el dedupe por subject NO atrapa → mail duplicado.
            # Bug reportado 21/may: V61/V62 Alberto, 2 mails cada uno con counters
            # distintos ("1 restante" vs "0 restantes (cliente cerrado)").
            # Fix: dedupe_key basado en file_id (idéntico para el mismo archivo).
            _completion_dedupe_key = f"completion-admin|{cliente.strip().lower()}|{file_id or file_name}"

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
        # MAILS DE CIERRE: SOLO al admin (Ignacio), NO al editor.
        # Razón: el editor sabe perfectamente que entregó. No le sirve recibir
        # un mail diciéndole 'entregaste X'. El admin sí quiere enterarse.
        # Cambio por pedido directo del user.
        destinatarios = [to]

        any_sent = False
        for dest in destinatarios:
            try:
                # dedupe_window=6h: si dos workers de GHA procesaron en paralelo
                # el mismo editado y mandaron el mismo mail, mail_log lo atrapa y
                # evitamos el duplicado (bug reportado por Ignacio 20/may).
                # Subido de 30min a 6h porque cuando un scan falla al pushear
                # tracker.db (bug 21/may con tracker.db conflict), el siguiente
                # scan re-procesa el archivo. Con ventana 30min, si pasó más,
                # se mandaba duplicado. 6h cubre casi cualquier retraso de retry.
                # Para correcciones, usamos dedupe_key_override estable (no depende
                # del editor ni del file_id) para atrapar el caso "Agus" vs "—".
                _override = _correction_dedupe_key if is_correction else _completion_dedupe_key
                # ventana 7 días (10080 min): el scan completo re-detecta
                # editados viejos por días cuando los claims no persisten. Con
                # 20h se re-mandaba a los 2 días (bug 04/jun Natalia Pereyra
                # "5. reel 5" re-enviado 2 días después). 7 días por file_id
                # es seguro: el mismo archivo físico no se re-notifica en una
                # semana, y para entonces el filtro de "editado viejo >3 días"
                # ya lo marcó baseline → deja de detectarse.
                msg_id = send_mail(to=dest, subject=subject, body_text=text, body_html=html,
                                   kind="completion", cliente=cliente, editor=editor,
                                   dedupe_window_minutes=10080,
                                   dedupe_key_override=_override)
                print(f"  ✅ mail cierre enviado a {dest}: {editor} → {cliente} (msg_id={msg_id})")
                any_sent = True
                sent += 1
            except Exception as e:
                print(f"  ❌ falló mail cierre a {dest} [{cliente}]: {e}")

        # Marcar el archivo en Drive como "mail mandado" — dedupe atómico que
        # NO depende de git. Aunque tracker.db/mail_log se pierda en un push
        # concurrente, este marcador persiste → no se re-manda nunca.
        if any_sent and file_id and not is_correction:
            try:
                from drive_client import drive_mark_mail_sent
                drive_mark_mail_sent(file_id, "completion")
            except Exception:
                pass

        # Push notification de cierre: SOLO al admin también (mismo criterio)
        if any_sent:
            try:
                from push_sender import send_push
                if closed:
                    push_title = f"✅ {cliente} completado"
                    push_body = f"{editor} entregó el último video"
                else:
                    push_title = f"📹 {cliente}"
                    push_body = f"{editor} entregó 1 video — quedan {new_count}"
                send_push(editor=None, title=push_title, body=push_body, url="/?admin=1", tag=f"cierre-{cliente}")
                # NO se manda push al editor (igual que el mail)
            except Exception as e:
                print(f"     ⚠️ push cierre: {e}")

        # Corrección: SIEMPRE avisar al cliente (sea que haya review pendiente o no).
        # Y best-effort marcar review pendiente como resuelta si matchea.
        if any_sent and is_correction:
            review_resolved_id = None
            try:
                from tracker import mark_review_resolved_for_client_video, get_client_review
                review_resolved_id = mark_review_resolved_for_client_video(cliente, file_name)
                if review_resolved_id:
                    review = get_client_review(review_resolved_id)
                    if review:
                        notify_revision_resolved(review_resolved_id, review)
            except Exception as e:
                print(f"     ⚠️ resolver revisión: {e}")
            # Si NO había review match (Ely subió cambios pero el review no estaba
            # registrado en el sistema, o el cliente nunca usó el form), igual
            # mandar mail al cliente avisándole que está la versión corregida.
            # Bug reportado por Ignacio 21/may: a Ely no le llegó nada.
            if not review_resolved_id:
                try:
                    notify_correction_ready_to_client(
                        cliente=cliente, file_name=file_name,
                        edited_folder_id=edited_folder_id,
                        client_folder_id=client_folder_id,
                        file_id=file_id,
                    )
                except Exception as e:
                    print(f"     ⚠️ mail cliente corrección: {e}")

        # MAIL AL CLIENTE (si está activado en cfg_clients). Solo si NO es
        # corrección. La corrección ya genera notify_revision_resolved /
        # notify_correction_ready_to_client arriba.
        if any_sent and not is_correction:
            try:
                send_client_delivery_mail(
                    cliente=cliente, file_name=file_name,
                    edited_folder_id=edited_folder_id,
                    client_folder_id=client_folder_id,
                    file_id=file_id,
                    editor=editor,
                )
            except Exception as e:
                print(f"     ⚠️ mail cliente: {e}")

        # Si el mail falló (any_sent=False), revertir el claim para que otro intento futuro
        # pueda reintentarlo. claim_completion_mail ya marcó mail_sent_at, así que en caso
        # de falla, lo desmarcamos.
        row_id = c.get("id")
        if row_id is not None and isinstance(row_id, int):
            if not any_sent:
                # Revertir: queremos retry en próximo scan
                from tracker import get_conn
                conn = get_conn()
                conn.execute(
                    "UPDATE pending_completion_mails SET mail_sent_at = NULL WHERE id = ?",
                    (row_id,),
                )
                conn.commit()
                conn.close()
                mark_completion_mail_failed(row_id)
            # Si any_sent=True, ya está marcado correctamente (claim lo marcó)

    return sent


# ─── MAIL AL CLIENTE: "tu video está listo" ──────────────────────────────────

def _build_vercel_url(path: str) -> str:
    """URL base para links en mails. Hoy hardcoded; podría salir de env."""
    return f"https://asistente-revolv.vercel.app{path}"


def send_client_delivery_mail(cliente: str, file_name: str,
                                edited_folder_id: Optional[str],
                                client_folder_id: Optional[str],
                                file_id: Optional[str],
                                editor: Optional[str] = None) -> bool:
    """Manda mail al cliente avisando que se entregó un video. SOLO si está
    en cfg_clients con notifications_enabled=1.

    Importante: NO mencionar nombre del editor (decisión del admin). Firma
    como "Nacho · Revolv". Link a la carpeta donde está el editado.

    Retorna True si se mandó, False si el cliente no estaba activado o falló.
    """
    from tracker import cfg_client_should_be_notified, is_client_archived
    if is_client_archived(cliente):
        return False
    target = cfg_client_should_be_notified(cliente)
    if not target:
        return False

    to_email = target["email"]
    display = target["display_name"] or cliente.split()[0]

    # Link al folder donde está el editado, con fallbacks
    if edited_folder_id:
        folder_url = f"https://drive.google.com/drive/folders/{edited_folder_id}"
    elif client_folder_id:
        folder_url = f"https://drive.google.com/drive/folders/{client_folder_id}"
    elif file_id:
        folder_url = f"https://drive.google.com/file/d/{file_id}/view"
    else:
        folder_url = None

    # Incluir nombre del archivo en el subject para que (a) el cliente vea
    # de qué video se trata, y (b) el dedupe por (to+subject) no bloquee
    # videos distintos del mismo cliente con la misma frase fija.
    _short_name = (file_name or "").rsplit(".", 1)[0][:60]
    subject = f"🎬 Tu video está listo — {_short_name}" if _short_name else "🎬 Tu video está listo"

    text = f"""Hola {display}!

Nacho te subió tu video nuevo:

  📹 {file_name}
"""
    if folder_url:
        text += f"\nEstá en tu carpeta de Drive:\n{folder_url}\n"
    text += "\nCualquier cambio que quieras hacer, avisame.\n\nUn abrazo,\nNacho\nRevolv\n"

    # Construir URLs de los botones SIN crear review pending por adelantado.
    # La review SOLO se crea cuando el cliente realmente toca "Quiero ajustar
    # algo" y envía el form (status='revision_requested' directo).
    # Si el cliente toca "Todo perfecto" o ignora el mail → no se registra
    # nada → no aparece en el dashboard de revisiones.
    # Pedido directo de Ignacio 21/may: 'solo quiero que aparezcan revisiones
    # cuando alguien toca revisar mi video y pone una revisión'.
    review_url_approve = None
    review_url_revise = None
    try:
        from api._shared import make_client_token
        token = make_client_token(cliente)
        # Los links llevan file_id + cliente; el endpoint crea la review
        # on-demand cuando el cliente pide cambios.
        import urllib.parse
        params_approve = urllib.parse.urlencode({
            "action": "approve",
            "cliente": cliente,
            "file_id": file_id or "",
            "file_name": file_name or "",
            "editor": editor or "",
            "t": token,
        })
        params_revise = urllib.parse.urlencode({
            "cliente": cliente,
            "file_id": file_id or "",
            "file_name": file_name or "",
            "editor": editor or "",
            "t": token,
        })
        review_url_approve = _build_vercel_url(f"/api/review?{params_approve}")
        review_url_revise = f"https://revolv-portal.vercel.app/r?{params_revise}"
    except Exception as e:
        print(f"   ⚠️ no se pudieron generar URLs de revisión: {e}")

    folder_button_html = (
        f'<div style="text-align:center;margin:28px 0 8px;">'
        f'<a href="{folder_url}" style="display:inline-block;background:#ff4747;color:white;'
        f'padding:14px 32px;border-radius:8px;text-decoration:none;font-weight:600;font-size:15px;">'
        f'📁 Ver en Drive</a></div>'
    ) if folder_url else ""

    # Sección "¿te gustó?" con dos botones grandes
    review_section_html = ""
    review_section_text = ""
    if review_url_approve and review_url_revise:
        review_section_html = f"""
      <div style="margin-top:32px;padding-top:24px;border-top:1px solid #eee;">
        <p style="font-size:15px;color:#333;text-align:center;margin:0 0 18px;font-weight:600;">
          ¿Te gustó cómo quedó?
        </p>
        <table style="margin:0 auto;border-collapse:collapse;">
          <tr>
            <td style="padding:0 6px;">
              <a href="{review_url_approve}" style="display:inline-block;background:#22c55e;color:white;padding:14px 24px;border-radius:8px;text-decoration:none;font-weight:600;font-size:14px;">
                ✅ Todo perfecto
              </a>
            </td>
            <td style="padding:0 6px;">
              <a href="{review_url_revise}" style="display:inline-block;background:#f3f4f6;color:#111;padding:14px 24px;border-radius:8px;text-decoration:none;font-weight:600;font-size:14px;border:1px solid #ddd;">
                📝 Quiero ajustar algo
              </a>
            </td>
          </tr>
        </table>
        <p style="font-size:12px;color:#999;text-align:center;margin:14px 0 0;">
          Si tocás "ajustar algo" te abre un formulario chiquito para que me cuentes qué cambiar.
        </p>
      </div>
"""
        review_section_text = (
            f"\n¿Te gustó cómo quedó?\n"
            f"   ✅ Todo perfecto:       {review_url_approve}\n"
            f"   📝 Quiero ajustar algo: {review_url_revise}\n"
        )

    text = f"""Hola {display}!

Nacho te subió tu video nuevo:

  📹 {file_name}
"""
    if folder_url:
        text += f"\nEstá en tu carpeta de Drive:\n{folder_url}\n"
    text += review_section_text
    text += "\nUn abrazo,\nNacho\nRevolv\n"

    html = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"></head>
<body style="margin:0;padding:0;background:#f5f5f5;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;color:#222;">
  <div style="max-width:560px;margin:0 auto;padding:32px 24px;">
    <div style="text-align:center;margin-bottom:28px;">
      <div style="font-size:14px;letter-spacing:2px;color:#888;text-transform:uppercase;font-weight:600;">REVOLV</div>
    </div>
    <div style="background:white;border-radius:14px;padding:32px 28px;box-shadow:0 2px 12px rgba(0,0,0,0.04);">
      <h1 style="margin:0 0 16px;font-size:24px;color:#111;font-weight:700;">🎬 Tu video está listo</h1>
      <p style="font-size:16px;line-height:1.55;color:#333;margin:0 0 24px;">
        Hola <strong>{display}</strong>! Te subí un video nuevo a tu carpeta:
      </p>
      <div style="background:#f8f8f8;border-left:4px solid #ff4747;padding:14px 18px;border-radius:6px;margin-bottom:28px;">
        <div style="font-size:12px;color:#888;text-transform:uppercase;letter-spacing:0.5px;font-weight:600;margin-bottom:4px;">VIDEO</div>
        <div style="font-size:16px;color:#111;font-weight:600;word-break:break-word;">{file_name}</div>
      </div>
      {folder_button_html}
      {review_section_html}
    </div>
    <div style="text-align:center;margin-top:28px;color:#888;font-size:13px;line-height:1.6;">
      Un abrazo,<br>
      <strong style="color:#222;">Nacho</strong><br>
      <span style="font-size:11px;letter-spacing:1px;text-transform:uppercase;color:#aaa;">REVOLV</span>
    </div>
  </div>
</body></html>
"""
    try:
        # dedupe 7 días + key estable por (cliente, file_id): el mismo video al
        # mismo cliente no se manda 2 veces aunque el scan re-detecte el
        # editado por días (bug concurrencia). Key independiente del subject.
        _cli_override = f"client-delivery|{cliente.strip().lower()}|{file_id or file_name}"
        msg_id = send_mail(to=to_email, subject=subject, body_text=text, body_html=html,
                            dedupe_window_minutes=10080,
                            dedupe_key_override=_cli_override)
        print(f"   📧 mail al cliente {cliente} ({to_email}): {file_name} (msg_id={msg_id})")
        return True
    except Exception as e:
        print(f"   ⚠️ falló mail al cliente {cliente} ({to_email}): {e}")
        return False


# ─── NOTIFICACIONES DE REVISIONES ────────────────────────────────────────────

def notify_revision_requested(review_id: int, review: dict, notes: str) -> None:
    """Cliente pidió revisión → mail + push al editor + admin.
    Si la revisión tiene attachments (fotos), incluye links + thumbnails
    en el mail."""
    cliente = review.get("cliente", "?")
    editor = review.get("editor")
    # Las reviews del portal suelen llegar SIN editor → resolverlo por cliente
    # para que el aviso le llegue TAMBIÉN al editor asignado (pedido 11/jun:
    # "cada correc tiene que llegarme a mí y al editor asignado").
    if not (editor or "").strip():
        try:
            from tracker import resolve_editor_for_cliente
            editor = resolve_editor_for_cliente(cliente) or editor
        except Exception:
            pass
    video = review.get("video_file_name", "(video)")

    # Listar attachments si hay
    attachments_info = []
    try:
        from tracker import list_attachments_for_review
        attachments_info = list_attachments_for_review(review_id) if review_id else []
    except Exception as e:
        print(f"   ⚠️ list_attachments_for_review: {e}")

    # Construir bloque de attachments para texto y HTML
    attach_text = ""
    attach_html = ""
    if attachments_info:
        attach_text = f"\n\n📷 {len(attachments_info)} foto(s) adjunta(s) — ver en el dashboard de revisiones:\n"
        admin_token = ""
        try:
            from api._shared import make_token
            admin_token = make_token("ADMIN")
        except Exception:
            pass
        for a in attachments_info:
            url = _build_vercel_url(f"/api/review_attachment?id={a['id']}&admin=1&t={admin_token}")
            attach_text += f"  · {a.get('filename') or 'imagen'}: {url}\n"
        attach_html = (
            f'<div style="margin:18px 0;padding:14px 18px;background:#f4f8fc;border-left:3px solid #60a5fa;border-radius:6px;">'
            f'<div style="font-size:11px;color:#888;text-transform:uppercase;letter-spacing:0.5px;font-weight:600;margin-bottom:8px;">'
            f'📷 {len(attachments_info)} foto(s) adjunta(s) por el cliente</div>'
            f'<div style="display:flex;gap:8px;flex-wrap:wrap;">'
        )
        for a in attachments_info:
            url = _build_vercel_url(f"/api/review_attachment?id={a['id']}&admin=1&t={admin_token}")
            attach_html += (
                f'<a href="{url}" target="_blank" style="display:block;">'
                f'<img src="{url}" alt="{a.get("filename") or ""}" style="width:120px;height:120px;object-fit:cover;border-radius:6px;border:1px solid #ddd;">'
                f'</a>'
            )
        attach_html += '</div></div>'

    subject = f"📝 Revisión pedida: {cliente} — {video}"
    body_text = f"""El cliente {cliente} pidió cambios en su último video:

  📹 {video}
  Editor: {editor or '(sin asignar)'}

Lo que pide cambiar:
─────────────────────
{notes}
─────────────────────{attach_text}

Cuando subas la corrección (mismo Video N, mismo nombre), el sistema
detecta la corrección y se cierra la revisión automáticamente. Le va
a llegar al cliente un mail nuevo con la versión corregida.

— Asistente Revolv
"""
    body_html = f"""<!DOCTYPE html>
<html><body style="font-family:-apple-system,Segoe UI,sans-serif;max-width:600px;color:#222;line-height:1.55;">
<h2 style="margin:0 0 8px;">📝 Revisión pedida</h2>
<p><strong>Cliente:</strong> {cliente}<br>
<strong>Video:</strong> <code>{video}</code><br>
<strong>Editor:</strong> {editor or '(sin asignar)'}</p>
<div style="background:#fff7e6;border-left:3px solid #ffaa00;padding:14px 18px;border-radius:6px;margin:18px 0;">
<div style="font-size:11px;color:#888;text-transform:uppercase;letter-spacing:0.5px;font-weight:600;margin-bottom:6px;">Lo que pide cambiar</div>
<div style="white-space:pre-wrap;font-size:14px;color:#222;">{notes}</div>
</div>
{attach_html}
<p style="font-size:13px;color:#666;">Cuando subas la corrección (mismo nombre de video) el sistema cierra la revisión y le manda mail al cliente automáticamente.</p>
<hr style="border:none;border-top:1px solid #eee;margin:20px 0;">
<p style="color:#888;font-size:12px;">— Asistente Revolv</p>
</body></html>
"""
    # Destinatarios: admin (siempre) + editor (si tiene mail)
    destinatarios = [TEST_EMAIL]
    try:
        from aliases import get_editor_email_for_notification
        if editor:
            ed_email = get_editor_email_for_notification(editor)
            if ed_email and ed_email.lower() != TEST_EMAIL.lower():
                destinatarios.append(ed_email)
    except Exception as e:
        print(f"   ⚠️ resolver mail editor: {e}")

    for dest in destinatarios:
        try:
            # dedupe 6h, mismo razonamiento que en send_completion_mails
            msg_id = send_mail(to=dest, subject=subject, body_text=body_text, body_html=body_html,
                                dedupe_window_minutes=360)
            print(f"   📧 revisión enviada a {dest} (msg_id={msg_id})")
        except Exception as e:
            print(f"   ⚠️ falló mail revisión a {dest}: {e}")

    # Push al editor + admin
    try:
        from push_sender import send_push
        push_title = f"📝 Revisión: {cliente}"
        push_body = (notes[:80] + "…") if len(notes) > 80 else notes
        send_push(editor=None, title=push_title, body=push_body, url="/?admin=1", tag=f"revision-{cliente}")
        if editor:
            send_push(editor=editor, title=push_title, body=push_body,
                       url=f"/?editor={editor}", tag=f"revision-{cliente}")
    except Exception as e:
        print(f"   ⚠️ push revisión: {e}")


def notify_pending_reviews(max_age_hours: int = 72) -> int:
    """Manda los avisos de 'revisión pedida' que el endpoint del portal no logró
    enviar (notified_at NULL). DURABLE: corre en el scan (con creds de mail y sin
    timeout HTTP), reintenta hasta lograrlo. Marca notified_at para no duplicar.
    El dedupe de send_mail evita re-mandar si el endpoint sí llegó a enviar.
    Devuelve cuántas notificó."""
    try:
        from tracker import (list_unnotified_reviews, mark_review_notified,
                             resolve_editor_for_cliente)
    except Exception as e:
        print(f"notify_pending_reviews import: {e}")
        return 0
    revs = list_unnotified_reviews(max_age_hours)
    n = 0
    for r in revs:
        editor = (r.get("editor") or "").strip()
        if not editor:
            try:
                editor = resolve_editor_for_cliente(r["cliente"]) or ""
            except Exception:
                editor = ""
        review = {
            "id": r["id"],
            "cliente": r["cliente"],
            "video_file_id": r.get("video_file_id"),
            "video_file_name": r.get("video_file_name") or "(video)",
            "editor": editor or None,
            "attachments_count": 0,
        }
        try:
            notify_revision_requested(r["id"], review, r.get("notes") or "(sin notas)")
            mark_review_notified(r["id"])
            n += 1
            print(f"   📝 aviso de revisión (durable): {r['cliente']} → {editor or 'admin'}")
        except Exception as e:
            print(f"   ⚠️ notify_pending_reviews review {r['id']}: {e}")
    return n


def notify_review_approved_lite(cliente: str, file_name: str, editor: Optional[str]) -> None:
    """Versión sin review_id: el cliente tocó '✅ Todo perfecto' en el mail.
    NO guardamos nada en DB. Solo mandamos info al admin (dedupe 60 min)."""
    subject = f"✅ {cliente} aprobó: {file_name or '(video)'}"
    body_text = f"""El cliente {cliente} aprobó el video '{file_name or "(video)"}'.
Editor: {editor or '-'}.

— Asistente Revolv
"""
    body_html = f"""<!DOCTYPE html>
<html><body style="font-family:-apple-system,Segoe UI,sans-serif;max-width:600px;color:#222;">
<h2 style="margin:0 0 8px;color:#1a8a3a;">✅ {cliente} aprobó el video</h2>
<p><strong>Video:</strong> <code>{file_name or '(video)'}</code><br>
<strong>Editor:</strong> {editor or '-'}</p>
<hr style="border:none;border-top:1px solid #eee;margin:20px 0;">
<p style="color:#888;font-size:12px;">— Asistente Revolv</p>
</body></html>
"""
    try:
        send_mail(to=TEST_EMAIL, subject=subject, body_text=body_text, body_html=body_html,
                   dedupe_window_minutes=60)
    except Exception as e:
        print(f"   ⚠️ falló mail approved-lite: {e}")


def notify_review_approved(review_id: int, review: dict) -> None:
    """Cliente aprobó el video → solo mail al admin (info, no urgente)."""
    cliente = review.get("cliente", "?")
    editor = review.get("editor")
    video = review.get("video_file_name", "(video)")

    subject = f"✅ {cliente} aprobó: {video}"
    body_text = f"""El cliente {cliente} aprobó el video '{video}'.
Editor: {editor or '-'}.

— Asistente Revolv
"""
    body_html = f"""<!DOCTYPE html>
<html><body style="font-family:-apple-system,Segoe UI,sans-serif;max-width:600px;color:#222;">
<h2 style="margin:0 0 8px;color:#1a8a3a;">✅ {cliente} aprobó el video</h2>
<p><strong>Video:</strong> <code>{video}</code><br>
<strong>Editor:</strong> {editor or '-'}</p>
<hr style="border:none;border-top:1px solid #eee;margin:20px 0;">
<p style="color:#888;font-size:12px;">— Asistente Revolv</p>
</body></html>
"""
    try:
        send_mail(to=TEST_EMAIL, subject=subject, body_text=body_text, body_html=body_html,
                   dedupe_window_minutes=30)
    except Exception as e:
        print(f"   ⚠️ falló mail approved: {e}")


def notify_correction_ready_to_client(cliente: str, file_name: str,
                                       edited_folder_id: Optional[str] = None,
                                       client_folder_id: Optional[str] = None,
                                       file_id: Optional[str] = None) -> None:
    """Avisa al cliente que está la versión corregida de un video, SIN depender
    de un review_id registrado. Se llama cuando el editor sube una corrección
    pero no había review match (ej. el cliente pidió el cambio por WhatsApp y
    no por el form del sistema). Bug reportado 21/may (Ely Fitness).

    Reusa el mismo mail que notify_revision_resolved (look & feel idéntico),
    pero arma el body desde los parámetros directos.
    """
    if not cliente or not file_name:
        return
    from tracker import cfg_client_should_be_notified, _correction_stem
    target = cfg_client_should_be_notified(cliente)
    if not target:
        return  # cliente sin mail registrado, no avisamos
    to_email = target["email"]
    display = target["display_name"] or cliente.split()[0]
    video = file_name

    # Link al video en Drive (mismas reglas de fallback que send_client_delivery_mail)
    if edited_folder_id:
        video_url = f"https://drive.google.com/drive/folders/{edited_folder_id}"
        video_label = "Ver carpeta del editado"
    elif client_folder_id:
        video_url = f"https://drive.google.com/drive/folders/{client_folder_id}"
        video_label = f"Ver carpeta de {cliente}"
    elif file_id:
        video_url = f"https://drive.google.com/file/d/{file_id}/view"
        video_label = "Ver video en Drive"
    else:
        video_url = None
        video_label = None

    link_text = f"\n📁 {video_label}: {video_url}\n" if video_url else ""
    link_html = (
        f'<p style="text-align:center;margin:24px 0 8px;">'
        f'<a href="{video_url}" style="background:#22c55e;color:white;padding:12px 22px;'
        f'border-radius:8px;text-decoration:none;font-weight:700;font-size:15px;display:inline-block;">'
        f'📁 {video_label}</a></p>'
    ) if video_url else ""

    subject = f"🎬 Tu revisión está lista — {video}"
    body_text = f"""Hola {display}!

Acabo de subir la versión corregida de tu video:

  📹 {video}
{link_text}
Pegale una mirada y avisame si quedó bien.

Un abrazo,
Nacho
Revolv
"""
    body_html = f"""<!DOCTYPE html>
<html><body style="margin:0;padding:0;background:#f5f5f5;font-family:-apple-system,Segoe UI,sans-serif;color:#222;">
<div style="max-width:560px;margin:0 auto;padding:32px 24px;">
<div style="text-align:center;margin-bottom:28px;">
<div style="font-size:14px;letter-spacing:2px;color:#888;text-transform:uppercase;font-weight:600;">REVOLV</div>
</div>
<div style="background:white;border-radius:14px;padding:32px 28px;box-shadow:0 2px 12px rgba(0,0,0,0.04);">
<h1 style="margin:0 0 16px;font-size:24px;color:#111;">🎬 Tu revisión está lista</h1>
<p style="font-size:16px;line-height:1.55;">Hola <strong>{display}</strong>! Subí la versión corregida:</p>
<div style="background:#f8f8f8;border-left:4px solid #22c55e;padding:14px 18px;border-radius:6px;margin:18px 0;">
<div style="font-size:12px;color:#888;text-transform:uppercase;letter-spacing:0.5px;font-weight:600;margin-bottom:4px;">VIDEO CORREGIDO</div>
<div style="font-size:16px;color:#111;font-weight:600;">{video}</div>
</div>
{link_html}
<p style="font-size:14px;color:#666;text-align:center;">Pegale una mirada y avisame si quedó bien.</p>
</div>
<div style="text-align:center;margin-top:28px;color:#888;font-size:13px;line-height:1.6;">
Un abrazo,<br><strong style="color:#222;">Nacho</strong><br>
<span style="font-size:11px;letter-spacing:1px;text-transform:uppercase;color:#aaa;">REVOLV</span>
</div></div></body></html>
"""
    # Dedupe estable por (cliente, stem del archivo) para que múltiples workers
    # detectando la misma corrección no manden múltiples mails al cliente.
    try:
        stem = _correction_stem(file_name)
    except Exception:
        stem = (file_name or "").lower().strip()
    override = f"correction-client|{cliente.strip().lower()}|{stem}"
    try:
        send_mail(to=to_email, subject=subject, body_text=body_text, body_html=body_html,
                  kind="client-correction-ready", cliente=cliente,
                  dedupe_window_minutes=60, dedupe_key_override=override)
        print(f"   📧 corrección lista → cliente {cliente} ({to_email})")
    except Exception as e:
        print(f"   ⚠️ falló mail corrección lista a {cliente}: {e}")


def notify_revision_resolved(review_id: int, review: dict, target: dict = None) -> None:
    """El editor subió la corrección (o el admin la marcó resuelta) → mail al
    cliente con la nueva versión. `target` ({email, display_name}) puede venir
    pre-resuelto por el caller (Vercel: la DB local es stale, el endpoint lo
    resuelve con su conn fresca)."""
    cliente = review.get("cliente", "?")
    video = review.get("video_file_name", "(video)")
    video_file_id = review.get("video_file_id")
    from tracker import cfg_client_should_be_notified, _correction_stem
    if target is None:
        target = cfg_client_should_be_notified(cliente)
    if not target:
        return  # cliente sin mail registrado, no avisamos
    to_email = target["email"]
    display = target["display_name"] or cliente.split()[0]

    # Link al video. El review solo guarda file_id del original (no folder),
    # así que armamos un link directo al archivo en Drive.
    if video_file_id:
        video_url = f"https://drive.google.com/file/d/{video_file_id}/view"
        video_label = "Ver video en Drive"
    else:
        video_url = None
        video_label = None
    link_text = f"\n📁 {video_label}: {video_url}\n" if video_url else ""
    link_html = (
        f'<p style="text-align:center;margin:24px 0 8px;">'
        f'<a href="{video_url}" style="background:#22c55e;color:white;padding:12px 22px;'
        f'border-radius:8px;text-decoration:none;font-weight:700;font-size:15px;display:inline-block;">'
        f'📁 {video_label}</a></p>'
    ) if video_url else ""

    subject = f"🎬 Tu revisión está lista — {video}"
    body_text = f"""Hola {display}!

Acabo de subir la versión corregida de tu video:

  📹 {video}
{link_text}
Pegale una mirada y avisame si quedó bien.

Un abrazo,
Nacho
Revolv
"""
    body_html = f"""<!DOCTYPE html>
<html><body style="margin:0;padding:0;background:#f5f5f5;font-family:-apple-system,Segoe UI,sans-serif;color:#222;">
<div style="max-width:560px;margin:0 auto;padding:32px 24px;">
<div style="text-align:center;margin-bottom:28px;">
<div style="font-size:14px;letter-spacing:2px;color:#888;text-transform:uppercase;font-weight:600;">REVOLV</div>
</div>
<div style="background:white;border-radius:14px;padding:32px 28px;box-shadow:0 2px 12px rgba(0,0,0,0.04);">
<h1 style="margin:0 0 16px;font-size:24px;color:#111;">🎬 Tu revisión está lista</h1>
<p style="font-size:16px;line-height:1.55;">Hola <strong>{display}</strong>! Subí la versión corregida:</p>
<div style="background:#f8f8f8;border-left:4px solid #22c55e;padding:14px 18px;border-radius:6px;margin:18px 0;">
<div style="font-size:12px;color:#888;text-transform:uppercase;letter-spacing:0.5px;font-weight:600;margin-bottom:4px;">VIDEO CORREGIDO</div>
<div style="font-size:16px;color:#111;font-weight:600;">{video}</div>
</div>
{link_html}
<p style="font-size:14px;color:#666;text-align:center;">Pegale una mirada y avisame si quedó bien.</p>
</div>
<div style="text-align:center;margin-top:28px;color:#888;font-size:13px;line-height:1.6;">
Un abrazo,<br><strong style="color:#222;">Nacho</strong><br>
<span style="font-size:11px;letter-spacing:1px;text-transform:uppercase;color:#aaa;">REVOLV</span>
</div></div></body></html>
"""
    # Dedupe estable para que también atrape duplicados entre workers
    try:
        stem = _correction_stem(video)
    except Exception:
        stem = (video or "").lower().strip()
    override = f"correction-client|{cliente.strip().lower()}|{stem}"
    try:
        send_mail(to=to_email, subject=subject, body_text=body_text, body_html=body_html,
                   kind="client-correction-ready", cliente=cliente,
                   dedupe_window_minutes=60, dedupe_key_override=override)
        print(f"   📧 revisión resuelta enviada a cliente {cliente} ({to_email})")
    except Exception as e:
        print(f"   ⚠️ falló mail revisión resuelta a {cliente}: {e}")


# ─── ALERTAS DE SUBFOLDER NO-MAPEADA ─────────────────────────────────────────

def enqueue_subfolder_alert(cliente: str, subfolder: str, tipo: str,
                              file_name: str, file_id: str,
                              default_editor: Optional[str]) -> None:
    """Manda mail INMEDIATO al admin avisando que detectamos una subfolder
    "tipo" (Youtube/Reels/Shorts/...) sin mapeo. Asignamos al editor default
    del Sheet pero el admin debería decidir si está bien.

    Idempotencia: el caller (scan_incremental) ya chequea
    register_subfolder_alert que es una sola vez por (cliente, subfolder)."""
    subject = f"🆕 Subfolder nueva: {cliente} / {subfolder}"
    file_url = f"https://drive.google.com/file/d/{file_id}/view" if file_id else None
    editor_txt = default_editor or "(sin editor)"
    text = f"""Detecté un crudo nuevo en una subfolder que no tengo mapeada:

  Cliente:   {cliente}
  Subfolder: {subfolder}  (tipo detectado: {tipo})
  Archivo:   {file_name}
  Owner:     {file_url or '(sin link)'}

Asigné al editor por default del Sheet: {editor_txt}

Si la subfolder '{subfolder}' va a OTRO editor (típico cuando un cliente
tiene reels + youtube con editores distintos), avisame para configurarlo.

A partir de ese momento, todos los crudos en /Material/{subfolder}/ van a
asignarse automáticamente al editor que digas.

— Asistente Revolv (auto-detección de subfolders)
"""
    html = f"""<!DOCTYPE html>
<html><body style="font-family:-apple-system,Segoe UI,sans-serif;max-width:600px;color:#222;line-height:1.55;">
<h2 style="margin:0 0 12px;">🆕 Subfolder nueva detectada</h2>
<p>Apareció un crudo en una subfolder dentro de <code>/Material/</code> que
<strong>no tengo mapeada a ningún editor</strong>.</p>
<table style="border-collapse:collapse;margin:12px 0;font-size:14px;">
  <tr><td style="padding:4px 12px 4px 0;color:#666;">Cliente</td><td><strong>{cliente}</strong></td></tr>
  <tr><td style="padding:4px 12px 4px 0;color:#666;">Subfolder</td><td><code>{subfolder}</code> <span style="color:#888;font-size:12px;">(tipo: {tipo})</span></td></tr>
  <tr><td style="padding:4px 12px 4px 0;color:#666;">Archivo</td><td>{file_name}</td></tr>
  <tr><td style="padding:4px 12px 4px 0;color:#666;">Editor asignado por default</td><td><strong>{editor_txt}</strong> <span style="color:#888;font-size:12px;">(del Sheet)</span></td></tr>
</table>
{f'<p><a href="{file_url}" style="background:#ff4747;color:white;padding:8px 16px;border-radius:6px;text-decoration:none;font-weight:600;">📁 Ver archivo en Drive</a></p>' if file_url else ''}
<p style="background:#fff7e6;border-left:3px solid #ffaa00;padding:10px 14px;border-radius:4px;font-size:13px;">
<strong>¿Está bien {editor_txt}?</strong> Si esta subfolder ('{subfolder}') la maneja
OTRO editor, avisame y la configuro. Una vez configurada, todos los próximos
crudos en <code>/Material/{subfolder}/</code> van a asignarse automáticamente.
</p>
<hr style="border:none;border-top:1px solid #eee;margin:20px 0;">
<p style="color:#888;font-size:12px;">— Asistente Revolv · auto-detección de subfolders</p>
</body></html>
"""
    try:
        msg_id = send_mail(to=TEST_EMAIL, subject=subject, body_text=text, body_html=html,
                            dedupe_window_minutes=30)
        print(f"   📧 alerta subfolder enviada: {cliente}/{subfolder} → {TEST_EMAIL} (msg_id={msg_id})")
    except Exception as e:
        print(f"   ⚠️ falló mail alerta subfolder {cliente}/{subfolder}: {e}")


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--dry-run", action="store_true", help="muestra qué mandaría sin enviar")
    p.add_argument("--to", help="override del destinatario (default: TEST_EMAIL)")
    args = p.parse_args()
    run(dry_run=args.dry_run, recipient=args.to)

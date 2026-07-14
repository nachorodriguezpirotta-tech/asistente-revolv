"""
Sistema de revisiones de cliente — v3 (con attachments, notes opcional con fotos).
Force rebuild marker: 2026-05-21T15:08

El cambio respecto a v1: NO se crea review por adelantado al mandar el mail
al cliente. La review SOLO se crea cuando el cliente realmente pide cambios.
Si el cliente aprueba o ignora, NO queda registro de "review pending".

Endpoints:
  GET /api/review?action=approve&cliente=X&file_id=Y&t=TOKEN
       → muestra HTML "¡Gracias!" — NO guarda nada en DB. Solo info al admin
         por mail.

  GET /api/review?action=info&cliente=X&file_id=Y&file_name=Z&editor=W&t=TOKEN
       → JSON con info para que revision.html arme el form.

  POST /api/review?cliente=X&file_id=Y&t=TOKEN
       Body: {"notes": "cambiar X en 0:23", "file_name": "...", "editor": "..."}
       → Cliente envía revisión: CREA row con status='revision_requested',
         dispara mail + push a editor y admin.

  DELETE /api/review?id=N&admin=1&t=ADMIN_TOKEN
       → Borrar review (admin only).
"""

import json
import os
import sys
import traceback
from http.server import BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

try:
    from _shared import check_client_token, check_token, json_response, with_db, read_db
    _IMPORT_ERROR = None
except Exception as _e:
    _IMPORT_ERROR = f"{type(_e).__name__}: {_e}\n{traceback.format_exc()}"


_APPROVE_HTML = """<!DOCTYPE html>
<html lang="es"><head><meta charset="utf-8"><title>¡Gracias!</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
       background: #f5f5f5; color: #222; margin: 0; padding: 40px 20px;
       display: flex; align-items: center; justify-content: center; min-height: 100vh; }
.card { background: white; border-radius: 14px; padding: 40px 32px; max-width: 460px;
        text-align: center; box-shadow: 0 4px 16px rgba(0,0,0,0.06); }
h1 { margin: 0 0 12px; font-size: 28px; color: #1a8a3a; }
p { font-size: 16px; line-height: 1.55; color: #555; margin: 0 0 8px; }
.brand { margin-top: 28px; font-size: 12px; letter-spacing: 2px; color: #aaa; text-transform: uppercase; }
</style></head>
<body><div class="card">
<div style="font-size:72px;line-height:1;margin-bottom:8px;">✅</div>
<h1>¡Listo, {cliente}!</h1>
<p>Marqué tu video como aprobado.</p>
<p>Gracias por la respuesta — Nacho.</p>
<div class="brand">Revolv</div>
</div></body></html>
"""

_ERROR_HTML = """<!DOCTYPE html>
<html lang="es"><head><meta charset="utf-8"><title>Error</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>body{font-family:-apple-system,sans-serif;background:#f5f5f5;color:#222;margin:0;padding:40px 20px;display:flex;align-items:center;justify-content:center;min-height:100vh}.card{background:white;border-radius:14px;padding:40px 32px;max-width:460px;text-align:center;box-shadow:0 4px 16px rgba(0,0,0,0.06)}h1{margin:0 0 12px;font-size:24px;color:#c33}p{color:#555}</style>
</head><body><div class="card">
<div style="font-size:60px;margin-bottom:8px;">⚠️</div>
<h1>{msg}</h1>
<p>{detail}</p>
</div></body></html>
"""


def _html_response(handler, html: str, status: int = 200):
    body = html.encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "text/html; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("Cache-Control", "no-store")
    handler.end_headers()
    handler.wfile.write(body)


def _escape_html(s: str) -> str:
    """Escapa HTML para que el nombre del cliente no rompa el template."""
    if s is None:
        return ""
    return (str(s)
            .replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
            .replace('"', "&quot;")
            .replace("'", "&#39;"))


def _render_approve(cliente: str) -> str:
    """Renderiza la pantalla de aprobación. Usa .replace() en vez de .format()
    porque el template tiene llaves CSS literales que romperían .format()
    con KeyError (bug 21/may: cliente clickeaba 'Todo perfecto' y veía un
    JSON con stack trace en lugar de la pantalla 'Listo!')."""
    return _APPROVE_HTML.replace("{cliente}", _escape_html(cliente))


def _render_error(msg: str, detail: str) -> str:
    """Igual que _render_approve, evita .format() por las llaves CSS."""
    return (_ERROR_HTML
            .replace("{msg}", _escape_html(msg))
            .replace("{detail}", _escape_html(detail)))


def _resolve_editor_with_conn(conn, cliente):
    """Editor de un cliente usando la conn FRESCA del endpoint (with_db/read_db).
    En Vercel, tracker.get_conn() abre la copia bundleada del deploy (stale/local)
    → resolve_editor_for_cliente devolvía None y el aviso de revisión solo le
    llegaba al admin (bug 12/jun, caso Álvaro #11228). Prioridad: override
    manual > Sheet > último completion."""
    import unicodedata
    def norm(s):
        s = unicodedata.normalize("NFD", s or "")
        s = "".join(ch for ch in s if unicodedata.category(ch) != "Mn")
        return " ".join(s.lower().split())
    target = norm(cliente)
    result = None
    try:
        for r in conn.execute(
            "SELECT cliente, editor, MAX(sent_at) FROM mail_log "
            "WHERE kind='completion' AND COALESCE(editor,'') NOT IN ('', '—') GROUP BY cliente"):
            if norm(r["cliente"]) == target:
                result = r["editor"]
    except Exception:
        pass
    try:
        for r in conn.execute("SELECT cliente, editor FROM cfg_excel_clients"):
            if (r["editor"] or "").strip() and norm(r["cliente"]) == target:
                result = r["editor"]
    except Exception:
        pass
    try:
        for r in conn.execute("SELECT cliente, editor FROM cfg_client_editor"):
            if (r["editor"] or "").strip() and norm(r["cliente"]) == target:
                result = r["editor"]
    except Exception:
        pass
    return result


def _client_mail_with_conn(conn, cliente):
    """{email, display_name} del cliente con la conn FRESCA (cfg_clients,
    match exacto → normalizado → token subset en ambas direcciones, como
    tracker.cfg_get_client). Solo si notifications_enabled=1."""
    import unicodedata
    def norm(s):
        s = unicodedata.normalize("NFD", s or "")
        s = "".join(ch for ch in s if unicodedata.category(ch) != "Mn")
        return " ".join(s.lower().split())
    STOP = {"de","del","la","el","los","las","y","e","o","u","a","con","sin","para","por"}
    def toks(s):
        return {t for t in norm(s).split() if len(t) >= 3 and t not in STOP}
    try:
        rows = conn.execute(
            "SELECT cliente, email, display_name, notifications_enabled FROM cfg_clients"
        ).fetchall()
    except Exception:
        return None
    target = norm(cliente)
    ttoks = toks(cliente)
    hit = None
    for r in rows:
        if norm(r["cliente"]) == target:
            hit = r; break
    if not hit and ttoks:
        subset = [r for r in rows if len(toks(r["cliente"])) >= 2 and toks(r["cliente"]).issubset(ttoks)]
        if len(subset) == 1: hit = subset[0]
    if not hit and ttoks:
        inv = [r for r in rows if toks(r["cliente"]) and ttoks.issubset(toks(r["cliente"]))]
        if len(inv) == 1: hit = inv[0]
    if not hit or not hit["notifications_enabled"] or not hit["email"]:
        return None
    return {"email": hit["email"], "display_name": hit["display_name"] or cliente.split()[0]}


class handler(BaseHTTPRequestHandler):

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self):
        try:
            if _IMPORT_ERROR:
                return _html_response(self, _render_error("Sistema no disponible", _IMPORT_ERROR[:200]), 500)
            params = parse_qs(urlparse(self.path).query)
            action = params.get("action", ["info"])[0]
            cliente = (params.get("cliente", [""])[0] or "").strip()
            file_id = (params.get("file_id", [""])[0] or "").strip()
            file_name = (params.get("file_name", [""])[0] or "").strip()
            editor = (params.get("editor", [""])[0] or "").strip()
            token = (params.get("t", [""])[0] or "").strip()

            # MODO resolve: admin o editor marca la review como corregida.
            # Auth NO va por cliente_token (que es del cliente, no del staff).
            if action == "resolve":
                review_id = (params.get("id", [""])[0] or "").strip()
                if not review_id or not review_id.isdigit():
                    return json_response(self, {"error": "id requerido"}, status=400)
                is_admin = params.get("admin", [""])[0] == "1"
                authorized = False
                if is_admin:
                    authorized = check_token("ADMIN", token)
                elif editor:
                    authorized = check_token(editor, token)
                if not authorized:
                    return json_response(self, {"error": "unauthorized"}, status=401)

                info_holder = {}
                def _do(conn):
                    row = conn.execute(
                        "SELECT cliente, video_file_name, video_file_id, status FROM client_reviews WHERE id=?",
                        (int(review_id),)).fetchone()
                    if row:
                        info_holder.update(dict(row))
                        # mail del cliente con la conn FRESCA (la DB local de
                        # Vercel es stale — gotcha 12/jun)
                        info_holder["target"] = _client_mail_with_conn(conn, row["cliente"])
                    conn.execute(
                        "UPDATE client_reviews SET status='resolved', resolved_at=datetime('now') WHERE id=?",
                        (int(review_id),),
                    )
                with_db(_do, message=f"review {review_id} marcada resuelta manualmente")
                # Avisar al CLIENTE que su corrección está lista (pedido Ignacio
                # 12/jun: el aviso debe salir tanto al subir la corrección como
                # al marcarla corregida a mano). Solo si estaba abierta (no
                # re-avisar al re-marcar una ya resuelta) y tiene mail.
                notified_client = False
                try:
                    if info_holder.get("cliente") and info_holder.get("status") == "revision_requested"                             and info_holder.get("target"):
                        from notifier import notify_revision_resolved
                        notify_revision_resolved(int(review_id), {
                            "cliente": info_holder["cliente"],
                            "video_file_name": info_holder.get("video_file_name") or "(video)",
                            "video_file_id": info_holder.get("video_file_id"),
                        }, target=info_holder["target"])
                        notified_client = True
                except Exception as e:
                    print(f"[RESOLVE NOTIFY ERROR] review={review_id}: {e}", flush=True)
                return json_response(self, {"ok": True, "id": int(review_id), "status": "resolved",
                                            "cliente_notificado": notified_client})

            # MODO resolve_all: marca TODAS las correcciones abiertas como
            # resueltas de un saque (pedido Ignacio 08/jul: "un botón que marque
            # todas como corregidas"). Admin: todas. Editor: solo las suyas
            # (misma lógica canónica que /api/reviews). A DIFERENCIA del resolve
            # individual, NO avisa a los clientes (es limpieza masiva — evitar
            # un aluvión de mails "tu revisión está lista" accidental).
            if action == "resolve_all":
                is_admin = params.get("admin", [""])[0] == "1"
                authorized = False
                if is_admin:
                    authorized = check_token("ADMIN", token)
                elif editor:
                    authorized = check_token(editor, token)
                if not authorized:
                    return json_response(self, {"error": "unauthorized"}, status=401)

                resolved_ids = []
                def _do_all(conn):
                    open_rows = conn.execute(
                        "SELECT id, cliente, editor FROM client_reviews "
                        "WHERE status='revision_requested'").fetchall()
                    targets = []
                    if is_admin:
                        targets = [r["id"] for r in open_rows]
                    else:
                        # filtrar por editor canónico (reutiliza la lógica de reviews.py)
                        from reviews import _cliente_editor_map
                        from tracker import canonical_editor
                        cmap, norm = _cliente_editor_map(conn)
                        try:
                            editors = [r["name"] for r in conn.execute(
                                "SELECT name FROM cfg_editors WHERE active=1").fetchall()]
                        except Exception:
                            editors = []
                        def _canon(n):
                            return canonical_editor(n, editors).strip().lower() if n else ""
                        ed_canon = _canon(editor)
                        for r in open_rows:
                            rev_ed = (r["editor"] or "").strip()
                            if rev_ed:
                                if _canon(rev_ed) == ed_canon:
                                    targets.append(r["id"])
                            else:
                                resolved_ed = cmap.get(norm(r["cliente"] or ""))
                                if resolved_ed and _canon(resolved_ed) == ed_canon:
                                    targets.append(r["id"])
                    if targets:
                        ph = ",".join("?" * len(targets))
                        conn.execute(
                            f"UPDATE client_reviews SET status='resolved', "
                            f"resolved_at=datetime('now') WHERE id IN ({ph})", targets)
                    resolved_ids.extend(targets)

                def _verify_all(conn):
                    if not resolved_ids:
                        return True
                    ph = ",".join("?" * len(resolved_ids))
                    n = conn.execute(
                        f"SELECT COUNT(*) FROM client_reviews WHERE id IN ({ph}) "
                        f"AND status='resolved'", resolved_ids).fetchone()[0]
                    return n == len(resolved_ids)

                who = "admin" if is_admin else editor
                with_db(_do_all, message=f"reviews: resolve_all por {who} ({0 if not resolved_ids else len(resolved_ids)})",
                        verify=_verify_all)
                return json_response(self, {"ok": True, "resolved": len(resolved_ids)})

            # MODO renotify: admin re-dispara la notificación de una review existente.            # MODO renotify: admin re-dispara la notificación de una review existente.
            # Útil cuando notify_revision_requested falló silenciosamente.
            if action == "renotify":
                review_id_param = (params.get("id", [""])[0] or "").strip()
                is_admin = params.get("admin", [""])[0] == "1"
                if not is_admin or not check_token("ADMIN", token):
                    return json_response(self, {"error": "unauthorized"}, status=401)
                if not review_id_param or not review_id_param.isdigit():
                    return json_response(self, {"error": "id requerido"}, status=400)

                def _fetch(conn):
                    row = conn.execute(
                        "SELECT id, cliente, video_file_id, video_file_name, editor, notes FROM client_reviews WHERE id=?",
                        (int(review_id_param),),
                    ).fetchone()
                    if not row:
                        return None
                    d = dict(row)
                    if not (d.get("editor") or "").strip():
                        d["editor"] = _resolve_editor_with_conn(conn, d["cliente"])
                    return d

                row = read_db(_fetch)
                if not row:
                    return json_response(self, {"error": "review not found"}, status=404)
                try:
                    from notifier import notify_revision_requested
                    review = {
                        "id": row["id"],
                        "cliente": row["cliente"],
                        "video_file_id": row["video_file_id"],
                        "video_file_name": row["video_file_name"] or "(video)",
                        "editor": row["editor"],
                        "attachments_count": 0,
                    }
                    notify_revision_requested(row["id"], review, row["notes"] or "(sin notas)")
                    return json_response(self, {"ok": True, "renotified": row["id"]})
                except Exception as e:
                    import traceback
                    tb = traceback.format_exc()
                    return json_response(self, {"error": str(e)[:200], "trace": tb[:1500]}, status=500)

            if not cliente:
                if action == "info":
                    return json_response(self, {"error": "cliente requerido"}, status=400)
                return _html_response(self, _render_error("Link inválido", "Falta cliente"), 400)

            # Auth — dos formas válidas (ver do_POST para más contexto):
            #  (1) token de cliente firmado con DASHBOARD_SECRET (link del mail)
            #  (2) header X-Portal-Bridge-Secret == PORTAL_BRIDGE_SECRET (portal→asistente)
            import os as _os
            _bridge = _os.environ.get("PORTAL_BRIDGE_SECRET", "").strip()
            _req_bridge = (self.headers.get("X-Portal-Bridge-Secret") or "").strip()
            _authorized_by_bridge = bool(_bridge) and _req_bridge == _bridge
            if not _authorized_by_bridge and not check_client_token(cliente, token):
                if action == "info":
                    return json_response(self, {"error": "unauthorized"}, status=401)
                return _html_response(self, _render_error("Link inválido", "El link expiró o no es válido."), 401)

            # MODO info: devolver datos para que revision.html arme el form
            if action == "info":
                return json_response(self, {
                    "ok": True,
                    "cliente": cliente,
                    "video_file_id": file_id,
                    "video_file_name": file_name,
                    "editor": editor,
                    "status": "ready_to_respond",  # estado virtual, no en DB
                })

            # MODO approve: guarda la aprobación en DB (UPSERT por cliente+file_id)
            # + manda mail info al admin.
            if action == "approve":
                if file_id:
                    def _upsert_approval(conn):
                        existing = conn.execute(
                            """SELECT id FROM client_reviews
                               WHERE cliente=? AND COALESCE(video_file_id,'')=COALESCE(?,'')
                               ORDER BY id DESC LIMIT 1""",
                            (cliente, file_id),
                        ).fetchone()
                        if existing:
                            conn.execute(
                                """UPDATE client_reviews
                                     SET status='approved',
                                         responded_at=datetime('now'),
                                         resolved_at=datetime('now')
                                   WHERE id=?""",
                                (existing[0],),
                            )
                        else:
                            conn.execute(
                                """INSERT INTO client_reviews
                                       (cliente, video_file_id, video_file_name, editor,
                                        status, notes, created_at, responded_at, resolved_at)
                                   VALUES (?, ?, ?, ?, 'approved', '(aprobado por cliente)',
                                           datetime('now'), datetime('now'), datetime('now'))""",
                                (cliente, file_id, file_name or None, editor),
                            )
                    try:
                        with_db(_upsert_approval, message=f"approve: {cliente} aprobó {file_id}")
                    except Exception as e:
                        print(f"approve upsert error: {e}")
                try:
                    from notifier import notify_review_approved_lite
                    notify_review_approved_lite(cliente, file_name, editor)
                except Exception as e:
                    print(f"notify_review_approved_lite error: {e}")
                # Bridge auth (portal lo llamó) → JSON. Cliente directo del mail → HTML.
                if _authorized_by_bridge:
                    return json_response(self, {"ok": True, "status": "approved"})
                return _html_response(self, _render_approve(cliente))

            return json_response(self, {"error": f"action inválida: {action}"}, status=400)
        except Exception as e:
            return json_response(self, {"error": str(e)[:200], "trace": traceback.format_exc()[:1500]}, status=500)

    def do_POST(self):
        try:
            if _IMPORT_ERROR:
                return json_response(self, {"error": "import", "detail": _IMPORT_ERROR}, status=500)
            params = parse_qs(urlparse(self.path).query)
            cliente = (params.get("cliente", [""])[0] or "").strip()
            file_id = (params.get("file_id", [""])[0] or "").strip()
            token = (params.get("t", [""])[0] or "").strip()
            if not cliente:
                return json_response(self, {"error": "cliente requerido"}, status=400)

            # Auth — dos formas válidas:
            #  (1) token de cliente firmado con DASHBOARD_SECRET (link del mail)
            #  (2) header X-Portal-Bridge-Secret == PORTAL_BRIDGE_SECRET (portal
            #      a asistente: el portal ya sabe quién es el cliente porque
            #      el cliente entró desde su panel /c/<cliente>?t=TOKEN, y el
            #      portal valida ese token con make_client_token. Acá usamos
            #      un secret aparte para que si DASHBOARD_SECRET se rota o
            #      desincroniza, el sync portal→asistente siga andando.)
            import os as _os
            bridge = _os.environ.get("PORTAL_BRIDGE_SECRET", "").strip()
            req_bridge = (self.headers.get("X-Portal-Bridge-Secret") or "").strip()
            authorized_by_bridge = bool(bridge) and req_bridge == bridge
            if not authorized_by_bridge and not check_client_token(cliente, token):
                return json_response(self, {"error": "unauthorized"}, status=401)

            length = int(self.headers.get("Content-Length", "0"))
            # NO lowercaseamos todo el Content-Type porque el boundary es
            # case-sensitive — el body lo respeta exactamente (camelcase, etc).
            # Bug 22/may: .lower() corrompía boundaries case-sensitive →
            # split no encontraba delimiter → multipart no parseaba.
            content_type = (self.headers.get("Content-Type") or "")
            content_type_lower = content_type.lower()  # solo para checks
            raw_body = self.rfile.read(length) if length > 0 else b""

            notes = ""
            file_name = ""
            editor = None
            attachments = []  # lista de (filename, mime, bytes)

            # DEBUG temporal: log de lo que llega
            print(f"[DEBUG] content_type={content_type!r}")
            print(f"[DEBUG] body length={len(raw_body)}")
            print(f"[DEBUG] body first 200 bytes={raw_body[:200]!r}")

            _parser_debug = {}
            if "multipart/form-data" in content_type_lower:
                # Parser multipart manual — más confiable que email.policy en
                # el runtime de Vercel (que tuvo problemas reportados).
                # Buscamos el boundary y spliteamos el body en partes.
                boundary = None
                for chunk in content_type.split(";"):
                    chunk = chunk.strip()
                    if chunk.lower().startswith("boundary="):
                        boundary = chunk[len("boundary="):].strip('"')
                        break
                if not boundary:
                    return json_response(self, {"error": "multipart sin boundary"}, status=400)

                # body splits por --boundary
                delim = ("--" + boundary).encode()
                parts = raw_body.split(delim)
                _parser_debug["boundary_len"] = len(boundary)
                _parser_debug["delim_len"] = len(delim)
                _parser_debug["num_parts"] = len(parts)
                _parser_debug["first_50_body"] = raw_body[:50].decode("utf-8", errors="replace")
                # primera parte es preamble (vacía), última es "--" + epilogo
                for raw_part in parts[1:-1]:
                    # Strip CRLF inicial y final
                    if raw_part.startswith(b"\r\n"):
                        raw_part = raw_part[2:]
                    if raw_part.endswith(b"\r\n"):
                        raw_part = raw_part[:-2]
                    # Separar headers de payload con doble CRLF
                    if b"\r\n\r\n" not in raw_part:
                        continue
                    headers_raw, payload = raw_part.split(b"\r\n\r\n", 1)
                    headers = {}
                    for line in headers_raw.split(b"\r\n"):
                        if b":" in line:
                            k, v = line.split(b":", 1)
                            headers[k.strip().lower().decode("ascii", "replace")] = v.strip().decode("utf-8", "replace")
                    disposition = headers.get("content-disposition", "")
                    if "form-data" not in disposition:
                        continue
                    params_disp = {}
                    for chunk in disposition.split(";"):
                        if "=" in chunk:
                            k, v = chunk.strip().split("=", 1)
                            params_disp[k.strip()] = v.strip(' "')
                    field_name = params_disp.get("name", "")
                    fname = params_disp.get("filename")
                    mime = headers.get("content-type") or "application/octet-stream"

                    if fname:
                        if len(attachments) >= 5:
                            continue
                        if len(payload) > 5 * 1024 * 1024:
                            return json_response(self, {"error": f"imagen '{fname}' muy grande (max 5MB)"}, status=413)
                        if not mime.startswith("image/"):
                            mime = "image/jpeg"  # fallback (capaz iPhone no manda mime correcto)
                        attachments.append((fname, mime, payload))
                    else:
                        text = payload.decode("utf-8", errors="replace")
                        if field_name == "notes":
                            notes = text.strip()
                        elif field_name == "file_name":
                            file_name = text.strip()
                        elif field_name == "editor":
                            editor = text.strip() or None
            else:
                # JSON clásico (sin imágenes)
                try:
                    body = json.loads(raw_body.decode("utf-8") if raw_body else "{}")
                except Exception:
                    return json_response(self, {"error": "body inválido"}, status=400)
                notes = (body.get("notes") or "").strip()
                file_name = (body.get("file_name") or "").strip()
                editor = (body.get("editor") or "").strip() or None

            # Notes opcional SI hay fotos. Force deploy v2.
            print(f"[DEBUG] notes={notes!r}, attachments={len(attachments)}, file_name={file_name!r}")
            if not notes and not attachments:
                return json_response(self, {"error": "Escribi algo o suma una foto",
                                              "_debug": {"ct_len": len(content_type), "body_len": len(raw_body), "ct": content_type[:80], "parser": _parser_debug}}, status=400)
            if len(notes) > 5000:
                return json_response(self, {"error": "notas muy largas (max 5000 chars)"}, status=400)
            if not notes:
                notes = "(Cliente adjunto fotos sin texto — ver imagenes)"

            # UPSERT: si ya hay review pedida abierta para (cliente, file_id),
            # actualizar las notas + reemplazar attachments. Si no, INSERT nueva.
            # Así el portal puede "Reenviar feedback" sin generar múltiples
            # entries en el dashboard del editor.
            review_id_holder = {}
            resolved_editor_holder = {}
            def _op(conn):
                existing = conn.execute("""
                    SELECT id FROM client_reviews
                    WHERE cliente=? AND COALESCE(video_file_id,'')=COALESCE(?,'')
                      AND status='revision_requested'
                    ORDER BY id DESC LIMIT 1
                """, (cliente, file_id or None)).fetchone()
                if existing:
                    rid = existing[0]
                    conn.execute("""
                        UPDATE client_reviews
                           SET notes=?,
                               video_file_name=COALESCE(?, video_file_name),
                               editor=COALESCE(?, editor),
                               responded_at=datetime('now')
                         WHERE id=?
                    """, (notes, file_name or None, editor, rid))
                    # Reemplazar attachments: borrar los viejos
                    conn.execute(
                        "DELETE FROM client_review_attachments WHERE review_id=?",
                        (rid,),
                    )
                else:
                    cur = conn.execute("""
                        INSERT INTO client_reviews
                            (cliente, video_file_id, video_file_name, editor, status, notes,
                             created_at, responded_at)
                        VALUES (?, ?, ?, ?, 'revision_requested', ?,
                                datetime('now'), datetime('now'))
                    """, (cliente, file_id or None, file_name or None, editor, notes))
                    rid = cur.lastrowid
                review_id_holder["id"] = rid
                # Insertar attachments si los hay
                for fname, mime, blob in attachments:
                    conn.execute("""
                        INSERT INTO client_review_attachments
                            (review_id, filename, mime_type, blob, size_bytes, created_at)
                        VALUES (?, ?, ?, ?, ?, datetime('now'))
                    """, (rid, fname, mime, blob, len(blob)))
            def _op_con_editor(conn):
                _op(conn)
                # Resolver el editor con la conn FRESCA y persistirlo en la review
                # (las reviews del portal llegan sin editor). Así el aviso le llega
                # al editor Y la review queda bien asignada para el dashboard.
                if not (editor or "").strip():
                    rid = review_id_holder.get("id")
                    resolved = _resolve_editor_with_conn(conn, cliente)
                    if resolved:
                        resolved_editor_holder["v"] = resolved
                        if rid:
                            conn.execute("UPDATE client_reviews SET editor=? WHERE id=? AND COALESCE(editor,'')=''",
                                         (resolved, rid))

            with_db(_op_con_editor, message=f"review: nueva revisión pedida por {cliente}" + (f" (+{len(attachments)} imgs)" if attachments else ""))
            review_id = review_id_holder.get("id")

            # Notificar editor + admin (mail + push) con links a las imágenes
            review = {
                "id": review_id,
                "cliente": cliente,
                "video_file_id": file_id,
                "video_file_name": file_name or "(video)",
                "editor": (editor or "").strip() or resolved_editor_holder.get("v"),
                "attachments_count": len(attachments),
            }
            try:
                from notifier import notify_revision_requested
                notify_revision_requested(review_id, review, notes)
            except Exception as e:
                import traceback
                print(f"[NOTIFY ERROR] review_id={review_id} cliente={cliente} editor={editor}: {e}", flush=True)
                traceback.print_exc()
                # Marcar el log para que si falla, Ignacio pueda recuperar con
                # /api/review?action=renotify&id=N&admin=1&t=...
                try:
                    from tracker import log_mail
                    log_mail(
                        to_email=editor or "(sin editor)",
                        subject=f"[NOTIFY FALLO] Revisión id={review_id} de {cliente}",
                        kind="notify-error",
                        cliente=cliente,
                        editor=editor,
                        msg_id="",
                        success=False,
                        error=f"{type(e).__name__}: {str(e)[:200]}",
                    )
                except Exception:
                    pass

            return json_response(self, {"ok": True, "id": review_id, "status": "revision_requested",
                                          "attachments": len(attachments)})
        except Exception as e:
            return json_response(self, {"error": str(e)[:200], "trace": traceback.format_exc()[:1500]}, status=500)

    def do_DELETE(self):
        """Borrar una review. SOLO admin. Body opcional con {ids:[N,...]} para
        batch delete, o query ?id=N para una sola."""
        try:
            if _IMPORT_ERROR:
                return json_response(self, {"error": _IMPORT_ERROR}, status=500)
            params = parse_qs(urlparse(self.path).query)
            token = (params.get("t", [""])[0] or "").strip()
            admin = params.get("admin", [""])[0] == "1"
            if not (admin and check_token("ADMIN", token)):
                return json_response(self, {"error": "admin required"}, status=401)

            ids = []
            single = params.get("id", [""])[0]
            if single:
                try:
                    ids = [int(single)]
                except Exception:
                    return json_response(self, {"error": "id inválido"}, status=400)
            else:
                length = int(self.headers.get("Content-Length", "0"))
                if length > 0:
                    try:
                        body = json.loads(self.rfile.read(length).decode("utf-8"))
                        ids = [int(x) for x in (body.get("ids") or [])]
                    except Exception:
                        return json_response(self, {"error": "body inválido"}, status=400)
            if not ids:
                return json_response(self, {"error": "faltan ids"}, status=400)

            def _op(conn):
                placeholders = ",".join("?" * len(ids))
                n = conn.execute(
                    f"DELETE FROM client_reviews WHERE id IN ({placeholders})", ids
                ).rowcount
                return n
            n = with_db(_op, message=f"reviews: delete {ids}")
            return json_response(self, {"ok": True, "deleted": n})
        except Exception as e:
            return json_response(self, {"error": str(e)[:200], "trace": traceback.format_exc()[:1500]}, status=500)

    def log_message(self, *a, **kw):
        pass

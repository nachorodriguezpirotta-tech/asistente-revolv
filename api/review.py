"""
Sistema de revisiones de cliente — v2 (sin reviews pending).

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
                return _html_response(self, _ERROR_HTML.format(msg="Sistema no disponible", detail=_IMPORT_ERROR[:200]), 500)
            params = parse_qs(urlparse(self.path).query)
            action = params.get("action", ["info"])[0]
            cliente = (params.get("cliente", [""])[0] or "").strip()
            file_id = (params.get("file_id", [""])[0] or "").strip()
            file_name = (params.get("file_name", [""])[0] or "").strip()
            editor = (params.get("editor", [""])[0] or "").strip()
            token = (params.get("t", [""])[0] or "").strip()

            if not cliente:
                if action == "info":
                    return json_response(self, {"error": "cliente requerido"}, status=400)
                return _html_response(self, _ERROR_HTML.format(msg="Link inválido", detail="Falta cliente"), 400)

            # Auth: token tiene que matchear con el cliente
            if not check_client_token(cliente, token):
                if action == "info":
                    return json_response(self, {"error": "unauthorized"}, status=401)
                return _html_response(self, _ERROR_HTML.format(msg="Link inválido", detail="El link expiró o no es válido."), 401)

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

            # MODO approve: solo HTML "Gracias", NO guarda nada.
            # Opcional: avisar al admin que el cliente lo aprobó (1 mail info,
            # con dedupe para que si toca varias veces, llegue 1 sola vez).
            if action == "approve":
                try:
                    from notifier import notify_review_approved_lite
                    notify_review_approved_lite(cliente, file_name, editor)
                except Exception as e:
                    print(f"notify_review_approved_lite error: {e}")
                return _html_response(self, _APPROVE_HTML.format(cliente=cliente))

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
            if not check_client_token(cliente, token):
                return json_response(self, {"error": "unauthorized"}, status=401)

            length = int(self.headers.get("Content-Length", "0"))
            content_type = (self.headers.get("Content-Type") or "").lower()
            raw_body = self.rfile.read(length) if length > 0 else b""

            notes = ""
            file_name = ""
            editor = None
            attachments = []  # lista de (filename, mime, bytes)

            if "multipart/form-data" in content_type:
                # Parsear multipart manualmente (Vercel runtime no trae cgi.FieldStorage limpio)
                import email
                import email.policy
                # Reconstruir un mensaje MIME para usar el parser de email
                msg = email.message_from_bytes(
                    b"Content-Type: " + content_type.encode() + b"\r\n\r\n" + raw_body,
                    policy=email.policy.default,
                )
                for part in msg.iter_parts():
                    disposition = part.get("Content-Disposition", "")
                    if "form-data" not in disposition:
                        continue
                    # Parsear nombre del field
                    params_disp = {}
                    for chunk in disposition.split(";"):
                        if "=" in chunk:
                            k, v = chunk.strip().split("=", 1)
                            params_disp[k.strip()] = v.strip(' "')
                    field_name = params_disp.get("name", "")
                    fname = params_disp.get("filename")
                    payload = part.get_payload(decode=True) or b""
                    if fname:
                        # Es archivo (imagen)
                        if len(attachments) >= 5:
                            continue  # cap 5 imgs
                        if len(payload) > 5 * 1024 * 1024:
                            return json_response(self, {"error": f"imagen '{fname}' muy grande (max 5MB)"}, status=413)
                        mime = part.get_content_type() or "image/jpeg"
                        if not mime.startswith("image/"):
                            return json_response(self, {"error": f"'{fname}' no es una imagen"}, status=400)
                        attachments.append((fname, mime, payload))
                    else:
                        # Es texto
                        text = payload.decode("utf-8", errors="replace") if isinstance(payload, bytes) else str(payload)
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

            # Notes opcional SI hay fotos (las imágenes pueden hablar por sí solas).
            # Si no hay nada → error.
            if not notes and not attachments:
                return json_response(self, {"error": "contanos qué cambiar o agregá al menos una foto"}, status=400)
            if len(notes) > 5000:
                return json_response(self, {"error": "notas muy largas (max 5000 chars)"}, status=400)
            # Si no hay texto pero sí fotos, poner placeholder informativo
            if not notes:
                notes = "(El cliente adjuntó fotos sin texto descriptivo — ver imágenes)"

            # Crear review + attachments en UNA sola transacción + un solo push DB
            review_id_holder = {}
            def _op(conn):
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
            with_db(_op, message=f"review: nueva revisión pedida por {cliente}" + (f" (+{len(attachments)} imgs)" if attachments else ""))
            review_id = review_id_holder.get("id")

            # Notificar editor + admin (mail + push) con links a las imágenes
            review = {
                "id": review_id,
                "cliente": cliente,
                "video_file_id": file_id,
                "video_file_name": file_name or "(video)",
                "editor": editor,
                "attachments_count": len(attachments),
            }
            try:
                from notifier import notify_revision_requested
                notify_revision_requested(review_id, review, notes)
            except Exception as e:
                print(f"notify error: {e}")

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

"""
Sistema de revisiones de cliente.

Endpoints:
  GET  /api/review?action=approve&id=N&t=TOKEN
       → un click "✅ Todo perfecto" desde el mail. Marca el review como
         approved y muestra HTML simple "Gracias!".

  GET  /api/review?action=info&id=N&t=TOKEN
       → JSON con info del review (para que la página revision.html
         muestre nombre del video, estado, etc).

  POST /api/review?id=N&t=TOKEN
       Body: {"approved": false, "notes": "cambiar X en 0:23"}
       → cliente envía revisión con notas.

Auth: el token se genera con make_client_token(cliente). En cada request
verificamos que el token corresponda al cliente del review.id.
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
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self):
        try:
            if _IMPORT_ERROR:
                return _html_response(self, _ERROR_HTML.format(msg="Sistema no disponible", detail=_IMPORT_ERROR[:200]), 500)
            params = parse_qs(urlparse(self.path).query)
            action = params.get("action", ["info"])[0]
            review_id_str = params.get("id", [""])[0]
            token = (params.get("t", [""])[0] or "").strip()

            try:
                review_id = int(review_id_str)
            except Exception:
                if action == "info":
                    return json_response(self, {"error": "id inválido"}, status=400)
                return _html_response(self, _ERROR_HTML.format(msg="Link inválido", detail="Falta o es inválido el id"), 400)

            # Cargar review (lectura)
            def _q(conn):
                r = conn.execute(
                    "SELECT id, cliente, video_file_id, video_file_name, editor, status, notes, created_at "
                    "FROM client_reviews WHERE id=?", (review_id,)
                ).fetchone()
                return dict(r) if r else None
            review = read_db(_q)
            if not review:
                if action == "info":
                    return json_response(self, {"error": "no encontrado"}, status=404)
                return _html_response(self, _ERROR_HTML.format(msg="No encontrado", detail="No existe ese review."), 404)

            # Auth: token debe matchear el cliente del review
            if not check_client_token(review["cliente"], token):
                if action == "info":
                    return json_response(self, {"error": "unauthorized"}, status=401)
                return _html_response(self, _ERROR_HTML.format(msg="Link inválido", detail="El link expiró o no es válido."), 401)

            # MODO info (para que revision.html cargue datos)
            if action == "info":
                return json_response(self, {"ok": True, **review})

            # MODO approve (link directo desde mail)
            if action == "approve":
                if review["status"] != "pending":
                    # Ya respondida — mostrar mensaje según estado
                    if review["status"] == "approved":
                        return _html_response(self, _APPROVE_HTML.format(cliente=review["cliente"]))
                    msg = "Este video ya tiene una revisión pedida"
                    return _html_response(self, _ERROR_HTML.format(msg=msg, detail="Si querés agregar algo más, escribí al admin."), 400)

                def _op(conn):
                    conn.execute(
                        "UPDATE client_reviews SET status='approved', responded_at=datetime('now') "
                        "WHERE id=? AND status='pending'", (review_id,)
                    )
                with_db(_op, message=f"review {review_id}: cliente aprobó")
                # Notificar admin (sin bloquear UX si falla)
                try:
                    from notifier import notify_review_approved
                    notify_review_approved(review_id, review)
                except Exception as e:
                    print(f"notify_review_approved error: {e}")
                return _html_response(self, _APPROVE_HTML.format(cliente=review["cliente"]))

            return json_response(self, {"error": f"action inválida: {action}"}, status=400)
        except Exception as e:
            return json_response(self, {"error": str(e)[:200], "trace": traceback.format_exc()[:1500]}, status=500)

    def do_POST(self):
        try:
            if _IMPORT_ERROR:
                return json_response(self, {"error": "import", "detail": _IMPORT_ERROR}, status=500)
            params = parse_qs(urlparse(self.path).query)
            review_id_str = params.get("id", [""])[0]
            token = (params.get("t", [""])[0] or "").strip()
            try:
                review_id = int(review_id_str)
            except Exception:
                return json_response(self, {"error": "id inválido"}, status=400)

            length = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(length).decode("utf-8") if length > 0 else "{}"
            try:
                body = json.loads(raw)
            except Exception:
                return json_response(self, {"error": "body inválido"}, status=400)

            approved = bool(body.get("approved", False))
            notes = (body.get("notes") or "").strip() or None
            if not approved and not notes:
                return json_response(self, {"error": "si pedís cambios, contanos qué cambiar"}, status=400)
            if notes and len(notes) > 5000:
                return json_response(self, {"error": "notas muy largas (max 5000 chars)"}, status=400)

            # Cargar review para auth
            def _q(conn):
                r = conn.execute(
                    "SELECT id, cliente, status, editor, video_file_id, video_file_name "
                    "FROM client_reviews WHERE id=?", (review_id,)
                ).fetchone()
                return dict(r) if r else None
            review = read_db(_q)
            if not review:
                return json_response(self, {"error": "no encontrado"}, status=404)
            if not check_client_token(review["cliente"], token):
                return json_response(self, {"error": "unauthorized"}, status=401)
            if review["status"] != "pending":
                return json_response(self, {"error": f"review ya respondida (status: {review['status']})"}, status=409)

            new_status = 'approved' if approved else 'revision_requested'

            def _op(conn):
                conn.execute(
                    "UPDATE client_reviews SET status=?, notes=?, responded_at=datetime('now') "
                    "WHERE id=? AND status='pending'",
                    (new_status, notes, review_id)
                )
            with_db(_op, message=f"review {review_id}: cliente {new_status}")

            # Notificar editor + admin (mail + push) si pidió revisión
            try:
                if approved:
                    from notifier import notify_review_approved
                    notify_review_approved(review_id, review)
                else:
                    from notifier import notify_revision_requested
                    notify_revision_requested(review_id, review, notes)
            except Exception as e:
                print(f"notify error: {e}")

            return json_response(self, {"ok": True, "status": new_status})
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

            # ids: por query ?id=N o body {"ids":[...]}
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

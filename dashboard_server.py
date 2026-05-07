"""
Mini servidor HTTP local que sirve el dashboard Y procesa cambios en la DB.

- GET /            → sirve dashboard.html (siempre regenerado fresh)
- GET /<archivo>   → sirve archivos estáticos del proyecto
- DELETE /api/task/<id>  → borra task y commitea+push al repo
- POST /api/task         → crea task pending manual y commitea+push

Estrategia robusta para evitar race conditions con el bot del cron:
  1. Lock global por proceso (threading.Lock)
  2. Pull antes de aplicar la operación a la DB
  3. Aplicar operación sobre la DB sincronizada
  4. Add + commit + push
  5. Si push falla → reset HEAD~1 + retry hasta 3 veces
"""

import http.server
import json
import os
import socketserver
import subprocess
import sys
import threading
import time
from urllib.parse import urlparse

from config import BASE_DIR
from tracker import get_conn, now_iso

PORT = 8767
GIT_BIN = "/usr/bin/git"

# Lock para serializar operaciones de DB+git (evita race entre múltiples requests)
_db_lock = threading.Lock()


def _run_git(args, **kwargs):
    return subprocess.run([GIT_BIN, "-C", BASE_DIR] + args, capture_output=True, **kwargs)


def regenerate_dashboard():
    from generate_dashboard import run as gen
    return gen()


def safe_db_change(message: str, operation):
    """
    Aplica una operación a la DB de forma robusta contra race conditions.

    `operation` es un callable que recibe una conexión SQLite y aplica el cambio.
    Devuelve True si el cambio se persistió y pusheó, False si no.

    Flujo:
      1. PULL primero (sincronizar con remote, traer commits del bot si los hay)
      2. Aplicar la operación sobre la DB actualizada
      3. Regenerar dashboard
      4. Add + commit
      5. Push
      6. Si push falla → reset y retry (hasta 3 veces)
    """
    with _db_lock:
        for attempt in range(3):
            try:
                # 1. Pull para sincronizar con remote
                pull = _run_git(["pull", "--rebase", "--autostash"], timeout=30)
                if pull.returncode != 0:
                    err = (pull.stderr or b"").decode()
                    print(f"[git] pull falló (intento {attempt + 1}): {err[:200]}", file=sys.stderr)
                    # Intentar limpiar estado si quedó a medias
                    _run_git(["rebase", "--abort"], timeout=5)
                    time.sleep(1)
                    continue

                # 2. Aplicar la operación sobre la DB actualizada
                conn = get_conn()
                try:
                    operation(conn)
                    conn.commit()
                finally:
                    conn.close()

                # 3. Regenerar dashboard
                try:
                    regenerate_dashboard()
                except Exception as e:
                    print(f"[dashboard] regen falló: {e}", file=sys.stderr)

                # 4. Add + commit
                _run_git(["add", "tracker.db", "dashboard.html"], timeout=15)
                commit = _run_git(["commit", "-m", message], timeout=15)
                if commit.returncode != 0:
                    out = (commit.stderr or commit.stdout or b"").decode()
                    if "nothing to commit" in out or "nothing added" in out:
                        print(f"[git] sin cambios reales: '{message}'")
                        return True
                    print(f"[git] commit falló: {out[:200]}", file=sys.stderr)
                    return False

                # 5. Push
                push = _run_git(["push"], timeout=30)
                if push.returncode == 0:
                    print(f"[git] '{message}' → pushed (intento {attempt + 1})")
                    return True

                # 6. Push falló (race con otro pusher). Resetear el commit local y retry.
                err = (push.stderr or b"").decode()
                print(f"[git] push falló (intento {attempt + 1}): {err[:200]}", file=sys.stderr)
                _run_git(["reset", "--hard", "HEAD~1"], timeout=10)
                time.sleep(1)

            except subprocess.TimeoutExpired:
                print(f"[git] timeout en intento {attempt + 1}", file=sys.stderr)
                _run_git(["rebase", "--abort"], timeout=5)
                time.sleep(2)
            except Exception as e:
                print(f"[git] error inesperado: {e}", file=sys.stderr)
                _run_git(["rebase", "--abort"], timeout=5)

        print(f"[git] FALLÓ tras 3 intentos: '{message}'", file=sys.stderr)
        return False


class Handler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=BASE_DIR, **kwargs)

    def log_message(self, format, *args):
        try:
            # Solo loguear API hits y errores reales
            args_str = " ".join(str(a) for a in args)
            if "/api/" in args_str or "404" in args_str or "500" in args_str:
                print(f"[{self.log_date_time_string()}] {format % args}")
        except Exception:
            pass  # nunca dejar que el logger crashee la request

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path in ("/", "/dashboard.html"):
            try:
                regenerate_dashboard()
            except Exception as e:
                print(f"[dashboard] error regenerando: {e}", file=sys.stderr)
            self.path = "/dashboard.html"
        return super().do_GET()

    def do_DELETE(self):
        parsed = urlparse(self.path)
        path = parsed.path
        if path.startswith("/api/task/"):
            try:
                task_id = int(path.split("/")[-1])
            except ValueError:
                return self._json({"ok": False, "error": "task_id inválido"}, status=400)

            # Pre-check: ver si la task existe (sin lock todavía)
            conn = get_conn()
            row = conn.execute("SELECT cliente, editor FROM tasks WHERE id = ?", (task_id,)).fetchone()
            conn.close()
            if row is None:
                return self._json({"ok": False, "error": f"task #{task_id} no existe"}, status=404)
            cliente, editor = row[0], row[1]

            # Definir la operación que va a aplicarse DESPUÉS del pull
            def op(conn):
                conn.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
                print(f"[delete] task #{task_id} → {cliente} / {editor}")

            # Ejecutar de forma robusta (en thread para no bloquear la respuesta)
            def runner():
                ok = safe_db_change(
                    message=f"manual: borrada task #{task_id} ({cliente} / {editor})",
                    operation=op,
                )
                if not ok:
                    print(f"[delete] FALLÓ persistir task #{task_id}", file=sys.stderr)

            threading.Thread(target=runner, daemon=True).start()
            return self._json({"ok": True, "task_id": task_id})

        self.send_error(404)

    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path
        if path == "/api/task":
            try:
                length = int(self.headers.get("Content-Length", "0"))
                body = self.rfile.read(length).decode("utf-8") if length > 0 else "{}"
                data = json.loads(body)
                cliente = (data.get("cliente") or "").strip()
                editor = (data.get("editor") or "").strip()
            except Exception as e:
                return self._json({"ok": False, "error": f"body inválido: {e}"}, status=400)

            if not cliente or not editor:
                return self._json({"ok": False, "error": "Falta cliente o editor"}, status=400)

            # Pre-check duplicado (sin lock)
            conn = get_conn()
            existing = conn.execute(
                "SELECT id FROM tasks WHERE cliente = ? AND editor = ? AND status = 'pending'",
                (cliente, editor),
            ).fetchone()
            conn.close()
            if existing:
                return self._json({"ok": False, "error": f"Ya hay un pendiente de '{cliente}' para {editor}"}, status=409)

            pseudo_id = f"manual:{editor.lower()}:{cliente.lower().replace(' ', '_')}:{int(time.time() * 1000000)}"

            def op(conn):
                # Re-check duplicado dentro del lock (otro request podría haber agregado)
                existing = conn.execute(
                    "SELECT id FROM tasks WHERE cliente = ? AND editor = ? AND status = 'pending'",
                    (cliente, editor),
                ).fetchone()
                if existing:
                    raise ValueError(f"Ya hay un pendiente de '{cliente}' para {editor}")
                conn.execute(
                    """INSERT INTO tasks (cliente, editor, file_id, file_name, detected_at, status, mail_sent_at)
                       VALUES (?, ?, ?, ?, ?, 'pending', ?)""",
                    (cliente, editor, pseudo_id, "(pendiente cargado manualmente)", now_iso(), now_iso()),
                )
                print(f"[create] {cliente} / {editor}")

            def runner():
                ok = safe_db_change(
                    message=f"manual: agregada {cliente} / {editor}",
                    operation=op,
                )
                if not ok:
                    print(f"[create] FALLÓ persistir {cliente}/{editor}", file=sys.stderr)

            threading.Thread(target=runner, daemon=True).start()
            return self._json({"ok": True, "cliente": cliente, "editor": editor})

        self.send_error(404)

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def _json(self, data, status=200):
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def main():
    socketserver.TCPServer.allow_reuse_address = True
    with socketserver.TCPServer(("127.0.0.1", PORT), Handler) as httpd:
        print(f"🌐 Dashboard server corriendo en http://localhost:{PORT}/")
        print(f"   Ctrl+C para parar.")
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\n👋 Server detenido.")


if __name__ == "__main__":
    main()

"""
Mini servidor HTTP local que sirve el dashboard Y procesa borrar tareas.

- GET /            → sirve dashboard.html (siempre regenerado fresh)
- GET /<archivo>   → sirve archivos estáticos del proyecto
- DELETE /api/task/<id>  → borra task de DB, regenera dashboard, commitea+push al repo

Pensado para correr en background. Iniciado por el .command, se queda vivo
hasta que el usuario lo mata o reinicia la Mac.
"""

import http.server
import socketserver
import json
import os
import subprocess
import sys
from urllib.parse import urlparse

from config import BASE_DIR
from tracker import get_conn

PORT = 8767  # 8765 lo usa el dashboard viejo de Revolv
GIT_BIN = "/usr/bin/git"


def regenerate_dashboard():
    from generate_dashboard import run as gen
    return gen()


def git_commit_push(message: str):
    """Commit + push silencioso. Si falla, log pero no rompe."""
    try:
        # pull first to avoid rejection
        subprocess.run([GIT_BIN, "-C", BASE_DIR, "pull", "--rebase", "--autostash"],
                       check=False, capture_output=True, timeout=30)
        subprocess.run([GIT_BIN, "-C", BASE_DIR, "add", "tracker.db", "dashboard.html"],
                       check=True, capture_output=True, timeout=15)
        # commit puede fallar si no hay cambios (pero eso no es error)
        result = subprocess.run([GIT_BIN, "-C", BASE_DIR, "commit", "-m", message],
                                capture_output=True, timeout=15)
        if result.returncode == 0:
            subprocess.run([GIT_BIN, "-C", BASE_DIR, "push"],
                           check=True, capture_output=True, timeout=30)
            print(f"[git] {message} → pushed")
        else:
            print(f"[git] no había cambios para commitear")
    except subprocess.CalledProcessError as e:
        print(f"[git] FALLÓ: {e.stderr.decode() if e.stderr else e}", file=sys.stderr)
    except Exception as e:
        print(f"[git] error: {e}", file=sys.stderr)


class Handler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=BASE_DIR, **kwargs)

    def log_message(self, format, *args):
        # menos ruido en logs
        if "/api/" in args[0] or "404" in args[1]:
            print(f"[{self.log_date_time_string()}] {format % args}")

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path in ("/", "/dashboard.html"):
            # Regenerar antes de servir
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

            conn = get_conn()
            row = conn.execute("SELECT cliente, editor FROM tasks WHERE id = ?", (task_id,)).fetchone()
            if row is None:
                conn.close()
                return self._json({"ok": False, "error": f"task #{task_id} no existe"}, status=404)
            cliente, editor = row[0], row[1]
            conn.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
            conn.commit()
            conn.close()
            print(f"[delete] task #{task_id} → {cliente} / {editor}")

            try:
                regenerate_dashboard()
            except Exception as e:
                print(f"[delete] no pude regenerar dashboard: {e}", file=sys.stderr)

            # Commit y push en thread separado para no bloquear la respuesta al browser
            import threading
            threading.Thread(
                target=git_commit_push,
                args=(f"manual: borrada task #{task_id} ({cliente} / {editor})",),
                daemon=True,
            ).start()

            return self._json({"ok": True, "task_id": task_id})

        self.send_error(404)

    def do_OPTIONS(self):
        # CORS preflight (por si el browser lo dispara)
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, DELETE, OPTIONS")
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

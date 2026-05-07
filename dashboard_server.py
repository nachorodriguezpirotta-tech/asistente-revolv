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


def _run_git(args: list[str], **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run([GIT_BIN, "-C", BASE_DIR] + args, capture_output=True, **kwargs)


def git_commit_push(message: str):
    """
    Commit + push robusto contra race conditions con el bot del cron.

    Flujo:
      1. Add + commit LOCAL (preservar nuestros cambios como commit local).
      2. Pull con rebase (sincronizar con remote).
      3. Si hay conflicts en tracker.db/dashboard.html: tomar OURS (nuestro cambio gana).
      4. Push.
      5. Si el push falla: retry hasta 3 veces.

    Esto previene el bug donde un pull anticipado pisaba los cambios del usuario
    antes de commitearlos.
    """
    for attempt in range(3):
        try:
            # 1. Stage y commit local
            _run_git(["add", "tracker.db", "dashboard.html"], timeout=15)
            commit_res = _run_git(["commit", "-m", message], timeout=15)
            if commit_res.returncode != 0:
                # Nada que commitear (DB no cambió). Salir silenciosamente.
                stderr = commit_res.stderr.decode() if commit_res.stderr else ""
                if "nothing to commit" in stderr or "nothing added" in stderr:
                    print(f"[git] no había cambios — '{message}' ignorado")
                    return
                print(f"[git] commit falló: {stderr}", file=sys.stderr)
                return

            # 2. Pull con rebase para sincronizar con remote
            pull_res = _run_git(["pull", "--rebase"], timeout=30)

            # 3. Si rebase falla por conflicts, resolver tomando OURS
            if pull_res.returncode != 0:
                stderr = pull_res.stderr.decode() if pull_res.stderr else ""
                stdout = pull_res.stdout.decode() if pull_res.stdout else ""
                if "conflict" in (stderr + stdout).lower() or "CONFLICT" in (stderr + stdout):
                    print(f"[git] conflict en rebase, resolviendo con OURS...")
                    _run_git(["checkout", "--ours", "tracker.db", "dashboard.html"], timeout=10)
                    _run_git(["add", "tracker.db", "dashboard.html"], timeout=10)
                    cont = _run_git(["rebase", "--continue"], timeout=15,
                                    env={"GIT_EDITOR": "true", **__import__("os").environ})
                    if cont.returncode != 0:
                        # Abortar y retry
                        _run_git(["rebase", "--abort"], timeout=10)
                        print(f"[git] rebase abortado, retry {attempt + 1}/3", file=sys.stderr)
                        continue
                else:
                    print(f"[git] pull falló sin conflict: {stderr}", file=sys.stderr)
                    return

            # 4. Push
            push_res = _run_git(["push"], timeout=30)
            if push_res.returncode == 0:
                print(f"[git] '{message}' → pushed (intento {attempt + 1})")
                return

            # 5. Push falló (alguien pusheó entre nuestro pull y push). Retry.
            stderr = push_res.stderr.decode() if push_res.stderr else ""
            print(f"[git] push falló (intento {attempt + 1}): {stderr[:200]}", file=sys.stderr)
            # Resetear el HEAD local para volver a hacer rebase
            # (NO destructivo: nuestro cambio en la DB sigue en el commit)

        except subprocess.TimeoutExpired:
            print(f"[git] timeout en intento {attempt + 1}", file=sys.stderr)
        except Exception as e:
            print(f"[git] error inesperado: {e}", file=sys.stderr)

    print(f"[git] FALLÓ tras 3 intentos: '{message}'", file=sys.stderr)


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

    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path
        if path == "/api/task":
            # Crear task manual: body {"cliente": "...", "editor": "..."}
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

            import time as _t
            from tracker import now_iso
            conn = get_conn()
            # No duplicar si ya hay pending para ese cliente+editor
            existing = conn.execute(
                "SELECT id FROM tasks WHERE cliente = ? AND editor = ? AND status = 'pending'",
                (cliente, editor)
            ).fetchone()
            if existing:
                conn.close()
                return self._json({"ok": False, "error": f"Ya hay un pendiente de '{cliente}' para {editor}"}, status=409)

            pseudo_id = f"manual:{editor.lower()}:{cliente.lower().replace(' ', '_')}:{int(_t.time() * 1000000)}"
            cur = conn.execute("""
                INSERT INTO tasks (cliente, editor, file_id, file_name, detected_at, status, mail_sent_at)
                VALUES (?, ?, ?, ?, ?, 'pending', ?)
            """, (cliente, editor, pseudo_id, "(pendiente cargado manualmente)", now_iso(), now_iso()))
            task_id = cur.lastrowid
            conn.commit()
            conn.close()
            print(f"[create] task #{task_id} → {cliente} / {editor}")

            try:
                regenerate_dashboard()
            except Exception as e:
                print(f"[create] no pude regenerar dashboard: {e}", file=sys.stderr)

            import threading
            threading.Thread(
                target=git_commit_push,
                args=(f"manual: agregada task #{task_id} ({cliente} / {editor})",),
                daemon=True,
            ).start()

            return self._json({"ok": True, "task_id": task_id, "cliente": cliente, "editor": editor})

        self.send_error(404)

    def do_OPTIONS(self):
        # CORS preflight (por si el browser lo dispara)
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

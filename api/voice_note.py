"""
Voice notes para tasks. Endpoint /api/voice_note.

POST   /api/voice_note?admin=1&t=TOKEN&task_id=N&duration=SEC
       Body = audio binario (audio/webm o audio/mp4). Content-Type del request.
       Guarda el BLOB en task_voice_notes y devuelve {id, created_at}.

GET    /api/voice_note?id=N&t=TOKEN
       Devuelve el audio binario con su mime_type. Requiere admin o editor
       dueño de la task.

GET    /api/voice_note?task_id=N&t=TOKEN&list=1
       Lista notas de una task: [{id, created_at, duration_sec, mime_type}].

DELETE /api/voice_note?id=N&admin=1&t=TOKEN
       Borra una nota (solo admin).
"""

import os
import sys
import traceback
from http.server import BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

try:
    from _shared import check_token, with_db, read_db, json_response, now_iso
    _IMPORT_ERROR = None
except Exception as _e:
    _IMPORT_ERROR = f"{type(_e).__name__}: {_e}\n{traceback.format_exc()}"


# Cap razonable para audio: 5MB. Notas suelen ser <500KB pero damos margen.
MAX_AUDIO_BYTES = 5 * 1024 * 1024


def _ensure_table(conn):
    """Crea la tabla on-demand si no existe (idempotente)."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS task_voice_notes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id INTEGER NOT NULL,
            audio_blob BLOB NOT NULL,
            mime_type TEXT NOT NULL DEFAULT 'audio/webm',
            duration_sec REAL,
            created_at TEXT NOT NULL,
            created_by TEXT
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_voice_task ON task_voice_notes(task_id);")


def _task_belongs_to_editor(conn, task_id: int, editor: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM tasks WHERE id=? AND TRIM(COALESCE(editor,''))=TRIM(?)",
        (task_id, editor),
    ).fetchone()
    return row is not None


class handler(BaseHTTPRequestHandler):

    def _auth(self, params):
        """Retorna (is_admin, editor) tras chequear token. Si nada matchea → (False, None)."""
        token = (params.get("t", [""])[0] or "").strip()
        admin = params.get("admin", [""])[0] == "1"
        editor = (params.get("editor", [""])[0] or "").strip()
        if admin and check_token("ADMIN", token):
            return True, None
        if editor and check_token(editor, token):
            return False, editor
        return False, None

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_POST(self):
        try:
            if _IMPORT_ERROR:
                return json_response(self, {"error": "import", "detail": _IMPORT_ERROR}, status=500)
            params = parse_qs(urlparse(self.path).query)
            is_admin, _editor = self._auth(params)
            if not is_admin:
                return json_response(self, {"error": "admin required"}, status=401)

            try:
                task_id = int(params.get("task_id", ["0"])[0])
            except Exception:
                return json_response(self, {"error": "task_id required"}, status=400)
            if task_id <= 0:
                return json_response(self, {"error": "task_id inválido"}, status=400)

            duration_str = params.get("duration", [""])[0]
            try:
                duration = float(duration_str) if duration_str else None
            except Exception:
                duration = None

            content_type = (self.headers.get("Content-Type") or "audio/webm").split(";")[0].strip()
            if not content_type.startswith("audio/"):
                return json_response(self, {"error": f"content-type debe ser audio/*, vino {content_type}"}, status=400)

            try:
                length = int(self.headers.get("Content-Length") or 0)
            except Exception:
                length = 0
            if length <= 0:
                return json_response(self, {"error": "body vacío"}, status=400)
            if length > MAX_AUDIO_BYTES:
                return json_response(self, {"error": f"audio demasiado grande ({length} bytes, max {MAX_AUDIO_BYTES})"}, status=413)
            audio = self.rfile.read(length)
            if not audio:
                return json_response(self, {"error": "no se pudo leer audio"}, status=400)

            def op(conn):
                _ensure_table(conn)
                cur = conn.execute("""
                    INSERT INTO task_voice_notes
                        (task_id, audio_blob, mime_type, duration_sec, created_at, created_by)
                    VALUES (?, ?, ?, ?, ?, ?)
                """, (task_id, audio, content_type, duration, now_iso(), "admin"))
                return cur.lastrowid

            note_id = with_db(op, message=f"voice-note: add task #{task_id}")
            return json_response(self, {"ok": True, "id": note_id, "task_id": task_id})
        except Exception as e:
            return json_response(self, {"error": str(e)[:200], "trace": traceback.format_exc()[:1000]}, status=500)

    def do_GET(self):
        try:
            if _IMPORT_ERROR:
                return json_response(self, {"error": "import", "detail": _IMPORT_ERROR}, status=500)
            params = parse_qs(urlparse(self.path).query)
            is_admin, editor = self._auth(params)
            if not is_admin and not editor:
                return json_response(self, {"error": "unauthorized"}, status=401)

            note_id = params.get("id", [""])[0]
            task_id = params.get("task_id", [""])[0]
            list_mode = params.get("list", [""])[0] == "1"

            # MODO 1: listar notas de una task (sin servir audio)
            if list_mode and task_id:
                try:
                    tid = int(task_id)
                except Exception:
                    return json_response(self, {"error": "task_id inválido"}, status=400)
                def q(conn):
                    _ensure_table(conn)
                    # Si es editor (no admin), verificar que la task es suya
                    if editor and not _task_belongs_to_editor(conn, tid, editor):
                        return None
                    rows = conn.execute("""
                        SELECT id, created_at, duration_sec, mime_type
                        FROM task_voice_notes
                        WHERE task_id = ?
                        ORDER BY id ASC
                    """, (tid,)).fetchall()
                    return [dict(r) for r in rows]
                data = read_db(q)
                if data is None:
                    return json_response(self, {"error": "forbidden"}, status=403)
                return json_response(self, {"ok": True, "notes": data})

            # MODO 2: servir audio binario por id
            if not note_id:
                return json_response(self, {"error": "id o (task_id+list=1) requerido"}, status=400)
            try:
                nid = int(note_id)
            except Exception:
                return json_response(self, {"error": "id inválido"}, status=400)

            def q_audio(conn):
                _ensure_table(conn)
                row = conn.execute("""
                    SELECT vn.audio_blob, vn.mime_type, vn.task_id, t.editor
                    FROM task_voice_notes vn
                    LEFT JOIN tasks t ON t.id = vn.task_id
                    WHERE vn.id = ?
                """, (nid,)).fetchone()
                return dict(row) if row else None
            row = read_db(q_audio)
            if not row:
                return json_response(self, {"error": "not found"}, status=404)
            # Si es editor, debe ser dueño de la task
            if editor and (row.get("editor") or "").strip() != editor.strip():
                return json_response(self, {"error": "forbidden"}, status=403)

            audio = row["audio_blob"]
            mime = row["mime_type"] or "audio/webm"
            self.send_response(200)
            self.send_header("Content-Type", mime)
            self.send_header("Content-Length", str(len(audio)))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Cache-Control", "private, max-age=300")
            self.end_headers()
            self.wfile.write(audio)
        except Exception as e:
            return json_response(self, {"error": str(e)[:200], "trace": traceback.format_exc()[:1000]}, status=500)

    def do_DELETE(self):
        try:
            if _IMPORT_ERROR:
                return json_response(self, {"error": "import", "detail": _IMPORT_ERROR}, status=500)
            params = parse_qs(urlparse(self.path).query)
            is_admin, _editor = self._auth(params)
            if not is_admin:
                return json_response(self, {"error": "admin required"}, status=401)
            note_id = params.get("id", [""])[0]
            try:
                nid = int(note_id)
            except Exception:
                return json_response(self, {"error": "id inválido"}, status=400)

            def op(conn):
                _ensure_table(conn)
                n = conn.execute("DELETE FROM task_voice_notes WHERE id=?", (nid,)).rowcount
                return n
            n = with_db(op, message=f"voice-note: delete #{nid}")
            return json_response(self, {"ok": True, "deleted": n})
        except Exception as e:
            return json_response(self, {"error": str(e)[:200], "trace": traceback.format_exc()[:1000]}, status=500)

    def log_message(self, *a, **kw):
        pass

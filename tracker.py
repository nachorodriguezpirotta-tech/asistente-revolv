"""
Tracker — DB local SQLite que guarda el estado del watcher de Drive.

Tablas:
  - clients: carpetas de cliente conocidas
  - known_files: archivos vistos en /Material/ de cada cliente (CRUDOS)
  - known_edited_files: archivos vistos en la carpeta del cliente fuera de /Material/ (EDITADOS)
  - tasks: tareas pendientes generadas cuando aparece crudo nuevo
"""

import os
import sqlite3
from datetime import datetime
from typing import Optional

from config import DB_PATH


def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_conn()
    c = conn.cursor()
    c.executescript("""
    CREATE TABLE IF NOT EXISTS clients (
        folder_id     TEXT PRIMARY KEY,
        cliente       TEXT NOT NULL,
        raw_folder_id TEXT,
        baseline_at   TEXT,
        last_scan_at  TEXT
    );

    CREATE TABLE IF NOT EXISTS known_files (
        file_id       TEXT PRIMARY KEY,
        cliente       TEXT NOT NULL,
        folder_id     TEXT NOT NULL,
        name          TEXT NOT NULL,
        size          INTEGER,
        created_time  TEXT,
        first_seen_at TEXT NOT NULL,
        is_baseline   INTEGER NOT NULL DEFAULT 0
    );

    CREATE TABLE IF NOT EXISTS tasks (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        cliente       TEXT NOT NULL,
        editor        TEXT,
        file_id       TEXT NOT NULL,
        file_name     TEXT NOT NULL,
        detected_at   TEXT NOT NULL,
        status        TEXT NOT NULL DEFAULT 'pending',  -- pending | done
        mail_sent_at  TEXT,
        completed_at  TEXT,
        completed_by_file_id TEXT,  -- file_id del editado que cerró la tarea (audit)
        FOREIGN KEY (file_id) REFERENCES known_files(file_id)
    );

    -- Editados: archivos en la carpeta del cliente FUERA de /Material/.
    -- Cada vez que aparece uno nuevo, cerramos la tarea pendiente más vieja del cliente.
    CREATE TABLE IF NOT EXISTS known_edited_files (
        file_id       TEXT PRIMARY KEY,
        cliente       TEXT NOT NULL,
        folder_id     TEXT NOT NULL,
        name          TEXT NOT NULL,
        size          INTEGER,
        created_time  TEXT,
        first_seen_at TEXT NOT NULL,
        is_baseline   INTEGER NOT NULL DEFAULT 0,
        closed_task_id INTEGER  -- id de la tarea que cerró este editado (puede ser NULL si no había pendientes)
    );

    CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);
    CREATE INDEX IF NOT EXISTS idx_tasks_cliente_status ON tasks(cliente, status);
    CREATE INDEX IF NOT EXISTS idx_known_cliente ON known_files(cliente);
    CREATE INDEX IF NOT EXISTS idx_known_edited_cliente ON known_edited_files(cliente);
    """)
    # Migración: agregar columna completed_by_file_id si la DB existe pero no la tiene
    cols = [r[1] for r in conn.execute("PRAGMA table_info(tasks)").fetchall()]
    if "completed_by_file_id" not in cols:
        conn.execute("ALTER TABLE tasks ADD COLUMN completed_by_file_id TEXT")
    if "pending_count" not in cols:
        conn.execute("ALTER TABLE tasks ADD COLUMN pending_count INTEGER NOT NULL DEFAULT 1")
    conn.commit()
    conn.close()


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def upsert_client(folder_id: str, cliente: str, raw_folder_id: Optional[str]):
    conn = get_conn()
    c = conn.cursor()
    c.execute("""
        INSERT INTO clients (folder_id, cliente, raw_folder_id, last_scan_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(folder_id) DO UPDATE SET
            cliente=excluded.cliente,
            raw_folder_id=excluded.raw_folder_id,
            last_scan_at=excluded.last_scan_at
    """, (folder_id, cliente, raw_folder_id, now_iso()))
    conn.commit()
    conn.close()


def set_baseline(folder_id: str):
    conn = get_conn()
    conn.execute("UPDATE clients SET baseline_at = ? WHERE folder_id = ?",
                 (now_iso(), folder_id))
    conn.commit()
    conn.close()


def add_known_file(file_id: str, cliente: str, folder_id: str, name: str,
                   size: Optional[int], created_time: Optional[str], is_baseline: bool = False):
    conn = get_conn()
    conn.execute("""
        INSERT OR IGNORE INTO known_files
        (file_id, cliente, folder_id, name, size, created_time, first_seen_at, is_baseline)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """, (file_id, cliente, folder_id, name, size, created_time, now_iso(), 1 if is_baseline else 0))
    conn.commit()
    conn.close()


def is_file_known(file_id: str) -> bool:
    conn = get_conn()
    row = conn.execute("SELECT 1 FROM known_files WHERE file_id = ?", (file_id,)).fetchone()
    conn.close()
    return row is not None


def create_task(cliente: str, editor: Optional[str], file_id: str, file_name: str) -> int:
    conn = get_conn()
    cur = conn.execute("""
        INSERT INTO tasks (cliente, editor, file_id, file_name, detected_at)
        VALUES (?, ?, ?, ?, ?)
    """, (cliente, editor, file_id, file_name, now_iso()))
    task_id = cur.lastrowid
    conn.commit()
    conn.close()
    return task_id


# ─── EDITADOS (cierre de tareas) ──────────────────────────────────────────────

def is_edited_known(file_id: str) -> bool:
    conn = get_conn()
    row = conn.execute("SELECT 1 FROM known_edited_files WHERE file_id = ?", (file_id,)).fetchone()
    conn.close()
    return row is not None


def add_known_edited_file(file_id: str, cliente: str, folder_id: str, name: str,
                          size: Optional[int], created_time: Optional[str],
                          is_baseline: bool = False,
                          closed_task_id: Optional[int] = None):
    conn = get_conn()
    conn.execute("""
        INSERT OR IGNORE INTO known_edited_files
        (file_id, cliente, folder_id, name, size, created_time, first_seen_at, is_baseline, closed_task_id)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (file_id, cliente, folder_id, name, size, created_time, now_iso(),
          1 if is_baseline else 0, closed_task_id))
    conn.commit()
    conn.close()


def edited_baseline_done(cliente: str) -> bool:
    """¿Ya tomamos baseline de los editados de este cliente alguna vez?"""
    conn = get_conn()
    row = conn.execute(
        "SELECT 1 FROM known_edited_files WHERE cliente = ? LIMIT 1", (cliente,)
    ).fetchone()
    conn.close()
    return row is not None


def close_oldest_pending(cliente: str, completed_by_file_id: str) -> Optional[int]:
    """
    Marca como 'done' la tarea pendiente MÁS VIEJA de este cliente.
    Retorna el id de la tarea cerrada o None si no había pendientes.
    """
    conn = get_conn()
    row = conn.execute("""
        SELECT id FROM tasks
        WHERE cliente = ? AND status = 'pending'
        ORDER BY detected_at ASC
        LIMIT 1
    """, (cliente,)).fetchone()
    if row is None:
        conn.close()
        return None
    task_id = row[0]
    conn.execute("""
        UPDATE tasks SET status='done', completed_at=?, completed_by_file_id=?
        WHERE id=?
    """, (now_iso(), completed_by_file_id, task_id))
    conn.commit()
    conn.close()
    return task_id


def count_pending_for_client(cliente: str) -> int:
    conn = get_conn()
    n = conn.execute("SELECT COUNT(*) FROM tasks WHERE TRIM(cliente)=TRIM(?) AND status='pending'",
                     (cliente,)).fetchone()[0]
    conn.close()
    return n


def increment_pending_count(cliente: str, editor: Optional[str]) -> bool:
    """Suma 1 al pending_count de la task pending de cliente+editor. Retorna True si encontró."""
    conn = get_conn()
    if editor:
        n = conn.execute("""
            UPDATE tasks SET pending_count = COALESCE(pending_count, 1) + 1
            WHERE TRIM(cliente)=TRIM(?) AND editor=? AND status='pending'
        """, (cliente, editor)).rowcount
    else:
        n = conn.execute("""
            UPDATE tasks SET pending_count = COALESCE(pending_count, 1) + 1
            WHERE TRIM(cliente)=TRIM(?) AND status='pending'
        """, (cliente,)).rowcount
    conn.commit()
    conn.close()
    return n > 0


def set_pending_count(cliente: str, editor: Optional[str], count: int) -> int:
    """Setea pending_count manualmente. Retorna cuántas filas afectó."""
    conn = get_conn()
    if editor:
        n = conn.execute("""
            UPDATE tasks SET pending_count = ?
            WHERE TRIM(cliente)=TRIM(?) AND editor=? AND status='pending'
        """, (count, cliente, editor)).rowcount
    else:
        n = conn.execute("""
            UPDATE tasks SET pending_count = ?
            WHERE TRIM(cliente)=TRIM(?) AND status='pending'
        """, (count, cliente)).rowcount
    conn.commit()
    conn.close()
    return n


def has_pending_for_client_editor(cliente: str, editor: Optional[str]) -> bool:
    conn = get_conn()
    if editor:
        row = conn.execute(
            "SELECT 1 FROM tasks WHERE TRIM(cliente)=TRIM(?) AND editor=? AND status='pending' LIMIT 1",
            (cliente, editor)
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT 1 FROM tasks WHERE TRIM(cliente)=TRIM(?) AND status='pending' LIMIT 1",
            (cliente,)
        ).fetchone()
    conn.close()
    return row is not None


def close_all_pending_for_client(cliente: str, completed_by_file_id: str) -> int:
    """Marca como 'done' TODAS las tareas pendientes de un cliente. Retorna cuántas cerró."""
    conn = get_conn()
    n = conn.execute("""
        UPDATE tasks SET status='done', completed_at=?, completed_by_file_id=?
        WHERE TRIM(cliente)=TRIM(?) AND status='pending'
    """, (now_iso(), completed_by_file_id, cliente)).rowcount
    conn.commit()
    conn.close()
    return n


# ─── TAREAS ───────────────────────────────────────────────────────────────────

def list_pending_tasks() -> list[sqlite3.Row]:
    conn = get_conn()
    rows = conn.execute("""
        SELECT * FROM tasks WHERE status='pending' ORDER BY detected_at ASC
    """).fetchall()
    conn.close()
    return rows


def list_clients() -> list[sqlite3.Row]:
    conn = get_conn()
    rows = conn.execute("SELECT * FROM clients ORDER BY cliente").fetchall()
    conn.close()
    return rows


def stats() -> dict:
    conn = get_conn()
    s = {
        "clients": conn.execute("SELECT COUNT(*) FROM clients").fetchone()[0],
        "known_crudos": conn.execute("SELECT COUNT(*) FROM known_files").fetchone()[0],
        "known_edited": conn.execute("SELECT COUNT(*) FROM known_edited_files").fetchone()[0],
        "pending_tasks": conn.execute("SELECT COUNT(*) FROM tasks WHERE status='pending'").fetchone()[0],
        "done_tasks": conn.execute("SELECT COUNT(*) FROM tasks WHERE status='done'").fetchone()[0],
    }
    conn.close()
    return s


if __name__ == "__main__":
    init_db()
    print(f"📦 DB inicializada en {DB_PATH}")
    print(f"   stats: {stats()}")

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
    # timeout=30: espera hasta 30s si la DB está lockeada (importante con threads)
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    # WAL mode: permite reads concurrentes con writes, mejora performance multi-thread
    try:
        conn.execute("PRAGMA journal_mode=WAL")
    except Exception:
        pass
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
    if "count_locked" not in cols:
        # Si el usuario editó el count desde el dashboard, no sobrescribir con estimaciones del scan
        conn.execute("ALTER TABLE tasks ADD COLUMN count_locked INTEGER NOT NULL DEFAULT 0")
    if "note" not in cols:
        # Nota libre del admin sobre la task (ej. "paga doble", "urgente esta semana")
        conn.execute("ALTER TABLE tasks ADD COLUMN note TEXT")
    if "urgent" not in cols:
        # Si urgent=1: recibe recordatorios más frecuentes (cada 2d vs 5d normal),
        # aparece destacada arriba del listado, badge rojo.
        conn.execute("ALTER TABLE tasks ADD COLUMN urgent INTEGER NOT NULL DEFAULT 0")

    # Tabla de "bloqueos de cliente": cuando el usuario borra un cliente manualmente,
    # NO se debe re-crear automáticamente hasta que pase un tiempo (24 horas).
    conn.execute("""
        CREATE TABLE IF NOT EXISTS client_blocks (
            cliente TEXT NOT NULL,
            editor TEXT,
            blocked_until TEXT NOT NULL,
            PRIMARY KEY (cliente, editor)
        )
    """)

    # Tabla meta: key/value para guardar estado del sistema (ej. drive_changes_page_token)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS meta (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            updated_at TEXT
        )
    """)

    # Tabla de suscripciones a Web Push notifications.
    # Cada browser/device que se suscribe queda con su endpoint + keys.
    # Cuando llega crudo nuevo o cierre, mandamos push a todos los suscriptos al editor.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS push_subscriptions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            editor TEXT,  -- NULL = admin (Ignacio)
            endpoint TEXT NOT NULL UNIQUE,
            p256dh TEXT NOT NULL,
            auth TEXT NOT NULL,
            created_at TEXT NOT NULL,
            last_used_at TEXT,
            failed_count INTEGER DEFAULT 0
        )
    """)

    # Tabla de carpetas Drive detectadas que esperan decisión del admin.
    # Cada vez que aparece una carpeta nueva en Mi Unidad que no es de un cliente conocido,
    # se mete acá. El admin decide en el dashboard: aprobar (= es cliente, asignar editor)
    # o rechazar (= no es cliente, no preguntar más).
    conn.execute("""
        CREATE TABLE IF NOT EXISTS pending_drive_folders (
            folder_id TEXT PRIMARY KEY,
            folder_name TEXT NOT NULL,
            detected_at TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            decided_at TEXT,
            decided_editor TEXT
        )
    """)

    # Tablas de CONFIGURACIÓN: editables desde el dashboard sin tocar código.
    # Reemplazan/extienden el contenido de aliases.py (que queda como seed inicial).
    conn.execute("""
        CREATE TABLE IF NOT EXISTS cfg_editors (
            name TEXT PRIMARY KEY,
            email TEXT,
            receives_daily_summary INTEGER NOT NULL DEFAULT 0,
            receives_notifications INTEGER NOT NULL DEFAULT 0,
            active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT,
            updated_at TEXT
        )
    """)
    # Migration: agregar receives_notifications si la tabla ya existía sin ese campo
    try:
        cols = [r[1] for r in conn.execute("PRAGMA table_info(cfg_editors)").fetchall()]
        if "receives_notifications" not in cols:
            conn.execute("ALTER TABLE cfg_editors ADD COLUMN receives_notifications INTEGER NOT NULL DEFAULT 0")
            # Seed: solo los 4 activos reciben mails reales (Rami, Fran, Benja, Valen).
            # Los otros tienen email solo para identificación de owner.
            for name in ("Rami", "Fran", "Benja", "Valen"):
                conn.execute("UPDATE cfg_editors SET receives_notifications=1 WHERE name=?", (name,))
            conn.commit()
    except Exception:
        pass
    conn.execute("""
        CREATE TABLE IF NOT EXISTS cfg_nicknames (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            nickname TEXT NOT NULL,
            cliente_real TEXT NOT NULL,
            editor TEXT,  -- NULL = universal; si tiene editor, solo aplica con ese editor
            created_at TEXT
        )
    """)
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS uq_nicknames_nick_editor ON cfg_nicknames(nickname, COALESCE(editor, ''))")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS cfg_aliases (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            drive_name TEXT NOT NULL UNIQUE,
            cliente_real TEXT NOT NULL,
            created_at TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS cfg_delivery_folders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            cliente TEXT NOT NULL UNIQUE,
            folder_id TEXT NOT NULL,
            description TEXT,
            created_at TEXT
        )
    """)

    # Seed inicial desde aliases.py (solo si las tablas están vacías)
    try:
        from aliases import (
            EDITORS_LIST, EDITOR_EMAILS, DAILY_SUMMARY_EDITORS,
            CLIENT_NICKNAMES, CLIENT_NICKNAMES_BY_EDITOR,
            CLIENT_ALIASES, CLIENT_DELIVERY_FOLDERS,
        )
        existing = conn.execute("SELECT COUNT(*) FROM cfg_editors").fetchone()[0]
        if existing == 0:
            now = now_iso()
            for ed in EDITORS_LIST:
                email = EDITOR_EMAILS.get(ed)
                receives = 1 if ed in DAILY_SUMMARY_EDITORS else 0
                conn.execute(
                    "INSERT OR IGNORE INTO cfg_editors (name, email, receives_daily_summary, active, created_at, updated_at) VALUES (?, ?, ?, 1, ?, ?)",
                    (ed, email, receives, now, now),
                )
        existing = conn.execute("SELECT COUNT(*) FROM cfg_nicknames").fetchone()[0]
        if existing == 0:
            now = now_iso()
            for nick, real in CLIENT_NICKNAMES.items():
                conn.execute(
                    "INSERT OR IGNORE INTO cfg_nicknames (nickname, cliente_real, editor, created_at) VALUES (?, ?, NULL, ?)",
                    (nick, real, now),
                )
            for (nick, editor), real in CLIENT_NICKNAMES_BY_EDITOR.items():
                conn.execute(
                    "INSERT OR IGNORE INTO cfg_nicknames (nickname, cliente_real, editor, created_at) VALUES (?, ?, ?, ?)",
                    (nick, real, editor, now),
                )
        existing = conn.execute("SELECT COUNT(*) FROM cfg_aliases").fetchone()[0]
        if existing == 0:
            now = now_iso()
            for drive_name, real in CLIENT_ALIASES.items():
                conn.execute(
                    "INSERT OR IGNORE INTO cfg_aliases (drive_name, cliente_real, created_at) VALUES (?, ?, ?)",
                    (drive_name, real, now),
                )
        existing = conn.execute("SELECT COUNT(*) FROM cfg_delivery_folders").fetchone()[0]
        if existing == 0:
            now = now_iso()
            for cli, folder_id in CLIENT_DELIVERY_FOLDERS.items():
                conn.execute(
                    "INSERT OR IGNORE INTO cfg_delivery_folders (cliente, folder_id, description, created_at) VALUES (?, ?, NULL, ?)",
                    (cli, folder_id, now),
                )
    except Exception as e:
        # Si aliases.py falla por algún motivo, no romper init_db
        pass

    # Tabla mail_log: audit log de TODOS los mails enviados.
    # Útil para debug ("¿se mandó este mail?") y visibilidad histórica en /config.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS mail_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sent_at TEXT NOT NULL,
            to_email TEXT NOT NULL,
            subject TEXT,
            kind TEXT,
            cliente TEXT,
            editor TEXT,
            msg_id TEXT,
            success INTEGER NOT NULL DEFAULT 1,
            error TEXT
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_mail_log_sent ON mail_log(sent_at)")

    # Tabla pending_completion_mails: cola persistente de mails de cierre/decremento.
    # Cuando el closer detecta un editado nuevo, INSERT acá ANTES de mandar mail.
    # El notifier lee filas con mail_sent_at IS NULL, manda, y marca.
    # Si el mail falla, queda NULL → próximo scan retry.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS pending_completion_mails (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id INTEGER,
            cliente TEXT NOT NULL,
            editor TEXT,
            file_id TEXT,
            file_name TEXT,
            edited_folder_id TEXT,
            client_folder_id TEXT,
            new_count INTEGER NOT NULL DEFAULT 0,
            closed INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            mail_sent_at TEXT,
            retry_count INTEGER NOT NULL DEFAULT 0
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_pending_completion_unsent ON pending_completion_mails(mail_sent_at) WHERE mail_sent_at IS NULL")

    # Tabla de progreso por editor — soporta MÚLTIPLES contadores por editor.
    # Migración si existe versión vieja sin columna 'label':
    has_progress_table = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='editor_progress'"
    ).fetchone()
    if has_progress_table:
        prog_cols = [r[1] for r in conn.execute("PRAGMA table_info(editor_progress)").fetchall()]
        if "label" not in prog_cols:
            # Backup data, recrear tabla con label
            old_rows = conn.execute("SELECT editor, current, total FROM editor_progress").fetchall()
            conn.execute("DROP TABLE editor_progress")
            conn.execute("""
                CREATE TABLE editor_progress (
                    editor TEXT NOT NULL,
                    label TEXT NOT NULL,
                    current INTEGER NOT NULL DEFAULT 0,
                    total INTEGER NOT NULL DEFAULT 0,
                    updated_at TEXT,
                    PRIMARY KEY (editor, label)
                )
            """)
            now = datetime.now().isoformat(timespec='seconds')
            for editor, current, total in old_rows:
                conn.execute(
                    "INSERT INTO editor_progress (editor, label, current, total, updated_at) VALUES (?, 'Básicos', ?, ?, ?)",
                    (editor, current, total, now),
                )
    else:
        conn.execute("""
            CREATE TABLE editor_progress (
                editor TEXT NOT NULL,
                label TEXT NOT NULL,
                current INTEGER NOT NULL DEFAULT 0,
                total INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT,
                PRIMARY KEY (editor, label)
            )
        """)

    # Seed: Benja con dos contadores
    now = datetime.now().isoformat(timespec='seconds')
    conn.execute("""
        INSERT OR IGNORE INTO editor_progress (editor, label, current, total, updated_at)
        VALUES ('Benja', 'Básicos', 0, 60, ?)
    """, (now,))
    conn.execute("""
        INSERT OR IGNORE INTO editor_progress (editor, label, current, total, updated_at)
        VALUES ('Benja', 'Avanzados', 0, 30, ?)
    """, (now,))
    conn.commit()
    conn.close()


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


# ─── Meta (key/value para estado del sistema) ────────────────────────────────

def meta_get(key: str) -> Optional[str]:
    conn = get_conn()
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    conn.close()
    return row[0] if row else None


def meta_set(key: str, value: str):
    conn = get_conn()
    conn.execute("""
        INSERT INTO meta (key, value, updated_at) VALUES (?, ?, ?)
        ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at
    """, (key, value, now_iso()))
    conn.commit()
    conn.close()


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


def claim_file(file_id: str, cliente: str, folder_id: str, name: str,
               size: Optional[int], created_time: Optional[str], is_baseline: bool = False) -> bool:
    """Versión atómica: INSERT y retorna True si efectivamente insertó (primero en verlo),
    False si ya existía (otro proceso/workflow lo claimó). Sirve como lock anti-race condition."""
    conn = get_conn()
    cur = conn.execute("""
        INSERT OR IGNORE INTO known_files
        (file_id, cliente, folder_id, name, size, created_time, first_seen_at, is_baseline)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """, (file_id, cliente, folder_id, name, size, created_time, now_iso(), 1 if is_baseline else 0))
    inserted = cur.rowcount > 0
    conn.commit()
    conn.close()
    return inserted


def is_file_baseline(file_id: str) -> bool:
    """¿El archivo está marcado como baseline (ya existía antes del baseline,
    no es trabajo pendiente)?"""
    conn = get_conn()
    row = conn.execute(
        "SELECT is_baseline FROM known_files WHERE file_id = ?", (file_id,)
    ).fetchone()
    conn.close()
    return bool(row and row["is_baseline"])


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


def claim_edited_file(file_id: str, cliente: str, folder_id: str, name: str,
                      size: Optional[int], created_time: Optional[str],
                      is_baseline: bool = False,
                      closed_task_id: Optional[int] = None) -> bool:
    """Versión atómica de add_known_edited_file: retorna True si efectivamente insertó
    (primero en verlo), False si ya existía. Sirve como lock anti-race condition
    para evitar mails de cierre duplicados cuando dos scans corren concurrentes."""
    conn = get_conn()
    cur = conn.execute("""
        INSERT OR IGNORE INTO known_edited_files
        (file_id, cliente, folder_id, name, size, created_time, first_seen_at, is_baseline, closed_task_id)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (file_id, cliente, folder_id, name, size, created_time, now_iso(),
          1 if is_baseline else 0, closed_task_id))
    inserted = cur.rowcount > 0
    conn.commit()
    conn.close()
    return inserted


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


def set_pending_count(cliente: str, editor: Optional[str], count: int, lock: bool = False) -> int:
    """
    Setea pending_count. Si lock=True, marca count_locked=1 (no será sobrescrito por scan).
    Retorna cuántas filas afectó.
    """
    conn = get_conn()
    locked_val = 1 if lock else None
    if editor:
        if lock:
            n = conn.execute("""
                UPDATE tasks SET pending_count=?, count_locked=1
                WHERE TRIM(cliente)=TRIM(?) AND editor=? AND status='pending'
            """, (count, cliente, editor)).rowcount
        else:
            # Solo actualizar si NO está locked
            n = conn.execute("""
                UPDATE tasks SET pending_count=?
                WHERE TRIM(cliente)=TRIM(?) AND editor=? AND status='pending'
                AND COALESCE(count_locked, 0) = 0
            """, (count, cliente, editor)).rowcount
    else:
        if lock:
            n = conn.execute("""
                UPDATE tasks SET pending_count=?, count_locked=1
                WHERE TRIM(cliente)=TRIM(?) AND status='pending'
            """, (count, cliente)).rowcount
        else:
            n = conn.execute("""
                UPDATE tasks SET pending_count=?
                WHERE TRIM(cliente)=TRIM(?) AND status='pending'
                AND COALESCE(count_locked, 0) = 0
            """, (count, cliente)).rowcount
    conn.commit()
    conn.close()
    return n


def has_manual_pending_for_client(cliente: str) -> bool:
    """¿El cliente tiene alguna task pending con count_locked=1 (decisión manual del admin)?
    Si sí, el scan NO debe crear duplicados para otro editor según el Sheet.
    Esto respeta cuando Ignacio asigna manualmente un cliente a un editor distinto al del Sheet."""
    conn = get_conn()
    row = conn.execute(
        "SELECT 1 FROM tasks WHERE TRIM(cliente)=TRIM(?) AND status='pending' AND COALESCE(count_locked, 0) = 1 LIMIT 1",
        (cliente,)
    ).fetchone()
    conn.close()
    return row is not None


def enqueue_completion_mail(task_id: Optional[int], cliente: str, editor: Optional[str],
                            file_id: Optional[str], file_name: Optional[str],
                            edited_folder_id: Optional[str], client_folder_id: Optional[str],
                            new_count: int, closed: bool) -> int:
    """Encola un mail de cierre/decremento para envío. Si el envío falla, queda en cola
    para retry. Retorna el id del row insertado."""
    conn = get_conn()
    cur = conn.execute("""
        INSERT INTO pending_completion_mails
        (task_id, cliente, editor, file_id, file_name, edited_folder_id, client_folder_id,
         new_count, closed, created_at, mail_sent_at, retry_count)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, 0)
    """, (task_id, cliente, editor, file_id, file_name, edited_folder_id, client_folder_id,
          new_count, 1 if closed else 0, now_iso()))
    row_id = cur.lastrowid
    conn.commit()
    conn.close()
    return row_id


def list_pending_completion_mails(max_age_days: int = 7) -> list[dict]:
    """Devuelve mails de cierre encolados sin enviar (mail_sent_at IS NULL).
    Filtra por max_age_days para no retry indefinidamente (descartar muy viejos).
    """
    from datetime import timedelta, datetime as _dt
    cutoff = (_dt.now() - timedelta(days=max_age_days)).isoformat(timespec="seconds")
    conn = get_conn()
    rows = conn.execute("""
        SELECT * FROM pending_completion_mails
        WHERE mail_sent_at IS NULL AND created_at >= ?
        ORDER BY created_at ASC
    """, (cutoff,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def mark_completion_mail_sent(row_id: int):
    conn = get_conn()
    conn.execute(
        "UPDATE pending_completion_mails SET mail_sent_at = ? WHERE id = ?",
        (now_iso(), row_id),
    )
    conn.commit()
    conn.close()


def log_mail(to_email: str, subject: str, kind: str = "",
             cliente: Optional[str] = None, editor: Optional[str] = None,
             msg_id: Optional[str] = None, success: bool = True,
             error: Optional[str] = None):
    """Registra un mail enviado (o intentado) en mail_log para auditoría."""
    try:
        conn = get_conn()
        conn.execute("""
            INSERT INTO mail_log (sent_at, to_email, subject, kind, cliente, editor, msg_id, success, error)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (now_iso(), to_email, subject, kind, cliente, editor, msg_id, 1 if success else 0, error))
        conn.commit()
        conn.close()
    except Exception:
        pass


def list_mail_log(limit: int = 200) -> list[dict]:
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM mail_log ORDER BY sent_at DESC LIMIT ?", (limit,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def claim_completion_mail(row_id: int) -> bool:
    """Intenta marcar el mail como 'siendo enviado' atómicamente. Retorna True si lo
    consiguió (este proceso es el primero), False si ya fue marcado por otro proceso.

    Sirve como lock antes de mandar el mail real para evitar duplicados cuando
    dos scans concurrentes leen la misma cola."""
    conn = get_conn()
    cur = conn.execute(
        "UPDATE pending_completion_mails SET mail_sent_at = ? WHERE id = ? AND mail_sent_at IS NULL",
        (now_iso(), row_id),
    )
    rows = cur.rowcount
    conn.commit()
    conn.close()
    return rows > 0


def mark_completion_mail_failed(row_id: int):
    """Incrementa retry_count para tracking. No marca como enviado."""
    conn = get_conn()
    conn.execute(
        "UPDATE pending_completion_mails SET retry_count = retry_count + 1 WHERE id = ?",
        (row_id,),
    )
    conn.commit()
    conn.close()


# ─── Config helpers (lee de cfg_* tablas, con fallback a aliases.py si vacío) ──

def cfg_list_editors() -> list[dict]:
    conn = get_conn()
    rows = conn.execute("""
        SELECT name, email, receives_daily_summary, active, created_at, updated_at
        FROM cfg_editors ORDER BY name
    """).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def cfg_get_editor_emails() -> dict:
    """Devuelve {editor_name: email} de TODOS los editores activos con email.
    Esta lista se usa para IDENTIFICAR al editor por owner del archivo en Drive,
    NO necesariamente para mandarles mails."""
    conn = get_conn()
    rows = conn.execute(
        "SELECT name, email FROM cfg_editors WHERE active=1 AND email IS NOT NULL AND email != ''"
    ).fetchall()
    conn.close()
    return {r["name"]: r["email"] for r in rows}


def cfg_get_notification_emails() -> dict:
    """Devuelve {editor_name: email} de editores que SÍ deben recibir mails
    (crudos nuevos, cierres, recordatorios). Es un subset de cfg_get_editor_emails."""
    conn = get_conn()
    rows = conn.execute(
        "SELECT name, email FROM cfg_editors WHERE active=1 AND receives_notifications=1 AND email IS NOT NULL AND email != ''"
    ).fetchall()
    conn.close()
    return {r["name"]: r["email"] for r in rows}


def cfg_get_daily_summary_editors() -> set:
    conn = get_conn()
    rows = conn.execute(
        "SELECT name FROM cfg_editors WHERE active=1 AND receives_daily_summary=1"
    ).fetchall()
    conn.close()
    return {r["name"] for r in rows}


def cfg_get_editors_list() -> list[str]:
    """Lista de editores activos (canónica para dashboard)."""
    conn = get_conn()
    rows = conn.execute("SELECT name FROM cfg_editors WHERE active=1 ORDER BY name").fetchall()
    conn.close()
    return [r["name"] for r in rows]


def cfg_upsert_editor(name: str, email: Optional[str], receives_daily_summary: bool, active: bool = True):
    conn = get_conn()
    now = now_iso()
    conn.execute("""
        INSERT INTO cfg_editors (name, email, receives_daily_summary, active, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(name) DO UPDATE SET
            email=excluded.email,
            receives_daily_summary=excluded.receives_daily_summary,
            active=excluded.active,
            updated_at=excluded.updated_at
    """, (name, email, 1 if receives_daily_summary else 0, 1 if active else 0, now, now))
    conn.commit()
    conn.close()


def cfg_delete_editor(name: str):
    conn = get_conn()
    conn.execute("DELETE FROM cfg_editors WHERE name = ?", (name,))
    conn.commit()
    conn.close()


def cfg_list_nicknames() -> list[dict]:
    conn = get_conn()
    rows = conn.execute("SELECT id, nickname, cliente_real, editor, created_at FROM cfg_nicknames ORDER BY nickname").fetchall()
    conn.close()
    return [dict(r) for r in rows]


def cfg_get_nicknames() -> dict:
    """Devuelve {nickname_lower: cliente_real} para los universales."""
    conn = get_conn()
    rows = conn.execute("SELECT nickname, cliente_real FROM cfg_nicknames WHERE editor IS NULL").fetchall()
    conn.close()
    return {r["nickname"].lower(): r["cliente_real"] for r in rows}


def cfg_get_nicknames_by_editor() -> dict:
    """Devuelve {(nick_lower, editor_lower): cliente_real}."""
    conn = get_conn()
    rows = conn.execute("SELECT nickname, cliente_real, editor FROM cfg_nicknames WHERE editor IS NOT NULL").fetchall()
    conn.close()
    return {(r["nickname"].lower(), r["editor"].lower()): r["cliente_real"] for r in rows}


def cfg_add_nickname(nickname: str, cliente_real: str, editor: Optional[str]) -> int:
    conn = get_conn()
    cur = conn.execute(
        "INSERT INTO cfg_nicknames (nickname, cliente_real, editor, created_at) VALUES (?, ?, ?, ?)",
        (nickname, cliente_real, editor, now_iso()),
    )
    rid = cur.lastrowid
    conn.commit()
    conn.close()
    return rid


def cfg_delete_nickname(row_id: int):
    conn = get_conn()
    conn.execute("DELETE FROM cfg_nicknames WHERE id = ?", (row_id,))
    conn.commit()
    conn.close()


def cfg_list_aliases() -> list[dict]:
    conn = get_conn()
    rows = conn.execute("SELECT id, drive_name, cliente_real, created_at FROM cfg_aliases ORDER BY drive_name").fetchall()
    conn.close()
    return [dict(r) for r in rows]


def cfg_get_aliases() -> dict:
    """Devuelve {drive_name_lower: cliente_real}."""
    conn = get_conn()
    rows = conn.execute("SELECT drive_name, cliente_real FROM cfg_aliases").fetchall()
    conn.close()
    return {r["drive_name"].lower(): r["cliente_real"] for r in rows}


def cfg_add_alias(drive_name: str, cliente_real: str) -> int:
    conn = get_conn()
    cur = conn.execute(
        "INSERT INTO cfg_aliases (drive_name, cliente_real, created_at) VALUES (?, ?, ?)",
        (drive_name, cliente_real, now_iso()),
    )
    rid = cur.lastrowid
    conn.commit()
    conn.close()
    return rid


def cfg_delete_alias(row_id: int):
    conn = get_conn()
    conn.execute("DELETE FROM cfg_aliases WHERE id = ?", (row_id,))
    conn.commit()
    conn.close()


def cfg_list_delivery_folders() -> list[dict]:
    conn = get_conn()
    rows = conn.execute("SELECT id, cliente, folder_id, description, created_at FROM cfg_delivery_folders ORDER BY cliente").fetchall()
    conn.close()
    return [dict(r) for r in rows]


def cfg_get_delivery_folders() -> dict:
    """Devuelve {cliente: folder_id}."""
    conn = get_conn()
    rows = conn.execute("SELECT cliente, folder_id FROM cfg_delivery_folders").fetchall()
    conn.close()
    return {r["cliente"]: r["folder_id"] for r in rows}


def cfg_add_delivery_folder(cliente: str, folder_id: str, description: Optional[str] = None) -> int:
    conn = get_conn()
    cur = conn.execute(
        "INSERT INTO cfg_delivery_folders (cliente, folder_id, description, created_at) VALUES (?, ?, ?, ?)",
        (cliente, folder_id, description, now_iso()),
    )
    rid = cur.lastrowid
    conn.commit()
    conn.close()
    return rid


def cfg_delete_delivery_folder(row_id: int):
    conn = get_conn()
    conn.execute("DELETE FROM cfg_delivery_folders WHERE id = ?", (row_id,))
    conn.commit()
    conn.close()


# ─── Detección de carpetas Drive nuevas ───────────────────────────────────

def upsert_pending_drive_folder(folder_id: str, folder_name: str):
    """Inserta carpeta pendiente de decisión. Si ya existe con status decidido (approved/rejected), NO la re-pone como pending."""
    conn = get_conn()
    existing = conn.execute(
        "SELECT status FROM pending_drive_folders WHERE folder_id = ?", (folder_id,)
    ).fetchone()
    if existing:
        # Ya decidida (approved/rejected) → no tocar. Solo si está pending, refrescar nombre.
        if existing["status"] == "pending":
            conn.execute("UPDATE pending_drive_folders SET folder_name = ? WHERE folder_id = ?", (folder_name, folder_id))
            conn.commit()
        conn.close()
        return
    conn.execute("""
        INSERT INTO pending_drive_folders (folder_id, folder_name, detected_at, status)
        VALUES (?, ?, ?, 'pending')
    """, (folder_id, folder_name, now_iso()))
    conn.commit()
    conn.close()


def list_pending_drive_folders(status: str = 'pending') -> list[dict]:
    conn = get_conn()
    rows = conn.execute(
        "SELECT folder_id, folder_name, detected_at, status, decided_at, decided_editor FROM pending_drive_folders WHERE status = ? ORDER BY detected_at DESC",
        (status,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def decide_pending_drive_folder(folder_id: str, decision: str, editor: Optional[str] = None):
    """decision: 'approved' o 'rejected'. Si approved, agrega a `clients` con folder_id."""
    if decision not in ("approved", "rejected"):
        raise ValueError("decision inválida")
    conn = get_conn()
    row = conn.execute(
        "SELECT folder_id, folder_name FROM pending_drive_folders WHERE folder_id = ?",
        (folder_id,)
    ).fetchone()
    if not row:
        conn.close()
        raise ValueError("folder no encontrado")
    conn.execute("""
        UPDATE pending_drive_folders SET status = ?, decided_at = ?, decided_editor = ?
        WHERE folder_id = ?
    """, (decision, now_iso(), editor, folder_id))
    if decision == "approved":
        # Agregar a tabla clients para que el dashboard lo linkee
        conn.execute("""
            INSERT INTO clients (folder_id, cliente, raw_folder_id, last_scan_at)
            VALUES (?, ?, NULL, ?)
            ON CONFLICT(folder_id) DO UPDATE SET cliente=excluded.cliente, last_scan_at=excluded.last_scan_at
        """, (folder_id, row["folder_name"], now_iso()))
    conn.commit()
    conn.close()


def find_similar_pending_client(cliente: str) -> Optional[str]:
    """Busca pending tasks con nombre similar (fuzzy match) al cliente dado.
    Sirve para detectar duplicados por apodos: 'Cisco' (manual) vs 'Cisco Amengual' (scan).

    Retorna el nombre del cliente que matchea si encuentra uno, None si no.

    Lógica:
      - Normaliza ambos nombres (sin acentos, minúsculas)
      - Match si:
          a) Uno es prefijo del otro (con espacio o final)
          b) Comparten al menos un token de >=4 chars
    """
    import unicodedata
    def _norm(s):
        s = unicodedata.normalize("NFD", s or "")
        s = "".join(c for c in s if unicodedata.category(c) != "Mn")
        return " ".join(s.lower().split())

    target = _norm(cliente)
    if not target:
        return None
    target_tokens = {t for t in target.split() if len(t) >= 4}

    conn = get_conn()
    rows = conn.execute(
        "SELECT DISTINCT cliente FROM tasks WHERE status='pending'"
    ).fetchall()
    conn.close()

    for r in rows:
        existing = _norm(r["cliente"])
        if not existing or existing == target:
            continue  # exact match no es duplicado por apodo
        # Caso prefijo: 'cisco' es prefijo de 'cisco amengual'
        if existing.startswith(target + " ") or target.startswith(existing + " "):
            return r["cliente"]
        # Caso token compartido (>=4 chars): 'roger marti' vs 'roger mendez' → ojo, false positive
        # Solo aceptar si compartido es UN token único distintivo
        existing_tokens = {t for t in existing.split() if len(t) >= 4}
        shared = target_tokens & existing_tokens
        # Solo si el shared token es ÚNICO en ambos lados (no genérico como "video", "edit")
        if shared and len(target_tokens) <= 2 and len(existing_tokens) <= 2:
            # Ambos nombres cortos (1-2 tokens) que comparten uno → probable misma persona
            return r["cliente"]
    return None


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


def block_client(cliente: str, editor: Optional[str], hours: int = 24):
    """
    Marca un cliente como 'no re-crear automáticamente' por X horas.
    Útil cuando el usuario borra manual y no quiere que vuelva a aparecer.
    """
    from datetime import datetime, timedelta
    blocked_until = (datetime.now() + timedelta(hours=hours)).isoformat(timespec="seconds")
    conn = get_conn()
    conn.execute("""
        INSERT INTO client_blocks (cliente, editor, blocked_until)
        VALUES (TRIM(?), ?, ?)
        ON CONFLICT(cliente, editor) DO UPDATE SET blocked_until=excluded.blocked_until
    """, (cliente, editor or "", blocked_until))
    conn.commit()
    conn.close()


def is_client_blocked(cliente: str, editor: Optional[str]) -> bool:
    """Devuelve True si el cliente+editor tiene un bloqueo activo (no expirado)."""
    conn = get_conn()
    row = conn.execute("""
        SELECT blocked_until FROM client_blocks
        WHERE TRIM(cliente)=TRIM(?) AND (editor=? OR editor='' OR editor IS NULL)
    """, (cliente, editor or "")).fetchone()
    conn.close()
    if not row:
        return False
    from datetime import datetime
    try:
        until = datetime.fromisoformat(row["blocked_until"])
        return datetime.now() < until
    except Exception:
        return False


def decrement_pending_count(cliente: str, completed_by_file_id: str) -> dict:
    """
    Decrementa pending_count en 1. Si llega a 0, marca la task como done.
    Retorna: {'task_id', 'new_count', 'closed': bool}
    Si NO había task pending, retorna {'task_id': None, ...}
    """
    conn = get_conn()
    row = conn.execute("""
        SELECT id, COALESCE(pending_count, 1) as cnt, editor FROM tasks
        WHERE TRIM(cliente)=TRIM(?) AND status='pending'
        ORDER BY detected_at ASC LIMIT 1
    """, (cliente,)).fetchone()
    if not row:
        conn.close()
        return {"task_id": None, "new_count": 0, "closed": False, "editor": None}

    task_id = row["id"]
    editor = row["editor"]
    new_count = (row["cnt"] or 1) - 1

    if new_count <= 0:
        conn.execute("""
            UPDATE tasks SET status='done', completed_at=?, completed_by_file_id=?, pending_count=0
            WHERE id=?
        """, (now_iso(), completed_by_file_id, task_id))
        result = {"task_id": task_id, "new_count": 0, "closed": True, "editor": editor}
    else:
        conn.execute("""
            UPDATE tasks SET pending_count=?
            WHERE id=?
        """, (new_count, task_id))
        result = {"task_id": task_id, "new_count": new_count, "closed": False, "editor": editor}

    conn.commit()
    conn.close()
    return result


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

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

try:
    from config import DB_PATH
except ImportError:
    # En Vercel, los endpoints insertan api/ primero en sys.path y 'config'
    # resuelve a api/config.py (el ENDPOINT) → ImportError. Cargar el config.py
    # de la RAÍZ por ruta absoluta. (Bug 11/jun: mails de revisión no salían.)
    import os as _os, importlib.util as _ilu
    _p = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "config.py")
    _s = _ilu.spec_from_file_location("_root_config", _p)
    _m = _ilu.module_from_spec(_s)
    _s.loader.exec_module(_m)
    DB_PATH = _m.DB_PATH


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

    # Migration: subfolder_name en known_files / known_edited_files. Sirve para
    # auto-detectar clientes con subcarpetas tipo "Youtube"/"Reels" y para
    # inferir el editor por subfolder mirando histórico de entregas.
    kf_cols = [r[1] for r in conn.execute("PRAGMA table_info(known_files)").fetchall()]
    if "subfolder_name" not in kf_cols:
        conn.execute("ALTER TABLE known_files ADD COLUMN subfolder_name TEXT")
    ke_cols = [r[1] for r in conn.execute("PRAGMA table_info(known_edited_files)").fetchall()]
    if "subfolder_name" not in ke_cols:
        conn.execute("ALTER TABLE known_edited_files ADD COLUMN subfolder_name TEXT")

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
    # Migration: agregar columnas faltantes en cfg_editors si la tabla ya existía
    try:
        cols = [r[1] for r in conn.execute("PRAGMA table_info(cfg_editors)").fetchall()]
        if "receives_notifications" not in cols:
            conn.execute("ALTER TABLE cfg_editors ADD COLUMN receives_notifications INTEGER NOT NULL DEFAULT 0")
            for name in ("Rami", "Fran", "Benja", "Valen"):
                conn.execute("UPDATE cfg_editors SET receives_notifications=1 WHERE name=?", (name,))
        if "on_vacation" not in cols:
            # 🌴 Modo vacaciones: editor activo pero pausado (no mails, no recordatorios)
            conn.execute("ALTER TABLE cfg_editors ADD COLUMN on_vacation INTEGER NOT NULL DEFAULT 0")
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

    # Tabla de clientes con mail + flag de notificaciones. Cuando se entrega un
    # video del cliente Y notifications_enabled=1, le mandamos mail estilo
    # "🎬 tu video está listo". `display_name` opcional: nombre amigable para
    # el saludo del mail (ej. "Roger" en vez de "Roger Marti"). Si está vacío
    # se usa el primer token del `cliente`.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS cfg_clients (
            cliente TEXT PRIMARY KEY,
            email TEXT NOT NULL,
            display_name TEXT,
            notifications_enabled INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
    """)

    # Tabla de overrides editor-por-subfolder. Para clientes con múltiples editores
    # según el tipo de contenido. Ej Roger Marti:
    #   (Roger Marti, '', Valen)        → archivos en /Material/ root → Valen (reels)
    #   (Roger Marti, 'Youtube', Fran)  → archivos en /Material/Youtube/ → Fran
    # subfolder='' o NULL = default para /Material/ root del cliente.
    # subfolder matchea por _normalize (case/acentos/espacios insensitive) y
    # acepta substring (ej "youtube" matchea "Youtube ENERO 24").
    conn.execute("""
        CREATE TABLE IF NOT EXISTS cfg_subfolder_editors (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            cliente TEXT NOT NULL,
            subfolder TEXT NOT NULL DEFAULT '',
            editor TEXT NOT NULL,
            created_at TEXT,
            UNIQUE(cliente, subfolder)
        )
    """)

    # Asignación editor por cliente (override del Sheet). Cuando Nacho edita
    # en el dashboard "este cliente lo maneja Juan", se guarda acá. El lookup
    # de editor primero mira esta tabla, después cae al Sheet. Pedido 28/may.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS cfg_client_editor (
            cliente TEXT PRIMARY KEY,
            editor TEXT NOT NULL,
            updated_at TEXT
        )
    """)

    # Clientes archivados desde el dashboard (no trabajamos más con ellos).
    # Backend filtra estos de client_editors para que no aparezcan en la
    # tab Asignaciones. Pedido 28/may.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS cfg_archived_clients (
            cliente TEXT PRIMARY KEY,
            archived_at TEXT
        )
    """)

    # Alertas idempotentes: una vez por (cliente, subfolder). Cuando aparece un
    # crudo en una subfolder "tipo" (Youtube/Reels/Shorts/...) que no está
    # mapeada en cfg_subfolder_editors, registramos acá para mandar UN mail al
    # admin. Si después aparecen más crudos en la misma subfolder, NO repetimos.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS subfolder_alerts (
            cliente TEXT NOT NULL,
            subfolder TEXT NOT NULL,
            alerted_at TEXT NOT NULL,
            inferred_type TEXT,
            example_file TEXT,
            example_file_id TEXT,
            default_editor_assigned TEXT,
            PRIMARY KEY (cliente, subfolder)
        )
    """)

    # Attachments de revisiones (fotos que el cliente sube para mostrar cambios).
    # Guardados como BLOB en SQLite. Cap por el endpoint POST (5MB cada una).
    conn.execute("""
        CREATE TABLE IF NOT EXISTS client_review_attachments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            review_id INTEGER NOT NULL,
            filename TEXT,
            mime_type TEXT NOT NULL DEFAULT 'image/jpeg',
            blob BLOB NOT NULL,
            size_bytes INTEGER,
            created_at TEXT NOT NULL,
            FOREIGN KEY (review_id) REFERENCES client_reviews(id) ON DELETE CASCADE
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_review_attach_review ON client_review_attachments(review_id);")

    # Revisiones de clientes: cuando entregamos un video, el cliente puede
    # aprobar (👍) o pedir cambios (📝). Cada revisión queda registrada con
    # el texto que dejó el cliente. Cuando el editor sube la corrección
    # (sistema ya lo detecta por nombre), se marca la revisión como resolved.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS client_reviews (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            cliente TEXT NOT NULL,
            video_file_id TEXT,
            video_file_name TEXT,
            editor TEXT,
            status TEXT NOT NULL DEFAULT 'pending',
                -- 'pending' (todavía no respondió cliente)
                -- 'approved' (cliente lo aprobó)
                -- 'revision_requested' (cliente pidió cambios)
                -- 'resolved' (editor subió la corrección, todo OK)
            notes TEXT,
            created_at TEXT NOT NULL,
            responded_at TEXT,
            resolved_at TEXT
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_review_cliente ON client_reviews(cliente);")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_review_video ON client_reviews(video_file_id);")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_review_status ON client_reviews(status);")
    # notified_at: cuándo se avisó al editor/admin de la revisión pedida. NULL =
    # falta avisar. El scan (durable) procesa las NULL — antes el aviso lo mandaba
    # solo el endpoint del portal (síncrono), que se perdía si la función Vercel
    # moría entre el commit y el envío → el editor nunca se enteraba.
    if "notified_at" not in [r[1] for r in conn.execute("PRAGMA table_info(client_reviews)").fetchall()]:
        conn.execute("ALTER TABLE client_reviews ADD COLUMN notified_at TEXT")

    # Voice notes: cada task puede tener N notas de voz dejadas por el admin
    # como feedback rápido al editor. El audio se guarda como BLOB en SQLite
    # (típicamente 5-30s, <500KB → no vale la pena montar Drive/Blob storage
    # para esto). Se sirve vía /api/voice-note?id=X que retorna audio/webm.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS task_voice_notes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id INTEGER NOT NULL,
            audio_blob BLOB NOT NULL,
            mime_type TEXT NOT NULL DEFAULT 'audio/webm',
            duration_sec REAL,
            created_at TEXT NOT NULL,
            created_by TEXT,
            FOREIGN KEY (task_id) REFERENCES tasks(id) ON DELETE CASCADE
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_voice_task ON task_voice_notes(task_id);")

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
    # Índice para que el dedupe (lookup por to_email + subject + sent_at) sea rápido.
    conn.execute("CREATE INDEX IF NOT EXISTS idx_mail_log_dedupe ON mail_log(to_email, subject, sent_at)")
    # Migration: agregar columna dedupe_key para anti-duplicados a nivel DB.
    # NO usamos UNIQUE constraint en la tabla (rompería con rows viejas que
    # tienen mismo to+subject de meses atrás). En su lugar, el lookup de
    # dedupe filtra por sent_at >= cutoff.
    ml_cols = [r[1] for r in conn.execute("PRAGMA table_info(mail_log)").fetchall()]
    if "dedupe_key" not in ml_cols:
        conn.execute("ALTER TABLE mail_log ADD COLUMN dedupe_key TEXT")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_mail_log_key ON mail_log(dedupe_key, sent_at)")

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
    # Migration: agregar columna is_correction si no existe
    try:
        cols = [r[1] for r in conn.execute("PRAGMA table_info(pending_completion_mails)").fetchall()]
        if "is_correction" not in cols:
            conn.execute("ALTER TABLE pending_completion_mails ADD COLUMN is_correction INTEGER NOT NULL DEFAULT 0")
    except Exception:
        pass

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

    # Seed: contadores por editor
    now = datetime.now().isoformat(timespec='seconds')
    conn.execute("""
        INSERT OR IGNORE INTO editor_progress (editor, label, current, total, updated_at)
        VALUES ('Benja', 'Básicos', 0, 60, ?)
    """, (now,))
    conn.execute("""
        INSERT OR IGNORE INTO editor_progress (editor, label, current, total, updated_at)
        VALUES ('Benja', 'Avanzados', 0, 30, ?)
    """, (now,))
    # Valen: Intermedios y Avanzados, ambos /10 (pedido Ignacio 26/may)
    conn.execute("""
        INSERT OR IGNORE INTO editor_progress (editor, label, current, total, updated_at)
        VALUES ('Valen', 'Intermedios', 0, 10, ?)
    """, (now,))
    conn.execute("""
        INSERT OR IGNORE INTO editor_progress (editor, label, current, total, updated_at)
        VALUES ('Valen', 'Avanzados', 0, 10, ?)
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
                   size: Optional[int], created_time: Optional[str], is_baseline: bool = False,
                   subfolder_name: Optional[str] = None):
    conn = get_conn()
    conn.execute("""
        INSERT OR IGNORE INTO known_files
        (file_id, cliente, folder_id, name, size, created_time, first_seen_at, is_baseline, subfolder_name)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (file_id, cliente, folder_id, name, size, created_time, now_iso(), 1 if is_baseline else 0,
          subfolder_name))
    conn.commit()
    conn.close()


def claim_file(file_id: str, cliente: str, folder_id: str, name: str,
               size: Optional[int], created_time: Optional[str], is_baseline: bool = False,
               subfolder_name: Optional[str] = None) -> bool:
    """Versión atómica: INSERT y retorna True si efectivamente insertó (primero en verlo),
    False si ya existía (otro proceso/workflow lo claimó). Sirve como lock anti-race condition.

    `subfolder_name`: nombre de la subfolder dentro de /Material/ donde estaba el
    archivo al detectarlo. '' = root de Material. NULL = desconocido (caller no
    pudo determinarlo). Sirve para auto-inferir editor por subfolder y para
    alertar al admin de subfolders "tipo" no-mapeadas."""
    conn = get_conn()
    cur = conn.execute("""
        INSERT OR IGNORE INTO known_files
        (file_id, cliente, folder_id, name, size, created_time, first_seen_at, is_baseline, subfolder_name)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (file_id, cliente, folder_id, name, size, created_time, now_iso(), 1 if is_baseline else 0,
          subfolder_name))
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
    """Crea task NUEVA SOLO si NO existe una task pending para (cliente, editor).
    Si ya existe → retorna el id de la existente (transparente para el caller).

    Antes era un INSERT directo. El cluster de scans con bot pushes paralelos
    pudo crear duplicados (caso Egdylu/Fran que tenía #267 + #273 ambas pending
    para el mismo par). Ahora la creación es idempotente — múltiples scans
    procesando el mismo cliente nunca van a crear task duplicada.

    Nota: el count_locked y pending_count NO se modifican si ya existía. Esa
    sigue siendo decisión del admin. Solo evitamos duplicar la row."""
    conn = get_conn()
    # Atómico: chequear y crear en una sola transacción
    existing = conn.execute(
        "SELECT id FROM tasks WHERE TRIM(cliente)=TRIM(?) "
        "AND COALESCE(editor,'')=COALESCE(?,'') AND status='pending' LIMIT 1",
        (cliente, editor)
    ).fetchone()
    if existing:
        conn.close()
        return existing[0]
    cur = conn.execute("""
        INSERT INTO tasks (cliente, editor, file_id, file_name, detected_at)
        VALUES (?, ?, ?, ?, ?)
    """, (cliente, editor, file_id, file_name, now_iso()))
    task_id = cur.lastrowid
    conn.commit()
    conn.close()
    return task_id


def mark_pending_task_for_renotification(cliente: str, editor: Optional[str],
                                          latest_file_id: str, latest_file_name: str,
                                          min_silence_hours: float = 6.0) -> Optional[int]:
    """Hay una task pending para (cliente, editor) y entró material nuevo.

    COMPORTAMIENTO (cambiado 01/jun por pedido de Ignacio):
      - Si la task pending YA fue notificada (mail_sent_at no es NULL) →
        NO re-notificar. El editor ya sabe que tiene material de ese cliente;
        que siga subiendo tandas durante el día NO debe generar más mails.
        Solo actualizamos file_id/file_name para que el dashboard muestre lo
        último. Retorna None (el notifier no la procesa).
      - Si la task pending NUNCA se notificó (mail_sent_at IS NULL) → la
        dejamos lista para que el notifier mande EL primer (y único) mail.

    Resultado: UN solo mail "material nuevo de X" por sesión de trabajo.
    Cuando el editor entrega (task se cierra) y el cliente sube material
    nuevo, se crea una task nueva → recién ahí otro mail.

    Bug 01/jun: Jennifer Díaz / Ismafeten mandaban mail toda vez que subían
    otra tanda (cada 6h). Ahora 1 mail y listo hasta que se cierre la task."""
    if not cliente:
        return None
    conn = get_conn()
    if editor:
        row = conn.execute(
            "SELECT id, mail_sent_at FROM tasks WHERE TRIM(cliente)=TRIM(?) "
            "AND COALESCE(editor,'')=COALESCE(?,'') AND status='pending' "
            "ORDER BY id DESC LIMIT 1",
            (cliente, editor)
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT id, mail_sent_at FROM tasks WHERE TRIM(cliente)=TRIM(?) "
            "AND status='pending' ORDER BY id DESC LIMIT 1",
            (cliente,)
        ).fetchone()
    if not row:
        conn.close()
        return None

    already_notified = bool(row["mail_sent_at"])
    if already_notified:
        # Ya se mandó el mail de esta task pending. NO re-notificar.
        # Solo actualizar el último archivo (para el dashboard), sin tocar
        # mail_sent_at → el notifier NO la va a re-procesar.
        conn.execute(
            "UPDATE tasks SET file_id=?, file_name=? WHERE id=?",
            (latest_file_id, latest_file_name, row["id"])
        )
        conn.commit()
        conn.close()
        return None

    # Nunca se notificó (mail_sent_at IS NULL): dejar lista para el PRIMER mail.
    conn.execute(
        "UPDATE tasks SET file_id=?, file_name=? WHERE id=?",
        (latest_file_id, latest_file_name, row["id"])
    )
    conn.commit()
    task_id = row["id"]
    conn.close()
    return task_id


def find_duplicate_pending_tasks() -> list[dict]:
    """Detecta tasks pending duplicadas: mismo (cliente, editor) con >1 row
    en status='pending'. Sirve para cleanup masivo."""
    conn = get_conn()
    rows = conn.execute("""
        SELECT TRIM(cliente) as cliente, COALESCE(editor,'') as editor, COUNT(*) as n,
               GROUP_CONCAT(id) as ids, SUM(COALESCE(pending_count, 1)) as total_count
        FROM tasks WHERE status='pending'
        GROUP BY TRIM(cliente), COALESCE(editor,'')
        HAVING n > 1
        ORDER BY n DESC
    """).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def consolidate_duplicate_tasks() -> int:
    """Para cada (cliente, editor) con >1 pending: mantiene la más vieja
    (ID más chico) con count_locked=1 y pending_count = max de los counts
    individuales (no suma — el count es por entregables, no por archivos).
    Borra las demás. Retorna cuántas tasks borró."""
    dupes = find_duplicate_pending_tasks()
    if not dupes:
        return 0
    conn = get_conn()
    n_deleted = 0
    for d in dupes:
        ids = sorted(int(x) for x in (d["ids"] or "").split(",") if x)
        if len(ids) < 2:
            continue
        keep_id = ids[0]  # la más vieja
        delete_ids = ids[1:]
        # Tomar el MAX pending_count de las duplicadas (no suma; cada row
        # representaba 'el mismo trabajo' no trabajo extra)
        max_pc = conn.execute(
            f"SELECT MAX(COALESCE(pending_count,1)) FROM tasks WHERE id IN ({','.join('?'*len(ids))})",
            ids
        ).fetchone()[0] or 1
        # Tomar urgent=1 si alguna lo tenía
        any_urgent = conn.execute(
            f"SELECT MAX(COALESCE(urgent,0)) FROM tasks WHERE id IN ({','.join('?'*len(ids))})",
            ids
        ).fetchone()[0] or 0
        # Tomar la note no vacía si la hay
        note_row = conn.execute(
            f"SELECT note FROM tasks WHERE id IN ({','.join('?'*len(ids))}) AND note IS NOT NULL AND TRIM(note) != '' LIMIT 1",
            ids
        ).fetchone()
        note_val = note_row[0] if note_row else None
        conn.execute(
            "UPDATE tasks SET pending_count=?, count_locked=1, urgent=?, note=COALESCE(note, ?) WHERE id=?",
            (max_pc, any_urgent, note_val, keep_id)
        )
        n_deleted += conn.execute(
            f"DELETE FROM tasks WHERE id IN ({','.join('?'*len(delete_ids))})",
            delete_ids
        ).rowcount
    conn.commit()
    conn.close()
    return n_deleted


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


# ─── HELPERS: subfolders "de tipo" + inferencia + alertas ──────────────────────

# Patrones que indican que la subfolder es un TIPO DE CONTENIDO (probable editor
# distinto), NO un agrupador por proyecto/tanda/fecha. La key del dict es el
# "tipo canónico" para mostrar en la alerta.
#
# Reglas:
#   - Match SUBSTRING case-insensitive del nombre _normalizado_.
#   - Si la subfolder es "Youtube tanda 5" → tipo=youtube (substring "youtube").
#   - Si es "Tanda 5" / "Pack 1" / "Mayo 2026" / "Polara 3" → NO tipo (None).
_SUBFOLDER_TYPE_PATTERNS = {
    "youtube":  ["youtube", "yt ", " yt", "long form", "long-form", "podcast", "vsl", "tutorial"],
    "reels":    ["reels", "reel ", " reel"],
    "shorts":   ["shorts", "short ", " short"],
    "tiktok":   ["tiktok", " tt", "tt "],
    "twitch":   ["twitch", "stream", "vod"],
    "ads":      ["anuncio", "ads ", " ads", "creativo", "publicidad"],
}


def _classify_subfolder_type(subfolder_name: Optional[str]) -> Optional[str]:
    """Si el nombre de la subfolder indica un TIPO de contenido (distinto editor
    probable), retorna el tipo canónico ('youtube', 'reels', 'shorts',
    'tiktok', etc). Si parece un agrupador por proyecto/tanda → retorna None.

    Ejemplos:
      'Youtube'           → 'youtube'
      'YT enero 24'       → 'youtube'
      'Reels'             → 'reels'
      'Shorts'            → 'shorts'
      'TikTok'            → 'tiktok'
      'Tanda 5'           → None
      'Pack 1'            → None
      'Ad Polara'         → None
      'Polara 3'          → None
      'Mayo 2026'         → None"""
    if not subfolder_name:
        return None
    import unicodedata
    s = unicodedata.normalize("NFD", subfolder_name)
    s = "".join(c for c in s if unicodedata.category(c) != "Mn")
    n = " " + " ".join(s.lower().split()) + " "  # padding para matches con espacios
    for tipo, patterns in _SUBFOLDER_TYPE_PATTERNS.items():
        for p in patterns:
            if p in n:
                return tipo
    return None


def infer_subfolder_editor_from_history(cliente: str, subfolder: str,
                                          min_consistent_deliveries: int = 2) -> Optional[str]:
    """Mira el histórico de entregas para este (cliente, subfolder) e infiere
    el editor responsable si hay consenso.

    Lógica:
      - Buscar tasks DONE cuyos crudos originales estaban en esta subfolder
        (matcheo por known_files.subfolder_name + tasks.file_id).
      - Si >= `min_consistent_deliveries` tasks fueron entregadas por el MISMO
        editor, retornar ese editor.
      - Si varios editores entregaron mezclados (sin consenso), retornar None.

    Notas:
      - Match de subfolder es por igualdad exacta normalizada (no substring;
        para no contaminar con subfolders nombradas parecido).
      - Si no hay datos, retorna None.
    """
    if not cliente or not subfolder:
        return None
    import unicodedata
    def _norm(s):
        s = unicodedata.normalize("NFD", s or "")
        s = "".join(c for c in s if unicodedata.category(c) != "Mn")
        return " ".join(s.lower().split())
    target = _norm(subfolder)
    conn = get_conn()
    rows = conn.execute("""
        SELECT t.editor
        FROM tasks t
        JOIN known_files kf ON kf.file_id = t.file_id
        WHERE TRIM(t.cliente) = TRIM(?)
          AND t.status = 'done'
          AND kf.subfolder_name IS NOT NULL
          AND TRIM(kf.subfolder_name) <> ''
    """, (cliente,)).fetchall()
    conn.close()
    if not rows:
        return None
    # Filtrar por subfolder normalizada
    by_editor = {}
    for r in rows:
        # No tenemos el subfolder en el row, lo refiltramos abajo. Mejor query:
        pass
    # Refacto: query con subfolder en el resultado para filtrar acá
    conn = get_conn()
    rows = conn.execute("""
        SELECT t.editor AS editor, kf.subfolder_name AS sf
        FROM tasks t
        JOIN known_files kf ON kf.file_id = t.file_id
        WHERE TRIM(t.cliente) = TRIM(?)
          AND t.status = 'done'
          AND t.editor IS NOT NULL
          AND kf.subfolder_name IS NOT NULL
    """, (cliente,)).fetchall()
    conn.close()
    counts = {}
    for r in rows:
        if _norm(r["sf"] or "") == target:
            counts[r["editor"]] = counts.get(r["editor"], 0) + 1
    if not counts:
        return None
    # Consenso: un editor solo, con >= min entregas
    if len(counts) == 1:
        editor, n = list(counts.items())[0]
        if n >= min_consistent_deliveries:
            return editor
    return None


def register_subfolder_alert(cliente: str, subfolder: str, inferred_type: Optional[str],
                              example_file: Optional[str], example_file_id: Optional[str],
                              default_editor: Optional[str]) -> bool:
    """Registra que detectamos una subfolder "tipo" no-mapeada. Retorna True si
    es la PRIMERA vez (caller debe mandar el mail), False si ya estaba alertada."""
    if not cliente or not subfolder:
        return False
    conn = get_conn()
    try:
        cur = conn.execute("""
            INSERT INTO subfolder_alerts
                (cliente, subfolder, alerted_at, inferred_type, example_file,
                 example_file_id, default_editor_assigned)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (cliente, subfolder, now_iso(), inferred_type, example_file,
              example_file_id, default_editor))
        conn.commit()
        return cur.rowcount > 0
    except Exception:
        # Ya existía (PK conflict)
        return False
    finally:
        conn.close()


def upsert_subfolder_editor(cliente: str, subfolder: str, editor: str) -> None:
    """Crea/actualiza una entrada en cfg_subfolder_editors. Útil para inferencia
    automática y para una API admin futura."""
    conn = get_conn()
    conn.execute("""
        INSERT INTO cfg_subfolder_editors (cliente, subfolder, editor, created_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(cliente, subfolder) DO UPDATE SET editor=excluded.editor
    """, (cliente, subfolder or "", editor, now_iso()))
    conn.commit()
    conn.close()


# ─── Asignación editor por cliente (override del Sheet) ──────────────────────

def cfg_set_client_editor(cliente: str, editor: Optional[str]) -> None:
    """Asignar editor a cliente manualmente desde el dashboard.
    Si editor es None/'', borra el override (vuelve a usar el Sheet)."""
    if not cliente:
        return
    conn = get_conn()
    if not editor:
        conn.execute("DELETE FROM cfg_client_editor WHERE TRIM(cliente)=TRIM(?)", (cliente,))
    else:
        conn.execute("""
            INSERT INTO cfg_client_editor (cliente, editor, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(cliente) DO UPDATE SET editor=excluded.editor, updated_at=excluded.updated_at
        """, (cliente, editor, now_iso()))
    conn.commit()
    conn.close()


def cfg_get_client_editor(cliente: str) -> Optional[str]:
    """Devuelve el editor asignado manualmente a este cliente, o None si
    no hay override (caller cae al Sheet con get_editor_for_client).

    Match: 1) exacto, 2) fallback accent/case-insensitive (resuelve casos
    como 'Cristian Garzón' override aplicado a 'Cristian Garzon' sin tilde).
    """
    if not cliente:
        return None
    conn = get_conn()
    # Match exacto rápido
    row = conn.execute(
        "SELECT editor FROM cfg_client_editor WHERE TRIM(cliente)=TRIM(?)",
        (cliente,)
    ).fetchone()
    if row:
        conn.close()
        return row["editor"]
    # Fallback accent/case-insensitive
    target = _normalize_client_name(cliente)
    if not target:
        conn.close()
        return None
    all_rows = conn.execute("SELECT cliente, editor FROM cfg_client_editor").fetchall()
    conn.close()
    for r in all_rows:
        if _normalize_client_name(r["cliente"]) == target:
            return r["editor"]
    return None


def cfg_list_client_editors() -> dict:
    """Devuelve {cliente: editor} de todos los overrides manuales."""
    conn = get_conn()
    rows = conn.execute("SELECT cliente, editor FROM cfg_client_editor").fetchall()
    conn.close()
    return {r["cliente"]: r["editor"] for r in rows}


def resolve_editor_for_cliente(cliente: str) -> Optional[str]:
    """Editor asignado de un cliente, para cuando un dato (ej. una review del
    portal) llega sin editor. Prioridad: cfg_client_editor (override manual del
    dashboard) > cfg_excel_clients (Sheet) > último completion del mail_log.
    Match normalizado acentos/case. Bug 11/jun: el mail de 'revisión pedida'
    solo le llegaba al admin porque review.editor venía vacío."""
    if not cliente:
        return None
    target = _normalize_client_name(cliente)
    conn = get_conn()
    result = None
    # menor prioridad primero, las siguientes pisan
    try:
        for r in conn.execute(
            "SELECT cliente, editor, MAX(sent_at) FROM mail_log "
            "WHERE kind='completion' AND COALESCE(editor,'') NOT IN ('', '—') "
            "GROUP BY cliente"):
            if _normalize_client_name(r["cliente"]) == target:
                result = r["editor"]
    except Exception:
        pass
    try:
        for r in conn.execute("SELECT cliente, editor FROM cfg_excel_clients"):
            if (r["editor"] or "").strip() and _normalize_client_name(r["cliente"]) == target:
                result = r["editor"]
    except Exception:
        pass
    try:
        for r in conn.execute("SELECT cliente, editor FROM cfg_client_editor"):
            if (r["editor"] or "").strip() and _normalize_client_name(r["cliente"]) == target:
                result = r["editor"]
    except Exception:
        pass
    conn.close()
    return result


def resolve_editor_rules(cliente: str, subfolder_name: Optional[str] = None,
                         file_name: Optional[str] = None,
                         duration_millis=None) -> Optional[str]:
    """Resuelve el editor de un CRUDO por reglas configuradas del cliente.

    Orden de prioridad (pedido Ignacio 10-11/jun, casos Egdylu y Roger Marti):
      1. Substring EXPLÍCITO de cfg_subfolder_editors contra el nombre de la
         subcarpeta, y si no matchea, contra el nombre del archivo.
         (Egdylu/'reel'→Fran; Roger/'yt'→Jere)
      2. DURACIÓN: cfg_duration_editors(cliente, min_minutes, editor) — si el
         video dura >= min_minutes → ese editor. (Roger: >5 min → Jere.)
         Nota: Drive tarda unos minutos en calcular videoMediaMetadata para
         uploads frescos; si aún no está, esta regla no aplica (best effort).
      3. DEFAULT del cliente: fila con subfolder='' (Roger: ''→Valen, pisa
         al Sheet para todo lo que no matchee arriba).
      4. None → el caller cae al Sheet.
    """
    import unicodedata
    def _n(s):
        s = unicodedata.normalize("NFD", s or "")
        s = "".join(ch for ch in s if unicodedata.category(ch) != "Mn")
        return " ".join(s.lower().split())

    conn = get_conn()
    rows = conn.execute(
        "SELECT subfolder, editor FROM cfg_subfolder_editors WHERE TRIM(cliente)=TRIM(?)",
        (cliente,)).fetchall()
    try:
        drows = conn.execute(
            "SELECT min_minutes, editor FROM cfg_duration_editors WHERE TRIM(cliente)=TRIM(?)",
            (cliente,)).fetchall()
    except Exception:
        drows = []
    conn.close()
    if not rows and not drows:
        return None

    # 1) substring explícito (subcarpeta primero, después nombre de archivo)
    for target in (subfolder_name, file_name):
        t = _n(target) if target else ""
        if not t:
            continue
        for r in rows:
            cfg = _n(r["subfolder"] or "")
            if cfg and cfg in t:
                return r["editor"]

    # 2) duración (la regla con mayor umbral que aplique gana)
    if duration_millis:
        try:
            mins = float(duration_millis) / 60000.0
            best = None
            for d in drows:
                if mins >= float(d["min_minutes"]):
                    if best is None or float(d["min_minutes"]) > best[0]:
                        best = (float(d["min_minutes"]), d["editor"])
            if best:
                return best[1]
        except (TypeError, ValueError):
            pass

    # 3) default del cliente (subfolder='')
    for r in rows:
        if not (r["subfolder"] or "").strip():
            return r["editor"]
    return None


def get_editor_for_subfolder(cliente: str, subfolder_name: Optional[str]) -> Optional[str]:
    """Resuelve editor según subfolder dentro de /Material/.

    Lookup order:
      1. Si `subfolder_name` es dado, busca match (substring + case insensitive)
         contra cfg_subfolder_editors filtrado por cliente. Primer match gana.
      2. Si no hay match O subfolder_name es None/'', busca default
         (subfolder='' o NULL) para ese cliente.
      3. Si no hay nada, retorna None (caller cae al Sheet)."""
    import unicodedata
    def _norm(s):
        s = unicodedata.normalize("NFD", s or "")
        s = "".join(c for c in s if unicodedata.category(c) != "Mn")
        return " ".join(s.lower().split())

    conn = get_conn()
    rows = conn.execute(
        "SELECT subfolder, editor FROM cfg_subfolder_editors WHERE TRIM(cliente)=TRIM(?)",
        (cliente,)
    ).fetchall()
    conn.close()
    if not rows:
        return None

    sub_norm = _norm(subfolder_name) if subfolder_name else ""
    # 1) Match por subfolder no-vacío (substring)
    if sub_norm:
        for r in rows:
            cfg_sub = _norm(r["subfolder"] or "")
            if cfg_sub and cfg_sub in sub_norm:
                return r["editor"]
    # 2) Default (subfolder vacío)
    for r in rows:
        if not (r["subfolder"] or "").strip():
            return r["editor"]
    return None


def has_manual_pending_for_client(cliente: str, editor: Optional[str] = None) -> bool:
    """¿El cliente tiene alguna task pending con count_locked=1 (decisión manual del admin)?
    Si sí, el scan NO debe crear duplicados para el MISMO editor según el Sheet.

    Si `editor` es None: chequea CUALQUIER editor (comportamiento legacy).
    Si `editor` es dado: solo chequea pending manual para ESE editor específico.

    Caso real (Roger Marti): tiene 2 editores — Fran para YouTube, Valen para reels.
    Si existe task manual pending Roger/Fran (Youtube), un reel nuevo en /Material/
    debe poder crear task Roger/Valen. Antes esto se bloqueaba porque el chequeo
    era cliente-only sin filtrar por editor."""
    conn = get_conn()
    if editor:
        row = conn.execute(
            "SELECT 1 FROM tasks WHERE TRIM(cliente)=TRIM(?) AND TRIM(COALESCE(editor,''))=TRIM(?) "
            "AND status='pending' AND COALESCE(count_locked, 0) = 1 LIMIT 1",
            (cliente, editor or "")
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT 1 FROM tasks WHERE TRIM(cliente)=TRIM(?) AND status='pending' AND COALESCE(count_locked, 0) = 1 LIMIT 1",
            (cliente,)
        ).fetchone()
    conn.close()
    return row is not None


def enqueue_completion_mail(task_id: Optional[int], cliente: str, editor: Optional[str],
                            file_id: Optional[str], file_name: Optional[str],
                            edited_folder_id: Optional[str], client_folder_id: Optional[str],
                            new_count: int, closed: bool,
                            is_correction: bool = False) -> int:
    """Encola un mail de cierre/decremento/corrección para envío.
    Si is_correction=True, el mail va a ser diferente (subject 'Corrección') y
    NO se asocia decremento del pending_count (eso lo maneja el caller)."""
    conn = get_conn()
    cur = conn.execute("""
        INSERT INTO pending_completion_mails
        (task_id, cliente, editor, file_id, file_name, edited_folder_id, client_folder_id,
         new_count, closed, created_at, mail_sent_at, retry_count, is_correction)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, 0, ?)
    """, (task_id, cliente, editor, file_id, file_name, edited_folder_id, client_folder_id,
          new_count, 1 if closed else 0, now_iso(), 1 if is_correction else 0))
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
             error: Optional[str] = None, dedupe_key: Optional[str] = None):
    """Registra un mail enviado (o intentado) en mail_log para auditoría."""
    try:
        conn = get_conn()
        conn.execute("""
            INSERT INTO mail_log (sent_at, to_email, subject, kind, cliente, editor, msg_id, success, error, dedupe_key)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (now_iso(), to_email, subject, kind, cliente, editor, msg_id, 1 if success else 0, error, dedupe_key))
        conn.commit()
        conn.close()
    except Exception:
        pass


def completion_mail_already_sent(cliente: str, file_id: str, file_name: str,
                                   minutes: int = 360) -> bool:
    """¿Ya se mandó un completion mail (admin) para este archivo en los últimos N min?

    Usa el MISMO dedupe_key estable que usa notifier.send_completion_mails para
    encolar completion mails. Si retorna True, el archivo YA FUE PROCESADO por
    otro worker (incremental o audit) y NO debe re-procesarse: ni re-claim, ni
    re-decrement, ni re-mail.

    Bug 21/may: el audit re-procesaba archivos que el incremental ya había
    completado pero cuyo tracker.db nunca se pusheó (conflicto), generando
    mail duplicado + double-decrement de pending_count.
    """
    if not cliente or not file_id:
        return False
    import hashlib
    key_str = f"completion-admin|{cliente.strip().lower()}|{file_id or file_name}"
    dkey = hashlib.sha1(key_str.encode()).hexdigest()[:24]
    return bool(check_recent_mail_by_key(dkey, minutes=minutes))


def check_recent_mail_by_key(dedupe_key: str, minutes: int = 30) -> Optional[str]:
    """Busca en mail_log si hay un envío success=1 con el dedupe_key dado
    en los últimos N minutos. Retorna el msg_id (o '(no-id)' si el envío
    fue success pero sin msg_id guardado) si existe; None si no encontró nada.

    El caller usa truthiness: si retorna cualquier cosa truthy → ya se mandó
    → skip. NO devolver None si el row existe (eso permitiría duplicados)."""
    if not dedupe_key:
        return None
    try:
        from datetime import datetime, timedelta
        cutoff = (datetime.now() - timedelta(minutes=minutes)).isoformat(timespec="seconds")
        conn = get_conn()
        row = conn.execute(
            "SELECT msg_id FROM mail_log "
            "WHERE dedupe_key=? AND success=1 AND sent_at >= ? "
            "ORDER BY sent_at DESC LIMIT 1",
            (dedupe_key, cutoff)
        ).fetchone()
        conn.close()
        if not row:
            return None
        return row[0] or "(no-id)"  # truthy aunque msg_id sea NULL
    except Exception:
        return None


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
    """Devuelve {editor_name: email} de editores que SÍ deben recibir mails.
    Excluye editores en modo vacaciones."""
    conn = get_conn()
    rows = conn.execute("""
        SELECT name, email FROM cfg_editors
        WHERE active=1 AND receives_notifications=1
          AND COALESCE(on_vacation, 0) = 0
          AND email IS NOT NULL AND email != ''
    """).fetchall()
    conn.close()
    return {r["name"]: r["email"] for r in rows}


def _video_key(name: str) -> Optional[str]:
    """Extrae la 'key' canónica de un video editado para detectar correcciones.
    Ej:
      'Video 1.mp4'            → 'video 1'
      'Video 1 corrección.mp4' → 'video 1'
      'Video 1 v2.mp4'         → 'video 1'
      'Video 1 final.mp4'      → 'video 1'
      'Reel 5.mov'             → 'reel 5'
      '46. AACC1.mp4'          → 'num 46'   (formato Jose Social Pulse, Aroa)
      '9. boton.mp4'           → 'num 9'
      '16 - OCTAVIAN.mp4'      → 'num 16'
      'IMG_4123.MOV'           → None
    """
    if not name:
        return None
    import re
    norm = name.lower().strip()
    # quitar extensión
    norm = re.sub(r'\.[a-z0-9]+$', '', norm)
    # buscar 'video N' / 'reel N' / 'tanda N'
    m = re.search(r'\b(video|reel|tanda)\s*(\d+)', norm)
    if m:
        return f"{m.group(1)} {m.group(2)}"
    # Formato '^N. X' / '^N - X' / '^N _ X' (común en Jose, Aroa, Roger, etc.)
    # Bug 27/may: '46. AACC1.mp4' y '46.AACC1.mp4' no se detectaban como video
    # numerado → corrections se mandaban como entregas nuevas → mails duplicados.
    m = re.match(r'^(\d+)\s*[\.\-–_:]\s*\w', norm)
    if m:
        return f"num {m.group(1)}"
    return None


def _correction_stem(name: str) -> str:
    """Devuelve el 'stem' de un nombre para detectar corrección. Quita
    extensión y sufijos comunes de re-entrega: v2, v3, final, corregido,
    corrección, fix, edit, nuevo, etc.

    Si dos archivos tienen el MISMO stem → corrección. Si solo tienen
    misma key pero stems distintos → archivos diferentes (mismo número
    de video pero contenido distinto, típico en clientes con muchas
    tandas como Ely Fitness).

    Ej:
      'Video 10.mp4'          → 'video 10'
      'Video 10 v2.mp4'       → 'video 10'
      'Video 10 corregido.mp4'→ 'video 10'
      'Video 10 JALON.mp4'    → 'video 10 jalon'  (≠ 'video 10' → NO es corrección)
    """
    if not name:
        return ""
    import re
    s = name.lower().strip()
    s = re.sub(r'\.[a-z0-9]+$', '', s)  # quitar extensión
    # quitar sufijos comunes de corrección/re-entrega
    suffix_patterns = [
        r'\s*[-_]?\s*v\d+\s*$',
        # 'ver 3' / 'version 2' / 'versión 2' / 'v. 3' — caso Ismafeten 10/jun:
        # 'Video 6 ver 3.mp4' era corrección de 'Video 6.mp4' pero el sufijo
        # 'ver N' no se reconocía → se mandó como entrega nueva.
        r'\s*[-_]?\s*(ver|versi[oó]n|v\.)\s*\d+\s*$',
        r'\s*[-_]?\s*(final|corregido|corregida|correcci[oó]n|corr|fix|nuevo|nueva|edit|editado|editada)\s*$',
        r'\s*\(\d+\)\s*$',  # "video 10 (2)"
    ]
    for p in suffix_patterns:
        s = re.sub(p, '', s).strip()
    # Normalizar separadores (./-_) entre dígitos y texto: "46.aacc1" y
    # "46. aacc1" deben dar el mismo stem. Bug 27/may Aroa.
    s = re.sub(r'(\d)\s*[\.\-_:]\s*', r'\1 ', s)
    return " ".join(s.split())  # colapsar espacios


def is_correction_for_client(cliente: str, file_name: str, current_file_id: Optional[str] = None,
                              max_age_days: int = 21) -> bool:
    """¿Este archivo es CORRECCIÓN de un editado previo del MISMO cliente?

    Flujo real: video entregado → el cliente pide cambios → el editor lo BORRA
    de Drive y resube con el MISMO nombre, POCOS DÍAS después.

    Señales (en orden):
      0. Hay una REVISIÓN PEDIDA abierta para este cliente cuyo video matchea
         por nombre/stem → el cliente lo pidió explícitamente → ES corrección
         (aunque la entrega original sea vieja).
      A. Mismo NOMBRE EXACTO que un editado previo subido HACE POCO
         (created_time dentro de max_age_days). Cubre el borrar-y-resubir.
      B. Misma key+stem (Video N, N. texto) de un editado previo reciente, con
         tolerancia a sufijos (v2, ver 3, corregido...). 'Video 10 JALON' !=
         'Video 10' (caso Ely: mismo número, contenido distinto = NO corrección).

    FIX 17/jun (caso Benja/Video 28/Rafa Rojas): se usa **created_time** (fecha
    REAL de subida a Drive), NO first_seen_at (fecha de DETECCIÓN). Antes, un
    'Video 28' VIEJO (subido en marzo, detectado tarde en mayo como baseline)
    parecía reciente por su first_seen, y un 'Video 28' NUEVO de junio (otra
    tanda, número reusado) se marcaba como corrección. Con created_time, el
    viejo de marzo queda FUERA de ventana → el nuevo se trata como entrega
    nueva. Ventana corta (21d) porque una corrección llega poco después de la
    entrega; un mismo número que reaparece semanas/meses después es una tanda
    nueva, NO una corrección. Se quitó la red por mail_log (daba falsos
    positivos por no tener created_time).
    """
    if not file_name:
        return False
    import re
    from datetime import datetime, timedelta

    def _norm_name(n):
        s = (n or "").lower().strip()
        s = re.sub(r"\.[a-z0-9]+$", "", s)
        return " ".join(s.split())

    target_name = _norm_name(file_name)
    key = _video_key(file_name)
    stem = _correction_stem(file_name)
    now = datetime.now()
    conn = get_conn()

    # ── SEÑAL 0: revisión pedida abierta que matchea (el cliente lo pidió) ──
    try:
        for rv in conn.execute(
            "SELECT video_file_name FROM client_reviews "
            "WHERE TRIM(cliente)=TRIM(?) AND status='revision_requested'", (cliente,)
        ).fetchall():
            rn = _norm_name(rv["video_file_name"])
            if not rn:
                continue
            if rn == target_name:
                conn.close(); return True
            if key and stem and _video_key(rv["video_file_name"]) == key \
                    and _correction_stem(rv["video_file_name"]) == stem:
                conn.close(); return True
    except Exception:
        pass

    # ── Editados previos subidos RECIENTEMENTE (por created_time real) ──
    cutoff = (now - timedelta(days=max_age_days)).isoformat(timespec="seconds")
    rows = conn.execute(
        "SELECT file_id, name FROM known_edited_files "
        "WHERE TRIM(cliente)=TRIM(?) AND COALESCE(created_time, first_seen_at) >= ?",
        (cliente, cutoff)
    ).fetchall()
    conn.close()

    for r in rows:
        if current_file_id and r["file_id"] == current_file_id:
            continue
        # A: nombre exacto normalizado
        if target_name and _norm_name(r["name"]) == target_name:
            return True
        # B: misma key + mismo stem (corrección con sufijo)
        if key and stem and _video_key(r["name"]) == key and _correction_stem(r["name"]) == stem:
            return True
    return False


def cfg_is_on_vacation(editor: str) -> bool:
    """¿El editor está en modo vacaciones?"""
    if not editor:
        return False
    conn = get_conn()
    row = conn.execute("SELECT on_vacation FROM cfg_editors WHERE name=?", (editor,)).fetchone()
    conn.close()
    return bool(row and row["on_vacation"])


def cfg_get_daily_summary_editors() -> set:
    conn = get_conn()
    rows = conn.execute(
        "SELECT name FROM cfg_editors WHERE active=1 AND receives_daily_summary=1"
    ).fetchall()
    conn.close()
    return {r["name"] for r in rows}


def canonical_editor(name, editors):
    """Mapea un nombre/apodo de editor a su forma CANÓNICA de cfg_editors.
    `editors`: lista de nombres canónicos (cfg_editors active). El Sheet a veces
    usa apodos ('Adri') que no matchean el nombre del dashboard ('Adrian') →
    una review/task con 'Adri' no le aparecía al editor 'Adrian'. Match: exacto
    normalizado > prefijo ÚNICO (apodo↔nombre largo). Bug Luis/Adri 17/jun."""
    if not name:
        return name
    n = (name or "").strip().lower()
    for e in editors:
        if (e or "").strip().lower() == n:
            return e
    cands = [e for e in editors
             if (e or "").strip().lower().startswith(n) or n.startswith((e or "").strip().lower())]
    if len(cands) == 1:
        return cands[0]
    return name


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


# ─── CLIENTES (mails + notif on/off) ────────────────────────────────────────

def cfg_list_clients() -> list[dict]:
    """Lista todos los clientes configurados con mail."""
    conn = get_conn()
    rows = conn.execute("""
        SELECT cliente, email, display_name, notifications_enabled, created_at, updated_at
        FROM cfg_clients
        ORDER BY cliente
    """).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def _normalize_client_name(s: str) -> str:
    """Lowercase + sin acentos + collapsed whitespace. Para matching tolerante
    entre 'David Hernandez' (scan sin acento) y 'David Hernández' (DB con
    acento). Bug 21/may: a David no le llegaba mail por mismatch de tilde."""
    if not s:
        return ""
    import unicodedata
    n = unicodedata.normalize("NFD", s)
    n = "".join(c for c in n if unicodedata.category(c) != "Mn")
    return " ".join(n.lower().split())


def cfg_get_client(cliente: str) -> Optional[dict]:
    """Devuelve la config del cliente o None si no está configurado.

    Match (orden de prioridad):
      1. Exacto TRIM=TRIM (rápido)
      2. Normalizado: lower + sin acentos (David Hernandez ↔ David Hernández)
      3. Token subset: cfg_client tokens (≥2) ⊆ lookup tokens
         (Cristina García ⊆ Cristina Garcia Munoz)
    """
    if not cliente:
        return None
    conn = get_conn()
    # 1) Match exacto (rápido, common case)
    row = conn.execute("""
        SELECT cliente, email, display_name, notifications_enabled, created_at, updated_at
        FROM cfg_clients WHERE TRIM(cliente)=TRIM(?)
    """, (cliente,)).fetchone()
    if row:
        conn.close()
        return dict(row)
    # 2 + 3) Fallback: lower + sin acentos + token subset
    target = _normalize_client_name(cliente)
    if not target:
        conn.close()
        return None
    all_rows = conn.execute("""
        SELECT cliente, email, display_name, notifications_enabled, created_at, updated_at
        FROM cfg_clients
    """).fetchall()
    conn.close()
    # Match exacto normalizado
    for r in all_rows:
        if _normalize_client_name(r["cliente"]) == target:
            return dict(r)
    # Token subset: cfg_client tokens (>=2 significativos) están todos en lookup.
    # Bug 29/may: Jere subió reel de "Cristina Garcia Munoz" pero cfg_clients
    # tenía "Cristina García" — mail al cliente no se mandó porque match falló.
    STOP = {"de","del","la","el","los","las","y","e","o","u","a","con","sin","para","por"}
    def _tokens(s):
        return {t for t in _normalize_client_name(s).split() if len(t) >= 3 and t not in STOP}
    target_tokens = _tokens(cliente)
    if not target_tokens:
        return None
    best = None
    best_overlap = 0
    for r in all_rows:
        cfg_tokens = _tokens(r["cliente"])
        if len(cfg_tokens) >= 2 and cfg_tokens.issubset(target_tokens):
            if len(cfg_tokens) > best_overlap:
                best_overlap = len(cfg_tokens)
                best = r
    if best:
        return dict(best)
    # 4) Token subset INVERSO: el nombre buscado (carpeta, corto) está contenido
    # en la config (larga). Bug Gaetan 10/jun: carpeta 'Gaetan' vs config
    # 'Gaetan Jsph' → el subset directo fallaba → el cliente no recibía su mail.
    # SOLO si hay UN candidato único: con 2+ posibles es ambiguo y devolvemos
    # None (jamás arriesgar mandarle el video al cliente equivocado).
    candidates = [
        r for r in all_rows
        if _tokens(r["cliente"]) and target_tokens.issubset(_tokens(r["cliente"]))
    ]
    if len(candidates) == 1:
        return dict(candidates[0])
    return None


def cfg_upsert_client(cliente: str, email: str, display_name: Optional[str] = None,
                       notifications_enabled: bool = True) -> None:
    """Crea/actualiza un cliente con mail. notifications_enabled controla si
    recibe mails de 'tu video está listo'."""
    if not cliente or not email:
        raise ValueError("cliente y email son requeridos")
    conn = get_conn()
    conn.execute("""
        INSERT INTO cfg_clients (cliente, email, display_name, notifications_enabled, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(cliente) DO UPDATE SET
            email=excluded.email,
            display_name=excluded.display_name,
            notifications_enabled=excluded.notifications_enabled,
            updated_at=excluded.updated_at
    """, (cliente, email.strip().lower(), (display_name or "").strip() or None,
          1 if notifications_enabled else 0, now_iso(), now_iso()))
    conn.commit()
    conn.close()


def cfg_delete_client(cliente: str) -> int:
    if not cliente:
        return 0
    conn = get_conn()
    n = conn.execute("DELETE FROM cfg_clients WHERE TRIM(cliente)=TRIM(?)", (cliente,)).rowcount
    conn.commit()
    conn.close()
    return n


def create_client_review(cliente: str, video_file_id: Optional[str],
                          video_file_name: str, editor: Optional[str]) -> int:
    """Registra que se mandó un video al cliente — queda en 'pending' hasta
    que el cliente apruebe o pida revisión. Retorna el id del review."""
    conn = get_conn()
    cur = conn.execute("""
        INSERT INTO client_reviews
            (cliente, video_file_id, video_file_name, editor, status, created_at)
        VALUES (?, ?, ?, ?, 'pending', ?)
    """, (cliente, video_file_id, video_file_name, editor, now_iso()))
    rid = cur.lastrowid
    conn.commit()
    conn.close()
    return rid


def get_client_review(review_id: int) -> Optional[dict]:
    conn = get_conn()
    r = conn.execute(
        "SELECT id, cliente, video_file_id, video_file_name, editor, status, notes, created_at, responded_at, resolved_at "
        "FROM client_reviews WHERE id=?", (review_id,)
    ).fetchone()
    conn.close()
    return dict(r) if r else None


def get_latest_pending_review_for_client(cliente: str) -> Optional[dict]:
    """Última review en estado 'pending' (sin respuesta del cliente todavía)."""
    if not cliente:
        return None
    conn = get_conn()
    r = conn.execute("""
        SELECT id, cliente, video_file_id, video_file_name, editor, status, notes, created_at
        FROM client_reviews
        WHERE TRIM(cliente)=TRIM(?) AND status='pending'
        ORDER BY id DESC LIMIT 1
    """, (cliente,)).fetchone()
    conn.close()
    return dict(r) if r else None


def list_unnotified_reviews(max_age_hours: int = 72) -> list[dict]:
    """Reviews 'revision_requested' que todavía NO se avisaron al editor/admin
    (notified_at NULL), de las últimas `max_age_hours`. El scan las procesa y manda
    el mail de forma DURABLE (a diferencia del endpoint del portal, que se perdía si
    la función Vercel moría). Límite de antigüedad para no remandar avisos viejos."""
    from datetime import timedelta
    conn = get_conn()
    try:
        cutoff = (datetime.now() - timedelta(hours=max_age_hours)).isoformat(timespec="seconds")
        rows = conn.execute("""
            SELECT id, cliente, video_file_id, video_file_name, editor, notes, created_at
            FROM client_reviews
            WHERE status='revision_requested' AND COALESCE(notified_at,'')=''
              AND created_at >= ?
            ORDER BY created_at ASC
        """, (cutoff,)).fetchall()
        return [dict(r) for r in rows]
    except Exception as e:
        print(f"list_unnotified_reviews error: {e}")
        return []
    finally:
        conn.close()


def mark_review_notified(review_id: int) -> None:
    """Marca una review como ya avisada (notified_at=now) para que el scan no
    vuelva a mandar el mail."""
    conn = get_conn()
    try:
        conn.execute("UPDATE client_reviews SET notified_at=? WHERE id=?", (now_iso(), review_id))
        conn.commit()
    finally:
        conn.close()


def get_latest_review_for_video(video_file_id: str) -> Optional[dict]:
    """Última review para un video específico (en cualquier estado)."""
    if not video_file_id:
        return None
    conn = get_conn()
    r = conn.execute("""
        SELECT id, cliente, video_file_id, video_file_name, editor, status, notes, created_at, responded_at, resolved_at
        FROM client_reviews
        WHERE video_file_id=?
        ORDER BY id DESC LIMIT 1
    """, (video_file_id,)).fetchone()
    conn.close()
    return dict(r) if r else None


def respond_to_review(review_id: int, approved: bool, notes: Optional[str] = None) -> bool:
    """El cliente responde: approved=True (aprueba) o False (pide revisión).
    Si pide revisión, `notes` tiene el texto que dejó. Retorna True si OK."""
    conn = get_conn()
    new_status = 'approved' if approved else 'revision_requested'
    n = conn.execute("""
        UPDATE client_reviews
        SET status=?, notes=?, responded_at=?
        WHERE id=? AND status='pending'
    """, (new_status, notes, now_iso(), review_id)).rowcount
    conn.commit()
    conn.close()
    return n > 0


def mark_review_resolved_for_client_video(cliente: str, video_file_name: str) -> Optional[int]:
    """Cuando el editor sube la corrección (sistema detecta por nombre de video),
    marca la review en 'revision_requested' como 'resolved' y retorna su id."""
    if not cliente or not video_file_name:
        return None
    # Normalizar nombre del video — el sistema de correcciones ya usa _video_key
    try:
        from tracker import _video_key
        vkey = _video_key(video_file_name) if callable(_video_key) else video_file_name
    except Exception:
        vkey = video_file_name
    conn = get_conn()
    # Buscar review en revision_requested para el cliente cuyo video_file_name
    # tenga el mismo "key" (substring/match del número de video)
    rows = conn.execute("""
        SELECT id, video_file_name FROM client_reviews
        WHERE TRIM(cliente)=TRIM(?) AND status='revision_requested'
        ORDER BY id DESC
    """, (cliente,)).fetchall()
    review_id = None
    for r in rows:
        try:
            k = _video_key(r["video_file_name"]) if callable(_video_key) else r["video_file_name"]
        except Exception:
            k = r["video_file_name"]
        if k == vkey:
            review_id = r["id"]
            break
    if review_id is None and rows:
        # Fallback: tomar la review más reciente si no hay match exacto
        review_id = rows[0]["id"]
    if review_id is None:
        conn.close()
        return None
    conn.execute(
        "UPDATE client_reviews SET status='resolved', resolved_at=? WHERE id=?",
        (now_iso(), review_id)
    )
    conn.commit()
    conn.close()
    return review_id


def list_attachments_for_review(review_id: int) -> list[dict]:
    """Lista metadata (sin el blob) de attachments de una review.
    El blob se sirve por GET /api/review_attachment?id=N."""
    if not review_id:
        return []
    conn = get_conn()
    rows = conn.execute("""
        SELECT id, review_id, filename, mime_type, size_bytes, created_at
        FROM client_review_attachments
        WHERE review_id = ?
        ORDER BY id ASC
    """, (review_id,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_attachment_blob(attachment_id: int) -> Optional[dict]:
    """Lee un attachment completo (con blob). Retorna None si no existe."""
    conn = get_conn()
    row = conn.execute("""
        SELECT id, review_id, filename, mime_type, blob, size_bytes
        FROM client_review_attachments WHERE id = ?
    """, (attachment_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def add_attachment_to_review(review_id: int, filename: str, mime_type: str,
                              blob: bytes) -> int:
    """Inserta un attachment para una review. Retorna el id."""
    conn = get_conn()
    cur = conn.execute("""
        INSERT INTO client_review_attachments
            (review_id, filename, mime_type, blob, size_bytes, created_at)
        VALUES (?, ?, ?, ?, ?, ?)
    """, (review_id, filename, mime_type, blob, len(blob), now_iso()))
    aid = cur.lastrowid
    conn.commit()
    conn.close()
    return aid


def count_open_reviews_for_editor(editor: str) -> int:
    """Cuántas revisiones de clientes están en estado revision_requested
    asignadas a este editor. Sirve para sumar al pending_count del editor."""
    if not editor:
        return 0
    conn = get_conn()
    n = conn.execute(
        "SELECT COUNT(*) FROM client_reviews WHERE editor=? AND status='revision_requested'",
        (editor,)
    ).fetchone()[0] or 0
    conn.close()
    return n


def list_open_reviews_for_editor(editor: str) -> list[dict]:
    """Lista de revisiones pendientes (revision_requested) de un editor.
    Para mostrar en el dashboard del editor."""
    if not editor:
        return []
    conn = get_conn()
    rows = conn.execute("""
        SELECT id, cliente, video_file_id, video_file_name, notes, created_at, responded_at
        FROM client_reviews
        WHERE editor=? AND status='revision_requested'
        ORDER BY responded_at DESC
    """, (editor,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def cfg_client_should_be_notified(cliente: str) -> Optional[dict]:
    """¿Hay que mandarle mail al cliente cuando se entregue un video?
    Retorna dict con {email, display_name} si SÍ, None si no (no configurado
    o notifications_enabled=0)."""
    c = cfg_get_client(cliente)
    if not c:
        return None
    if not c.get("notifications_enabled"):
        return None
    if not c.get("email"):
        return None
    return {"email": c["email"], "display_name": c.get("display_name") or cliente.split()[0]}


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


def is_client_archived(cliente: str) -> bool:
    """True si el cliente está ARCHIVADO (Ignacio ya no trabaja con él).
    Archivado = el sistema lo ignora por completo: no se crean tasks, no se
    mandan mails (ni material, ni completion, ni delivery), no aparece en el
    dashboard. Match normalizado (acentos/case) contra cfg_archived_clients.
    Pedido Ignacio 10/jun: "borrarlos y que no aparezcan más ni lleguen mails"."""
    if not cliente:
        return False
    conn = get_conn()
    try:
        rows = conn.execute("SELECT cliente FROM cfg_archived_clients").fetchall()
    except Exception:
        conn.close()
        return False
    conn.close()
    target = _normalize_client_name(cliente)
    return any(_normalize_client_name(r["cliente"]) == target for r in rows)


def list_archived_clients() -> list:
    """Lista de clientes archivados con fecha."""
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT cliente, archived_at FROM cfg_archived_clients ORDER BY archived_at DESC"
        ).fetchall()
    except Exception:
        rows = []
    conn.close()
    return [{"cliente": r["cliente"], "archived_at": r["archived_at"]} for r in rows]


def is_client_blocked(cliente: str, editor: Optional[str]) -> bool:
    """Devuelve True si el cliente tiene un bloqueo activo (no expirado).

    Match: 1) exacto TRIM. 2) fallback normalizado (acentos/case) — para que
    un block de 'Ronny Matrix Painting' atrape también 'Ronny Matrix painting'
    o variaciones. Bug 04/jun: clientes reaparecían por mismatch de nombre.
    """
    # Cliente ARCHIVADO = bloqueado para todo (cubre los 4 puntos de creación
    # de tasks en scan/incremental/audit sin tocarlos).
    if is_client_archived(cliente):
        return True
    from datetime import datetime
    now = datetime.now()
    target = _normalize_client_name(cliente)
    ed = editor or ""
    conn = get_conn()
    rows = conn.execute(
        "SELECT cliente, editor, blocked_until FROM client_blocks"
    ).fetchall()
    conn.close()
    # Revisar TODOS los bloqueos: devolver True si CUALQUIERA está activo y
    # matchea el cliente (exacto o normalizado por acentos/case) y el editor
    # (wildcard editor='' = cliente entero, o editor igual).
    # Bug 09/jun Alicia Ramírez: la versión vieja hacía un match exacto que
    # encontraba un bloqueo VENCIDO (de 'Alicia Ramírez' con tilde) y se
    # saltaba el fallback que tenía el bloqueo PERMANENTE de 'Alicia Ramirez'
    # (sin tilde, editor='') → devolvía False → el scan re-creaba la task todos
    # los días. Ahora juntamos todos y nos quedamos con cualquier activo.
    for r in rows:
        name_match = (
            (r["cliente"] or "").strip() == (cliente or "").strip()
            or _normalize_client_name(r["cliente"]) == target
        )
        if not name_match:
            continue
        # editor: wildcard (vacío/NULL) aplica a cualquiera; si no, debe coincidir
        if r["editor"] and r["editor"] != ed:
            continue
        try:
            if datetime.fromisoformat(r["blocked_until"]) > now:
                return True
        except Exception:
            continue
    return False


def delivered_against_task(conn, cliente: str, editor: Optional[str], since_iso: str) -> int:
    """Cuántos editados de ENTREGA (mails kind='completion' NO corrección) entregó
    este editor para este cliente desde `since_iso`. Fuente DURABLE: mail_log
    (respaldado por dedupe Turso), que no se pierde aunque un push de tracker.db
    pise el descuento. Match de editor tolerante a apodos (Adri/Adrian) vía
    canonical_editor; cuenta también mails sin editor (resueltos por cliente)."""
    try:
        eds = [r["name"] for r in conn.execute(
            "SELECT name FROM cfg_editors WHERE active=1").fetchall()]
    except Exception:
        eds = []
    tgt = canonical_editor(editor or "", eds).strip().lower()
    try:
        rows = conn.execute(
            "SELECT editor FROM mail_log WHERE kind='completion' AND COALESCE(success,1)=1 "
            "AND TRIM(cliente)=TRIM(?) AND subject NOT LIKE '%🔧%' "
            "AND LOWER(subject) NOT LIKE '%correc%' AND sent_at >= ?",
            (cliente or "", since_iso or "")
        ).fetchall()
    except Exception:
        return 0
    n = 0
    for r in rows:
        me = (r["editor"] or "").strip()
        if not me or canonical_editor(me, eds).strip().lower() == tgt:
            n += 1
    return n


def reconcile_locked_tasks(conn=None) -> int:
    """Cierra las tasks pending MANUALES (count_locked=1) cuyas entregas reales
    (mails de completion) ya cubren el count asignado. IDEMPOTENTE y DURABLE: el
    restante se re-deriva de mail_log, así si un push perdió el decrement, el
    próximo scan re-cierra. Devuelve cuántas cerró. Las tasks auto NO se tocan
    (esas las cierra decrement + el closer contando crudos vs editados)."""
    own = conn is None
    if own:
        conn = get_conn()
    n = 0
    try:
        tasks = conn.execute(
            "SELECT id, TRIM(cliente) AS c, TRIM(COALESCE(editor,'')) AS e, "
            "COALESCE(pending_count,1) AS pc, detected_at "
            "FROM tasks WHERE status='pending' AND COALESCE(count_locked,0)=1"
        ).fetchall()
        for t in tasks:
            d = delivered_against_task(conn, t["c"], t["e"], t["detected_at"])
            if d >= (t["pc"] or 1):
                conn.execute(
                    "UPDATE tasks SET status='done', completed_at=?, pending_count=0 WHERE id=?",
                    (now_iso(), t["id"]))
                n += 1
        if n:
            conn.commit()
    except Exception as e:
        print(f"reconcile_locked_tasks error: {e}")
    finally:
        if own:
            conn.close()
    return n


def decrement_pending_count(cliente: str, completed_by_file_id: str) -> dict:
    """
    Decrementa pending_count en 1. Si llega a 0, marca la task como done.
    Retorna: {'task_id', 'new_count', 'closed': bool}
    Si NO había task pending, retorna {'task_id': None, ...}

    Tasks MANUALES (count_locked=1): NO se muta el pending_count (= lo que fijó el
    admin, estable). El restante se DERIVA de los mails de entrega ya enviados
    (durable, no se pierde con un push). Solo se marca done cuando las entregas
    cubren el count. Esto evita el bug de manual-no-descontada: el descuento ya no
    depende de que el UPDATE perdure en tracker.db.
    """
    conn = get_conn()
    row = conn.execute("""
        SELECT id, COALESCE(pending_count, 1) as cnt, editor,
               COALESCE(count_locked, 0) as lk, detected_at FROM tasks
        WHERE TRIM(cliente)=TRIM(?) AND status='pending'
        ORDER BY detected_at ASC LIMIT 1
    """, (cliente,)).fetchone()
    if not row:
        conn.close()
        return {"task_id": None, "new_count": 0, "closed": False, "editor": None}

    task_id = row["id"]
    editor = row["editor"]

    if row["lk"]:
        # MANUAL: restante = count_asignado - entregas (mails) ya hechas. Este
        # editado todavía NO está en mail_log (el mail se encola después), por eso
        # sumamos 1 a las entregas previas.
        delivered_prev = delivered_against_task(conn, cliente, editor, row["detected_at"])
        new_count = max(0, (row["cnt"] or 1) - (delivered_prev + 1))
        closed = new_count <= 0
        if closed:
            conn.execute("""
                UPDATE tasks SET status='done', completed_at=?, completed_by_file_id=?, pending_count=0
                WHERE id=?
            """, (now_iso(), completed_by_file_id, task_id))
            conn.commit()
        conn.close()
        return {"task_id": task_id, "new_count": new_count, "closed": closed, "editor": editor}

    # AUTO: descuento clásico (mutando pending_count)
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

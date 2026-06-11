"""Reglas de Roger Marti: yt/youtube→Jere, >5min→Jere, default→Valen. Borrar después."""
import sqlite3
from datetime import datetime
conn = sqlite3.connect("tracker.db")
conn.execute("""CREATE TABLE IF NOT EXISTS cfg_subfolder_editors (
    id INTEGER PRIMARY KEY AUTOINCREMENT, cliente TEXT NOT NULL,
    subfolder TEXT NOT NULL DEFAULT '', editor TEXT NOT NULL,
    created_at TEXT, UNIQUE(cliente, subfolder))""")
conn.execute("""CREATE TABLE IF NOT EXISTS cfg_duration_editors (
    cliente TEXT NOT NULL, min_minutes REAL NOT NULL, editor TEXT NOT NULL,
    PRIMARY KEY (cliente, min_minutes))""")
now = datetime.now().isoformat(timespec="seconds")
for sub, ed in [("yt","Jere"),("youtube","Jere"),("","Valen")]:
    conn.execute("""INSERT INTO cfg_subfolder_editors (cliente,subfolder,editor,created_at)
        VALUES ('Roger Marti',?,?,?) ON CONFLICT(cliente,subfolder) DO UPDATE SET editor=excluded.editor""",(sub,ed,now))
conn.execute("""INSERT INTO cfg_duration_editors VALUES ('Roger Marti', 5, 'Jere')
    ON CONFLICT(cliente,min_minutes) DO UPDATE SET editor=excluded.editor""")
conn.commit()
for r in conn.execute("SELECT subfolder,editor FROM cfg_subfolder_editors WHERE cliente='Roger Marti'"):
    print(f"  subfolder '{r[0]}' -> {r[1]}")
for r in conn.execute("SELECT min_minutes,editor FROM cfg_duration_editors WHERE cliente='Roger Marti'"):
    print(f"  duración >={r[0]}min -> {r[1]}")
conn.close()

"""Siembra reglas de editor por subcarpeta para Egdylu. Borrar después."""
import sqlite3
from datetime import datetime
conn = sqlite3.connect("tracker.db")
conn.execute("""CREATE TABLE IF NOT EXISTS cfg_subfolder_editors (
    id INTEGER PRIMARY KEY AUTOINCREMENT, cliente TEXT NOT NULL,
    subfolder TEXT NOT NULL DEFAULT '', editor TEXT NOT NULL,
    created_at TEXT, UNIQUE(cliente, subfolder))""")
now = datetime.now().isoformat(timespec="seconds")
for sub, ed in [("reel", "Fran"), ("yt", "Rami"), ("youtube", "Rami")]:
    conn.execute("""INSERT INTO cfg_subfolder_editors (cliente, subfolder, editor, created_at)
        VALUES ('Egdylu', ?, ?, ?)
        ON CONFLICT(cliente, subfolder) DO UPDATE SET editor=excluded.editor""", (sub, ed, now))
conn.commit()
for r in conn.execute("SELECT cliente, subfolder, editor FROM cfg_subfolder_editors WHERE cliente='Egdylu'"):
    print(f"  regla: {r[0]} / '{r[1]}' -> {r[2]}")
conn.close()

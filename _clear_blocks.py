"""One-off: limpia TODOS los bloqueos de clientes (client_blocks) sobre el
tracker.db checked-out. El workflow hace el git commit+push. Pedido Ignacio
09/jun: "dejalos a todos activos por ahora". Borrar script + workflow después.
"""
import sqlite3

conn = sqlite3.connect("tracker.db")
before = conn.execute("SELECT COUNT(*) FROM client_blocks").fetchone()[0]
conn.execute("DELETE FROM client_blocks")
conn.commit()
after = conn.execute("SELECT COUNT(*) FROM client_blocks").fetchone()[0]
conn.close()
print(f"client_blocks: {before} -> {after}")

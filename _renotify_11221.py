"""Re-dispara el aviso de la review #11221 (Daniel) para que le llegue a Rafa.
La copia del admin se deduplica (6h); la de Rafa nunca salió. Borrar después."""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from tracker import get_conn
from notifier import notify_revision_requested
conn = get_conn()
row = conn.execute("SELECT * FROM client_reviews WHERE id=11221").fetchone()
conn.close()
if not row:
    print("review no encontrada"); sys.exit(1)
review = dict(row)
print(f"review #{review['id']} {review['cliente']} editor_db={review.get('editor')!r}")
notify_revision_requested(11221, review, review.get("notes") or "(sin notas)")

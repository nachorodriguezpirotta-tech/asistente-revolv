"""One-off: achica tracker.db. Borra reviews approved/resolved viejas (>7d, casi
todas basura de migración) + attachments de reviews ya cerradas + VACUUM. NO
toca known_files/known_edited (el scan los re-inserta) ni reviews abiertas.
Bug 18/jun: tracker.db creció a 12MB → with_db timeouteaba en /api/task. Borrar después."""
import sqlite3, os
from datetime import datetime, timedelta
conn = sqlite3.connect("tracker.db")
before = os.path.getsize("tracker.db") // 1024
d7 = (datetime.now() - timedelta(days=7)).isoformat()
a = conn.execute("DELETE FROM client_review_attachments WHERE review_id NOT IN "
                 "(SELECT id FROM client_reviews WHERE status='revision_requested')").rowcount
b = conn.execute("DELETE FROM client_reviews WHERE status IN ('approved','resolved') "
                 "AND COALESCE(created_at,'1970') < ?", (d7,)).rowcount
conn.commit()
open_n = conn.execute("SELECT COUNT(*) FROM client_reviews WHERE status='revision_requested'").fetchone()[0]
conn.execute("VACUUM"); conn.commit(); conn.close()
after = os.path.getsize("tracker.db") // 1024
print(f"purga: {a} attachments, {b} reviews viejas | abiertas conservadas: {open_n}")
print(f"tracker.db: {before} KB -> {after} KB")

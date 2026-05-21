"""
Test del flujo de mails de corrección.

Simula:
  1. Editor "Agus" sube corrección de "video TEST 10.mp4" para cliente PRUEBA REVOLV
     → debería mandar 2 mails (admin Ignacio + cliente Ignacio)
  2. Segundo worker de GHA detecta la MISMA corrección pero como editor "—"
     (caso real del bug 21/may con Ely Fitness)
     → debería DEDUPEAR ambos mails (admin y cliente) por el stable key

Cleanup automático al final salvo TEST_KEEP=1.
"""

import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
os.chdir(ROOT)

from tracker import (
    init_db, get_conn,
    cfg_upsert_client, cfg_delete_client,
    enqueue_completion_mail,
)
from notifier import send_completion_mails
from config import TEST_EMAIL

from datetime import datetime
_ts = datetime.now().strftime("%H%M%S")

CLIENTE = "PRUEBA REVOLV"
FILE_NAME = f"video TEST {_ts}.mp4"  # único por run para no chocar con mail_log previo
FILE_ID_1 = f"1FAKE-corr-{_ts}-a"
FILE_ID_2 = f"1FAKE-corr-{_ts}-b"


def main():
    init_db()

    print(f"\n{'='*60}")
    print(f"TEST: mails de corrección + dedupe estable")
    print(f"{'='*60}\n")

    print(f"⚙️  Setup: cliente '{CLIENTE}' → {TEST_EMAIL}")
    cfg_upsert_client(
        cliente=CLIENTE,
        email=TEST_EMAIL,
        display_name="Ignacio",
        notifications_enabled=True,
    )

    # ── ROUND 1: editor "Agus" ────────────────────────────────────────
    print(f"\n▶️  ROUND 1: editor='Agus', file='{FILE_NAME}'")
    print(f"   Esperado: 1 mail al admin + 1 mail al cliente (mismo Ignacio)")

    row1 = enqueue_completion_mail(
        task_id=None,
        cliente=CLIENTE,
        editor="Agus",
        file_id=FILE_ID_1,
        file_name=FILE_NAME,
        edited_folder_id=None,
        client_folder_id=None,
        new_count=0,
        closed=False,
        is_correction=True,
    )
    print(f"   queue row id={row1}")

    sent1 = send_completion_mails(recipient=TEST_EMAIL)
    print(f"   ✉️  mails admin enviados: {sent1}")

    # ── ROUND 2: editor "—" (simulando 2do worker que perdió el editor) ─
    print(f"\n▶️  ROUND 2: editor='—', misma file_name (simulando race condition)")
    print(f"   Esperado: 0 mails (ambos deben dedupearse por stable key)")

    time.sleep(2)
    row2 = enqueue_completion_mail(
        task_id=None,
        cliente=CLIENTE,
        editor="—",
        file_id=FILE_ID_2,
        file_name=FILE_NAME,  # MISMO archivo
        edited_folder_id=None,
        client_folder_id=None,
        new_count=0,
        closed=False,
        is_correction=True,
    )
    print(f"   queue row id={row2}")

    sent2 = send_completion_mails(recipient=TEST_EMAIL)
    print(f"   ✉️  mails admin enviados (esperado 0): {sent2}")

    # ── Verificación en mail_log ──────────────────────────────────────
    print(f"\n📊 Verificación en mail_log para '{FILE_NAME}':")
    conn = get_conn()
    rows = conn.execute("""
        SELECT id, datetime(sent_at,'localtime') as t, subject, kind, error
        FROM mail_log
        WHERE subject LIKE ? OR (kind='client-correction-ready' AND cliente=?)
        ORDER BY id ASC
    """, (f"%{FILE_NAME}%", CLIENTE)).fetchall()

    admin_sent = 0
    admin_skipped = 0
    client_sent = 0
    client_skipped = 0
    for r in rows:
        is_dedupe = (r["error"] == "dedupe-skip")
        is_admin = "🔧 Corrección" in (r["subject"] or "")
        is_client = "Tu revisión está lista" in (r["subject"] or "") or r["kind"] == "client-correction-ready"
        tag = "DEDUPE-SKIP" if is_dedupe else "ENVIADO"
        who = "ADMIN" if is_admin else ("CLIENT" if is_client else "?")
        print(f"   [{r['t']}] {who:<6} {tag:<12} {(r['subject'] or '')[:60]}")
        if is_admin and is_dedupe: admin_skipped += 1
        elif is_admin: admin_sent += 1
        elif is_client and is_dedupe: client_skipped += 1
        elif is_client: client_sent += 1
    conn.close()

    print(f"\n📈 Resumen:")
    print(f"   admin enviados: {admin_sent} (esperado: 1)")
    print(f"   admin skipped (dedupe): {admin_skipped} (esperado: 1)")
    print(f"   cliente enviados: {client_sent} (esperado: 1)")
    print(f"   cliente skipped (dedupe): {client_skipped} (esperado: 1)")

    ok = (admin_sent == 1 and admin_skipped == 1
          and client_sent == 1 and client_skipped == 1)
    if ok:
        print(f"\n✅ TEST PASS — el dedupe funciona y el cliente recibe mail de corrección")
    else:
        print(f"\n❌ TEST FAIL — chequear el flujo")

    # ── Cleanup ───────────────────────────────────────────────────────
    if os.environ.get("TEST_KEEP", "").strip() == "1":
        print(f"\n💡 TEST_KEEP=1 — dejo las filas (cliente + queue + mail_log) para inspección")
    else:
        print(f"\n🧹 Cleanup...")
        conn = get_conn()
        conn.execute("DELETE FROM pending_completion_mails WHERE cliente=?", (CLIENTE,))
        conn.execute("DELETE FROM mail_log WHERE subject LIKE ? OR (kind='client-correction-ready' AND cliente=?)",
                     (f"%{FILE_NAME}%", CLIENTE))
        conn.commit()
        conn.close()
        cfg_delete_client(CLIENTE)
        print(f"   ✅ cleanup ok")


if __name__ == "__main__":
    main()

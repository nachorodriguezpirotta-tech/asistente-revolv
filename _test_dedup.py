"""Test end-to-end del dedupe atómico: manda el MISMO mail dos veces seguidas.
1ro = msg_id real | 2do = 'turso-dedupe-skip' (bloqueado por Turso).
Prueba que un duplicado es imposible. Re-ejecutable (key con timestamp). Borrar después.
"""
import os, sys, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from mail_client import send_mail

TEST_TO = os.environ.get("TEST_EMAIL") or "nacho.rodriguezpirotta@gmail.com"
KEY = f"DEDUP-TEST-{int(time.time())}"
SUBJ = "🧪 Test dedupe Turso (este mail tiene que llegar UNA sola vez)"
BODY = ("Esto es una prueba del sistema anti-duplicados.\n"
        "Aunque el sistema intentó mandarlo DOS veces, Turso bloqueó el segundo.\n"
        "Si te llegó una sola copia, funciona perfecto.\n\n— Asistente Revolv")

print(f">>> dedupe_key = {KEY}")
print(">>> ENVÍO 1 (debe mandar):")
r1 = send_mail(to=TEST_TO, subject=SUBJ, body_text=BODY, kind="dedup-test",
               dedupe_window_minutes=10080, dedupe_key_override=KEY)
print(f"    -> {r1}")

print(">>> ENVÍO 2 mismo mail (debe deduplicarse):")
r2 = send_mail(to=TEST_TO, subject=SUBJ, body_text=BODY, kind="dedup-test",
               dedupe_window_minutes=10080, dedupe_key_override=KEY)
print(f"    -> {r2}")

print(">>> VEREDICTO:")
ok = (r2 == "turso-dedupe-skip")
print("    ✅ DUPLICADO BLOQUEADO POR TURSO" if ok
      else f"    ⚠️ 2do envío no fue bloqueado por Turso (r2={r2}) — revisar")
# limpiar el claim de prueba para no dejar basura
try:
    from turso_dedupe import release_mail
    import hashlib
    release_mail(hashlib.sha1(KEY.encode()).hexdigest()[:24])
except Exception:
    pass
sys.exit(0 if ok else 1)

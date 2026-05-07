"""
Script local que genera los URLs únicos por editor.
Después del deploy en Vercel, corré:
    DASHBOARD_SECRET="tu-secret" VERCEL_URL="https://tu-app.vercel.app" python3 generate_links.py

Imprime los links para copiar y pegar en WhatsApp/mail a cada editor.
"""

import os
import sys
import hashlib
import hmac

EDITORS = ["Rami", "Benja", "Fran", "Valen", "Santi", "Agus", "Samu"]


def make_token(editor: str, secret: str) -> str:
    return hmac.new(
        secret.encode(),
        editor.lower().encode(),
        hashlib.sha256,
    ).hexdigest()[:16]


def main():
    secret = os.environ.get("DASHBOARD_SECRET", "")
    base_url = os.environ.get("VERCEL_URL", "")

    if not secret:
        print("❌ Falta DASHBOARD_SECRET (la misma que pusiste en Vercel).")
        print("   Ejemplo: DASHBOARD_SECRET='mi-secret-largo' python3 generate_links.py")
        sys.exit(1)

    if not base_url:
        base_url = "https://TU-PROYECTO.vercel.app"
        print("⚠️  VERCEL_URL no seteada. Reemplazá 'TU-PROYECTO' por tu dominio real.\n")

    base_url = base_url.rstrip("/")
    print(f"🔗 Links únicos por editor (base: {base_url})\n")
    print("-" * 70)
    print()

    for editor in EDITORS:
        token = make_token(editor, secret)
        url = f"{base_url}/?editor={editor}&t={token}"
        print(f"  📩 {editor}")
        print(f"     {url}")
        print()

    # Admin (Ignacio)
    admin_token = make_token("ADMIN", secret)
    admin_url = f"{base_url}/?admin=1&t={admin_token}"
    print("-" * 70)
    print(f"  👑 Admin (vos)")
    print(f"     {admin_url}")
    print()


if __name__ == "__main__":
    main()

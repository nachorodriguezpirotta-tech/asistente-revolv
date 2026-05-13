"""
Resumen semanal — manda 1 mail los lunes 9am SOLO a Ignacio (admin).

Incluye:
- Total entregados esta semana vs semana anterior
- Top 3 editores de la semana
- Clientes ghost (no subieron en 14+ días)
- Tendencia (mejor/peor que semana anterior)
"""

from datetime import datetime, timedelta
from config import TEST_EMAIL
from tracker import get_conn
from mail_client import send_mail


def _parse_iso(s):
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "").split(".")[0])
    except Exception:
        return None


def run(dry_run: bool = False):
    print("📊 Resumen semanal — lunes")
    conn = get_conn()
    now = datetime.now()

    # Esta semana (7 días) y semana anterior (8-14 días atrás)
    week_ago = (now - timedelta(days=7)).isoformat(timespec="seconds")
    two_weeks_ago = (now - timedelta(days=14)).isoformat(timespec="seconds")

    # Entregados esta semana (status=done, completed_at en últimos 7d)
    delivered_now = conn.execute(
        "SELECT COUNT(*) FROM tasks WHERE status='done' AND completed_at >= ?", (week_ago,)
    ).fetchone()[0]
    delivered_prev = conn.execute(
        "SELECT COUNT(*) FROM tasks WHERE status='done' AND completed_at >= ? AND completed_at < ?",
        (two_weeks_ago, week_ago),
    ).fetchone()[0]

    # Top editores esta semana
    top_eds = conn.execute("""
        SELECT editor, COUNT(*) as n FROM tasks
        WHERE status='done' AND completed_at >= ? AND editor IS NOT NULL
        GROUP BY editor ORDER BY n DESC LIMIT 5
    """, (week_ago,)).fetchall()

    # Pendientes ahora
    pending = conn.execute(
        "SELECT COALESCE(SUM(COALESCE(pending_count, 1)), 0) FROM tasks WHERE status='pending'"
    ).fetchone()[0]
    pending_clientes = conn.execute(
        "SELECT COUNT(DISTINCT TRIM(cliente)) FROM tasks WHERE status='pending'"
    ).fetchone()[0]

    # Crudos subidos esta semana
    crudos_now = conn.execute(
        "SELECT COUNT(*) FROM known_files WHERE first_seen_at >= ? AND is_baseline=0", (week_ago,)
    ).fetchone()[0]

    # Clientes ghost (no subieron en 14+ días)
    rows = conn.execute("""
        SELECT cliente, MAX(first_seen_at) as last_upload
        FROM known_files
        WHERE is_baseline=0
        GROUP BY cliente
    """).fetchall()
    ghost_count = 0
    for r in rows:
        d = _parse_iso(r["last_upload"])
        if d and (now - d).days >= 14:
            ghost_count += 1

    conn.close()

    # Calcular cambio % vs semana anterior
    if delivered_prev > 0:
        pct = ((delivered_now - delivered_prev) / delivered_prev) * 100
        trend_text = f"{pct:+.0f}%" + (" 📈" if pct > 0 else " 📉" if pct < 0 else "")
    elif delivered_now > 0:
        trend_text = "primera semana con entregas"
    else:
        trend_text = "—"

    fecha_label = now.strftime("%d/%m/%Y")
    subject = f"📊 Resumen semanal Revolv — {fecha_label}"

    text = f"""Buen lunes Nacho,

📦 ENTREGAS ESTA SEMANA: {delivered_now}
   vs semana anterior: {delivered_prev} ({trend_text})

🎬 CRUDOS RECIBIDOS: {crudos_now}

📋 PENDIENTE AHORA: {pending} videos ({pending_clientes} clientes)

🏆 TOP EDITORES DE LA SEMANA:
"""
    for i, ed in enumerate(top_eds, 1):
        text += f"   {i}. {ed['editor']}: {ed['n']} entregas\n"
    if not top_eds:
        text += "   (sin entregas)\n"

    text += f"\n👻 CLIENTES GHOST (+14d sin subir): {ghost_count}\n"
    text += f"\nVer dashboard: https://asistente-revolv.vercel.app/?admin=1\n"
    text += "\n— Asistente Revolv\n"

    top_html = ""
    if top_eds:
        for i, ed in enumerate(top_eds, 1):
            medal = ["🥇", "🥈", "🥉", "4.", "5."][i-1]
            top_html += f'<li>{medal} <strong>{ed["editor"]}</strong>: {ed["n"]} entregas</li>'
    else:
        top_html = '<li style="color:#888">Sin entregas esta semana</li>'

    trend_color = "#4ade80" if delivered_now > delivered_prev else "#f87171" if delivered_now < delivered_prev else "#888"

    html = f"""<html><body style="font-family:-apple-system,Segoe UI,sans-serif;max-width:640px;color:#222;line-height:1.5;">
<h1 style="margin-bottom:4px;">📊 Resumen semanal</h1>
<p style="color:#666;margin-top:0;">{fecha_label}</p>

<div style="background:#f5f5f5;padding:16px;border-radius:8px;margin:20px 0;">
  <div style="font-size:36px;font-weight:700;">{delivered_now}</div>
  <div style="color:#666;">entregas esta semana</div>
  <div style="margin-top:8px;color:{trend_color};font-weight:600;">{trend_text} vs anterior ({delivered_prev})</div>
</div>

<div style="display:flex;gap:12px;margin:20px 0;">
  <div style="flex:1;background:#f5f5f5;padding:12px;border-radius:8px;">
    <div style="font-size:24px;font-weight:600;">{crudos_now}</div>
    <div style="color:#666;font-size:12px;">crudos recibidos</div>
  </div>
  <div style="flex:1;background:#f5f5f5;padding:12px;border-radius:8px;">
    <div style="font-size:24px;font-weight:600;">{pending}</div>
    <div style="color:#666;font-size:12px;">pending ahora ({pending_clientes} clientes)</div>
  </div>
  <div style="flex:1;background:#f5f5f5;padding:12px;border-radius:8px;">
    <div style="font-size:24px;font-weight:600;color:{'#d4a070' if ghost_count > 0 else '#888'};">{ghost_count}</div>
    <div style="color:#666;font-size:12px;">👻 ghost (14+d)</div>
  </div>
</div>

<h3>🏆 Top de la semana</h3>
<ul style="line-height:1.7;">{top_html}</ul>

<p style="margin-top:24px;"><a href="https://asistente-revolv.vercel.app/?admin=1" style="background:#ff4747;color:white;padding:10px 18px;border-radius:6px;text-decoration:none;font-weight:600;">📋 Ver dashboard completo</a></p>

<hr style="border:none;border-top:1px solid #eee;margin:24px 0;">
<p style="color:#888;font-size:12px;">— Asistente Revolv · Cada lunes 9am · Solo a vos</p>
</body></html>"""

    print(f"   Entregas esta semana: {delivered_now} (prev: {delivered_prev}, {trend_text})")
    print(f"   Top: {[(e['editor'], e['n']) for e in top_eds]}")

    if dry_run:
        print("(dry-run)")
        return

    try:
        msg_id = send_mail(to=TEST_EMAIL, subject=subject, body_text=text, body_html=html)
        print(f"   ✅ Enviado a {TEST_EMAIL}: {msg_id}")
    except Exception as e:
        print(f"   ❌ Falló: {e}")


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()
    run(dry_run=args.dry_run)

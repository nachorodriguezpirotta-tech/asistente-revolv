"""
Resumen mensual — manda 1 mail el día 1 de cada mes 9am, SOLO a Ignacio (admin).

Incluye:
- Entregas del mes vs mes anterior
- Top 5 editores del mes
- Top 5 clientes más activos
- Clientes ghost identificados
- Tendencia visual
"""

from datetime import datetime, timedelta
from config import TEST_EMAIL
from tracker import get_conn
from mail_client import send_mail

SPANISH_MONTHS = {
    1: "enero", 2: "febrero", 3: "marzo", 4: "abril", 5: "mayo", 6: "junio",
    7: "julio", 8: "agosto", 9: "septiembre", 10: "octubre", 11: "noviembre", 12: "diciembre",
}


def _parse(s):
    if not s: return None
    try:
        return datetime.fromisoformat(s.replace("Z", "").split(".")[0])
    except Exception:
        return None


def _month_range(now):
    """Devuelve (start_this_month, start_last_month, end_last_month)."""
    first_this = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    # Último día del mes anterior = primer día de este mes - 1 día
    end_last = first_this - timedelta(seconds=1)
    # Primer día del mes anterior
    first_last = end_last.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    return first_this, first_last, end_last


def run(dry_run: bool = False):
    print("📊 Resumen mensual")
    conn = get_conn()
    now = datetime.now()
    first_this, first_last, end_last = _month_range(now)

    # Si el script corre el día 1 de un mes, queremos reporte del MES ANTERIOR.
    # Mes "actual" para el reporte = mes anterior completo.
    report_start = first_last.isoformat(timespec="seconds")
    report_end = end_last.isoformat(timespec="seconds")
    prev_start = (first_last - timedelta(days=32)).replace(day=1, hour=0, minute=0, second=0).isoformat(timespec="seconds")
    prev_end = (first_last - timedelta(seconds=1)).isoformat(timespec="seconds")
    month_label = f"{SPANISH_MONTHS[first_last.month]} {first_last.year}"

    # Entregas mes vs anterior
    delivered_this = conn.execute(
        "SELECT COUNT(*) FROM tasks WHERE status='done' AND completed_at >= ? AND completed_at <= ?",
        (report_start, report_end),
    ).fetchone()[0]
    delivered_prev = conn.execute(
        "SELECT COUNT(*) FROM tasks WHERE status='done' AND completed_at >= ? AND completed_at <= ?",
        (prev_start, prev_end),
    ).fetchone()[0]

    # Top editores
    top_eds = conn.execute("""
        SELECT editor, COUNT(*) as n FROM tasks
        WHERE status='done' AND completed_at >= ? AND completed_at <= ? AND editor IS NOT NULL
        GROUP BY editor ORDER BY n DESC LIMIT 5
    """, (report_start, report_end)).fetchall()

    # Top clientes (más crudos en el mes)
    top_clients = conn.execute("""
        SELECT cliente, COUNT(*) as n FROM known_files
        WHERE first_seen_at >= ? AND first_seen_at <= ? AND is_baseline=0
        GROUP BY cliente ORDER BY n DESC LIMIT 5
    """, (report_start, report_end)).fetchall()

    # Crudos totales recibidos
    crudos = conn.execute(
        "SELECT COUNT(*) FROM known_files WHERE first_seen_at >= ? AND first_seen_at <= ? AND is_baseline=0",
        (report_start, report_end),
    ).fetchone()[0]

    # Ghost clients: NO subieron en 60+ días
    ghost_count = 0
    rows = conn.execute(
        "SELECT cliente, MAX(first_seen_at) as last FROM known_files WHERE is_baseline=0 GROUP BY cliente"
    ).fetchall()
    for r in rows:
        d = _parse(r["last"])
        if d and (now - d).days >= 60:
            ghost_count += 1

    conn.close()

    # Tendencia
    if delivered_prev > 0:
        pct = ((delivered_this - delivered_prev) / delivered_prev) * 100
        trend = f"{pct:+.0f}%" + (" 📈" if pct > 0 else " 📉" if pct < 0 else " ➡️")
    elif delivered_this > 0:
        trend = "primer mes con entregas"
    else:
        trend = "—"

    subject = f"📊 Resumen mensual Revolv — {month_label}"

    text = f"""Buen día Nacho,

📦 ENTREGAS DEL MES: {delivered_this}
   vs mes anterior: {delivered_prev} ({trend})

🎬 CRUDOS RECIBIDOS: {crudos}

🏆 TOP EDITORES DEL MES:
"""
    for i, ed in enumerate(top_eds, 1):
        text += f"   {i}. {ed['editor']}: {ed['n']} entregas\n"
    text += "\n📈 TOP CLIENTES MÁS ACTIVOS:\n"
    for i, c in enumerate(top_clients, 1):
        text += f"   {i}. {c['cliente']}: {c['n']} crudos\n"
    text += f"\n👻 CLIENTES GHOST (+60d sin subir): {ghost_count}\n"
    text += f"\nDashboard: https://asistente-revolv.vercel.app/?admin=1\n— Asistente Revolv\n"

    top_eds_html = "".join(
        f"<li>{['🥇','🥈','🥉','4.','5.'][i]} <strong>{ed['editor']}</strong>: {ed['n']} entregas</li>"
        for i, ed in enumerate(top_eds)
    ) or '<li style="color:#888">Sin entregas</li>'
    top_clients_html = "".join(
        f"<li><strong>{c['cliente']}</strong>: {c['n']} crudos</li>"
        for c in top_clients
    ) or '<li style="color:#888">Sin actividad</li>'
    trend_color = "#4ade80" if delivered_this > delivered_prev else "#f87171" if delivered_this < delivered_prev else "#888"

    html = f"""<html><body style="font-family:-apple-system,Segoe UI,sans-serif;max-width:640px;color:#222;line-height:1.5;">
<h1 style="margin-bottom:4px;">📊 Resumen mensual</h1>
<p style="color:#666;margin-top:0;text-transform:capitalize;">{month_label}</p>

<div style="background:#f5f5f5;padding:20px;border-radius:10px;margin:20px 0;">
  <div style="font-size:42px;font-weight:700;">{delivered_this}</div>
  <div style="color:#666;">entregas en {month_label}</div>
  <div style="margin-top:8px;color:{trend_color};font-weight:600;font-size:14px;">{trend} vs mes anterior ({delivered_prev})</div>
</div>

<div style="background:#f5f5f5;padding:14px;border-radius:8px;margin:16px 0;">
  <div style="font-size:24px;font-weight:600;">{crudos}</div>
  <div style="color:#666;font-size:13px;">crudos recibidos en el mes</div>
</div>

<h3 style="margin-top:24px;">🏆 Top editores</h3>
<ol style="line-height:1.7;padding-left:0;list-style:none;">{top_eds_html}</ol>

<h3 style="margin-top:24px;">📈 Top clientes (más material)</h3>
<ol style="line-height:1.7;">{top_clients_html}</ol>

<p style="margin-top:24px;background:{('#fef3c7' if ghost_count > 0 else '#f5f5f5')};padding:12px;border-radius:6px;">
  👻 <strong>{ghost_count} clientes ghost</strong> (+60 días sin subir nada). {'Considerá contactarlos antes de que se vayan.' if ghost_count > 0 else ''}
</p>

<p style="margin-top:24px;"><a href="https://asistente-revolv.vercel.app/?admin=1" style="background:#ff4747;color:white;padding:10px 18px;border-radius:6px;text-decoration:none;font-weight:600;">📋 Ver dashboard</a></p>
<hr style="border:none;border-top:1px solid #eee;margin:24px 0;">
<p style="color:#888;font-size:12px;">— Asistente Revolv · Primer día de cada mes</p>
</body></html>"""

    print(f"   {month_label}: {delivered_this} entregas (vs {delivered_prev}, {trend})")

    if dry_run:
        print("(dry-run)")
        return

    try:
        msg_id = send_mail(to=TEST_EMAIL, subject=subject, body_text=text, body_html=html, kind="monthly_summary")
        print(f"   ✅ Enviado: {msg_id}")
    except Exception as e:
        print(f"   ❌ Falló: {e}")


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()
    run(dry_run=args.dry_run)

"""
Genera dashboard.html con el estado actual de pendientes.

Lee la DB local (que viene del último pull del repo) y produce un HTML
estático listo para abrir en cualquier browser.
"""

import os
from collections import defaultdict
from datetime import datetime

from config import BASE_DIR
from tracker import get_conn, stats


OUTPUT_PATH = os.path.join(BASE_DIR, "dashboard.html")

SPANISH_MONTHS = {
    1: "enero", 2: "febrero", 3: "marzo", 4: "abril",
    5: "mayo", 6: "junio", 7: "julio", 8: "agosto",
    9: "septiembre", 10: "octubre", 11: "noviembre", 12: "diciembre",
}


def get_data():
    conn = get_conn()

    # Pendientes agrupados por editor → cliente → archivos
    pending_rows = conn.execute("""
        SELECT editor, cliente, file_name, detected_at
        FROM tasks
        WHERE status = 'pending'
        ORDER BY editor, cliente, detected_at
    """).fetchall()

    by_editor = defaultdict(lambda: defaultdict(list))
    for r in pending_rows:
        editor = r["editor"] or "— sin editor en Sheet —"
        by_editor[editor][r["cliente"].strip()].append({
            "file": r["file_name"],
            "detected_at": r["detected_at"],
        })

    # Últimos cierres (audit)
    closed_rows = conn.execute("""
        SELECT cliente, file_name, completed_at
        FROM tasks
        WHERE status = 'done'
        ORDER BY completed_at DESC
        LIMIT 10
    """).fetchall()

    last_closed = [
        {"cliente": r["cliente"].strip(), "file": r["file_name"], "at": r["completed_at"]}
        for r in closed_rows
    ]

    # Últimas detecciones (qué llegó hoy)
    recent_pending = conn.execute("""
        SELECT cliente, editor, file_name, detected_at
        FROM tasks
        WHERE status = 'pending'
        ORDER BY detected_at DESC
        LIMIT 10
    """).fetchall()
    recent = [
        {"cliente": r["cliente"].strip(), "editor": r["editor"] or "—",
         "file": r["file_name"], "at": r["detected_at"]}
        for r in recent_pending
    ]

    conn.close()

    return {
        "stats": stats(),
        "by_editor": by_editor,
        "last_closed": last_closed,
        "recent": recent,
    }


def _human_date(iso: str) -> str:
    if not iso:
        return "—"
    try:
        dt = datetime.fromisoformat(iso)
        return dt.strftime("%d/%m %H:%M")
    except Exception:
        return iso


def build_html(data: dict) -> str:
    s = data["stats"]
    by_editor = data["by_editor"]
    now = datetime.now()
    fecha = f"{now.day} de {SPANISH_MONTHS[now.month]} de {now.year}"
    hora = now.strftime("%H:%M")

    total_pendientes = sum(
        sum(len(files) for files in clientes.values())
        for clientes in by_editor.values()
    )
    total_clientes_pend = sum(len(clientes) for clientes in by_editor.values())
    editores_activos = len([e for e in by_editor if not e.startswith("—")])

    # Sort editores por cantidad de pendientes desc
    editor_blocks = []
    for editor in sorted(by_editor.keys(), key=lambda e: -sum(len(f) for f in by_editor[e].values())):
        clientes = by_editor[editor]
        total_editor = sum(len(f) for f in clientes.values())
        clientes_html = ""
        for cliente, files in sorted(clientes.items()):
            files_list = "".join(
                f'<li><span class="file-name">{f["file"]}</span><span class="file-date">{_human_date(f["detected_at"])}</span></li>'
                for f in files
            )
            n = len(files)
            badge = f'<span class="badge">{n}</span>'
            clientes_html += f"""
                <details class="cliente-card">
                    <summary><span class="cliente-name">{cliente}</span> {badge}</summary>
                    <ul class="files-list">{files_list}</ul>
                </details>
            """
        editor_blocks.append(f"""
            <section class="editor-block">
                <header class="editor-header">
                    <h2>{editor}</h2>
                    <span class="editor-count">{total_editor} {'video' if total_editor == 1 else 'videos'}</span>
                </header>
                <div class="clientes-grid">
                    {clientes_html}
                </div>
            </section>
        """)

    if not editor_blocks:
        editor_blocks_html = '<div class="empty-state">✅ No hay pendientes en este momento.</div>'
    else:
        editor_blocks_html = "".join(editor_blocks)

    # Recent activity
    recent_html = "".join(
        f'<li><strong>{r["cliente"]}</strong> · {r["editor"]} · <span class="dim">{_human_date(r["at"])}</span></li>'
        for r in data["recent"]
    ) or '<li class="dim">Sin actividad reciente</li>'

    closed_html = "".join(
        f'<li><strong>{c["cliente"]}</strong> · <span class="dim">{_human_date(c["at"])}</span></li>'
        for c in data["last_closed"]
    ) or '<li class="dim">Sin tareas cerradas todavía</li>'

    return f"""<!DOCTYPE html>
<html lang="es">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Asistente Revolv — Dashboard</title>
    <style>
        :root {{
            --bg: #0a0a0a;
            --bg-card: #141414;
            --bg-card-2: #1c1c1c;
            --border: #262626;
            --text: #e8e8e8;
            --text-dim: #888;
            --accent: #ff4747;
            --green: #4ade80;
            --yellow: #fbbf24;
        }}
        * {{ box-sizing: border-box; margin: 0; padding: 0; }}
        body {{
            font-family: -apple-system, BlinkMacSystemFont, 'Inter', 'Segoe UI', sans-serif;
            background: var(--bg);
            color: var(--text);
            line-height: 1.5;
            padding: 32px;
            max-width: 1400px;
            margin: 0 auto;
        }}
        header.main-header {{
            display: flex;
            justify-content: space-between;
            align-items: flex-end;
            margin-bottom: 32px;
            border-bottom: 1px solid var(--border);
            padding-bottom: 24px;
        }}
        header.main-header h1 {{
            font-size: 28px;
            font-weight: 700;
            letter-spacing: -0.02em;
        }}
        header.main-header h1 .red-dot {{
            display: inline-block;
            width: 10px;
            height: 10px;
            background: var(--accent);
            border-radius: 50%;
            margin-right: 12px;
            vertical-align: middle;
        }}
        .header-meta {{
            text-align: right;
            color: var(--text-dim);
            font-size: 13px;
        }}
        .refresh-btn {{
            background: var(--accent);
            color: white;
            border: none;
            padding: 10px 18px;
            font-size: 13px;
            font-weight: 600;
            border-radius: 8px;
            cursor: pointer;
            margin-top: 8px;
            transition: opacity 0.2s;
        }}
        .refresh-btn:hover {{ opacity: 0.85; }}
        .stats-grid {{
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
            gap: 12px;
            margin-bottom: 40px;
        }}
        .stat-card {{
            background: var(--bg-card);
            padding: 20px;
            border-radius: 12px;
            border: 1px solid var(--border);
        }}
        .stat-label {{
            color: var(--text-dim);
            font-size: 12px;
            text-transform: uppercase;
            letter-spacing: 0.05em;
            margin-bottom: 8px;
        }}
        .stat-value {{
            font-size: 32px;
            font-weight: 700;
            letter-spacing: -0.02em;
        }}
        .stat-value.pending {{ color: var(--yellow); }}
        .stat-value.done {{ color: var(--green); }}
        h2.section-title {{
            font-size: 13px;
            text-transform: uppercase;
            letter-spacing: 0.08em;
            color: var(--text-dim);
            margin: 32px 0 16px;
            font-weight: 600;
        }}
        .editor-block {{
            background: var(--bg-card);
            border: 1px solid var(--border);
            border-radius: 12px;
            padding: 20px;
            margin-bottom: 16px;
        }}
        .editor-header {{
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 16px;
            padding-bottom: 12px;
            border-bottom: 1px solid var(--border);
        }}
        .editor-header h2 {{
            font-size: 18px;
            font-weight: 600;
        }}
        .editor-count {{
            background: var(--bg-card-2);
            padding: 4px 12px;
            border-radius: 16px;
            font-size: 12px;
            color: var(--text-dim);
            font-weight: 500;
        }}
        .clientes-grid {{
            display: grid;
            gap: 8px;
        }}
        details.cliente-card {{
            background: var(--bg-card-2);
            padding: 12px 16px;
            border-radius: 8px;
            cursor: pointer;
        }}
        details.cliente-card summary {{
            display: flex;
            justify-content: space-between;
            align-items: center;
            list-style: none;
            outline: none;
        }}
        details.cliente-card summary::-webkit-details-marker {{ display: none; }}
        details.cliente-card summary::before {{
            content: "▸";
            color: var(--text-dim);
            margin-right: 8px;
            transition: transform 0.15s;
            display: inline-block;
        }}
        details.cliente-card[open] summary::before {{
            transform: rotate(90deg);
        }}
        .cliente-name {{
            font-weight: 500;
            flex: 1;
        }}
        .badge {{
            background: var(--accent);
            color: white;
            font-size: 11px;
            font-weight: 700;
            padding: 2px 8px;
            border-radius: 10px;
            min-width: 22px;
            text-align: center;
        }}
        .files-list {{
            list-style: none;
            margin-top: 12px;
            padding-top: 12px;
            border-top: 1px solid var(--border);
        }}
        .files-list li {{
            padding: 6px 0;
            display: flex;
            justify-content: space-between;
            align-items: center;
            font-size: 13px;
        }}
        .file-name {{
            color: var(--text);
            word-break: break-all;
            margin-right: 12px;
        }}
        .file-date {{
            color: var(--text-dim);
            font-size: 11px;
            white-space: nowrap;
        }}
        .empty-state {{
            background: var(--bg-card);
            border: 1px solid var(--border);
            padding: 40px;
            text-align: center;
            border-radius: 12px;
            color: var(--text-dim);
            font-size: 16px;
        }}
        .activity-grid {{
            display: grid;
            grid-template-columns: 1fr 1fr;
            gap: 16px;
            margin-top: 24px;
        }}
        @media (max-width: 700px) {{
            .activity-grid {{ grid-template-columns: 1fr; }}
            body {{ padding: 16px; }}
        }}
        .activity-card {{
            background: var(--bg-card);
            border: 1px solid var(--border);
            border-radius: 12px;
            padding: 20px;
        }}
        .activity-card h3 {{
            font-size: 14px;
            color: var(--text-dim);
            text-transform: uppercase;
            letter-spacing: 0.05em;
            margin-bottom: 12px;
        }}
        .activity-card ul {{
            list-style: none;
        }}
        .activity-card li {{
            padding: 8px 0;
            border-bottom: 1px solid var(--border);
            font-size: 13px;
        }}
        .activity-card li:last-child {{ border-bottom: none; }}
        .dim {{ color: var(--text-dim); font-size: 12px; }}
        footer {{
            margin-top: 48px;
            padding-top: 16px;
            border-top: 1px solid var(--border);
            color: var(--text-dim);
            font-size: 11px;
            text-align: center;
        }}
    </style>
</head>
<body>
    <header class="main-header">
        <div>
            <h1><span class="red-dot"></span>Asistente Revolv</h1>
            <p style="color: var(--text-dim); margin-top: 4px; font-size: 14px;">
                Dashboard de pendientes — {fecha}
            </p>
        </div>
        <div class="header-meta">
            <div>Última actualización: {hora}</div>
            <button class="refresh-btn" onclick="window.location.reload()">🔄 Recargar</button>
        </div>
    </header>

    <div class="stats-grid">
        <div class="stat-card">
            <div class="stat-label">Pendientes</div>
            <div class="stat-value pending">{total_pendientes}</div>
        </div>
        <div class="stat-card">
            <div class="stat-label">Clientes con pendientes</div>
            <div class="stat-value">{total_clientes_pend}</div>
        </div>
        <div class="stat-card">
            <div class="stat-label">Editores activos</div>
            <div class="stat-value">{editores_activos}</div>
        </div>
        <div class="stat-card">
            <div class="stat-label">Tareas cerradas (total)</div>
            <div class="stat-value done">{s['done_tasks']}</div>
        </div>
        <div class="stat-card">
            <div class="stat-label">Clientes monitoreados</div>
            <div class="stat-value">{s['clients']}</div>
        </div>
    </div>

    <h2 class="section-title">📦 Pendientes por editor</h2>
    {editor_blocks_html}

    <div class="activity-grid">
        <div class="activity-card">
            <h3>🆕 Últimos detectados</h3>
            <ul>{recent_html}</ul>
        </div>
        <div class="activity-card">
            <h3>✅ Últimos cerrados</h3>
            <ul>{closed_html}</ul>
        </div>
    </div>

    <footer>
        Asistente Revolv · datos del último scan en GitHub Actions ·
        para datos en vivo, hacé doble click en el ícono "Asistente Revolv" del Dock
    </footer>
</body>
</html>
"""


def run():
    data = get_data()
    html = build_html(data)
    with open(OUTPUT_PATH, "w") as f:
        f.write(html)
    print(f"✅ Dashboard generado en {OUTPUT_PATH}")
    print(f"   {data['stats']}")
    return OUTPUT_PATH


if __name__ == "__main__":
    run()

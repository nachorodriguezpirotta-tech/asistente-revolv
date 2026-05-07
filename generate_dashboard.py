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

# Orden fijo de editores en el menú y bloques. Los que no estén en esta
# lista van al final en orden alfabético.
EDITOR_ORDER = ["Rami", "Benja", "Fran", "Valen", "Santi", "Agus", "Samu"]


def _order_editors(all_editors: list[str]) -> list[str]:
    """Ordena editores: primero los de EDITOR_ORDER (en ese orden exacto),
    después los demás alfabéticamente."""
    in_order = [e for e in EDITOR_ORDER if e in all_editors]
    extras = sorted([e for e in all_editors if e not in EDITOR_ORDER])
    return in_order + extras


def get_data():
    conn = get_conn()

    # Pendientes agrupados por editor → cliente → archivos
    pending_rows = conn.execute("""
        SELECT id, editor, cliente, file_name, detected_at
        FROM tasks
        WHERE status = 'pending'
        ORDER BY editor, cliente, detected_at
    """).fetchall()

    by_editor = defaultdict(lambda: defaultdict(list))
    for r in pending_rows:
        editor = r["editor"] or "— sin editor en Sheet —"
        by_editor[editor][r["cliente"].strip()].append({
            "task_id": r["id"],
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

    editores_activos = len([e for e in by_editor if not e.startswith("—")])
    total_clientes_pend = sum(len(clientes) for clientes in by_editor.values())

    # Orden fijo de editores (definido al tope del módulo) + extras al final
    editores_ordenados = _order_editors(list(by_editor.keys()))

    # Generar tabs del menú
    tabs_html = '<button class="tab active" data-target="all" onclick="filterEditor(this, \'all\')">Todos</button>'
    for editor in editores_ordenados:
        slug = editor.lower().replace(" ", "-")
        tabs_html += f'<button class="tab" data-target="{slug}" onclick="filterEditor(this, \'{slug}\')">{editor}</button>'
    # Botón "+ Editor" al final de los tabs
    tabs_html += '<button class="tab tab-new" onclick="addEditor()" title="Agregar nuevo editor con su primer cliente pendiente">+ Editor</button>'

    editor_blocks = []
    for editor in editores_ordenados:
        clientes = by_editor[editor]
        clientes_html = ""
        for cliente in sorted(clientes.keys()):
            files = clientes[cliente]
            # task_id viene en cada file (lo agregamos en get_data)
            task_id = files[0]["task_id"]
            clientes_html += (
                f'<div class="cliente-card" data-task-id="{task_id}">'
                f'<span class="cliente-name">{cliente}</span>'
                f'<button class="delete-btn" onclick="deleteTask({task_id}, this)" title="Marcar como hecho">🗑️</button>'
                f'</div>'
            )
        slug = editor.lower().replace(" ", "-")
        editor_blocks.append(f"""
            <section class="editor-block" data-editor="{slug}">
                <header class="editor-header">
                    <h2>{editor}</h2>
                    <button class="add-btn" onclick="addClient('{editor}')" title="Agregar cliente pendiente">+</button>
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

    # Recent activity (sin números, solo cliente + editor + fecha)
    recent_html = "".join(
        f'<li><strong>{r["cliente"]}</strong> · {r["editor"]} <span class="dim">· {_human_date(r["at"])}</span></li>'
        for r in data["recent"]
    ) or '<li class="dim">Sin actividad reciente</li>'

    closed_html = "".join(
        f'<li><strong>{c["cliente"]}</strong> <span class="dim">· {_human_date(c["at"])}</span></li>'
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
        .tabs {{
            display: flex;
            gap: 6px;
            margin-bottom: 24px;
            flex-wrap: wrap;
            background: var(--bg-card);
            padding: 8px;
            border-radius: 12px;
            border: 1px solid var(--border);
        }}
        .tab {{
            background: transparent;
            color: var(--text-dim);
            border: none;
            padding: 10px 18px;
            font-size: 14px;
            font-weight: 500;
            border-radius: 8px;
            cursor: pointer;
            transition: background 0.15s, color 0.15s;
            font-family: inherit;
        }}
        .tab:hover {{
            background: var(--bg-card-2);
            color: var(--text);
        }}
        .tab.active {{
            background: var(--accent);
            color: white;
        }}
        .tab.tab-new {{
            border: 1px dashed var(--text-dim);
            background: transparent;
            color: var(--text-dim);
            margin-left: 8px;
        }}
        .tab.tab-new:hover {{
            border-color: var(--accent);
            color: var(--accent);
            background: transparent;
        }}
        .editor-block.hidden {{
            display: none;
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
        .add-btn {{
            background: var(--bg-card-2);
            color: var(--text-dim);
            border: 1px solid var(--border);
            width: 28px;
            height: 28px;
            border-radius: 50%;
            font-size: 18px;
            font-weight: 400;
            cursor: pointer;
            line-height: 1;
            display: inline-flex;
            align-items: center;
            justify-content: center;
            transition: background 0.15s, color 0.15s, transform 0.15s;
            font-family: inherit;
            padding: 0;
        }}
        .add-btn:hover {{
            background: var(--accent);
            color: white;
            transform: scale(1.1);
        }}
        .clientes-grid {{
            display: grid;
            gap: 8px;
        }}
        .cliente-card {{
            background: var(--bg-card-2);
            padding: 10px 16px;
            border-radius: 8px;
            font-size: 14px;
            display: flex;
            justify-content: space-between;
            align-items: center;
            transition: opacity 0.2s;
        }}
        .cliente-card.removing {{ opacity: 0; transform: translateX(20px); }}
        .cliente-name {{
            font-weight: 500;
            color: var(--text);
        }}
        .delete-btn {{
            background: transparent;
            border: none;
            color: var(--text-dim);
            font-size: 16px;
            cursor: pointer;
            padding: 4px 8px;
            border-radius: 6px;
            opacity: 0.4;
            transition: opacity 0.2s, background 0.2s;
        }}
        .cliente-card:hover .delete-btn {{ opacity: 1; }}
        .delete-btn:hover {{
            background: rgba(255, 71, 71, 0.15);
            opacity: 1;
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
            <div class="stat-label">Clientes con pendientes</div>
            <div class="stat-value pending">{total_clientes_pend}</div>
        </div>
        <div class="stat-card">
            <div class="stat-label">Editores activos</div>
            <div class="stat-value">{editores_activos}</div>
        </div>
        <div class="stat-card">
            <div class="stat-label">Clientes monitoreados</div>
            <div class="stat-value">{s['clients']}</div>
        </div>
    </div>

    <h2 class="section-title">📦 Pendientes por editor</h2>
    <div class="tabs">
        {tabs_html}
    </div>
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

    <script>
    async function addClient(editor) {{
        const cliente = prompt('Agregar cliente pendiente para ' + editor + ':');
        if (!cliente || !cliente.trim()) return;
        try {{
            const res = await fetch('/api/task', {{
                method: 'POST',
                headers: {{ 'Content-Type': 'application/json' }},
                body: JSON.stringify({{ cliente: cliente.trim(), editor: editor }})
            }});
            const data = await res.json();
            if (!res.ok || !data.ok) {{
                alert('No se pudo agregar:\\n' + (data.error || ('HTTP ' + res.status)));
                return;
            }}
            // Recargar la página para que aparezca el nuevo cliente
            window.location.reload();
        }} catch (e) {{
            alert('Error de red: ' + e.message + '\\n\\nAsegurate de haber abierto desde el ícono del Dock.');
        }}
    }}

    async function addEditor() {{
        const editor = prompt('Nombre del nuevo editor:');
        if (!editor || !editor.trim()) return;
        const cliente = prompt('Cliente pendiente para ' + editor.trim() + ':');
        if (!cliente || !cliente.trim()) return;
        try {{
            const res = await fetch('/api/task', {{
                method: 'POST',
                headers: {{ 'Content-Type': 'application/json' }},
                body: JSON.stringify({{ cliente: cliente.trim(), editor: editor.trim() }})
            }});
            const data = await res.json();
            if (!res.ok || !data.ok) {{
                alert('No se pudo agregar:\\n' + (data.error || ('HTTP ' + res.status)));
                return;
            }}
            window.location.reload();
        }} catch (e) {{
            alert('Error de red: ' + e.message + '\\n\\nAsegurate de haber abierto desde el ícono del Dock.');
        }}
    }}

    function filterEditor(btn, target) {{
        document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
        btn.classList.add('active');
        document.querySelectorAll('.editor-block').forEach(block => {{
            if (target === 'all' || block.dataset.editor === target) {{
                block.classList.remove('hidden');
            }} else {{
                block.classList.add('hidden');
            }}
        }});
    }}

    async function deleteTask(taskId, btn) {{
        if (!confirm('¿Marcar este pendiente como hecho?')) return;
        const card = btn.closest('.cliente-card');
        btn.disabled = true;
        try {{
            const res = await fetch('/api/task/' + taskId, {{ method: 'DELETE' }});
            if (!res.ok) throw new Error('HTTP ' + res.status);
            const data = await res.json();
            if (!data.ok) throw new Error(data.error || 'Error desconocido');
            // Animar y remover
            card.classList.add('removing');
            setTimeout(() => card.remove(), 250);
        }} catch (e) {{
            btn.disabled = false;
            alert('No se pudo eliminar:\\n' + e.message + '\\n\\nAsegurate de haber abierto el dashboard desde el ícono "Asistente Revolv" del Dock (no abrir el HTML directamente).');
        }}
    }}
    </script>
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

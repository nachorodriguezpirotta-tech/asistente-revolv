"""Conteo de entregas REALES por editor — fuente única para stats y dashboard.

Cuenta known_edited_files (TODOS, baseline incluidos: un baseline es un editado
real que entró sin mail), por fecha real de subida (created_time), deduplicando
correcciones (cliente+subfolder+stem), atribuido al editor del cliente.
Reemplaza el conteo viejo por completion-mails (perdía baseline) y por tasks-done
(contaba clientes, no videos). Bug Benja/Electro 16/jun: 77 entregados, contaba 6.
"""

def _norm_cli(s):
    import unicodedata
    s = unicodedata.normalize("NFD", s or "")
    s = "".join(c for c in s if unicodedata.category(c) != "Mn")
    return " ".join(s.lower().split())


def _stem(name):
    """Nombre sin extensión ni sufijos de re-entrega — para que una corrección
    (mismo video resubido) NO cuente como entrega nueva."""
    import re
    s = (name or "").lower().strip()
    s = re.sub(r"\.[a-z0-9]+$", "", s)
    for p in [r"\s*[-_]?\s*v\d+\s*$",
              r"\s*[-_]?\s*(ver|versi[oó]n|v\.)\s*\d+\s*$",
              r"\s*[-_]?\s*(final|corregido|corregida|correcci[oó]n|corr|fix|nuevo|nueva|edit|editado|editada)\s*$",
              r"\s*\(\d+\)\s*$"]:
        s = re.sub(p, "", s).strip()
    return " ".join(s.split())


def _cliente_editor_map(conn):
    """cliente_norm -> editor. Prioridad: cfg_client_editor (override manual) >
    cfg_excel_clients (Sheet) > último completion del mail_log. Con la conn
    FRESCA del endpoint (en Vercel tracker.get_conn es stale)."""
    m = {}
    try:
        for r in conn.execute("SELECT cliente, editor, MAX(sent_at) FROM mail_log "
                              "WHERE kind='completion' AND COALESCE(editor,'') NOT IN ('','—') GROUP BY cliente"):
            m[_norm_cli(r["cliente"])] = r["editor"]
    except Exception: pass
    try:
        for r in conn.execute("SELECT cliente, editor FROM cfg_excel_clients"):
            if (r["editor"] or "").strip(): m[_norm_cli(r["cliente"])] = r["editor"]
    except Exception: pass
    try:
        for r in conn.execute("SELECT cliente, editor FROM cfg_client_editor"):
            if (r["editor"] or "").strip(): m[_norm_cli(r["cliente"])] = r["editor"]
    except Exception: pass
    return m


def _delivered_by_editor(conn, since_iso, ce_map=None):
    """{editor: N} videos editados REALES entregados desde `since_iso`.
    Cuenta known_edited_files (TODOS, baseline incluidos — un baseline es un
    editado real, solo que entró sin mail), por fecha REAL de subida a Drive
    (created_time, fallback first_seen_at). Deduplica correcciones: agrupa por
    (cliente, subfolder, stem) → cada grupo = 1 entrega. Atribuye al editor del
    cliente. FIX 16/jun: antes contaba completion mails y se perdía todo lo que
    entró baseline (caso Benja/Electro: 77 entregados, contaba 6)."""
    if ce_map is None:
        ce_map = _cliente_editor_map(conn)
    rows = conn.execute("""
        SELECT cliente, name, subfolder_name,
               COALESCE(created_time, first_seen_at) as ts
        FROM known_edited_files
        WHERE COALESCE(created_time, first_seen_at) >= ?
    """, (since_iso,)).fetchall()
    seen = {}   # editor -> set de claves (cliente, subfolder, stem)
    for r in rows:
        cn = _norm_cli(r["cliente"])
        editor = ce_map.get(cn)
        if not editor:
            continue
        key = (cn, (r["subfolder_name"] or "").lower().strip(), _stem(r["name"]))
        seen.setdefault(editor, set()).add(key)
    return {ed: len(s) for ed, s in seen.items()}



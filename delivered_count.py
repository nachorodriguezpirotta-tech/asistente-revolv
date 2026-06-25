"""Conteo de entregas por editor — fuente: los COMPLETION MAILS (mail_log).

Cada mail "📹 X entregó Video Y de Z" = 1 video subido por el editor X. El editor
del mail es el REAL (identify_editor_by_owner), no adivinado por cliente. Pedido
Ignacio 18/jun: "arrancá a cargar las stats desde los mails — cada mail de que
subimos un video = +1 video de ese editor". Ventaja: coincide con lo que Ignacio
ve en su inbox + atribución correcta (antes, contar known_edited_files atribuía
por cliente→editor y fallaba: Rafa perdía 16 videos, aparecían editores fantasma).
Excluye correcciones (🔧 / 'correc'). Editor canonicalizado ('Adri'→'Adrian').
"""
_EXCLUDE_CORR = "AND subject NOT LIKE '%🔧%' AND LOWER(subject) NOT LIKE '%correc%'"


def _editors_active(conn):
    try:
        return [r["name"] for r in conn.execute("SELECT name FROM cfg_editors WHERE active=1").fetchall()]
    except Exception:
        return []


def _delivered_by_editor(conn, since_iso, ce_map=None):
    """{editor_canonico: N videos entregados desde since_iso} contando completion
    mails únicos (DISTINCT dedupe_key/msg_id/subject)."""
    try:
        from tracker import canonical_editor
    except Exception:
        canonical_editor = lambda n, e: n
    editors = _editors_active(conn)
    rows = conn.execute(
        f"""SELECT editor, COUNT(DISTINCT COALESCE(NULLIF(dedupe_key,''), msg_id, subject)) n
            FROM mail_log
            WHERE kind='completion' AND COALESCE(success,1)=1 AND sent_at >= ?
              AND COALESCE(editor,'') NOT IN ('','—') {_EXCLUDE_CORR}
            GROUP BY editor""", (since_iso,)).fetchall()
    out = {}
    for r in rows:
        e = canonical_editor(r["editor"], editors)
        out[e] = out.get(e, 0) + (r["n"] or 0)
    return out


def _delivery_events(conn, since_iso):
    """Lista de (editor_canonico, sent_at) de cada entrega — para horarios.
    Un row por mail completion único."""
    try:
        from tracker import canonical_editor
    except Exception:
        canonical_editor = lambda n, e: n
    editors = _editors_active(conn)
    rows = conn.execute(
        f"""SELECT editor, MIN(sent_at) sent_at
            FROM mail_log
            WHERE kind='completion' AND COALESCE(success,1)=1 AND sent_at >= ?
              AND COALESCE(editor,'') NOT IN ('','—') {_EXCLUDE_CORR}
            GROUP BY COALESCE(NULLIF(dedupe_key,''), msg_id, subject), editor""",
        (since_iso,)).fetchall()
    return [(canonical_editor(r["editor"], editors), r["sent_at"]) for r in rows if r["sent_at"]]

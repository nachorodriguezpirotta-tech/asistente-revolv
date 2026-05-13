"""
Aliases: mapeo de "nombre de carpeta en Drive" → "nombre real del cliente en el Sheet".

Sirve para cuando la carpeta en Drive se llama distinto al cliente del Sheet.
El sistema usa el nombre real para buscar editor, crear tarea, etc.

Cuando agregues un alias acá, en el próximo scan el sistema lo aplica.
"""

import unicodedata


# Aliases: "nombre normalizado de la carpeta en Drive" → "nombre del cliente real"
CLIENT_ALIASES = {
    "content roger rm founders": "Roger Marti",
    # Agregar más acá cuando aparezcan:
    # "nombre raro en drive": "Nombre Real Cliente",
}


# Apodos universales: válidos para cualquier editor.
CLIENT_NICKNAMES = {
    "delfi": "Delfina Orange Power",
    "pao": "Paola Maqueda",
    "cris": "Cristhian Fonseca",
    "roger": "Roger Marti",
    "jorge": "Jorge y Darien",
    "angel": "Electro Angel",
    "dani": "Daniel Ramirez",
    "cisco": "Cisco Amengual",
    # Agregar más universales acá
}


# Apodos que dependen del editor (mismo apodo, distinto cliente según quién edita).
# Clave: (apodo normalizado, editor normalizado)
CLIENT_NICKNAMES_BY_EDITOR = {
    ("rafa", "benja"): "Rafa Rojas",
    ("rafa", "rami"): "Rafa Elvram",
    ("rafa", "ramiro"): "Rafa Elvram",
    # Agregar más casos editor-específicos
}


# Emails de editores: cuando se setea, el editor también recibe sus mails
# (los mails siguen yendo a Ignacio adicionalmente).
EDITOR_EMAILS = {
    "Benja": "Guillermobenjaminrojas@gmail.com",
    "Fran": "francoelagar@gmail.com",
    "Rami": "ramirolema00@gmail.com",
    "Valen": "valencoto12@gmail.com",
    "Agus": "agustinbernalrdp@gmail.com",
    "Santi": "santyoficial2009@gmail.com",
    "Jere": "jeremart.1605@gmail.com",
    "Samu": "samueltm097@gmail.com",
    # Faltan: Lean, Jose, Lucho (otros editores que vi en el Sheet)
}


# Editores que reciben el resumen diario de 8am.
# Por ahora solo los 4 más activos. Los demás los agregamos cuando Ignacio diga.
DAILY_SUMMARY_EDITORS = {"Rami", "Fran", "Benja", "Valen"}


# Lista canónica de editores que SIEMPRE aparecen en el dashboard admin,
# aunque no tengan pendientes en ese momento.
EDITORS_LIST = ["Rami", "Benja", "Fran", "Valen", "Santi", "Agus", "Samu", "Jere"]


# Carpetas extra donde se entregan editados para ciertos clientes.
# Útil cuando el editor sube editados a una carpeta DISTINTA a la del cliente.
# Cliente → folder_id donde se entregan.
CLIENT_DELIVERY_FOLDERS = {
    "Rafa Elvram": "1PUIRQ80fV9ZffdhDnOz4sUJLCLr6bjDH",  # "Editados Nacho abril 2026"
    # Agregar más casos cuando aparezcan
}


# ─── DB-backed config loaders ─────────────────────────────────────────────
# Las constantes arriba (EDITOR_EMAILS, CLIENT_NICKNAMES, etc.) son SEED inicial.
# Una vez que la DB tiene datos en cfg_*, esas son la fuente de verdad.
# Las funciones de abajo prefieren DB, fallback a hardcoded si DB no disponible.

def _safe_db_call(fn, default):
    try:
        return fn()
    except Exception:
        return default


def get_editor_emails_runtime() -> dict:
    """{editor_name: email} desde DB. Fallback a hardcoded EDITOR_EMAILS."""
    from tracker import cfg_get_editor_emails
    db = _safe_db_call(cfg_get_editor_emails, None)
    return db if db is not None else dict(EDITOR_EMAILS)


def get_editors_list_runtime() -> list:
    """Lista de editores activos desde DB. Fallback a EDITORS_LIST hardcoded."""
    from tracker import cfg_get_editors_list
    db = _safe_db_call(cfg_get_editors_list, None)
    return db if db else list(EDITORS_LIST)


def get_daily_summary_editors_runtime() -> set:
    from tracker import cfg_get_daily_summary_editors
    db = _safe_db_call(cfg_get_daily_summary_editors, None)
    return db if db is not None else set(DAILY_SUMMARY_EDITORS)


def get_nicknames_runtime() -> dict:
    from tracker import cfg_get_nicknames
    db = _safe_db_call(cfg_get_nicknames, None)
    return db if db else dict(CLIENT_NICKNAMES)


def get_nicknames_by_editor_runtime() -> dict:
    from tracker import cfg_get_nicknames_by_editor
    db = _safe_db_call(cfg_get_nicknames_by_editor, None)
    return db if db else dict(CLIENT_NICKNAMES_BY_EDITOR)


def get_aliases_runtime() -> dict:
    from tracker import cfg_get_aliases
    db = _safe_db_call(cfg_get_aliases, None)
    return db if db else dict(CLIENT_ALIASES)


def get_delivery_folders_runtime() -> dict:
    from tracker import cfg_get_delivery_folders
    db = _safe_db_call(cfg_get_delivery_folders, None)
    return db if db else dict(CLIENT_DELIVERY_FOLDERS)


def get_editor_email(editor: str):
    """Devuelve el mail del editor si está configurado, None si no.
    Lee de la DB (fuente de verdad runtime) con fallback al hardcoded."""
    if not editor:
        return None
    emails = get_editor_emails_runtime()
    for k, v in emails.items():
        if _normalize(k) == _normalize(editor):
            return v
    return None


def resolve_nickname_static(text: str, editor: str = None) -> str:
    """
    Resuelve un apodo conocido al nombre real del cliente.
    Si el editor está dado, primero busca en nicknames_by_editor.
    Si no encuentra, busca en nicknames universales.
    Si no es apodo conocido, devuelve el original.
    Lee de DB con fallback al hardcoded.
    """
    if not text:
        return text
    norm = _normalize(text)

    nicknames_by_editor = get_nicknames_by_editor_runtime()
    nicknames = get_nicknames_runtime()

    # 1. Buscar en dict editor-específico
    if editor:
        norm_editor = _normalize(editor)
        key = (norm, norm_editor)
        if key in nicknames_by_editor:
            return nicknames_by_editor[key]

    # 2. Buscar en universal
    return nicknames.get(norm, text)


def _normalize(s: str) -> str:
    s = unicodedata.normalize("NFD", s)
    s = "".join(c for c in s if unicodedata.category(c) != "Mn")
    return " ".join(s.lower().split())


def resolve_alias(drive_folder_name: str) -> str:
    """
    Si el nombre de carpeta de Drive matchea con un alias conocido,
    devuelve el nombre real del cliente. Si no, devuelve el original.
    Lee de DB con fallback al hardcoded.
    """
    if not drive_folder_name:
        return drive_folder_name
    norm = _normalize(drive_folder_name)
    aliases = get_aliases_runtime()
    if norm in aliases:
        return aliases[norm]
    return drive_folder_name


def reverse_alias(cliente_real: str) -> list:
    """
    Dado el nombre real de un cliente, devuelve los nombres de carpeta de Drive
    que mapean a él. Lista vacía si no hay alias inverso.
    Lee de DB con fallback al hardcoded.
    """
    target = _normalize(cliente_real)
    aliases = get_aliases_runtime()
    return [drive_name for drive_name, real in aliases.items()
            if _normalize(real) == target]


if __name__ == "__main__":
    tests = [
        "Content Roger RM Founders",
        "content roger rm founders",
        "Ali Baig",
    ]
    for t in tests:
        print(f"  '{t}' → '{resolve_alias(t)}'")

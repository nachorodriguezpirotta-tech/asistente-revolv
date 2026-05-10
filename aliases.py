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


# Apodos conocidos: cuando Ignacio escribe el apodo en el dashboard, se resuelve al cliente real.
# Sirve para clientes que NO están en la DB local todavía (ej. cliente nuevo del Sheet).
CLIENT_NICKNAMES = {
    "delfi": "Delfina Orange Power",
    "pao": "Paola Maqueda",
    # Agregar más cuando aparezcan
}


def resolve_nickname_static(text: str) -> str:
    """Resuelve un apodo conocido al nombre real. Si no es apodo, devuelve el original."""
    if not text:
        return text
    norm = _normalize(text)
    return CLIENT_NICKNAMES.get(norm, text)


def _normalize(s: str) -> str:
    s = unicodedata.normalize("NFD", s)
    s = "".join(c for c in s if unicodedata.category(c) != "Mn")
    return " ".join(s.lower().split())


def resolve_alias(drive_folder_name: str) -> str:
    """
    Si el nombre de carpeta de Drive matchea con un alias conocido,
    devuelve el nombre real del cliente. Si no, devuelve el original.
    """
    if not drive_folder_name:
        return drive_folder_name
    norm = _normalize(drive_folder_name)
    if norm in CLIENT_ALIASES:
        return CLIENT_ALIASES[norm]
    return drive_folder_name


def reverse_alias(cliente_real: str) -> list:
    """
    Dado el nombre real de un cliente, devuelve los nombres de carpeta de Drive
    que mapean a él. Lista vacía si no hay alias inverso.
    """
    target = _normalize(cliente_real)
    return [drive_name for drive_name, real in CLIENT_ALIASES.items()
            if _normalize(real) == target]


if __name__ == "__main__":
    tests = [
        "Content Roger RM Founders",
        "content roger rm founders",
        "Ali Baig",
    ]
    for t in tests:
        print(f"  '{t}' → '{resolve_alias(t)}'")

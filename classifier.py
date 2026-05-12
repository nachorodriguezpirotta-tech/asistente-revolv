"""
Clasificador de videos: distingue CRUDOS (subidos por cliente) de EDITADOS (entregados por editor).

Estrategia: combina señales de varios tipos para decidir.
  - **Owner del archivo en Drive** (señal PRIMARIA): si el dueño/último-modificador
    es un editor conocido (EDITOR_EMAILS) → EDITADO. Cualquier otro mail → CRUDO.
  - Carpeta padre: "Material/Raw/Crudos" vs "Editados/Pack X/Tanda N/etc"
  - Nombre del archivo: patrones de cámara/celular vs nombres descriptivos numerados

Devuelve:
  True  → es editado (alto valor de confianza)
  False → es crudo (alto valor de confianza)
  None  → ambiguo (no podemos decidir solo con estas señales)

Filosofía: ser CONSERVADOR. Mejor "ambiguo" que falso positivo.
"""

import re
import unicodedata
from typing import Optional

from aliases import EDITOR_EMAILS

# Set de mails de editores en lowercase para matching rápido
_EDITOR_EMAILS_LOWER = {e.strip().lower() for e in EDITOR_EMAILS.values() if e}


def _normalize(s: str) -> str:
    s = unicodedata.normalize("NFD", s)
    s = "".join(c for c in s if unicodedata.category(c) != "Mn")
    return " ".join(s.lower().split())


# --- Carpetas padre ---

PARENT_CRUDO_NAMES = {
    "material", "raw", "crudos", "brutos", "originales",
    "footage", "material crudo", "crudo",
}

PARENT_EDITADO_NAMES = {
    "editados", "editado", "final", "finales", "entregables",
    "terminados", "listos", "deliveries", "delivered", "entregado",
    "entregas",
}

PACK_PATTERNS = [
    re.compile(r"^pack\s*\d+", re.I),
    re.compile(r"^tanda\s*\d+", re.I),
]


def _parent_signals(parent_name: Optional[str]) -> Optional[bool]:
    if not parent_name:
        return None
    p = _normalize(parent_name)
    if p in PARENT_CRUDO_NAMES:
        return False  # es crudo
    if p in PARENT_EDITADO_NAMES:
        return True  # es editado
    for pattern in PACK_PATTERNS:
        if pattern.match(p):
            return True
    return None


# --- Nombre de archivo ---

# Patrones típicos de archivos de cámara / celular → CRUDO
NAME_CRUDO_PATTERNS = [
    re.compile(r"^img_\d+", re.I),         # IMG_4123.mp4
    re.compile(r"^mvi_\d+", re.I),         # MVI_0234.mp4
    re.compile(r"^mov_\d+", re.I),
    re.compile(r"^dsc_\d+", re.I),
    re.compile(r"^vid_\d+", re.I),         # VID_20260504.mp4
    re.compile(r"^dji_\d+", re.I),         # DJI drone
    re.compile(r"^gx\d+", re.I),           # GoPro
    re.compile(r"^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}", re.I),  # UUID puro
    re.compile(r"^[a-f0-9]{8}_[a-f0-9]{4}", re.I),  # hashes
    re.compile(r"^hf_\d{8}", re.I),        # higgsfield
    re.compile(r"^\d{8}_\d{6}", re.I),     # timestamps tipo 20260504_150952
    re.compile(r"^copy_[a-f0-9]", re.I),   # copy_0FB540A1
    re.compile(r"^pxl_\d", re.I),          # Pixel
    re.compile(r"^whatsapp", re.I),        # WhatsApp Video
    re.compile(r"^screen.*record", re.I),  # Screen Recording
]

# Patrones típicos de archivos editados → EDITADO
NAME_EDITADO_PATTERNS = [
    re.compile(r"^\d+\s*[\.\-_]\s*[a-zñáéíóú]", re.I),  # "1. melesio", "16 - octavian", "01_natalia"
    re.compile(r"^video\s*\d+", re.I),                  # Video 1, Video 27
    re.compile(r"^reel\s*\d+", re.I),                   # Reel 4
    re.compile(r"^short\s*\d+", re.I),
    re.compile(r"final", re.I),                          # cualquier cosa con "final" en el nombre
    re.compile(r"editad", re.I),                         # "editado", "editada"
    re.compile(r"con\s*musica", re.I),                   # "con musica"
    re.compile(r"sin\s*musica", re.I),
]


def _name_signals(name: str) -> Optional[bool]:
    if not name:
        return None
    # Sacar extensión para evaluar el stem
    n = re.sub(r"\.[a-z0-9]+$", "", name, flags=re.I).strip()

    for p in NAME_CRUDO_PATTERNS:
        if p.search(n):
            return False
    for p in NAME_EDITADO_PATTERNS:
        if p.search(n):
            return True
    return None


def _owner_signal(file: dict) -> Optional[bool]:
    """Devuelve True si el dueño/último-modificador es un editor de Revolv (=editado).
    False si es claramente otro mail (=crudo). None si no hay info.

    Drive API expone:
      - owners[0].emailAddress → dueño del archivo
      - lastModifyingUser.emailAddress → último que lo modificó/subió
    """
    if not _EDITOR_EMAILS_LOWER:
        return None  # sin mails de editores configurados, no podemos decidir

    candidates = []
    for o in (file.get("owners") or []):
        em = (o.get("emailAddress") or "").strip().lower()
        if em:
            candidates.append(em)
    lm = (file.get("lastModifyingUser") or {}).get("emailAddress") or ""
    lm = lm.strip().lower()
    if lm:
        candidates.append(lm)

    if not candidates:
        return None  # sin owner info

    # Si CUALQUIERA de los candidatos es editor → editado (alta confianza)
    if any(em in _EDITOR_EMAILS_LOWER for em in candidates):
        return True

    # Si owner NO es editor conocido → NO podemos afirmar "es crudo" porque puede
    # ser un editor que no tenemos mapeado (Lean, Agus, Jose, Lucho, Santi, Samu, Jere...).
    # Caer al fallback de heurística por nombre/carpeta para evitar falsos positivos.
    return None


def classify(file: dict, parent_name: Optional[str] = None) -> Optional[bool]:
    """
    Clasifica un archivo de video.
    Retorna True (editado), False (crudo) o None (ambiguo).

    `file` es un dict de Drive API (al menos con key 'name').
                Si incluye `owners` y/o `lastModifyingUser`, se usa como señal PRIMARIA.
    `parent_name` es el nombre de la carpeta inmediatamente arriba.
    """
    # Señal PRIMARIA: owner del archivo. Si tenemos info confiable, override total.
    owner_sig = _owner_signal(file)
    if owner_sig is not None:
        return owner_sig

    # Fallback a heurísticas si no hay owner info
    parent_sig = _parent_signals(parent_name)
    name_sig = _name_signals(file.get("name", ""))

    # Si las dos señales coinciden → confianza máxima
    if parent_sig is not None and name_sig is not None:
        if parent_sig == name_sig:
            return parent_sig
        # Conflicto: parent dice una cosa, nombre otra. Priorizar parent
        # (porque la organización en carpetas es más explícita que el nombre).
        return parent_sig

    if parent_sig is not None:
        return parent_sig
    if name_sig is not None:
        return name_sig

    return None  # ambiguo


def is_likely_editado(file: dict, parent_name: Optional[str] = None) -> bool:
    """Versión binaria: ambiguo se trata como 'editado' por compatibilidad con código viejo."""
    result = classify(file, parent_name)
    if result is None:
        return True  # default permisivo
    return result


def is_likely_crudo(file: dict, parent_name: Optional[str] = None) -> bool:
    """Versión binaria: ambiguo se trata como 'crudo' (más conservador para detección de tareas nuevas)."""
    result = classify(file, parent_name)
    if result is None:
        return False
    return result is False


if __name__ == "__main__":
    # Tests
    cases = [
        # Owner-based (PRIMARIO) — override de todo
        ({"name": "1. melesio.mp4", "owners": [{"emailAddress": "cliente@gmail.com"}]},
         "Pack 1", False, "owner=cliente override pack/nombre"),
        ({"name": "IMG_4123.mp4", "owners": [{"emailAddress": "ramirolema00@gmail.com"}]},
         "Material", True, "owner=editor override material/IMG"),
        ({"name": "foo.mp4", "lastModifyingUser": {"emailAddress": "francoelagar@gmail.com"}},
         None, True, "lastModifying=editor"),
        ({"name": "foo.mp4", "owners": [{"emailAddress": "lilirohe@gmail.com"}]},
         None, False, "owner=cliente (no editor)"),
        # Heurísticas (fallback cuando NO hay owner)
        ({"name": "IMG_4123.mp4"}, None, False, "cámara"),
        ({"name": "MVI_0234.MOV"}, None, False, "Canon"),
        ({"name": "hf_20260504_150952_441b8d9d.mp4"}, None, False, "higgsfield hash"),
        ({"name": "1. melesio.mp4"}, None, True, "editado numerado"),
        ({"name": "Video 27.mp4"}, None, True, "video numerado"),
        ({"name": "16 - OCTAVIAN.mp4"}, None, True, "editado dash"),
        ({"name": "Reel 4.mp4"}, None, True, "reel"),
        ({"name": "foo.mp4"}, "Material", False, "parent material"),
        ({"name": "foo.mp4"}, "Editados", True, "parent editados"),
        ({"name": "foo.mp4"}, "Pack 1", True, "parent pack"),
        ({"name": "foo.mp4"}, "Mayo", None, "parent mes (ambiguo)"),
        ({"name": "VIDEO 15 CON MUSICA.mp4"}, None, True, "con musica"),
    ]
    for file, parent, expected, desc in cases:
        result = classify(file, parent)
        ok = "✅" if result == expected else "❌"
        print(f"  {ok} {desc:<25} {file['name']:<40} parent={parent!r:<15} → {result}")

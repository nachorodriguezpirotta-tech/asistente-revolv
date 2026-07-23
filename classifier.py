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
# Normalize Gmail dots/aliases (ramiro.lema00@gmail vs ramirolema00@gmail).
# Recalculamos esto en cada check con _normalize_email() definido más abajo.


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


def _normalize_email(em: str) -> str:
    """Normaliza email para comparación, respetando reglas de Gmail:
      - lowercase
      - quita puntos del local part SOLO si es @gmail.com / @googlemail.com
        (Gmail trata ab.c@gmail == abc@gmail como el mismo mailbox)
      - quita "+alias" del local part (ab+x@... → ab@...)

    Bug 26/may: ramiro.lema00@gmail.com (real owner del archivo) no matcheaba
    con ramirolema00@gmail.com (en cfg_editors) → identify_editor_by_owner
    retornaba None → fallback al Sheet → Sheet asignaba "Ale" a Jose Social
    Pulse Media (cliente de Rami) → mails mal atribuidos."""
    if not em:
        return ""
    em = em.strip().lower()
    if "@" not in em:
        return em
    local, _, domain = em.partition("@")
    # Strip +alias
    if "+" in local:
        local = local.split("+", 1)[0]
    # Gmail/Googlemail ignoran puntos en local part
    if domain in ("gmail.com", "googlemail.com"):
        local = local.replace(".", "")
    return f"{local}@{domain}"


def identify_editor_by_owner(file: dict) -> Optional[str]:
    """Si el owner del archivo es uno de los editores conocidos, retorna su NOMBRE.
    Útil para saber QUIÉN REALMENTE entregó un editado (puede no coincidir con
    el editor asignado a la task, por ejemplo cuando un editor cubre a otro).

    Retorna None si no se puede identificar (owner no es editor conocido, o
    no hay info de owner)."""
    from aliases import get_editor_emails_runtime
    emails_map = get_editor_emails_runtime()  # {Name: email}
    # Invertir: {email_normalized: Name} — normalize Gmail dots/+aliases
    by_email = {}
    for k, v in emails_map.items():
        if not v:
            continue
        by_email[_normalize_email(v)] = k
    # Cuentas SECUNDARIAS de editores (cfg_editor_extra_emails, espejo local):
    # p.ej. Santi también sube con francomartinezcorte71@gmail.com (23/jul, caso
    # Duna 2ª Tanda: 4 entregas silenciadas como "material del cliente").
    try:
        from tracker import get_conn as _gc
        _c = _gc()
        try:
            for r in _c.execute("SELECT email, editor FROM cfg_editor_extra_emails").fetchall():
                if r[0]:
                    by_email.setdefault(_normalize_email(r[0]), r[1])
        finally:
            _c.close()
    except Exception:
        pass
    if not by_email:
        return None
    for em in _get_owner_emails(file):
        if _normalize_email(em) in by_email:
            return by_email[_normalize_email(em)]
    return None


def looks_like_client_upload(file: dict) -> bool:
    """True si el archivo lo subió alguien que NO es un editor conocido — es
    decir, el CLIENTE subiendo material a la carpeta de editados (verificado
    20/jul: cada cuenta desconocida mapea a 1 solo cliente: Duna/eamnietoalberto,
    Iván/astranexgen, Luis/tigercutez...). Sirve para NO contar eso como entrega.

    SEGURO: solo devuelve True si HAY info de owner/lastModifier y ese owner no
    es editor. Sin info de owner → False (mantiene el comportamiento viejo, no
    descarta por las dudas)."""
    owners = file.get("owners") or []
    lm = (file.get("lastModifyingUser") or {}).get("emailAddress")
    if not owners and not lm:
        return False
    return identify_editor_by_owner(file) is None


def _get_owner_emails(file: dict) -> list[str]:
    """Devuelve la lista de mails relevantes del archivo (owners + lastModifyingUser), lowercase."""
    candidates = []
    for o in (file.get("owners") or []):
        em = (o.get("emailAddress") or "").strip().lower()
        if em:
            candidates.append(em)
    lm = (file.get("lastModifyingUser") or {}).get("emailAddress") or ""
    lm = lm.strip().lower()
    if lm and lm not in candidates:
        candidates.append(lm)
    return candidates


# Tokens demasiado genéricos para usarlos como signal del cliente.
# Si el nombre del cliente es "Roger Marti", token "marti" es útil, "roger" también.
# Pero "y", "de", "el" son ruido.
_STOPWORDS_TOKEN = {
    "de", "del", "el", "la", "los", "las", "y", "e", "o", "u", "a",
    "the", "and", "or", "of",
}


def _client_tokens(cliente_name: str) -> list[str]:
    """Tokens útiles del nombre del cliente para matchear contra local-parts de mail."""
    if not cliente_name:
        return []
    norm = _normalize(cliente_name)
    tokens = [t for t in norm.split() if len(t) >= 3 and t not in _STOPWORDS_TOKEN]
    return tokens


def _is_owner_the_client(file: dict, cliente_name: Optional[str]) -> bool:
    """¿El owner del archivo es el propio cliente? Dos estrategias:

    1. **EXACT match contra cfg_clients.email** (más confiable): si el cliente
       tiene mail registrado en cfg_clients, comparamos directo. Caso Asthend:
       el owner real es 'saracv126126@gmail.com' que no tiene parecido al
       nombre 'Asthend' — pero si está registrado en cfg_clients[Asthend].email,
       lo identificamos directo.

    2. **Fuzzy match por tokens** (fallback): tokens del nombre del cliente
       (>=3 chars, sin stopwords) contra el local-part del mail. Sirve para
       clientes que NO tienen mail registrado todavía.
    """
    if not cliente_name:
        return False
    owner_emails = _get_owner_emails(file)
    if not owner_emails:
        return False

    # 1) Match exacto contra cfg_clients (si está configurado)
    try:
        from tracker import cfg_get_client
        cfg = cfg_get_client(cliente_name)
        if cfg and cfg.get("email"):
            cfg_email = cfg["email"].strip().lower()
            if cfg_email and cfg_email in owner_emails:
                return True
    except Exception:
        # tracker no disponible (test directo del classifier) → skip
        pass

    # 2) Fuzzy match por tokens (heurística de respaldo)
    tokens = _client_tokens(cliente_name)
    if not tokens:
        return False
    for em in owner_emails:
        local = em.split("@", 1)[0]
        local_norm = _normalize(local).replace(".", "").replace("_", "").replace("-", "")
        for t in tokens:
            if t in local_norm:
                return True
            if len(t) >= 4 and t[:4] in local_norm:
                return True
            for i in range(4, min(len(local_norm), len(t)) + 1):
                if local_norm[:i] == t[:i]:
                    return True
                break
    return False


def _owner_signal(file: dict, cliente_name: Optional[str] = None) -> Optional[bool]:
    """Devuelve True si owner es editor conocido (=editado).
    Devuelve False si owner es el cliente (=crudo).
    None si no se puede determinar (caer al fallback heurístico).

    Drive API expone:
      - owners[0].emailAddress → dueño del archivo
      - lastModifyingUser.emailAddress → último que lo modificó/subió
    """
    candidates = _get_owner_emails(file)
    if not candidates:
        return None

    # 1) Editor conocido → EDITADO (alta confianza)
    _editor_norm = {_normalize_email(e) for e in _EDITOR_EMAILS_LOWER}
    if _editor_norm and any(_normalize_email(em) in _editor_norm for em in candidates):
        return True

    # 2) Owner matchea el nombre del cliente → CRUDO (cliente subió)
    if _is_owner_the_client(file, cliente_name):
        return False

    # 3) No sabemos → caer al fallback de heurística
    return None


def classify(file: dict, parent_name: Optional[str] = None,
             cliente_name: Optional[str] = None) -> Optional[bool]:
    """
    Clasifica un archivo de video. Retorna True (editado), False (crudo) o None (ambiguo).

    PRIORIDAD DE SEÑALES (de mayor a menor confianza):
      1. Editor conocido subió → EDITADO (override total). El cliente JAMÁS sube
         con cuenta del editor.
      2. Owner matchea el cliente (fuzzy) → CRUDO (override). Si el cliente
         mismo subió el archivo, ES MATERIAL por más que se llame "Video 1" —
         muchos clientes nombran sus crudos así (caso Shaila Ochoa, etc).
      3. Nombre del archivo claramente editado ('Video 15', 'Reel 4', '16 - X')
         → EDITADO. Aplica cuando el owner NO es el cliente (típicamente Drive
         de Revolv o un editor no-listado todavía).
      4. Nombre del archivo claramente crudo ('IMG_4123', 'MVI_0234', 'hf_xxx')
         → CRUDO. Patrón típico de cámara/celu.
      5. Carpeta padre → fallback (/Material/ → crudo, /Editados/ → editado).
    """
    # 1. Editor conocido → EDITADO (override total)
    candidates = _get_owner_emails(file)
    _editor_norm = {_normalize_email(e) for e in _EDITOR_EMAILS_LOWER}
    if _editor_norm and any(_normalize_email(em) in _editor_norm for em in candidates):
        return True

    # 2. Owner es el cliente (fuzzy) → CRUDO (override sobre nombre)
    # Si el cliente sube desde su cuenta, ES MATERIAL por más que se llame "Video 1".
    # Caso real: Shaila Ochoa sube crudos llamados "Video 1 - sabias que la IA"
    # desde shaila@shailaochoa.com. NO podemos confundirlo con un editado.
    if _is_owner_the_client(file, cliente_name):
        return False

    # 3 + 4. Nombre del archivo (señal fuerte cuando owner no matchea)
    name_sig = _name_signals(file.get("name", ""))
    if name_sig is not None:
        return name_sig

    # 5. Carpeta padre (fallback)
    parent_sig = _parent_signals(parent_name)
    if parent_sig is not None:
        return parent_sig

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
    # Tests del fuzzy match cliente
    print("== Fuzzy match owner vs cliente ==")
    fuzzy_cases = [
        ({"owners": [{"emailAddress": "electroangel@gmail.com"}]}, "Electro Angel", True, "electroangel + Electro Angel"),
        ({"owners": [{"emailAddress": "rogermart@gmail.com"}]}, "Roger Marti", True, "rogermart + Roger Marti"),
        ({"owners": [{"emailAddress": "jorgegonzalez@gmail.com"}]}, "Jorge y Darien", True, "jorge match Jorge"),
        ({"owners": [{"emailAddress": "lilirohe@gmail.com"}]}, "Liliana Rohenes", True, "rohe match Rohenes"),
        ({"owners": [{"emailAddress": "totallyunrelated@gmail.com"}]}, "Roger Marti", False, "unrelated NO match"),
        ({"owners": [{"emailAddress": "ramirolema00@gmail.com"}]}, "Roger Marti", False, "editor != cliente"),
    ]
    for f, cli, expected, desc in fuzzy_cases:
        result = _is_owner_the_client(f, cli)
        ok = "✅" if result == expected else "❌"
        print(f"  {ok} {desc:<40} → {result}")

    print("\n== classify() tests ==")
    # firma: (file, parent_name, cliente_name, expected, desc)
    cases_with_cliente = [
        # Fuzzy match cliente → CRUDO sin importar nombre/carpeta
        ({"name": "1. melesio.mp4", "owners": [{"emailAddress": "electroangel@gmail.com"}]},
         "Pack 1", "Electro Angel", False, "Electro Angel sube en Pack 1 → crudo por owner"),
        ({"name": "Video 5 final.mp4", "owners": [{"emailAddress": "jorge.gz@gmail.com"}]},
         "Editados", "Jorge y Darien", False, "Jorge sube en Editados → crudo por owner"),
        # Editor conocido: editado siempre
        ({"name": "IMG_4123.mp4", "owners": [{"emailAddress": "ramirolema00@gmail.com"}]},
         "Material", "Roger Marti", True, "editor sube IMG en Material → editado"),
    ]
    for f, pn, cli, expected, desc in cases_with_cliente:
        result = classify(f, parent_name=pn, cliente_name=cli)
        ok = "✅" if result == expected else "❌"
        print(f"  {ok} {desc:<45} → {result}")

    # Tests legacy (sin cliente_name, fallback heurístico)
    print("\n== classify() tests (fallback heurístico, sin cliente_name) ==")
    cases = [
        ({"name": "foo.mp4", "lastModifyingUser": {"emailAddress": "francoelagar@gmail.com"}},
         None, True, "lastModifying=editor"),
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

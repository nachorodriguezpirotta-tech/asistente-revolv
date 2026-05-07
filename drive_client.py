"""
Cliente Drive — versión simplificada para el modelo nuevo.

Lo único que importa:
  1. Encontrar todas las carpetas de cliente (las que tienen subcarpeta Material/Raw/Crudos)
  2. Listar archivos en /Material/ de un cliente
  3. Listar archivos editados (todo lo que NO está en /Material/, recursivamente)
"""

import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from googleapiclient.discovery import build

from config import VIDEO_EXTS, RAW_SUBFOLDER_NAMES
from auth import get_credentials


@dataclass
class ClientFolder:
    cliente: str           # nombre de carpeta tal cual está en Drive
    folder_id: str
    raw_folder_id: Optional[str] = None  # id de subcarpeta Material/Raw/Crudos


def _normalize(s: str) -> str:
    s = unicodedata.normalize("NFD", s)
    s = "".join(c for c in s if unicodedata.category(c) != "Mn")
    return " ".join(s.lower().split())


def _is_video(name: str, mime: str = "") -> bool:
    if Path(name).suffix.lower() in VIDEO_EXTS:
        return True
    return mime.startswith("video/")


_service_cache = None


def get_service():
    global _service_cache
    if _service_cache is None:
        creds = get_credentials()
        _service_cache = build("drive", "v3", credentials=creds, cache_discovery=False)
    return _service_cache


def _list_subfolders(parent_id: str) -> list[dict]:
    service = get_service()
    folders = []
    page_token = None
    while True:
        res = service.files().list(
            q=f"'{parent_id}' in parents and mimeType='application/vnd.google-apps.folder' and trashed=false",
            fields="nextPageToken, files(id, name)",
            pageSize=200,
            pageToken=page_token,
        ).execute()
        folders.extend(res.get("files", []))
        page_token = res.get("nextPageToken")
        if not page_token:
            break
    return folders


def _list_files(parent_id: str, only_videos: bool = False) -> list[dict]:
    """Lista archivos directos (no recursivo) de una carpeta."""
    service = get_service()
    files = []
    page_token = None
    while True:
        res = service.files().list(
            q=f"'{parent_id}' in parents and trashed=false and mimeType != 'application/vnd.google-apps.folder'",
            fields="nextPageToken, files(id, name, mimeType, size, createdTime, modifiedTime, md5Checksum)",
            pageSize=200,
            pageToken=page_token,
        ).execute()
        files.extend(res.get("files", []))
        page_token = res.get("nextPageToken")
        if not page_token:
            break
    if only_videos:
        files = [f for f in files if _is_video(f["name"], f.get("mimeType", ""))]
    return files


def list_root_folders() -> list[dict]:
    """Lista todas las carpetas en la raíz de Mi Unidad."""
    service = get_service()
    folders = []
    page_token = None
    while True:
        res = service.files().list(
            q="'root' in parents and mimeType='application/vnd.google-apps.folder' and trashed=false",
            fields="nextPageToken, files(id, name)",
            pageSize=200,
            pageToken=page_token,
        ).execute()
        folders.extend(res.get("files", []))
        page_token = res.get("nextPageToken")
        if not page_token:
            break
    return folders


def _resolve_shortcut(folder: dict) -> dict:
    """Si la carpeta es un shortcut, devuelve el folder real al que apunta."""
    if folder.get("mimeType") == "application/vnd.google-apps.shortcut":
        target_id = folder.get("shortcutDetails", {}).get("targetId")
        if target_id:
            return {"id": target_id, "name": folder["name"], "mimeType": "application/vnd.google-apps.folder"}
    return folder


_STOPWORDS = {"y", "de", "la", "el", "los", "las", "del", "videos", "video", "reels", "reel"}


def _split_camel(s: str) -> str:
    """Separa CamelCase: 'TheEye' → 'The Eye', 'iPhoneCase' → 'i Phone Case'."""
    import re as _re
    return _re.sub(r"([a-z])([A-Z])", r"\1 \2", s)


def _tokens(s: str) -> set[str]:
    """Normaliza y tokeniza un nombre. Tokens >=3 chars y no-stopwords.
    Maneja CamelCase: 'TheEye' se trata como 'The Eye'."""
    s = _split_camel(s)
    norm = _normalize(s)
    return {t for t in norm.split() if len(t) >= 3 and t not in _STOPWORDS}


def find_folder_by_name(name: str, all_folders: Optional[list[dict]] = None) -> Optional[dict]:
    """
    Busca una carpeta/shortcut por nombre con varias estrategias:
      1. Match exacto normalizado
      2. Substring (uno solo)
      3. Starts-with (uno solo)
      4. Tokens compartidos: el candidato con MÁS tokens del target en común gana,
         si esa cantidad es >=2 y único.
    Resuelve shortcuts automáticamente.
    Retorna None si NO hay match claro (ambiguo).
    """
    if all_folders is None:
        all_folders = _list_root_items_with_shortcuts()
    target = _normalize(name)
    if not target:
        return None

    # 1. Match exacto
    for f in all_folders:
        if _normalize(f["name"]) == target:
            return _resolve_shortcut(f)

    # 2. Substring
    candidates = [f for f in all_folders if target in _normalize(f["name"])]
    if len(candidates) == 1:
        return _resolve_shortcut(candidates[0])

    # 3. Starts-with
    candidates = [f for f in all_folders if _normalize(f["name"]).startswith(target)]
    if len(candidates) == 1:
        return _resolve_shortcut(candidates[0])

    # 4. Tokens compartidos (con matching exacto Y por prefijo común >=4 chars)
    target_tokens = _tokens(name)
    if len(target_tokens) >= 1:
        scored = []
        for f in all_folders:
            f_tokens = _tokens(f["name"])
            shared = _count_token_overlap(target_tokens, f_tokens)
            if shared > 0:
                scored.append((shared, f))
        if scored:
            scored.sort(key=lambda x: -x[0])
            best_score = scored[0][0]
            top = [f for s, f in scored if s == best_score]
            min_required = 2 if len(target_tokens) >= 2 else 1
            if len(top) == 1 and best_score >= min_required:
                return _resolve_shortcut(top[0])

    return None


def _count_token_overlap(a: set[str], b: set[str]) -> int:
    """
    Cuenta cuántos tokens de A 'matchean' con tokens de B.
    Match = tokens iguales O comparten prefijo de >=4 caracteres.
    Ej: 'liliana' matchea con 'lili' (prefijo "lili"), 'rohenes' con 'rohe'.
    """
    count = 0
    for ta in a:
        for tb in b:
            if ta == tb:
                count += 1
                break
            # Prefijo común >= 4 chars (y al menos uno de los dos tiene >= 4 chars)
            common_prefix = 0
            for ca, cb in zip(ta, tb):
                if ca == cb:
                    common_prefix += 1
                else:
                    break
            if common_prefix >= 4 and (len(ta) >= 4 or len(tb) >= 4):
                count += 1
                break
    return count


def _list_root_items_with_shortcuts() -> list[dict]:
    """Lista carpetas Y shortcuts a carpetas en la raíz de Mi Unidad."""
    service = get_service()
    items = []
    page_token = None
    while True:
        res = service.files().list(
            q=("'root' in parents and trashed=false and ("
               "mimeType='application/vnd.google-apps.folder' or "
               "(mimeType='application/vnd.google-apps.shortcut')"
               ")"),
            fields="nextPageToken, files(id, name, mimeType, shortcutDetails)",
            pageSize=200,
            pageToken=page_token,
        ).execute()
        for f in res.get("files", []):
            # Solo shortcuts a folders, no a archivos
            if f["mimeType"] == "application/vnd.google-apps.shortcut":
                if f.get("shortcutDetails", {}).get("targetMimeType") != "application/vnd.google-apps.folder":
                    continue
            items.append(f)
        page_token = res.get("nextPageToken")
        if not page_token:
            break
    return items


def find_raw_subfolder(client_folder_id: str) -> Optional[dict]:
    """Busca la subcarpeta 'Material' (o Raw, Crudos) dentro de la carpeta del cliente."""
    for f in _list_subfolders(client_folder_id):
        if _normalize(f["name"]) in RAW_SUBFOLDER_NAMES:
            return f
    return None


def discover_client_folders() -> list[ClientFolder]:
    """
    Recorre la raíz de Mi Unidad y devuelve todas las carpetas que parecen ser
    de cliente (las que tienen subcarpeta Material/Raw/Crudos).
    Esto es la forma más robusta: no dependemos del Sheet ni del nombre exacto.
    """
    candidates = list_root_folders()
    clients: list[ClientFolder] = []
    for folder in candidates:
        raw = find_raw_subfolder(folder["id"])
        if raw is None:
            continue
        clients.append(ClientFolder(
            cliente=folder["name"],
            folder_id=folder["id"],
            raw_folder_id=raw["id"],
        ))
    return clients


def list_material_files(raw_folder_id: str) -> list[dict]:
    """Lista archivos (videos) dentro de la carpeta /Material/ de un cliente."""
    return _list_files(raw_folder_id, only_videos=True)


def list_crudos_anywhere(client_folder_id: str, client_folder_name: Optional[str] = None) -> list[dict]:
    """
    Detecta crudos en CUALQUIER lugar de la carpeta del cliente, sin requerir /Material/.

    Estrategia:
      1. Si hay subcarpeta Material/Raw/Crudos: archivos ahí son crudos.
      2. Si hay subcarpetas tipo "mes" (mayo, abril...) con Crudos adentro: capturar también.
      3. Para archivos en otras subcarpetas y en raíz del cliente: aplicar clasificador
         (is_likely_crudo) — solo se consideran crudos si el clasificador está SEGURO
         (NO los ambiguos, para evitar falsos positivos).
    """
    from classifier import classify

    crudos: list[dict] = []

    # 1) Subcarpeta directa Material/Raw/Crudos
    raw = find_raw_subfolder(client_folder_id)
    if raw:
        for f in _list_files(raw["id"], only_videos=True):
            crudos.append(f)

    # 2) Recursivo en subcarpetas: si encuentra otra Material/Raw/Crudos en niveles más profundos
    for sub in _list_subfolders(client_folder_id):
        if raw and sub["id"] == raw["id"]:
            continue
        if _normalize(sub["name"]) in RAW_SUBFOLDER_NAMES:
            # debería estar capturado en #1 pero por las dudas
            for f in _list_files(sub["id"], only_videos=True):
                crudos.append(f)
            continue
        # Buscar subcarpetas anidadas tipo "Mayo/Crudos"
        for nested in _list_subfolders(sub["id"]):
            if _normalize(nested["name"]) in RAW_SUBFOLDER_NAMES:
                for f in _list_files(nested["id"], only_videos=True):
                    crudos.append(f)

    # 3) Archivos en raíz del cliente.
    #    - Si el cliente TIENE Material/Raw: solo CRUDOS seguros (los editados van afuera).
    #    - Si NO tiene Material: incluimos también los AMBIGUOS porque pueden ser crudos
    #      subidos sueltos (los EDITADOS sí los descartamos por nombre).
    has_material_subfolder = raw is not None
    for f in _list_files(client_folder_id, only_videos=True):
        sig = classify(f, parent_name=client_folder_name)
        if sig is False:  # crudo seguro
            crudos.append(f)
        elif sig is None and not has_material_subfolder:
            # Ambiguo en cliente sin /Material/ → tratamos como crudo
            crudos.append(f)
        # sig is True (editado seguro) → ignorar

    return crudos


def list_edited_files(client_folder_id: str, raw_folder_id: Optional[str],
                       client_folder_name: Optional[str] = None) -> list[dict]:
    """
    Lista videos editados dentro de la carpeta del cliente, recursivamente.

    Lógica:
      - EXCLUYE subcarpetas de crudos (Material/Raw/Crudos) en cualquier nivel.
      - En la RAÍZ del cliente:
          * Si TIENE /Material/: ambiguos en raíz se asumen editados (default permisivo)
          * Si NO TIENE /Material/: ambiguos en raíz son CRUDOS (no editados),
            porque ahí es donde el cliente sube material. Solo cuentan los EDITADOS
            seguros (Video N, Reel N, etc.).
      - En subcarpetas (Pack/Tanda/Editados/etc): ambiguos cuentan como editados.

    Esta lógica es coherente con list_crudos_anywhere para evitar que un archivo
    aparezca como crudo Y editado simultáneamente.
    """
    from classifier import classify, is_likely_editado

    has_material = raw_folder_id is not None
    edited: list[dict] = []

    # Videos directos en raíz del cliente
    direct = _list_files(client_folder_id, only_videos=True)
    for f in direct:
        sig = classify(f, parent_name=client_folder_name)
        if sig is True:  # editado seguro
            edited.append(f)
        elif sig is None and has_material:
            # Ambiguo en cliente CON Material → tratamos como editado (los crudos
            # están en /Material/, lo que está afuera asumimos editado)
            edited.append(f)
        # sig is False (crudo seguro) → no es editado
        # sig is None y NO has_material → tratado como crudo en list_crudos_anywhere

    # Recurrir en subcarpetas (no crudos)
    for sub in _list_subfolders(client_folder_id):
        if raw_folder_id and sub["id"] == raw_folder_id:
            continue
        if _normalize(sub["name"]) in RAW_SUBFOLDER_NAMES:
            continue
        edited.extend(_list_recursive_videos(sub["id"], parent_name=sub["name"]))
    return edited


def _list_recursive_videos(folder_id: str, parent_name: Optional[str] = None) -> list[dict]:
    """
    Lista videos editados recursivamente, EXCLUYENDO subcarpetas Material/Raw/Crudos
    Y filtrando por clasificador (excluye archivos que parecen crudos por nombre).
    """
    from classifier import is_likely_editado

    out = []
    files = _list_files(folder_id, only_videos=True)
    for f in files:
        if is_likely_editado(f, parent_name=parent_name):
            out.append(f)
    for sub in _list_subfolders(folder_id):
        if _normalize(sub["name"]) in RAW_SUBFOLDER_NAMES:
            continue
        out.extend(_list_recursive_videos(sub["id"], parent_name=sub["name"]))
    return out


if __name__ == "__main__":
    print("📁 Descubriendo carpetas de cliente (las que tienen Material/Raw/Crudos)...")
    clients = discover_client_folders()
    print(f"   {len(clients)} carpetas de cliente detectadas en Mi Unidad.\n")
    for c in sorted(clients, key=lambda x: x.cliente.lower())[:20]:
        materials = list_material_files(c.raw_folder_id)
        print(f"   {c.cliente:<35} → {len(materials)} archivos en /Material/")
    if len(clients) > 20:
        print(f"   ... y {len(clients) - 20} más")

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
    # Rechazar AppleDouble / macOS resource forks (archivos ocultos tipo ._C2119.MP4
    # que aparecen cuando se copian videos desde Mac a Drive). Aunque tienen
    # extensión .MP4 no son videos reales, son metadata. Bug visto el 12/may
    # cuando Jose Social Pulse Media generó 354 "editados" en un día — 346 eran ._
    if name.startswith("._"):
        return False
    if Path(name).suffix.lower() in VIDEO_EXTS:
        return True
    return mime.startswith("video/")


def _is_raw_subfolder_name(name: str) -> bool:
    """¿Es nombre de carpeta de material crudo?

    Match por exact + prefijo. Cubre variantes reales encontradas en Drive:
      "Material" ✅ (exact)
      "Material VIDEOS REELS" ✅ (caso Ely Fitness)
      "Material (12/14)" ✅ (varios clientes con contadores)
      "Material - Abril 2025" ✅ (Alejanro Araya tiene 5 así)
      "Materiales" ✅ (plural, Javier Peñalver)
      "Material de apoyo" ✅
      "Raw footage" ✅
      "Crudos viejos" ✅
      "Material gráfico" ⚠️ (no es video pero ningún cliente lo tiene; si aparece, lista negra explícita)

    Antes el match era EXACT contra {"material","raw","crudos","material crudo"}
    y dejaba afuera al 65% de los clientes (87/134).
    """
    n = _normalize(name)
    if n in RAW_SUBFOLDER_NAMES:
        return True
    # Prefix match. "material" cubre "materiales", "material videos reels",
    # "material - abril 2025", "material (12/14)", "material de apoyo", etc.
    if n.startswith("material") or n.startswith("crudo"):
        return True
    # "raw" más restrictivo (palabra exacta o seguida de algo) para evitar
    # falsos positivos con palabras que empiecen con raw-.
    if n == "raw" or n.startswith("raw "):
        return True
    return False


import threading
_thread_local = threading.local()


def get_service():
    """Service de Drive por-thread (necesario para que ThreadPoolExecutor sea seguro:
    el objeto http subyacente de googleapiclient no es thread-safe)."""
    if not hasattr(_thread_local, "service"):
        creds = get_credentials()
        _thread_local.service = build("drive", "v3", credentials=creds, cache_discovery=False)
    return _thread_local.service


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
    """Lista archivos directos (no recursivo) de una carpeta.
    Cada file incluye `_parent_id` con el folder_id desde el que se listó (para tracking)."""
    service = get_service()
    files = []
    page_token = None
    while True:
        res = service.files().list(
            q=f"'{parent_id}' in parents and trashed=false and mimeType != 'application/vnd.google-apps.folder'",
            fields="nextPageToken, files(id, name, mimeType, size, createdTime, modifiedTime, md5Checksum, owners(emailAddress), lastModifyingUser(emailAddress))",
            pageSize=200,
            pageToken=page_token,
        ).execute()
        for f in res.get("files", []):
            f["_parent_id"] = parent_id  # tracking: carpeta donde encontramos este archivo
            files.append(f)
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
    """Busca la subcarpeta 'Material' (o Raw, Crudos) dentro de la carpeta del cliente.

    Usa prefix match (`_is_raw_subfolder_name`) para detectar variantes como
    'Material VIDEOS REELS', 'Material (12/14)', 'Materiales', etc. Antes
    hacía exact match y dejaba el 65% de los clientes sin raw_folder_id.
    """
    for f in _list_subfolders(client_folder_id):
        if _is_raw_subfolder_name(f["name"]):
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
    """
    Lista videos dentro de /Material/ de un cliente.

    Cada archivo retornado tiene un campo extra `_subfolder_name`:
      - '' si está directo en /Material/ (root)
      - 'NombreDeLaSubfolder' si está dentro de una subfolder directa
        (ej. 'V63 - Autos Abraham' para archivos de Alberto Carretero)
      - Mantiene el nombre del primer nivel aunque esté anidado más profundo
        (ej. /Material/V63/sub/x.mov → _subfolder_name = 'V63 - Autos Abraham')

    Incluye:
      - Videos directos en /Material/ (_subfolder_name = '')
      - Videos dentro de subcarpetas de /Material/ (_subfolder_name = nombre subfolder)
      - Videos dentro de SHORTCUTS a carpetas que estén en /Material/
    """
    service = get_service()
    files = []

    # 1. Videos directos en root → _subfolder_name = ''
    for f in _list_files(raw_folder_id, only_videos=True):
        f["_subfolder_name"] = ""
        files.append(f)

    # 2. Subcarpetas (recursivo) → marcar con nombre de la subfolder de nivel 1
    for sub in _list_subfolders(raw_folder_id):
        sub_name = sub["name"]
        for f in _list_recursive_videos_all(sub["id"]):
            f["_subfolder_name"] = sub_name
            files.append(f)

    # 3. Shortcuts a carpetas → marcar con nombre del shortcut como subfolder virtual
    res = service.files().list(
        q=f"'{raw_folder_id}' in parents and trashed=false and mimeType='application/vnd.google-apps.shortcut'",
        fields="files(id, name, shortcutDetails)",
        pageSize=100,
    ).execute()
    for sc in res.get("files", []):
        details = sc.get("shortcutDetails", {})
        target_id = details.get("targetId")
        target_mime = details.get("targetMimeType")
        if target_id and target_mime == "application/vnd.google-apps.folder":
            for f in _list_recursive_videos_all(target_id):
                f["_subfolder_name"] = sc["name"]
                files.append(f)
        elif target_id and (target_mime or "").startswith("video/"):
            try:
                f = service.files().get(fileId=target_id,
                                        fields="id, name, mimeType, size, createdTime, modifiedTime, owners(emailAddress), lastModifyingUser(emailAddress)").execute()
                f["_subfolder_name"] = ""
                files.append(f)
            except Exception:
                pass

    return files


def _list_recursive_videos_all(folder_id: str) -> list[dict]:
    """Lista TODOS los videos recursivamente (sin filtrar por crudo/editado)."""
    out = []
    out.extend(_list_files(folder_id, only_videos=True))
    for sub in _list_subfolders(folder_id):
        out.extend(_list_recursive_videos_all(sub["id"]))
    return out


def estimate_pending_videos(raw_folder_id: Optional[str], client_folder_id: Optional[str] = None) -> int:
    """
    Estima cuántos videos lógicos hay esperando entrega.

    Heurística:
      - Cada subcarpeta dentro de /Material/ que tiene AL MENOS UN archivo NO conocido
        (no está aún en known_files) = 1 video pendiente.
      - Subcarpetas con TODOS los archivos ya procesados (videos viejos que ya
        fueron contados/entregados) = NO se cuentan.
      - Archivos sueltos en raíz de /Material/ NO conocidos → agrupados por
        sesión temporal (±1 hora = 1 video).
      - Si NO hay /Material/ pero el cliente tiene archivos en raíz: 1 video.

    Esto resuelve el caso: Natalia tiene Reel 1..12, ya entregó videos en
    Reel 1..11, sube 1 archivo nuevo a Reel 12 → cuenta 1 (no 12).
    """
    from datetime import datetime, timedelta
    from tracker import is_file_known

    def _parse(s):
        if not s:
            return None
        try:
            return datetime.fromisoformat(s.replace("Z", "+00:00").split(".")[0] + "+00:00")
        except Exception:
            return None

    def _has_unknown_file(folder_id: str) -> bool:
        """¿La carpeta tiene al menos un archivo de video que NO esté ya conocido?"""
        try:
            files = _list_files(folder_id, only_videos=True)
            for f in files:
                if not is_file_known(f["id"]):
                    return True
            return False
        except Exception:
            # Si falla el listado, ser conservador: NO contar (mejor que sobre-contar)
            return False

    if not raw_folder_id:
        # Sin /Material/: si hay actividad reciente NO conocida en raíz, 1 video
        if client_folder_id:
            try:
                recent = _list_files(client_folder_id, only_videos=True)
                for f in recent:
                    if not is_file_known(f["id"]):
                        return 1
            except Exception:
                pass
        return 0

    # 1. Subcarpetas dentro de /Material/ con archivos no conocidos
    subfolders = _list_subfolders(raw_folder_id)
    sub_count = sum(1 for sf in subfolders if _has_unknown_file(sf["id"]))

    # Shortcuts a carpetas: idem
    service = get_service()
    sc_res = service.files().list(
        q=f"'{raw_folder_id}' in parents and trashed=false and mimeType='application/vnd.google-apps.shortcut'",
        fields="files(id, name, shortcutDetails)",
        pageSize=100,
    ).execute()
    for sc in sc_res.get("files", []):
        details = sc.get("shortcutDetails", {})
        if details.get("targetMimeType") == "application/vnd.google-apps.folder":
            target = details.get("targetId")
            if target and _has_unknown_file(target):
                sub_count += 1

    # 2. Archivos sueltos NO CONOCIDOS en raíz de /Material/ → agrupar por sesión temporal
    files = _list_files(raw_folder_id, only_videos=True)
    unknown_files = [f for f in files if not is_file_known(f["id"])]
    times = sorted(
        [t for t in (_parse(f.get("createdTime")) for f in unknown_files) if t is not None]
    )
    sessions = 0
    last_t = None
    for t in times:
        if last_t is None or (t - last_t) > timedelta(hours=1):
            sessions += 1
        last_t = t

    files_without_time = sum(1 for f in unknown_files if not f.get("createdTime"))
    if files_without_time and sessions == 0:
        sessions = 1

    return sub_count + sessions


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
        if _is_raw_subfolder_name(sub["name"]):
            # debería estar capturado en #1 pero por las dudas
            for f in _list_files(sub["id"], only_videos=True):
                crudos.append(f)
            continue
        # Buscar subcarpetas anidadas tipo "Mayo/Crudos"
        for nested in _list_subfolders(sub["id"]):
            if _is_raw_subfolder_name(nested["name"]):
                for f in _list_files(nested["id"], only_videos=True):
                    crudos.append(f)

    # 3) Archivos en raíz del cliente.
    #    - Si el cliente TIENE Material/Raw: solo CRUDOS seguros (los editados van afuera).
    #    - Si NO tiene Material: incluimos también los AMBIGUOS porque pueden ser crudos
    #      subidos sueltos (los EDITADOS sí los descartamos por nombre).
    has_material_subfolder = raw is not None
    for f in _list_files(client_folder_id, only_videos=True):
        sig = classify(f, parent_name=client_folder_name, cliente_name=client_folder_name)
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
        sig = classify(f, parent_name=client_folder_name, cliente_name=client_folder_name)
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
        if _is_raw_subfolder_name(sub["name"]):
            continue
        edited.extend(_list_recursive_videos(sub["id"], parent_name=sub["name"],
                                              cliente_name=client_folder_name))
    return edited


def _list_recursive_videos(folder_id: str, parent_name: Optional[str] = None,
                            cliente_name: Optional[str] = None) -> list[dict]:
    """
    Lista videos editados recursivamente, EXCLUYENDO subcarpetas Material/Raw/Crudos
    Y filtrando por clasificador (excluye archivos que parecen crudos por nombre).

    cliente_name: si se pasa, se usa para el chequeo owner-vs-cliente del
    classifier (override total para que el cliente subiendo crudos a
    subcarpetas NO se confunda con editado). Bug 26/may: REEL 5.MOV de
    Asthend en subfolder se contaba como editado porque sin cliente_name
    el classifier caía al name pattern "^reel\\s*\\d+" → editado.
    """
    from classifier import classify

    out = []
    files = _list_files(folder_id, only_videos=True)
    for f in files:
        sig = classify(f, parent_name=parent_name, cliente_name=cliente_name)
        # is_likely_editado equivalent: True/None → editado, False → crudo
        if sig is False:
            continue  # owner = cliente → CRUDO, no contamos como editado
        out.append(f)
    for sub in _list_subfolders(folder_id):
        if _is_raw_subfolder_name(sub["name"]):
            continue
        out.extend(_list_recursive_videos(sub["id"], parent_name=sub["name"],
                                           cliente_name=cliente_name))
    return out


# ─── Drive Changes API (incremental scan) ────────────────────────────────────

def get_start_page_token() -> str:
    """Obtiene el page token actual de Drive (marca el estado HOY).
    Cualquier cambio futuro se puede listar pidiendo desde este token."""
    service = get_service()
    res = service.changes().getStartPageToken().execute()
    return res["startPageToken"]


def list_changes_since(page_token: str, max_pages: int = 5) -> tuple[list[dict], str]:
    """Lista cambios en Drive desde el page_token dado.
    Devuelve (lista_de_changes, nuevo_page_token).

    Cada change tiene:
      - fileId: id del archivo cambiado
      - file: dict con info del archivo (si fue modificado, no eliminado)
      - removed: True si el archivo fue eliminado
      - time: timestamp del cambio

    El nuevo_page_token sirve para la PRÓXIMA llamada incremental.

    max_pages: límite de páginas (default 5 × 1000 = 5000 cambios max por scan).
    Bug 27/may: cuando el page_token tenía 40h+ de cambios acumulados (1M
    eventos), la paginación tardaba >5min y GHA cancelaba el job. Cada scan
    siguiente repetía lo mismo. Con max_pages, después de N páginas avanzamos
    el token y la próxima ronda continúa desde donde quedamos.
    """
    service = get_service()
    all_changes = []
    current_token = page_token
    pages_fetched = 0
    while pages_fetched < max_pages:
        res = service.changes().list(
            pageToken=current_token,
            pageSize=1000,
            fields="nextPageToken, newStartPageToken, changes(fileId, removed, time, "
                   "file(id, name, mimeType, size, createdTime, modifiedTime, "
                   "parents, owners(emailAddress), lastModifyingUser(emailAddress), trashed))",
            spaces="drive",
            includeRemoved=True,
        ).execute()
        all_changes.extend(res.get("changes", []))
        pages_fetched += 1
        next_page = res.get("nextPageToken")
        if next_page:
            current_token = next_page
            continue
        # Última página devuelve newStartPageToken para usar la próxima vez
        new_start = res.get("newStartPageToken") or current_token
        return all_changes, new_start
    # Hit max_pages — devolvemos lo que tenemos y el current_token (no avanzamos
    # al newStartPageToken porque no llegamos al final). Próximo scan continúa.
    print(f"   ⚠️  list_changes_since: hit max_pages ({max_pages}). "
          f"Procesando {len(all_changes)} cambios. Próximo scan continúa.")
    return all_changes, current_token


if __name__ == "__main__":
    print("📁 Descubriendo carpetas de cliente (las que tienen Material/Raw/Crudos)...")
    clients = discover_client_folders()
    print(f"   {len(clients)} carpetas de cliente detectadas en Mi Unidad.\n")
    for c in sorted(clients, key=lambda x: x.cliente.lower())[:20]:
        materials = list_material_files(c.raw_folder_id)
        print(f"   {c.cliente:<35} → {len(materials)} archivos en /Material/")
    if len(clients) > 20:
        print(f"   ... y {len(clients) - 20} más")

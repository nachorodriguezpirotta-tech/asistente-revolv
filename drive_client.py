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


def list_edited_files(client_folder_id: str, raw_folder_id: Optional[str]) -> list[dict]:
    """
    Lista TODOS los videos editados dentro de la carpeta del cliente, recursivamente,
    EXCLUYENDO los que están dentro de la subcarpeta /Material/.
    """
    edited: list[dict] = []
    # videos directos en raíz del cliente
    edited.extend(_list_files(client_folder_id, only_videos=True))
    # recurrir en subcarpetas que NO sean Material
    for sub in _list_subfolders(client_folder_id):
        if raw_folder_id and sub["id"] == raw_folder_id:
            continue
        edited.extend(_list_recursive_videos(sub["id"]))
    return edited


def _list_recursive_videos(folder_id: str) -> list[dict]:
    out = []
    out.extend(_list_files(folder_id, only_videos=True))
    for sub in _list_subfolders(folder_id):
        out.extend(_list_recursive_videos(sub["id"]))
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

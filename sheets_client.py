"""
Lector del Sheet de packs (SOLO LECTURA — el sistema NUNCA escribe).

Estructura del Sheet 'Editores Excel':
  - Hoja '$' contiene los packs.
  - Headers en la fila 4: [Columna 1, Editor, Cliente, entradas, salidas,
    segunda salida, Videos pedidos, Videos hechos, profit].
  - Filas a partir de la 5 son los packs.

Uso principal acá: dado el nombre de un cliente, devolver el editor responsable
(usamos la fila MÁS RECIENTE de ese cliente como fuente de verdad del editor actual).
"""

import unicodedata
from dataclasses import dataclass
from typing import Optional

from googleapiclient.discovery import build

try:
    from config import SHEET_ID, PACKS_TAB, PACKS_HEADER_ROW
except ImportError:
    # En Vercel, los endpoints insertan api/ primero en sys.path y 'config'
    # resuelve a api/config.py (el ENDPOINT) → ImportError. Cargar el config.py
    # de la RAÍZ por ruta absoluta. (Bug 11/jun: mails de revisión no salían.)
    import os as _os, importlib.util as _ilu
    _p = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "config.py")
    _s = _ilu.spec_from_file_location("_root_config", _p)
    _m = _ilu.module_from_spec(_s)
    _s.loader.exec_module(_m)
    SHEET_ID = _m.SHEET_ID
    PACKS_TAB = _m.PACKS_TAB
    PACKS_HEADER_ROW = _m.PACKS_HEADER_ROW
from auth import get_credentials


@dataclass
class Pack:
    row: int
    fecha: str
    editor: str
    cliente: str
    videos_pedidos: int
    videos_hechos: int


def _normalize(s: str) -> str:
    s = unicodedata.normalize("NFD", s)
    s = "".join(c for c in s if unicodedata.category(c) != "Mn")
    return " ".join(s.lower().split())


def _to_int(v) -> int:
    if v is None or v == "":
        return 0
    try:
        return int(float(str(v).replace(",", ".").strip()))
    except (ValueError, TypeError):
        return 0


import threading
_thread_local = threading.local()


def _get_service():
    """Service per-thread para thread-safety con ThreadPoolExecutor."""
    if not hasattr(_thread_local, "service"):
        creds = get_credentials()
        _thread_local.service = build("sheets", "v4", credentials=creds, cache_discovery=False)
    return _thread_local.service


def get_sheet_metadata():
    svc = _get_service()
    meta = svc.spreadsheets().get(spreadsheetId=SHEET_ID).execute()
    return {
        "title": meta["properties"]["title"],
        "tabs": [s["properties"]["title"] for s in meta["sheets"]],
    }


def read_packs() -> list[Pack]:
    svc = _get_service()
    rng = f"'{PACKS_TAB}'!A{PACKS_HEADER_ROW}:Z"
    res = svc.spreadsheets().values().get(spreadsheetId=SHEET_ID, range=rng).execute()
    values = res.get("values", [])
    if not values:
        return []

    header = [(h or "").strip().lower() for h in values[0]]

    def col(*names):
        for n in names:
            if n.lower() in header:
                return header.index(n.lower())
        return None

    i_fecha = col("columna 1", "fecha")
    i_editor = col("editor")
    i_cliente = col("cliente")
    i_pedidos = col("videos pedidos", "pedidos")
    i_hechos = col("videos hechos", "hechos")

    if i_editor is None or i_cliente is None:
        raise RuntimeError(f"No encuentro Editor/Cliente. Header: {header}")

    packs: list[Pack] = []
    for offset, row in enumerate(values[1:], start=1):
        abs_row = PACKS_HEADER_ROW + offset

        def cell(i):
            return row[i] if i is not None and i < len(row) else ""

        editor = str(cell(i_editor)).strip()
        cliente = str(cell(i_cliente)).strip()
        if not cliente:
            continue

        packs.append(Pack(
            row=abs_row,
            fecha=str(cell(i_fecha)).strip(),
            editor=editor,
            cliente=cliente,
            videos_pedidos=_to_int(cell(i_pedidos)),
            videos_hechos=_to_int(cell(i_hechos)),
        ))

    return packs


def get_editor_for_client(cliente_drive_name: str, packs: Optional[list[Pack]] = None) -> Optional[str]:
    """
    Resuelve el editor asignado a un cliente.

    Orden de prioridad:
      1. cfg_client_editor (DB) — override manual desde el dashboard
      2. Sheet (filas más recientes con match de nombre)
      3. None

    El override tiene prioridad porque Nacho lo puso a mano en el dashboard.
    """
    # 1) Override manual desde el dashboard
    try:
        from tracker import cfg_get_client_editor
        override = cfg_get_client_editor(cliente_drive_name)
        if override:
            return override
    except Exception:
        pass

    # 2) Sheet (lookup original)
    if packs is None:
        packs = read_packs()

    target = _normalize(cliente_drive_name)
    matches = [p for p in packs if p.editor and _normalize(p.cliente) == target]

    if not matches:
        # fallback: contiene — con GUARDA DE UNICIDAD (16/jul, caso Alejandro
        # Visas): si el matching parcial encuentra 2+ clientes DISTINTOS del
        # Sheet ('Alejandro Araya', 'Alejandro Suarez'...), es AMBIGUO → None →
        # la tarjeta se crea SIN editor y la asigna Ignacio a mano. Antes
        # elegía "la fila más reciente" entre candidatos que no eran el cliente.
        matches = [p for p in packs if p.editor and target in _normalize(p.cliente)]
        if not matches:
            matches = [p for p in packs if p.editor and _normalize(p.cliente) in target]
        distinct = {_normalize(p.cliente) for p in matches}
        if len(distinct) > 1:
            return None

    if not matches:
        return None

    # La fila más reciente (mayor row) es la fuente de verdad del editor actual
    matches.sort(key=lambda p: p.row, reverse=True)
    return matches[0].editor


if __name__ == "__main__":
    meta = get_sheet_metadata()
    print(f"📊 Sheet: {meta['title']}")
    packs = read_packs()
    print(f"   {len(packs)} packs leídos.\n")
    # Test resolver editor
    test_clients = ["Cristina Brox", "Egdylu", "Gamalier", "Melesio", "Jaime", "Inexistente XYZ"]
    print("Test 'editor del cliente':")
    for c in test_clients:
        e = get_editor_for_client(c, packs)
        print(f"   {c:<25} → {e or '❌ no encontrado'}")

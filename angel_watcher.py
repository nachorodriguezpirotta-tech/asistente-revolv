"""
Angel Watcher — corre cada 5 min vía launchd.

Detecta cuando hay un crudo NUEVO en la carpeta de Electro Angel
(en cualquier ubicación: raíz, /Material/, subcarpetas), lo analiza
con Claude y manda un mail con un HTML que recomienda sonidos,
hooks y mejoras para viralidad.

Filosofía:
  - "Crudo" = video que NO matchea el patrón "Video N" (los Video N son los editados).
  - Estado persiste en angel_state.json (lista de file_ids ya procesados).
  - El primer run hace BASELINE: marca todos los crudos existentes como ya
    procesados sin analizarlos. Solo los crudos posteriores disparan análisis.

Uso:
    python3 angel_watcher.py            # un run normal
    python3 angel_watcher.py --baseline # marca todo lo actual como visto
    python3 angel_watcher.py --force <file_id>  # fuerza re-análisis
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

# KILL SWITCH (igual filosofía que scan.py)
_HERE = Path(__file__).resolve().parent

# Data dirs FUERA de ~/Documents porque launchd no tiene Full Disk Access
# para Documents en macOS moderna. Toda la persistencia vive en ~/.revolv/angel/.
# En GHA, vive en el propio repo (para commitear el state).
if os.environ.get("CI") or os.environ.get("GITHUB_ACTIONS"):
    DATA_ROOT = _HERE / "angel_data"
else:
    DATA_ROOT = Path.home() / ".revolv" / "angel"
DATA_ROOT.mkdir(parents=True, exist_ok=True)

if (DATA_ROOT / ".angel_disabled").exists() or (_HERE / ".angel_disabled").exists():
    print("🛑 .angel_disabled existe — angel_watcher deshabilitado.")
    sys.exit(0)

from drive_client import (
    find_folder_by_name, _list_root_items_with_shortcuts,
    _list_files, _list_subfolders, get_service,
)
from classifier import _EDITOR_EMAILS_LOWER
from aliases import CLIENT_NICKNAMES, EDITOR_EMAILS


# ─── Config ────────────────────────────────────────────────────────────────

ANGEL_CLIENT_NAME = CLIENT_NICKNAMES.get("angel", "Electro Angel")
ANGEL_OWNER_EMAILS = {"electroangelexpert@gmail.com"}
STATE_FILE = DATA_ROOT / "state" / "angel_state.json"
LOG_FILE = DATA_ROOT / "logs" / "angel_watcher.log"
WORK_DIR = DATA_ROOT / "work"          # workdir temporal por video
REPORTS_DIR = DATA_ROOT / "reports"    # HTMLs generados (archivo histórico)
for d in (STATE_FILE.parent, LOG_FILE.parent, WORK_DIR, REPORTS_DIR):
    d.mkdir(parents=True, exist_ok=True)

# Patrón de "editado": video con número (Video 1, Video 2, Reel 4, etc).
# Si el nombre matchea esto, NO es crudo.
EDITED_PATTERN = re.compile(
    r"^(video|reel|short|edit|final)\s*\d+",
    re.IGNORECASE,
)

# Patrones típicos de archivos de cámara/celular = CRUDO seguro
CRUDO_NAME_PATTERNS = [
    re.compile(r"^img_\d+", re.I),
    re.compile(r"^mvi_\d+", re.I),
    re.compile(r"^mov_\d+", re.I),
    re.compile(r"^vid_\d+", re.I),
    re.compile(r"^dsc_\d+", re.I),
    re.compile(r"^dji_\d+", re.I),
    re.compile(r"^gx\d+", re.I),
    re.compile(r"^\d{8}_\d{6}", re.I),
    re.compile(r"^pxl_\d", re.I),
    re.compile(r"^whatsapp", re.I),
]

# Extensiones de video válidas
VIDEO_EXTS = {".mp4", ".mov", ".m4v", ".avi", ".mkv", ".webm"}

# Mails de editores conocidos (no son crudos los archivos owned por ellos)
_KNOWN_EDITOR_EMAILS = {e.strip().lower() for e in EDITOR_EMAILS.values() if e}
_KNOWN_EDITOR_EMAILS |= _EDITOR_EMAILS_LOWER
# Editores adicionales que vemos en la carpeta de Ángel
_KNOWN_EDITOR_EMAILS |= {
    "pedronicosalgado@gmail.com",
    "lcampos.2616@gmail.com",
    "murialcazar741@gmail.com",
    "mazurna05@gmail.com",
}


# ─── Logging ───────────────────────────────────────────────────────────────

LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger("angel")


# ─── State ─────────────────────────────────────────────────────────────────

def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception as e:
            log.warning(f"State file inválido, reseteando: {e}")
    return {"processed_ids": {}, "last_run": None}


def save_state(state: dict):
    state["last_run"] = datetime.now(timezone.utc).isoformat()
    STATE_FILE.write_text(json.dumps(state, indent=2, ensure_ascii=False))


# ─── Heurística: ¿es crudo? ────────────────────────────────────────────────

def _owner_emails(file: dict) -> set[str]:
    out = set()
    for o in (file.get("owners") or []):
        em = (o.get("emailAddress") or "").strip().lower()
        if em:
            out.add(em)
    lm = (file.get("lastModifyingUser") or {}).get("emailAddress") or ""
    lm = lm.strip().lower()
    if lm:
        out.add(lm)
    return out


def is_raw_video(file: dict) -> bool:
    """Decide si un archivo es CRUDO (no editado) PARA ELECTRO ANGEL.

    Reglas (en orden):
      1. Si la extensión no es de video → False
      2. Si owner es Ángel (electroangelexpert@gmail.com) → True (crudo)
      3. Si owner es un editor conocido → False (editado)
      4. Si el nombre matchea pattern de cámara (DJI_, IMG_, MVI_...) → True
      5. Si el nombre matchea ^(video|reel|...) N → False
      6. Default conservador: False (no es crudo claro)
    """
    name = file.get("name", "")
    stem = re.sub(r"\.[a-z0-9]+$", "", name, flags=re.I).strip()

    ext = Path(name).suffix.lower()
    if ext not in VIDEO_EXTS:
        return False

    owners = _owner_emails(file)

    if owners & ANGEL_OWNER_EMAILS:
        return True

    if owners & _KNOWN_EDITOR_EMAILS:
        return False

    for p in CRUDO_NAME_PATTERNS:
        if p.search(stem):
            return True

    if EDITED_PATTERN.match(stem):
        return False

    return False  # default conservador: solo procesamos lo que estamos seguros


# ─── Resolver carpeta de Electro Angel ─────────────────────────────────────

def find_angel_folder() -> Optional[dict]:
    """Busca la carpeta de Electro Angel en Mi Unidad."""
    all_root = _list_root_items_with_shortcuts()

    # 1. Buscar por nombre canónico
    folder = find_folder_by_name(ANGEL_CLIENT_NAME, all_root)
    if folder:
        return folder

    # 2. Variantes comunes
    for variant in ["Electro Ángel", "Electroangel", "Electro angel", "Angel"]:
        folder = find_folder_by_name(variant, all_root)
        if folder:
            return folder

    return None


# ─── Listar crudos nuevos ──────────────────────────────────────────────────

def _walk_all_videos(folder_id: str, depth: int = 0, max_depth: int = 4) -> list[dict]:
    """Recorre recursivamente y devuelve TODOS los videos."""
    if depth > max_depth:
        return []
    out = []
    out.extend(_list_files(folder_id, only_videos=True))
    for sub in _list_subfolders(folder_id):
        out.extend(_walk_all_videos(sub["id"], depth + 1, max_depth))
    return out


def list_new_crudos(folder: dict, state: dict) -> list[dict]:
    """Lista crudos en la carpeta de Ángel que aún no fueron procesados."""
    all_videos = _walk_all_videos(folder["id"])

    processed_ids = set(state.get("processed_ids", {}).keys())
    new = []
    seen_ids = set()
    for f in all_videos:
        if f["id"] in seen_ids:
            continue
        seen_ids.add(f["id"])
        if f["id"] in processed_ids:
            continue
        if not is_raw_video(f):
            continue
        new.append(f)

    new.sort(key=lambda x: x.get("createdTime", ""))
    return new


# ─── Run loop ──────────────────────────────────────────────────────────────

def run(baseline: bool = False, force_file_id: Optional[str] = None,
        dry_run: bool = False):
    log.info("═" * 60)
    log.info(f"🎬 Angel Watcher iniciando | baseline={baseline} | dry_run={dry_run}")

    state = load_state()

    folder = find_angel_folder()
    if not folder:
        log.error(f"❌ No encontré la carpeta '{ANGEL_CLIENT_NAME}' en Drive.")
        return 0
    log.info(f"📁 Carpeta de Ángel: {folder['name']} (id={folder['id']})")

    if force_file_id:
        service = get_service()
        try:
            f = service.files().get(
                fileId=force_file_id,
                fields="id, name, mimeType, size, createdTime, modifiedTime, "
                       "owners(emailAddress), lastModifyingUser(emailAddress), parents",
            ).execute()
            videos = [f]
            log.info(f"⚡ Forzando re-análisis de: {f['name']}")
        except Exception as e:
            log.error(f"❌ No pude leer file_id={force_file_id}: {e}")
            return 0
    else:
        videos = list_new_crudos(folder, state)
        log.info(f"🎯 {len(videos)} crudos nuevos detectados.")

    if not videos:
        save_state(state)
        return 0

    if baseline:
        # Marcar todos como ya procesados sin analizar
        for v in videos:
            state["processed_ids"][v["id"]] = {
                "name": v["name"],
                "marked_at": datetime.now(timezone.utc).isoformat(),
                "baseline": True,
            }
        save_state(state)
        log.info(f"📌 BASELINE: marqué {len(videos)} crudos como ya vistos.")
        return len(videos)

    # Procesar cada uno
    from angel_analyzer import analyze_and_report

    processed = 0
    for v in videos:
        log.info(f"─── Procesando: {v['name']} ({v['id']})")
        if dry_run:
            log.info("   (dry-run, saltado)")
            continue
        try:
            ok, info = analyze_and_report(
                file_meta=v,
                work_dir=WORK_DIR,
                reports_dir=REPORTS_DIR,
            )
            if ok:
                state["processed_ids"][v["id"]] = {
                    "name": v["name"],
                    "processed_at": datetime.now(timezone.utc).isoformat(),
                    "report": info.get("report_path"),
                    "mail_id": info.get("mail_id"),
                }
                save_state(state)
                processed += 1
                log.info(f"   ✅ Procesado OK → mail_id={info.get('mail_id')}")
            else:
                log.error(f"   ❌ Falló análisis: {info.get('error')}")
        except Exception as e:
            log.exception(f"   ❌ Excepción procesando {v['name']}: {e}")

    log.info(f"🏁 Done. {processed}/{len(videos)} crudos procesados con éxito.")
    return processed


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--baseline", action="store_true",
                   help="Marcar todos los crudos actuales como ya vistos (no analiza).")
    p.add_argument("--force", help="Forzar re-análisis de un file_id.")
    p.add_argument("--dry-run", action="store_true",
                   help="Listar crudos pero no descargar/analizar/mailear.")
    args = p.parse_args()
    n = run(baseline=args.baseline, force_file_id=args.force, dry_run=args.dry_run)
    sys.exit(0 if n >= 0 else 1)

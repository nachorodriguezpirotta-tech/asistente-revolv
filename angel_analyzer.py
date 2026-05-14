"""
Angel Analyzer — descarga, extrae frames, llama a Claude CLI y manda mail.

Para cada crudo de Electro Angel:
  1. Descarga el video con la API de Drive.
  2. Extrae 6 keyframes vía ffmpeg.
  3. Saca duración + resolución básica vía ffmpeg.
  4. Le pasa frames + metadata a `claude -p` (Claude Code CLI) y captura el HTML.
  5. Manda mail a TEST_EMAIL con el HTML inline + adjunto.
  6. Guarda copia del HTML en angel_reports/.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import textwrap
import time
from datetime import datetime, timezone
from email.mime.base import MIMEBase
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email import encoders
from pathlib import Path
from typing import Optional

from googleapiclient.http import MediaIoBaseDownload, MediaFileUpload

from drive_client import get_service
from config import TEST_EMAIL


log = logging.getLogger("angel.analyzer")


# ─── Config ────────────────────────────────────────────────────────────────

FFMPEG = os.environ.get("FFMPEG_BIN", "ffmpeg") if os.environ.get("CI") else "/Users/ignaciorodriguezpirotta/bin/ffmpeg"
CLAUDE_CLI = "/Users/ignaciorodriguezpirotta/.local/bin/claude"
N_FRAMES = 6
CLAUDE_TIMEOUT_SEC = 600  # 10 min

# Modelo Gemini gratuito con vision (tier free: 1500 rpd, 1M tokens/día).
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")

# Carpeta de Drive donde se suben TODOS los briefs HTML generados.
ANGEL_BRIEFS_DRIVE_FOLDER_ID = "14waBJpikjD-X5sGjx9wyGA7KVpMVanOf"

# Template HTML de referencia (mismo formato Pantera-Rosa).
# En GHA vive en el repo; en local, hardlinked en ~/.revolv/angel/.
_TEMPLATE_REPO = Path(__file__).resolve().parent / "angel_brief_template.html"
_TEMPLATE_LOCAL = Path.home() / ".revolv" / "angel" / "template_referencia.html"
TEMPLATE_REFERENCE = _TEMPLATE_REPO if _TEMPLATE_REPO.exists() else _TEMPLATE_LOCAL


# ─── Drive download ────────────────────────────────────────────────────────

def download_video(file_id: str, dest: Path) -> Path:
    service = get_service()
    req = service.files().get_media(fileId=file_id)
    dest.parent.mkdir(parents=True, exist_ok=True)
    with dest.open("wb") as fh:
        dl = MediaIoBaseDownload(fh, req, chunksize=8 * 1024 * 1024)
        done = False
        while not done:
            status, done = dl.next_chunk()
            if status:
                log.info(f"   ⬇  {int(status.progress() * 100)}%")
    return dest


# ─── ffmpeg helpers ────────────────────────────────────────────────────────

def probe_duration_seconds(video: Path) -> float:
    """Devuelve duración en segundos. Parsea 'Duration: HH:MM:SS.ss' del stderr."""
    p = subprocess.run(
        [FFMPEG, "-i", str(video)],
        stderr=subprocess.PIPE, stdout=subprocess.DEVNULL,
        text=True, errors="replace",
    )
    m = re.search(r"Duration:\s+(\d+):(\d+):(\d+\.\d+)", p.stderr)
    if not m:
        return 0.0
    h, mi, s = m.groups()
    return int(h) * 3600 + int(mi) * 60 + float(s)


def probe_resolution(video: Path) -> Optional[str]:
    p = subprocess.run(
        [FFMPEG, "-i", str(video)],
        stderr=subprocess.PIPE, stdout=subprocess.DEVNULL,
        text=True, errors="replace",
    )
    m = re.search(r",\s*(\d{2,5})x(\d{2,5})", p.stderr)
    return f"{m.group(1)}x{m.group(2)}" if m else None


def extract_frames(video: Path, dest_dir: Path, n: int = N_FRAMES) -> list[Path]:
    """Extrae N frames evenly distributed por toda la duración."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    duration = probe_duration_seconds(video)
    if duration <= 0:
        # Fallback: dejar a ffmpeg que elija con thumbnail filter
        subprocess.run(
            [FFMPEG, "-y", "-i", str(video),
             "-vf", "thumbnail=100,scale=720:-1",
             "-frames:v", str(n),
             str(dest_dir / "frame_%02d.jpg")],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        return sorted(dest_dir.glob("frame_*.jpg"))

    out = []
    for i in range(n):
        t = duration * (i + 0.5) / n  # centro de cada bin
        outpath = dest_dir / f"frame_{i+1:02d}.jpg"
        subprocess.run(
            [FFMPEG, "-y", "-ss", f"{t:.2f}", "-i", str(video),
             "-frames:v", "1", "-q:v", "3",
             "-vf", "scale=720:-1",
             str(outpath)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        if outpath.exists() and outpath.stat().st_size > 0:
            out.append(outpath)
    return out


def extract_audio_mp3(video: Path, dest: Path) -> Optional[Path]:
    """Extrae el audio en mp3 (opcional, para futuras integraciones)."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    p = subprocess.run(
        [FFMPEG, "-y", "-i", str(video),
         "-vn", "-acodec", "libmp3lame", "-q:a", "5",
         str(dest)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    if p.returncode == 0 and dest.exists():
        return dest
    return None


# ─── Claude CLI: generación del HTML ───────────────────────────────────────

PROMPT_TEMPLATE = """Sos editor experto y consultor de viralidad para Reels/TikTok. Cliente: Electro Ángel — creador de contenido cómico/educativo sobre electrodomésticos en español argentino (rioplatense). Hace sketches narrativos con tono thriller-cómico, twists, y mucho juego de personaje.

TU TAREA: ver este crudo y generar un BRIEF DE EDICIÓN segundo-a-segundo, listo para que el editor lo ejecute. No es un análisis genérico de viralidad — es un brief de SFX + zooms + timing con instrucciones concretísimas.

═══════════════════════════════════════════════════════════════════════
INPUTS (usá la tool Read en CADA UNO antes de escribir nada)
═══════════════════════════════════════════════════════════════════════
Archivo: {filename}
Duración: {duration_str}
Resolución: {resolution}
Frames (orden cronológico):{frames_paths}
{audio_line}

PASO 1 — Read cada frame de arriba. Identificá: qué pasa, qué objeto/persona aparece, qué tipo de toma es, en qué momento del video estamos.

PASO 2 — Read el HTML de referencia abajo. Ese es el FORMATO EXACTO que tenés que replicar (mismos estilos CSS, misma estructura de secciones, mismos badges, misma estética rosa-dark). NO inventes otro diseño.
   Referencia: {template_path}

PASO 3 — Generá el HTML del brief para ESTE video y guardalo con Write en:
   {output_path}

═══════════════════════════════════════════════════════════════════════
ESTRUCTURA OBLIGATORIA del brief (en ESTE orden, igual que la referencia)
═══════════════════════════════════════════════════════════════════════

1. **Header**: <div class="sub">Brief de edición</div> + título del video en <div class="vid-title"> con un nombre creativo y atractivo (estilo "EL ROMPE MATRIMONIOS", "LA TRAMPA DEL LAVARROPAS", etc.) — algo que tire un beneficio o intriga + segunda línea con el nombre real / tema del video. + sub-línea descriptiva con duración real.

2. **% Viral**: <div class="viral">XX% VIRAL <span>potencial estimado</span></div>. Pone un % realista basado en lo que viste (40-95%).

3. **.nota**: párrafo narrativo (3-5 líneas) explicando QUÉ es el video, por qué tiene potencial (o no), y qué necesita.

4. **.alert** con "⚡ Prioridad absoluta:" → el beat MÁS importante del video y qué tiene que pasar ahí sí o sí (timing, audio, transición).

5. **.context** con "Contexto del video:" → resumen del flujo de escenas, beat por beat narrativo, separado por flechas →.

6. **<h2>SFX + Zooms — segundo a segundo</h2>** + <table> con columnas Seg | Qué agregar. Una fila por cada momento importante del video. Cada fila debe tener:
   - Marcas (badges) de qué tipo es: <span class="fire">🔥 CLAVE</span> para los críticos (2-3 máx por video), <span class="sfx">SFX</span> siempre que haya sonido, <span class="zoom">ZOOM</span> cuando haya zoom.
   - <span class="name">Nombre del SFX/Track + descripción del zoom</span> — concreto, con NOMBRES de sonidos reales (Freesound, Epidemic Sound, YouTube). Ej: "Pink Panther Theme", "Sad Trombone Wah Wah", "Cash Register Cha-Ching", "Suspense Sting Short", "Drum Roll".
   - <span class="note">explicación técnica: timing exacto del zoom (ej "Zoom in 100→118% en 4 frames"), source del SFX, y POR QUÉ ese SFX encaja en ESE beat de ESTE video.
   - Distribuí 6 a 10 filas a lo largo del video, basadas en los frames que viste.

7. **<h2>Música de fondo</h2>** + párrafo con recomendación de track de fondo (volumen, mood, fuente: Epidemic Sound / Artlist / YouTube Audio Library) que NO tape los SFX. Sé específico sobre estilo (comedy-suspense, spy-comedy, electrónica latina, hip-hop tonal, etc) y al menos 1 nombre concreto de track o keyword.

═══════════════════════════════════════════════════════════════════════
REGLAS DE ESTILO (igual que la referencia)
═══════════════════════════════════════════════════════════════════════
- Copiá los <style> tal cual de la referencia. Dark #0e0e0e, acento rosa #ff6bff, Bebas Neue + Space Mono.
- max-width 700px, padding 32px 20px.
- Tono del copy: directo, en español rioplatense, sin formalidades. Tipo "El más original de todos", "Si ese audio entra justo, el hook es 10/10", "El cha-ching marca el momento exacto".
- Citá nombres de SFX/tracks reales (Freesound / Epidemic Sound / YouTube Audio Library / Artlist) — no inventes URLs.
- NUNCA uses palabras genéricas como "considerar agregar X" — siempre afirmativo: "Acá entra el X". El editor lee y ejecuta.

═══════════════════════════════════════════════════════════════════════
OUTPUT
═══════════════════════════════════════════════════════════════════════
HTML completo desde <!DOCTYPE html> hasta </html>, escrito con la tool Write al path indicado. Después respondé sólo: OK: <path>
Nada de markdown fences. Nada de preámbulos. Solo el HTML.
"""


def _build_prompt(file_meta: dict, video_path: Path, frames: list[Path],
                  audio_path: Optional[Path], duration_sec: float,
                  resolution: Optional[str], output_path: Path) -> str:
    duration_str = (
        f"{int(duration_sec // 60)}:{int(duration_sec % 60):02d}"
        if duration_sec > 0 else "desconocida"
    )
    audio_line = (
        f"Audio extraído: {audio_path}" if audio_path
        else "(audio no disponible)"
    )
    return PROMPT_TEMPLATE.format(
        filename=file_meta.get("name", "(sin nombre)"),
        duration_str=duration_str,
        resolution=resolution or "desconocida",
        frames_paths="\n  - " + "\n  - ".join(str(f) for f in frames),
        audio_line=audio_line,
        output_path=str(output_path),
        template_path=str(TEMPLATE_REFERENCE),
    )


def _strip_markdown_fences(html: str) -> str:
    s = html.strip()
    # remove leading ```html / ``` and trailing ```
    s = re.sub(r"^```(?:html)?\s*\n?", "", s)
    s = re.sub(r"\n?```\s*$", "", s)
    return s.strip()


def generate_html_via_claude(prompt: str, work_dir: Path, output_path: Path) -> str:
    """Invoca `claude -p`, espera que escriba el HTML en output_path y lo lee."""
    cmd = [
        CLAUDE_CLI,
        "-p", prompt,
        "--add-dir", str(work_dir.resolve()),
        "--add-dir", str(Path.home() / ".revolv" / "angel"),
        "--dangerously-skip-permissions",
        "--allowedTools", "Read", "Write",
        "--model", "claude-opus-4-7",
        "--effort", "high",
    ]
    log.info(f"   🤖 Llamando a claude CLI (timeout {CLAUDE_TIMEOUT_SEC}s)...")
    p = subprocess.run(
        cmd,
        capture_output=True, text=True,
        timeout=CLAUDE_TIMEOUT_SEC,
        stdin=subprocess.DEVNULL,
        env={**os.environ, "TERM": "dumb"},
    )
    if p.returncode != 0:
        log.error(f"   ❌ claude CLI rc={p.returncode}")
        log.error(f"   STDERR: {p.stderr[:2000]}")
        log.error(f"   STDOUT: {p.stdout[:2000]}")
        raise RuntimeError(
            f"claude CLI falló (rc={p.returncode}). stderr={p.stderr[:300]} stdout={p.stdout[:300]}"
        )
    if not output_path.exists():
        # Fallback: a veces Claude lo escribe en un path parecido
        cand = list(work_dir.glob("*.html"))
        if cand:
            log.warning(f"   ⚠️  Claude escribió en {cand[0]} en vez de {output_path}")
            output_path = cand[0]
        else:
            raise RuntimeError(
                f"Claude no creó el HTML esperado en {output_path}. "
                f"Stdout primeros 400 chars:\n{p.stdout[:400]}"
            )
    html = output_path.read_text(encoding="utf-8")
    html = _strip_markdown_fences(html)
    if "<html" not in html.lower() and "<!doctype" not in html.lower():
        raise RuntimeError(
            f"El archivo generado no parece HTML válido. Primeros 400 chars:\n{html[:400]}"
        )
    return html


# ─── Generador con Gemini (gratis vía Google AI Studio) ───────────────────

def generate_html_via_gemini(prompt: str, frames: list[Path], output_path: Path) -> str:
    """Llama a Gemini 2.5 Flash con los frames como imágenes y devuelve el HTML.

    Para usar en GHA o en cualquier lugar SIN consumir Claude Code credits.
    Requiere GEMINI_API_KEY env var (gratis en https://aistudio.google.com/apikey).
    """
    if not GEMINI_API_KEY:
        raise RuntimeError("GEMINI_API_KEY no seteada — no puedo usar Gemini.")

    # Cargar template del archivo en disco (Gemini no tiene Read tool).
    template_html = ""
    if TEMPLATE_REFERENCE.exists():
        template_html = TEMPLATE_REFERENCE.read_text(encoding="utf-8")

    # Adaptar prompt para output directo (sin Read/Write).
    prompt_inline = (
        prompt
        + "\n\n═══ TEMPLATE DE REFERENCIA (replicalo CSS-by-CSS) ═══\n"
        + template_html
        + "\n\n═══ INSTRUCCIONES FINALES ═══\n"
        + "Devolvé ÚNICAMENTE el HTML del brief para el video. Sin markdown fences. "
        + "Empezá con <!DOCTYPE html> y terminá con </html>. Nada más."
    )

    import google.generativeai as genai
    genai.configure(api_key=GEMINI_API_KEY)
    model = genai.GenerativeModel(GEMINI_MODEL)

    # Cargar las imágenes como bytes para el SDK.
    image_parts = []
    for fr in frames:
        image_parts.append({
            "mime_type": "image/jpeg",
            "data": fr.read_bytes(),
        })

    log.info(f"   🤖 Llamando a Gemini ({GEMINI_MODEL}) con {len(image_parts)} frames...")
    response = model.generate_content(
        [prompt_inline, *image_parts],
        generation_config={
            "temperature": 0.7,
            "max_output_tokens": 16000,
        },
        request_options={"timeout": CLAUDE_TIMEOUT_SEC},
    )
    raw = (response.text or "").strip()
    html = _strip_markdown_fences(raw)
    if "<html" not in html.lower() and "<!doctype" not in html.lower():
        raise RuntimeError(
            f"Gemini no devolvió HTML válido. Primeros 400 chars:\n{raw[:400]}"
        )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(html, encoding="utf-8")
    return html


def generate_html(prompt: str, work_dir: Path, output_path: Path,
                  frames: list[Path]) -> str:
    """Dispatcher: usa Gemini si GEMINI_API_KEY está seteada, sino Claude CLI."""
    if GEMINI_API_KEY:
        return generate_html_via_gemini(prompt, frames, output_path)
    return generate_html_via_claude(prompt, work_dir, output_path)


# ─── Subida a Drive ────────────────────────────────────────────────────────

def upload_html_to_drive(html_path: Path, folder_id: str = ANGEL_BRIEFS_DRIVE_FOLDER_ID,
                         drive_name: Optional[str] = None) -> Optional[dict]:
    """Sube el HTML del brief a la carpeta de Drive indicada.
    `drive_name`: nombre del archivo en Drive (por defecto el filename local).
    Devuelve {id, name, webViewLink} o None si falló."""
    service = get_service()
    media = MediaFileUpload(str(html_path), mimetype="text/html", resumable=False)
    metadata = {"name": drive_name or html_path.name, "parents": [folder_id]}
    try:
        f = service.files().create(
            body=metadata, media_body=media,
            fields="id, name, webViewLink",
        ).execute()
        log.info(f"   ☁️  Subido a Drive: {f.get('webViewLink')}")
        return f
    except Exception as e:
        log.exception(f"   ⚠️  Falló subir HTML a Drive: {e}")
        return None


# ─── Mail ──────────────────────────────────────────────────────────────────

def send_html_report(file_meta: dict, html: str, html_path: Path,
                     drive_link: Optional[str] = None) -> str:
    from mail_client import _get_service
    service = _get_service()

    subject = f"🎬 Brief de edición — {file_meta.get('name', 'crudo')}"
    drive_line = f"\n        Link en Drive (Briefs Angel): {drive_link}\n" if drive_link else ""
    text_body = textwrap.dedent(f"""\
        Hola Nacho,

        Llegó un crudo nuevo a la carpeta de Electro Ángel:
            {file_meta.get('name')}

        Adjunto va el HTML con el brief de edición (SFX, zooms,
        timing segundo a segundo, % viral, contexto, etc.).

        También está embebido en este mismo mail.{drive_line}
        — Asistente Revolv (angel_watcher)
    """)

    msg = MIMEMultipart("mixed")
    msg["To"] = TEST_EMAIL
    msg["Subject"] = subject
    msg["From"] = "Asistente Revolv"

    alt = MIMEMultipart("alternative")
    alt.attach(MIMEText(text_body, "plain", "utf-8"))
    alt.attach(MIMEText(html, "html", "utf-8"))
    msg.attach(alt)

    # Adjunto: el HTML como archivo descargable
    part = MIMEBase("text", "html", charset="utf-8")
    part.set_payload(html.encode("utf-8"))
    encoders.encode_base64(part)
    fname = html_path.name
    part.add_header("Content-Disposition", f'attachment; filename="{fname}"')
    msg.attach(part)

    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode("ascii")
    sent = service.users().messages().send(userId="me", body={"raw": raw}).execute()
    return sent["id"]


# ─── Pipeline principal ────────────────────────────────────────────────────

def _safe_filename(name: str) -> str:
    s = re.sub(r"[^a-zA-Z0-9_.\- ]", "_", name)
    s = re.sub(r"\s+", "_", s).strip("._")
    return s[:80] or "video"


def analyze_and_report(file_meta: dict, work_dir: Path, reports_dir: Path) -> tuple[bool, dict]:
    """Pipeline completo para 1 crudo. Devuelve (ok, info_dict)."""
    file_id = file_meta["id"]
    name = file_meta.get("name", "video.mp4")
    safe = _safe_filename(name)
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")

    job_dir = work_dir / f"{ts}_{file_id[:10]}"
    frames_dir = job_dir / "frames"
    video_path = job_dir / safe
    audio_path = job_dir / "audio.mp3"

    try:
        log.info(f"   ⬇ Descargando {name}...")
        download_video(file_id, video_path)

        duration = probe_duration_seconds(video_path)
        resolution = probe_resolution(video_path)
        log.info(f"   📏 {duration:.1f}s | {resolution or '?'}")

        log.info(f"   🖼  Extrayendo {N_FRAMES} frames...")
        frames = extract_frames(video_path, frames_dir, n=N_FRAMES)
        if not frames:
            return False, {"error": "no se pudieron extraer frames"}
        log.info(f"      {len(frames)} frames listos en {frames_dir}")

        log.info(f"   🎵 Extrayendo audio...")
        audio = extract_audio_mp3(video_path, audio_path)

        output_html = job_dir / "report.html"
        prompt = _build_prompt(
            file_meta=file_meta,
            video_path=video_path,
            frames=frames,
            audio_path=audio,
            duration_sec=duration,
            resolution=resolution,
            output_path=output_html,
        )

        html = generate_html(prompt, work_dir=job_dir, output_path=output_html, frames=frames)

        reports_dir.mkdir(parents=True, exist_ok=True)
        report_path = reports_dir / f"{ts}_{Path(safe).stem}.html"
        report_path.write_text(html, encoding="utf-8")
        log.info(f"   💾 HTML guardado: {report_path}")

        # Subir a Drive (carpeta "Briefs Angel") con el nombre original del video.
        # Ej: "TRAGICO SUCESO.mp4" → "TRAGICO SUCESO.html"
        original_stem = Path(file_meta.get("name", safe)).stem or safe
        drive_name = f"{original_stem}.html"
        drive_info = upload_html_to_drive(report_path, drive_name=drive_name)
        drive_link = drive_info.get("webViewLink") if drive_info else None
        drive_file_id = drive_info.get("id") if drive_info else None

        log.info(f"   📧 Enviando mail a {TEST_EMAIL}...")
        mail_id = send_html_report(file_meta, html, report_path, drive_link=drive_link)

        # Cleanup: borrar workdir (frames + video) — el report queda en reports/
        try:
            shutil.rmtree(job_dir)
        except Exception:
            pass

        return True, {
            "report_path": str(report_path),
            "mail_id": mail_id,
            "drive_file_id": drive_file_id,
            "drive_link": drive_link,
            "duration_sec": duration,
            "resolution": resolution,
        }

    except subprocess.TimeoutExpired:
        return False, {"error": "claude CLI timeout"}
    except Exception as e:
        log.exception(f"   ❌ Error en pipeline: {e}")
        return False, {"error": str(e)}

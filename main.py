from __future__ import annotations

import json
import math
import os
import re
import shutil
import subprocess
import textwrap
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

BASE_DIR = Path(__file__).resolve().parent.parent
WEB_DIR = BASE_DIR / "web"
DATA_DIR = Path(os.getenv("CLIPNEX_DATA_DIR", str(BASE_DIR / "data"))).resolve()
UPLOAD_DIR = DATA_DIR / "uploads"
PROJECT_DIR = DATA_DIR / "projects"
DOWNLOAD_DIR = DATA_DIR / "youtube"
ZIP_DIR = DATA_DIR / "zips"
MODEL_DIR = DATA_DIR / "models"
TEMP_DIR = DATA_DIR / "temp"
MAX_CLIPS = int(os.getenv("CLIPNEX_MAX_CLIPS", "100"))
MAX_UPLOAD_MB = int(os.getenv("CLIPNEX_MAX_UPLOAD_MB", "2048"))

for folder in (DATA_DIR, UPLOAD_DIR, PROJECT_DIR, DOWNLOAD_DIR, ZIP_DIR, MODEL_DIR, TEMP_DIR):
    folder.mkdir(parents=True, exist_ok=True)

app = FastAPI(title="ClipNex AI", version="5.0.0-hosting-ready")

_cors_origins = [origin.strip() for origin in os.getenv("CLIPNEX_CORS_ORIGINS", "").split(",") if origin.strip()]
if _cors_origins:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=_cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

app.mount("/static", StaticFiles(directory=str(WEB_DIR)), name="static")

ALLOWED_ASPECTS = {"9:16", "16:9", "1:1", "original"}
ALLOWED_MODELS = {"tiny", "base", "small"}
ALLOWED_SPEEDS = {"ultra", "turbo", "balanced", "quality"}
ALLOWED_ENCODERS = {"auto", "cpu", "nvenc", "qsv", "amf"}
ALLOWED_DOWNLOAD_QUALITY = {"fast", "standard", "quality"}
WHISPER_CACHE = {}
ENCODER_CACHE: dict[str, object] = {}
JOBS: dict[str, dict] = {}
JOB_EXECUTOR = ThreadPoolExecutor(max_workers=int(os.getenv("CLIPNEX_JOB_WORKERS", "1")))


def clean_error(text: str, limit: int = 1600) -> str:
    text = text or ""
    text = re.sub(r"\x1b\[[0-9;]*m", "", text)
    text = text.replace("\r", "\n")
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    important = []
    for line in lines:
        lower = line.lower()
        if any(key in lower for key in [
            "error", "erro", "failed", "falha", "requested format", "unable",
            "private", "copyright", "sign in", "age", "not available", "libass",
            "ass", "subtitles", "whisper", "model", "permission", "encoder",
            "invalid", "no such", "cannot", "unsupported", "login", "blocked",
        ]):
            important.append(line)
    if not important:
        important = lines[-10:]
    result = "\n".join(important[-12:])
    return result[-limit:] if result else "Erro desconhecido."


def safe_stem(name: str) -> str:
    name = Path(name).stem if name else "video"
    name = re.sub(r"[^a-zA-Z0-9._-]+", "_", name).strip("._-")
    return name[:80] or "video"


def looks_like_url(value: str) -> bool:
    if not value:
        return False
    parsed = urlparse(value.strip())
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def assert_safe_id(value: str, label: str = "id") -> str:
    value = (value or "").strip()
    if not re.fullmatch(r"[a-zA-Z0-9_-]{6,64}", value):
        raise HTTPException(status_code=400, detail=f"{label} invalido.")
    return value


def assert_safe_filename(value: str) -> str:
    value = Path(value or "").name
    if not re.fullmatch(r"[a-zA-Z0-9._-]{1,160}", value):
        raise HTTPException(status_code=400, detail="Nome de arquivo invalido.")
    return value


def cleanup_old_files(max_age_hours: int = 24) -> None:
    """Limpeza simples para hospedagem: remove projetos antigos e evita disco infinito."""
    cutoff = time.time() - (max_age_hours * 3600)
    for base in (PROJECT_DIR, UPLOAD_DIR, DOWNLOAD_DIR, ZIP_DIR, TEMP_DIR):
        if not base.exists():
            continue
        for item in base.iterdir():
            try:
                if item.stat().st_mtime < cutoff:
                    if item.is_dir():
                        shutil.rmtree(item, ignore_errors=True)
                    else:
                        item.unlink(missing_ok=True)
            except Exception:
                pass


def get_ffmpeg_path() -> str:
    try:
        import imageio_ffmpeg
        path = imageio_ffmpeg.get_ffmpeg_exe()
        if path and Path(path).exists():
            return path
    except Exception:
        pass
    found = shutil.which("ffmpeg")
    if found:
        return found
    raise RuntimeError("FFmpeg interno nao foi encontrado. Execute o INICIAR_CLIPNEX_WINDOWS.bat novamente.")


def run_process(args: list[str], timeout: int = 7200) -> subprocess.CompletedProcess:
    return subprocess.run(
        args,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )


def get_duration_seconds(video_path: Path, ffmpeg: str) -> float:
    proc = run_process([ffmpeg, "-hide_banner", "-i", str(video_path)], timeout=60)
    output = f"{proc.stdout}\n{proc.stderr}"
    match = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", output)
    if not match:
        raise RuntimeError("Nao consegui ler a duracao do video. Tente outro arquivo/link.")
    hours, minutes, seconds = match.groups()
    return int(hours) * 3600 + int(minutes) * 60 + float(seconds)


def available_encoders(ffmpeg: str) -> str:
    cached = ENCODER_CACHE.get("encoders_text")
    if isinstance(cached, str):
        return cached
    proc = run_process([ffmpeg, "-hide_banner", "-encoders"], timeout=60)
    text = f"{proc.stdout}\n{proc.stderr}".lower()
    ENCODER_CACHE["encoders_text"] = text
    return text


def pick_video_encoder(ffmpeg: str, encoder_mode: str, speed_mode: str) -> dict:
    encoder_mode = encoder_mode if encoder_mode in ALLOWED_ENCODERS else "auto"
    speed_mode = speed_mode if speed_mode in ALLOWED_SPEEDS else "ultra"
    encoders = available_encoders(ffmpeg)

    def has(name: str) -> bool:
        return name.lower() in encoders

    # Auto prioriza GPU. Se nao existir, cai para CPU sem quebrar.
    chosen = "cpu"
    if encoder_mode == "nvenc" or (encoder_mode == "auto" and has("h264_nvenc")):
        chosen = "nvenc"
    elif encoder_mode == "qsv" or (encoder_mode == "auto" and has("h264_qsv")):
        chosen = "qsv"
    elif encoder_mode == "amf" or (encoder_mode == "auto" and has("h264_amf")):
        chosen = "amf"

    if chosen == "nvenc":
        qp = "31" if speed_mode in {"ultra", "turbo"} else "27"
        return {
            "label": "GPU NVIDIA NVENC",
            "args": ["-c:v", "h264_nvenc", "-preset", "fast", "-rc", "constqp", "-qp", qp],
            "hardware": True,
        }
    if chosen == "qsv":
        qp = "30" if speed_mode in {"ultra", "turbo"} else "25"
        return {
            "label": "GPU Intel Quick Sync",
            "args": ["-c:v", "h264_qsv", "-preset", "veryfast", "-global_quality", qp],
            "hardware": True,
        }
    if chosen == "amf":
        qp = "30" if speed_mode in {"ultra", "turbo"} else "25"
        return {
            "label": "GPU AMD AMF",
            "args": ["-c:v", "h264_amf", "-quality", "speed", "-qp_i", qp, "-qp_p", qp],
            "hardware": True,
        }

    crf = "32" if speed_mode == "ultra" else "28" if speed_mode == "turbo" else "24" if speed_mode == "balanced" else "21"
    preset = "ultrafast" if speed_mode in {"ultra", "turbo"} else "veryfast"
    return {
        "label": "CPU libx264",
        "args": ["-c:v", "libx264", "-preset", preset, "-crf", crf, "-tune", "fastdecode"],
        "hardware": False,
    }


def download_youtube(url: str, project_id: str, ffmpeg: str, download_quality: str = "fast") -> Path:
    try:
        import yt_dlp
    except Exception as exc:
        raise RuntimeError("yt-dlp nao esta instalado. Execute o INICIAR_CLIPNEX_WINDOWS.bat novamente.") from exc

    project_download_dir = DOWNLOAD_DIR / project_id
    if project_download_dir.exists():
        shutil.rmtree(project_download_dir, ignore_errors=True)
    project_download_dir.mkdir(parents=True, exist_ok=True)

    download_quality = download_quality if download_quality in ALLOWED_DOWNLOAD_QUALITY else "fast"
    if download_quality == "quality":
        format_attempts = [
            "bv*[height<=1080]+ba/b[height<=1080]/bv*+ba/best",
            "bv*[height<=720]+ba/b[height<=720]/best",
            "best",
        ]
    elif download_quality == "standard":
        format_attempts = [
            "bv*[height<=720]+ba/b[height<=720]/b[height<=720]/best",
            "bv*[height<=480]+ba/b[height<=480]/b[height<=480]/best",
            "best",
        ]
    else:
        # Mais rapido: baixa menos dados. Depois o FFmpeg padroniza a saida.
        format_attempts = [
            "bv*[height<=480]+ba/b[height<=480]/b[height<=480]/best",
            "bv*[height<=720]+ba/b[height<=720]/b[height<=720]/best",
            "best",
        ]

    last_error = ""
    for fmt in format_attempts:
        for old_file in project_download_dir.glob("*"):
            old_file.unlink(missing_ok=True)
        ydl_opts = {
            "format": fmt,
            "outtmpl": str(project_download_dir / "source.%(ext)s"),
            "noplaylist": True,
            "quiet": True,
            "no_warnings": True,
            "retries": 10,
            "fragment_retries": 10,
            "continuedl": True,
            "windowsfilenames": True,
            "ffmpeg_location": ffmpeg,
            "merge_output_format": "mp4",
            "socket_timeout": 30,
            "extractor_args": {"youtube": {"player_client": ["android", "web"]}},
        }
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                ydl.extract_info(url, download=True)
            files = [p for p in project_download_dir.iterdir() if p.is_file() and not p.name.endswith((".part", ".ytdl", ".temp"))]
            video_files = [p for p in files if p.suffix.lower() in {".mp4", ".mkv", ".webm", ".mov", ".m4v"}]
            if video_files:
                return max(video_files, key=lambda p: p.stat().st_size)
        except Exception as exc:
            last_error = str(exc)

    cleaned = clean_error(last_error)
    raise RuntimeError(
        "Falha ao baixar o video do YouTube. O ClipNex tentou formatos alternativos. "
        "Se o YouTube bloquear este link, envie o MP4 pelo upload. "
        f"Detalhe: {cleaned}"
    )


def save_upload(upload: UploadFile, project_id: str) -> Path:
    suffix = Path(upload.filename or "video.mp4").suffix.lower() or ".mp4"
    if suffix not in {".mp4", ".mov", ".mkv", ".webm", ".m4v", ".avi"}:
        suffix = ".mp4"
    target = UPLOAD_DIR / f"{project_id}_{safe_stem(upload.filename or 'video')}{suffix}"
    max_bytes = MAX_UPLOAD_MB * 1024 * 1024
    total = 0
    with target.open("wb") as out:
        while True:
            chunk = upload.file.read(1024 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > max_bytes:
                target.unlink(missing_ok=True)
                raise RuntimeError(f"Arquivo muito grande. Limite atual: {MAX_UPLOAD_MB} MB.")
            out.write(chunk)
    if target.stat().st_size <= 0:
        raise RuntimeError("Arquivo enviado esta vazio.")
    return target


def ffmpeg_time(seconds: float | int) -> str:
    seconds = int(max(0, round(seconds)))
    h = seconds // 3600
    m = (seconds % 3600) // 60
    s = seconds % 60
    return f"{h:02d}:{m:02d}:{s:02d}"


def build_segments(total_duration: float, clip_duration: int, clip_count: int) -> list[tuple[int, int]]:
    clip_duration = max(5, min(int(clip_duration or 60), 600))
    available = int(math.floor(total_duration / clip_duration))
    if available <= 0:
        raise RuntimeError(f"Video muito curto. Ele nao tem duracao suficiente para gerar cortes de {ffmpeg_time(clip_duration)[3:] }.")
    if clip_count <= 0:
        count = min(available, MAX_CLIPS)
    else:
        requested = int(clip_count)
        if requested > MAX_CLIPS:
            raise RuntimeError(f"O limite maximo desta versao e {MAX_CLIPS} cortes por projeto. Escolha de 1 a {MAX_CLIPS}.")
        if requested > available:
            raise RuntimeError(
                f"Esse video permite no maximo {available} cortes completos de {ffmpeg_time(clip_duration)[3:]}. "
                f"Voce pediu {requested}. Reduza a quantidade ou diminua a duracao de cada corte."
            )
        count = max(1, requested)
    return [(i * clip_duration, clip_duration) for i in range(count)]


def ass_time(seconds: float) -> str:
    seconds = max(0.0, float(seconds))
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    cs = int(round((seconds - int(seconds)) * 100))
    if cs >= 100:
        s += 1
        cs = 0
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


def ass_escape(text: str) -> str:
    text = re.sub(r"\s+", " ", text or "").strip()
    text = text.replace("{", "(").replace("}", ")").replace("\\", "")
    wrapped = textwrap.wrap(text, width=34)
    if len(wrapped) > 2:
        wrapped = [" ".join(wrapped[:-1]), wrapped[-1]]
    return r"\N".join(wrapped[:2]) if wrapped else ""


def filter_path(path: Path) -> str:
    value = str(path.resolve()).replace("\\", "/")
    value = value.replace(":", r"\:").replace("'", r"\'")
    return value


def output_profile(aspect_ratio: str, speed_mode: str = "ultra") -> dict:
    aspect_ratio = aspect_ratio if aspect_ratio in ALLOWED_ASPECTS else "9:16"
    speed_mode = speed_mode if speed_mode in ALLOWED_SPEEDS else "ultra"

    if speed_mode == "quality":
        size_916, size_169, size_11, original_box = (1080, 1920), (1920, 1080), (1080, 1080), (1920, 1080)
        speed_label = "Qualidade"
    elif speed_mode == "balanced":
        size_916, size_169, size_11, original_box = (900, 1600), (1600, 900), (900, 900), (1600, 900)
        speed_label = "Equilibrado"
    elif speed_mode == "turbo":
        size_916, size_169, size_11, original_box = (720, 1280), (1280, 720), (720, 720), (1280, 720)
        speed_label = "Turbo"
    else:
        # Ultra: feito para entregar rápido em PC comum.
        size_916, size_169, size_11, original_box = (540, 960), (960, 540), (540, 540), (960, 540)
        speed_label = "Ultra rápido"

    common = {"speed_mode": speed_mode, "speed_label": speed_label}
    if aspect_ratio == "16:9":
        w, h = size_169
        return {**common, "label": "16:9", "name": "PC / YouTube", "width": w, "height": h,
                "base_filter": f"scale={w}:{h}:force_original_aspect_ratio=decrease,pad={w}:{h}:(ow-iw)/2:(oh-ih)/2,setsar=1",
                "font_size": max(28, int(h * 0.052)), "margin_v": max(26, int(h * 0.06))}
    if aspect_ratio == "1:1":
        w, h = size_11
        return {**common, "label": "1:1", "name": "Feed quadrado", "width": w, "height": h,
                "base_filter": f"scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h},setsar=1",
                "font_size": max(28, int(h * 0.05)), "margin_v": max(28, int(h * 0.055))}
    if aspect_ratio == "original":
        w, h = original_box
        return {**common, "label": "Original", "name": "Original", "width": w, "height": h,
                "base_filter": f"scale={w}:{h}:force_original_aspect_ratio=decrease,setsar=1",
                "font_size": max(28, int(h * 0.052)), "margin_v": max(26, int(h * 0.06))}

    w, h = size_916
    return {**common, "label": "9:16", "name": "Celular / TikTok", "width": w, "height": h,
            "base_filter": f"scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h},setsar=1",
            "font_size": max(32, int(w * 0.064)), "margin_v": max(58, int(h * 0.065))}


def get_whisper_model(model_size: str):
    model_size = model_size if model_size in ALLOWED_MODELS else "tiny"
    if model_size in WHISPER_CACHE:
        return WHISPER_CACHE[model_size]
    try:
        from faster_whisper import WhisperModel
    except Exception as exc:
        raise RuntimeError(
            "Modulo de legenda nao instalado. Execute o INICIAR_CLIPNEX_WINDOWS.bat novamente e aguarde finalizar."
        ) from exc
    cpu_threads = max(2, (os.cpu_count() or 4) - 1)
    model = WhisperModel(model_size, device="cpu", compute_type="int8", cpu_threads=cpu_threads, download_root=str(MODEL_DIR))
    WHISPER_CACHE[model_size] = model
    return model


def extract_audio_range(source: Path, out_dir: Path, start: int, length: int, ffmpeg: str) -> Path:
    audio_path = out_dir / "clipnex_transcricao_range.wav"
    args = [ffmpeg, "-hide_banner", "-y", "-ss", str(start), "-i", str(source), "-t", str(length),
            "-vn", "-ac", "1", "-ar", "16000", "-f", "wav", str(audio_path)]
    proc = run_process(args, timeout=1800)
    if proc.returncode != 0 or not audio_path.exists() or audio_path.stat().st_size == 0:
        raise RuntimeError(f"Falha ao preparar audio para legenda. {clean_error(proc.stderr or proc.stdout)}")
    return audio_path


def transcribe_audio_range(audio_path: Path, model_size: str, language: str, offset_seconds: float) -> list[dict]:
    model = get_whisper_model(model_size)
    kwargs = {
        "beam_size": 1,
        "vad_filter": False,
        "word_timestamps": False,
        "condition_on_previous_text": False,
        "temperature": 0,
    }
    if language and language != "auto":
        kwargs["language"] = language
    segments_iter, _info = model.transcribe(str(audio_path), **kwargs)
    result = []
    for seg in segments_iter:
        text = re.sub(r"\s+", " ", seg.text or "").strip()
        if text:
            result.append({"start": float(seg.start) + offset_seconds, "end": float(seg.end) + offset_seconds, "text": text})
    return result


def create_clip_ass(out_dir: Path, clip_index: int, transcript: list[dict], start: int, length: int, profile: dict) -> Optional[Path]:
    clip_end = start + length
    entries = []
    for seg in transcript:
        if seg["end"] <= start or seg["start"] >= clip_end:
            continue
        rel_start = max(0.0, seg["start"] - start)
        rel_end = min(float(length), seg["end"] - start)
        if rel_end - rel_start < 0.25:
            rel_end = min(float(length), rel_start + 0.7)
        text = ass_escape(seg["text"])
        if text:
            entries.append((rel_start, rel_end, text))
    if not entries:
        return None

    ass_path = out_dir / f"clipnex_corte_{clip_index:03d}.ass"
    w, h = profile["width"], profile["height"]
    font_size, margin_v = profile["font_size"], profile["margin_v"]
    content = [
        "[Script Info]",
        "ScriptType: v4.00+",
        f"PlayResX: {w}",
        f"PlayResY: {h}",
        "ScaledBorderAndShadow: yes",
        "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding",
        f"Style: Default,Arial,{font_size},&H00FFFFFF,&H00FFFFFF,&H00000000,&H90000000,-1,0,0,0,100,100,0,0,1,5,2,2,62,62,{margin_v},1",
        "",
        "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]
    for rel_start, rel_end, text in entries:
        content.append(f"Dialogue: 0,{ass_time(rel_start)},{ass_time(rel_end)},Default,,0,0,0,,{text}")
    ass_path.write_text("\n".join(content), encoding="utf-8")
    return ass_path


def build_video_filter(profile: dict, ass_file: Optional[Path]) -> str:
    vf = profile["base_filter"]
    if ass_file:
        vf += f",ass='{filter_path(ass_file)}'"
    return vf


def render_one_clip(source: Path, out_dir: Path, idx: int, start: int, length: int, ffmpeg: str, profile: dict,
                    ass_file: Optional[Path], encoder_mode: str, speed_mode: str, allow_fast_copy: bool) -> dict:
    output_name = f"clipnex_corte_{idx:03d}.mp4"
    output_path = out_dir / output_name

    # Caminho ultra instantâneo: sem legenda + proporção original = sem reencode.
    if allow_fast_copy and not ass_file and profile["label"] == "Original":
        args = [ffmpeg, "-hide_banner", "-y", "-ss", str(start), "-i", str(source), "-t", str(length),
                "-map", "0:v:0", "-map", "0:a?", "-c", "copy", "-avoid_negative_ts", "make_zero", str(output_path)]
        proc = run_process(args, timeout=1800)
        if proc.returncode == 0 and output_path.exists() and output_path.stat().st_size > 0:
            return _clip_dict(idx, output_name, output_path, length, start, profile, False, "Copia direta")

    vf = build_video_filter(profile, ass_file)
    encoder = pick_video_encoder(ffmpeg, encoder_mode, speed_mode)
    base_args = [ffmpeg, "-hide_banner", "-y", "-ss", str(start), "-i", str(source), "-t", str(length),
                 "-vf", vf, "-map", "0:v:0", "-map", "0:a?", *encoder["args"], "-threads", "0",
                 "-c:a", "aac", "-b:a", "96k", "-movflags", "+faststart", str(output_path)]
    proc = run_process(base_args, timeout=7200)

    # Fallback seguro se encoder GPU der problema no PC do usuário.
    if proc.returncode != 0 or not output_path.exists() or output_path.stat().st_size == 0:
        if encoder["hardware"]:
            output_path.unlink(missing_ok=True)
            cpu_encoder = pick_video_encoder(ffmpeg, "cpu", speed_mode)
            fallback = [ffmpeg, "-hide_banner", "-y", "-ss", str(start), "-i", str(source), "-t", str(length),
                        "-vf", vf, "-map", "0:v:0", "-map", "0:a?", *cpu_encoder["args"], "-threads", "0",
                        "-c:a", "aac", "-b:a", "96k", "-movflags", "+faststart", str(output_path)]
            proc = run_process(fallback, timeout=7200)
            encoder = cpu_encoder

    if proc.returncode != 0 or not output_path.exists() or output_path.stat().st_size == 0:
        detail = clean_error(proc.stderr or proc.stdout)
        raise RuntimeError(f"Falha ao processar o corte {idx}. {detail}")

    return _clip_dict(idx, output_name, output_path, length, start, profile, bool(ass_file), encoder["label"])


def _clip_dict(idx: int, output_name: str, output_path: Path, length: int, start: int, profile: dict, captions: bool, encoder_label: str) -> dict:
    return {
        "index": idx,
        "title": f"Corte {idx:03d}",
        "duration": ffmpeg_time(length)[3:] if length < 3600 else ffmpeg_time(length),
        "source_range": f"{ffmpeg_time(start)} — {ffmpeg_time(start + length)}",
        "filename": output_name,
        "download_url": f"/api/download/{output_path.parent.name}/{output_name}",
        "size_mb": round(output_path.stat().st_size / 1024 / 1024, 2),
        "aspect_ratio": profile["label"],
        "captions": captions,
        "encoder": encoder_label,
    }


def recommended_workers(clip_count: int, captions: bool, speed_mode: str, encoder_mode: str) -> int:
    cpus = os.cpu_count() or 4
    if clip_count <= 1:
        return 1
    if encoder_mode in {"auto", "nvenc", "qsv", "amf"} and speed_mode in {"ultra", "turbo"}:
        return min(3, max(1, clip_count))
    if captions:
        return min(2, max(1, cpus // 3), clip_count)
    return min(3, max(1, cpus // 2), clip_count)


def render_clips(source: Path, project_id: str, duration_seconds: int, clip_count: int, aspect_ratio: str,
                 captions: bool, subtitle_model: str, language: str, speed_mode: str = "ultra",
                 encoder_mode: str = "auto", download_quality: str = "fast") -> dict:
    ffmpeg = get_ffmpeg_path()
    total = get_duration_seconds(source, ffmpeg)
    duration_seconds = max(5, min(int(duration_seconds or 60), 600))
    available_clips = int(math.floor(total / duration_seconds))
    segments = build_segments(total, duration_seconds, clip_count)

    aspect_ratio = aspect_ratio if aspect_ratio in ALLOWED_ASPECTS else "9:16"
    speed_mode = speed_mode if speed_mode in ALLOWED_SPEEDS else "ultra"
    encoder_mode = encoder_mode if encoder_mode in ALLOWED_ENCODERS else "auto"
    profile = output_profile(aspect_ratio, speed_mode)

    out_dir = PROJECT_DIR / project_id
    if out_dir.exists():
        shutil.rmtree(out_dir, ignore_errors=True)
    out_dir.mkdir(parents=True, exist_ok=True)

    transcript: list[dict] = []
    ass_files: dict[int, Optional[Path]] = {}
    if captions:
        # GRANDE CORRECAO DE VELOCIDADE:
        # Antes: transcrevia cada corte separadamente.
        # Agora: extrai/transcreve UMA vez apenas o range total necessário.
        min_start = min(start for start, _length in segments)
        max_end = max(start + length for start, length in segments)
        audio_path = extract_audio_range(source, out_dir, min_start, max_end - min_start, ffmpeg)
        transcript = transcribe_audio_range(audio_path, subtitle_model, language, offset_seconds=min_start)
        for idx, (start, length) in enumerate(segments, start=1):
            ass_files[idx] = create_clip_ass(out_dir, idx, transcript, start, length, profile)
    else:
        for idx, _seg in enumerate(segments, start=1):
            ass_files[idx] = None

    workers = recommended_workers(len(segments), captions, speed_mode, encoder_mode)
    clips: list[dict] = []
    with ThreadPoolExecutor(max_workers=workers) as executor:
        future_map = {}
        for idx, (start, length) in enumerate(segments, start=1):
            future = executor.submit(
                render_one_clip,
                source, out_dir, idx, start, length, ffmpeg, profile, ass_files.get(idx),
                encoder_mode, speed_mode, True,
            )
            future_map[future] = idx
        for future in as_completed(future_map):
            clips.append(future.result())
    clips.sort(key=lambda item: item["index"])

    zip_path = ZIP_DIR / f"{project_id}_clipnex_cortes.zip"
    if zip_path.exists():
        zip_path.unlink()
    shutil.make_archive(str(zip_path.with_suffix("")), "zip", root_dir=out_dir)

    encoder_info = pick_video_encoder(ffmpeg, encoder_mode, speed_mode)
    meta = {
        "project_id": project_id,
        "created_at": time.time(),
        "source": str(source),
        "source_duration_seconds": total,
        "clip_duration_seconds": duration_seconds,
        "clip_count": len(clips),
        "requested_clip_count": int(clip_count),
        "available_clips": available_clips,
        "aspect_ratio": profile["label"],
        "aspect_name": profile["name"],
        "captions_enabled": captions,
        "subtitle_model": subtitle_model if captions else None,
        "speed_mode": profile["speed_mode"],
        "speed_label": profile["speed_label"],
        "encoder": encoder_info["label"],
        "workers": workers,
        "download_quality": download_quality,
        "transcript_segments": len(transcript),
        "clips": clips,
        "zip_url": f"/api/zip/{project_id}",
    }
    (out_dir / "metadata.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    return meta




def process_source_to_meta(project_id: str, source_path: Optional[Path], video_url: str, params: dict) -> dict:
    ffmpeg = get_ffmpeg_path()
    if source_path:
        source = Path(source_path)
    else:
        source = download_youtube(video_url, project_id, ffmpeg, params.get("download_quality", "fast"))
    return render_clips(
        source=source,
        project_id=project_id,
        duration_seconds=int(params.get("duration_seconds", 60)),
        clip_count=int(params.get("clip_count", 10)),
        aspect_ratio=str(params.get("aspect_ratio", "9:16")),
        captions=bool(params.get("captions", True)),
        subtitle_model=str(params.get("subtitle_model", "tiny")),
        language=str(params.get("language", "pt")),
        speed_mode=str(params.get("speed_mode", "ultra")),
        encoder_mode=str(params.get("encoder_mode", "auto")),
        download_quality=str(params.get("download_quality", "fast")),
    )


def run_job(job_id: str, project_id: str, source_path: Optional[str], video_url: str, params: dict) -> None:
    JOBS[job_id]["status"] = "running"
    JOBS[job_id]["message"] = "Baixando/analisando video e preparando cortes."
    try:
        meta = process_source_to_meta(project_id, Path(source_path) if source_path else None, video_url, params)
        JOBS[job_id].update({"status": "done", "message": "Cortes prontos.", "result": meta, "finished_at": time.time()})
    except Exception as exc:
        JOBS[job_id].update({"status": "error", "message": "Falha ao processar.", "error": clean_error(str(exc), limit=1800), "finished_at": time.time()})


def normalize_params(duration_seconds: int, clip_count: int, aspect_ratio: str, captions: bool, subtitle_model: str,
                     language: str, speed_mode: str, encoder_mode: str, download_quality: str, vertical: Optional[bool] = None) -> dict:
    if aspect_ratio not in ALLOWED_ASPECTS:
        aspect_ratio = "9:16" if (vertical is None or vertical is True) else "original"
    return {
        "duration_seconds": max(5, min(int(duration_seconds or 60), 600)),
        "clip_count": max(0, min(int(clip_count or 10), MAX_CLIPS)),
        "aspect_ratio": aspect_ratio,
        "captions": bool(captions),
        "subtitle_model": subtitle_model if subtitle_model in ALLOWED_MODELS else "tiny",
        "language": language if language in {"pt", "en", "es", "auto"} else "pt",
        "speed_mode": speed_mode if speed_mode in ALLOWED_SPEEDS else "ultra",
        "encoder_mode": encoder_mode if encoder_mode in ALLOWED_ENCODERS else "auto",
        "download_quality": download_quality if download_quality in ALLOWED_DOWNLOAD_QUALITY else "fast",
    }

@app.get("/", response_class=HTMLResponse)
def home() -> HTMLResponse:
    index = WEB_DIR / "index.html"
    return HTMLResponse(index.read_text(encoding="utf-8"))


@app.post("/api/process")
async def process_video(
    video_url: str = Form(""),
    duration_seconds: int = Form(60),
    clip_count: int = Form(10),
    aspect_ratio: str = Form("9:16"),
    captions: bool = Form(True),
    subtitle_model: str = Form("tiny"),
    language: str = Form("pt"),
    speed_mode: str = Form("ultra"),
    encoder_mode: str = Form("auto"),
    download_quality: str = Form("fast"),
    video_file: Optional[UploadFile] = File(None),
    vertical: Optional[bool] = Form(None),
):
    project_id = uuid.uuid4().hex[:12]
    try:
        cleanup_old_files(max_age_hours=int(os.getenv("CLIPNEX_CLEANUP_HOURS", "24")))
        video_url = (video_url or "").strip()
        has_file = bool(video_file and video_file.filename)
        has_url = looks_like_url(video_url)
        if not has_url and not has_file:
            raise HTTPException(status_code=400, detail="Cole um link do YouTube ou envie um arquivo de video.")
        params = normalize_params(duration_seconds, clip_count, aspect_ratio, captions, subtitle_model, language, speed_mode, encoder_mode, download_quality, vertical)
        source_path = save_upload(video_file, project_id) if has_file else None
        meta = process_source_to_meta(project_id, source_path, video_url, params)
        return JSONResponse(meta)
    except HTTPException:
        raise
    except Exception as exc:
        return JSONResponse(status_code=500, content={"error": clean_error(str(exc), limit=1800)})


@app.post("/api/jobs")
async def create_processing_job(
    video_url: str = Form(""),
    duration_seconds: int = Form(60),
    clip_count: int = Form(10),
    aspect_ratio: str = Form("9:16"),
    captions: bool = Form(True),
    subtitle_model: str = Form("tiny"),
    language: str = Form("pt"),
    speed_mode: str = Form("ultra"),
    encoder_mode: str = Form("auto"),
    download_quality: str = Form("fast"),
    video_file: Optional[UploadFile] = File(None),
    vertical: Optional[bool] = Form(None),
):
    project_id = uuid.uuid4().hex[:12]
    job_id = uuid.uuid4().hex[:12]
    try:
        cleanup_old_files(max_age_hours=int(os.getenv("CLIPNEX_CLEANUP_HOURS", "24")))
        video_url = (video_url or "").strip()
        has_file = bool(video_file and video_file.filename)
        has_url = looks_like_url(video_url)
        if not has_url and not has_file:
            raise HTTPException(status_code=400, detail="Cole um link do YouTube ou envie um arquivo de video.")
        params = normalize_params(duration_seconds, clip_count, aspect_ratio, captions, subtitle_model, language, speed_mode, encoder_mode, download_quality, vertical)
        source_path = str(save_upload(video_file, project_id)) if has_file else None
        JOBS[job_id] = {
            "job_id": job_id,
            "project_id": project_id,
            "status": "queued",
            "message": "Projeto recebido. Aguardando processamento.",
            "created_at": time.time(),
            "params": params,
        }
        JOB_EXECUTOR.submit(run_job, job_id, project_id, source_path, video_url, params)
        return JSONResponse({"ok": True, "job_id": job_id, "status": "queued", "message": JOBS[job_id]["message"]})
    except HTTPException:
        raise
    except Exception as exc:
        return JSONResponse(status_code=500, content={"error": clean_error(str(exc), limit=1800)})


@app.get("/api/jobs/{job_id}")
def get_processing_job(job_id: str):
    job_id = assert_safe_id(job_id, "job_id")
    job = JOBS.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job nao encontrado.")
    return JSONResponse(job)


@app.get("/api/download/{project_id}/{filename}")
def download_clip(project_id: str, filename: str):
    project_id = assert_safe_id(project_id, "project_id")
    filename = assert_safe_filename(filename)
    path = (PROJECT_DIR / project_id / filename).resolve()
    project_root = (PROJECT_DIR / project_id).resolve()
    if project_root not in path.parents or not path.exists() or path.suffix.lower() != ".mp4":
        raise HTTPException(status_code=404, detail="Corte nao encontrado.")
    return FileResponse(str(path), filename=filename, media_type="video/mp4")


@app.get("/api/zip/{project_id}")
def download_zip(project_id: str):
    project_id = assert_safe_id(project_id, "project_id")
    path = (ZIP_DIR / f"{project_id}_clipnex_cortes.zip").resolve()
    if ZIP_DIR.resolve() not in path.parents or not path.exists():
        raise HTTPException(status_code=404, detail="ZIP nao encontrado.")
    return FileResponse(str(path), filename="clipnex_cortes.zip", media_type="application/zip")


@app.get("/healthz")
def healthz():
    return {"ok": True, "service": "clipnex-ai"}


@app.get("/api/health")
def health():
    try:
        ffmpeg = get_ffmpeg_path()
        encoder_auto = pick_video_encoder(ffmpeg, "auto", "ultra")
        return {
            "ok": True,
            "ffmpeg": ffmpeg,
            "data_dir": str(DATA_DIR),
            "max_clips": MAX_CLIPS,
            "max_upload_mb": MAX_UPLOAD_MB,
            "encoder": encoder_auto["label"],
            "features": [
                "youtube", "upload", "aspect_ratio", "captions", "ultra_mode",
                "single_range_transcription", "gpu_encoder_auto", "parallel_render",
                "docker_ready", "persistent_data_dir", "safe_download_paths",
            ],
        }
    except Exception as exc:
        return {"ok": False, "error": str(exc)}

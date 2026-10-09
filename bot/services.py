import asyncio
import logging
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from yt_dlp import YoutubeDL

from . import config

try:
    from shazamio import Shazam
except Exception:  # pragma: no cover
    Shazam = None

log = logging.getLogger(__name__)

JOBS = asyncio.Semaphore(config.MAX_JOBS)
_shazam = None


class Skipped(Exception):
    """Yuklash ataylab o'tkazib yuborildi: reason = 'long' | 'live'."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


@dataclass
class Result:
    files: list[Path] = field(default_factory=list)
    title: str = ""


@dataclass
class Song:
    path: Path
    title: str
    artist: str


def tmp(ext: str, prefix: str = "f") -> Path:
    config.TMP_DIR.mkdir(parents=True, exist_ok=True)
    return config.TMP_DIR / f"{prefix}_{uuid.uuid4().hex[:10]}{ext}"


# ---------------- ffmpeg ----------------
async def ffmpeg(*args: str, timeout: int = 300) -> bool:
    proc = await asyncio.create_subprocess_exec(
        "ffmpeg", "-y", "-loglevel", "error", *args,
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
    try:
        _, err = await asyncio.wait_for(proc.communicate(), timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return False
    if proc.returncode != 0:
        log.warning("ffmpeg xato: %s", err.decode(errors="ignore")[-400:])
    return proc.returncode == 0


async def make_circle(src: Path) -> Path | None:
    out = tmp(".mp4", "circle")
    ok = await ffmpeg(
        "-i", str(src), "-t", "60",
        "-vf", "crop='min(iw,ih)':'min(iw,ih)',scale=640:640,setsar=1",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "26", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "96k", "-movflags", "+faststart", str(out))
    return out if ok and out.exists() else None


async def to_mp3(src: Path) -> Path | None:
    out = tmp(".mp3", "audio")
    ok = await ffmpeg("-i", str(src), "-vn", "-c:a", "libmp3lame", "-b:a", "192k", str(out))
    return out if ok and out.exists() else None


async def shrink(src: Path) -> Path | None:
    """Katta videoni 480p ga kichraytiradi. Sig'masa None."""
    out = tmp(".mp4", "small")
    ok = await ffmpeg(
        "-i", str(src), "-vf", "scale=-2:'min(480,ih)'",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "32", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "96k", "-movflags", "+faststart", str(out), timeout=900)
    if ok and out.exists() and out.stat().st_size <= config.MAX_UPLOAD:
        return out
    out.unlink(missing_ok=True)
    return None


# ---------------- Shazam ----------------
async def recognize(src: Path) -> dict | None:
    global _shazam
    if Shazam is None:
        return None
    if _shazam is None:
        _shazam = Shazam()
    sample = tmp(".mp3", "sample")
    try:
        if not await ffmpeg("-i", str(src), "-vn", "-t", "30", "-ac", "1", "-ar", "44100", str(sample)):
            return None
        fn = getattr(_shazam, "recognize", None) or getattr(_shazam, "recognize_song")
        res = await fn(str(sample))
    finally:
        sample.unlink(missing_ok=True)
    track = (res or {}).get("track")
    if not track:
        return None
    return {
        "title": track.get("title", "?"),
        "artist": track.get("subtitle", "?"),
        "url": track.get("url"),
        "cover": (track.get("images") or {}).get("coverarthq") or (track.get("images") or {}).get("coverart"),
    }


# ---------------- yt-dlp ----------------
def _base() -> dict:
    o = {"quiet": True, "no_warnings": True, "noprogress": True,
         "socket_timeout": 30, "retries": 3, "noplaylist": True}
    if config.COOKIES_FILE and Path(config.COOKIES_FILE).exists():
        o["cookiefile"] = config.COOKIES_FILE
    return o


VIDEO_FORMAT = ("bv*[height<=720][vcodec^=avc]+ba[acodec^=mp4a]"
                "/bv*[height<=720][ext=mp4]+ba[ext=m4a]"
                "/b[height<=720][ext=mp4]/bv*[height<=720]+ba/b")


def _download(url: str) -> Result:
    config.TMP_DIR.mkdir(parents=True, exist_ok=True)
    prefix = uuid.uuid4().hex[:8]
    skip: dict[str, str] = {}

    def flt(info, *, incomplete):
        if info.get("is_live"):
            skip["r"] = "live"
            return "live"
        d = info.get("duration")
        if d and d > config.MAX_VIDEO_SEC:
            skip["r"] = "long"
            return "long"
        return None

    opts = _base() | {
        "outtmpl": str(config.TMP_DIR / f"{prefix}_%(id).50s.%(ext)s"),
        "format": VIDEO_FORMAT, "merge_output_format": "mp4",
        "playlist_items": "1-10", "match_filter": flt,
    }
    with YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)

    found: list[Path] = []
    entries = (info or {}).get("entries")
    for e in (list(entries) if entries else [info]):
        for d in (e or {}).get("requested_downloads") or []:
            p = Path(d.get("filepath", ""))
            if p.is_file() and p not in found:
                found.append(p)
    if not found:
        found = sorted(config.TMP_DIR.glob(f"{prefix}_*"))
    bad = {".part", ".ytdl", ".json", ".vtt", ".temp"}
    found = [p for p in found if p.suffix.lower() not in bad]
    if not found and skip:
        raise Skipped(skip["r"])
    return Result(found, (info or {}).get("title") or "")


async def download(url: str) -> Result:
    async with JOBS:
        return await asyncio.to_thread(_download, url)


def _song(target: str) -> Song | None:
    config.TMP_DIR.mkdir(parents=True, exist_ok=True)
    prefix = "song_" + uuid.uuid4().hex[:8]

    def flt(info, *, incomplete):
        d = info.get("duration")
        return "long" if d and d > config.MAX_SONG_SEC else None

    opts = _base() | {
        "outtmpl": str(config.TMP_DIR / f"{prefix}.%(ext)s"),
        "format": "bestaudio/best", "match_filter": flt,
        "postprocessors": [{"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "192"}],
    }
    with YoutubeDL(opts) as ydl:
        info = ydl.extract_info(target, download=True)
    entries = (info or {}).get("entries")
    entry = (list(entries)[0] if entries else info) or {}
    files = sorted(config.TMP_DIR.glob(f"{prefix}.*"))
    mp3 = [p for p in files if p.suffix == ".mp3"]
    if not mp3:
        for p in files:
            p.unlink(missing_ok=True)
        return None
    for p in files:
        if p not in mp3:
            p.unlink(missing_ok=True)
    return Song(mp3[0], entry.get("track") or entry.get("title") or "Audio",
                entry.get("artist") or entry.get("uploader") or entry.get("channel") or "")


async def download_song(target: str) -> Song | None:
    async with JOBS:
        return await asyncio.to_thread(_song, target)


def _search(q: str, n: int) -> list[dict]:
    opts = _base() | {"extract_flat": True, "skip_download": True}
    with YoutubeDL(opts) as ydl:
        info = ydl.extract_info(f"ytsearch{n}:{q}", download=False)
    out = []
    for e in (info or {}).get("entries") or []:
        if not e or not e.get("id"):
            continue
        d = int(e.get("duration") or 0)
        if d > config.MAX_SONG_SEC:
            continue
        out.append({"id": e["id"], "title": e.get("title") or "?", "duration": d,
                    "channel": e.get("channel") or e.get("uploader") or ""})
    return out


async def search(q: str, n: int = 8) -> list[dict]:
    return await asyncio.to_thread(_search, q, n)

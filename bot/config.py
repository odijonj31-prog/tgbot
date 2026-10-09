import os
import tempfile
from pathlib import Path


def _ids(raw: str) -> set[int]:
    return {int(x) for x in raw.replace(" ", "").split(",") if x.lstrip("-").isdigit()}


BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
ADMIN_IDS = _ids(os.getenv("ADMIN_IDS", ""))
DATA_DIR = Path(os.getenv("DATA_DIR", "data"))
DB_PATH = DATA_DIR / "bot.db"
TMP_DIR = Path(tempfile.gettempdir()) / "tgbot"
COOKIES_FILE = os.getenv("COOKIES_FILE") or None
MAX_JOBS = int(os.getenv("MAX_JOBS", "3"))

MAX_UPLOAD = 49 * 1024 * 1024            # botdan yuborish limiti (50 MB dan biroz past)
MAX_DOWNLOAD_FROM_TG = 20 * 1024 * 1024  # botga yuborilgan fayl limiti
FILE_TTL = 3600                          # vaqtinchalik fayllar 1 soat
MAX_VIDEO_SEC = int(os.getenv("MAX_VIDEO_MIN", "30")) * 60
MAX_SONG_SEC = 15 * 60
SUB_CACHE_TTL = 60                       # obuna tekshiruvi natijasi necha soniya eslab qolinadi

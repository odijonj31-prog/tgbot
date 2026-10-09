import json
import time
from datetime import datetime, timezone

import aiosqlite

_db: aiosqlite.Connection | None = None
_S: dict[str, str] = {}

SCHEMA = """
CREATE TABLE IF NOT EXISTS users(
  id INTEGER PRIMARY KEY, username TEXT, first_name TEXT,
  lang TEXT DEFAULT 'uz', lang_set INTEGER DEFAULT 0,
  joined_at INTEGER, last_seen INTEGER,
  banned INTEGER DEFAULT 0, active INTEGER DEFAULT 1,
  auto_circle INTEGER DEFAULT 1, auto_music INTEGER DEFAULT 1,
  downloads INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS channels(chat_id INTEGER PRIMARY KEY, title TEXT, link TEXT);
CREATE TABLE IF NOT EXISTS stats(day TEXT, key TEXT, n INTEGER DEFAULT 0, PRIMARY KEY(day, key));
CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS usage(uid INTEGER, day TEXT, n INTEGER DEFAULT 0, PRIMARY KEY(uid, day));
CREATE TABLE IF NOT EXISTS media_cache(key TEXT PRIMARY KEY, payload TEXT, title TEXT, ts INTEGER);
CREATE TABLE IF NOT EXISTS history(id INTEGER PRIMARY KEY AUTOINCREMENT, uid INTEGER, url TEXT, title TEXT, ts INTEGER);
CREATE INDEX IF NOT EXISTS h_uid ON history(uid, id);
"""


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _midnight() -> int:
    n = datetime.now(timezone.utc)
    return int(n.replace(hour=0, minute=0, second=0, microsecond=0).timestamp())


async def init(path) -> None:
    global _db
    path.parent.mkdir(parents=True, exist_ok=True)
    _db = await aiosqlite.connect(path)
    _db.row_factory = aiosqlite.Row
    await _db.execute("PRAGMA journal_mode=WAL")
    await _db.executescript(SCHEMA)
    await _db.commit()
    async with _db.execute("SELECT key, value FROM settings") as cur:
        for r in await cur.fetchall():
            _S[r["key"]] = r["value"]


async def close() -> None:
    if _db:
        await _db.close()


async def _one(sql, *args):
    async with _db.execute(sql, args) as cur:
        return await cur.fetchone()


async def _all(sql, *args):
    async with _db.execute(sql, args) as cur:
        return await cur.fetchall()


async def _run(sql, *args):
    await _db.execute(sql, args)
    await _db.commit()


# ---------- settings ----------
def setting(key: str, default: str = "") -> str:
    return _S.get(key, default)


async def set_setting(key: str, value: str) -> None:
    _S[key] = value
    await _run("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", key, value)


# ---------- users ----------
def _lang_from(code) -> str:
    code = (code or "")[:2].lower()
    return code if code in ("ru", "en") else "uz"


async def get_user(uid: int):
    return await _one("SELECT * FROM users WHERE id=?", uid)


async def upsert_user(u):
    now = int(time.time())
    row = await get_user(u.id)
    if row is None:
        await _run("INSERT OR IGNORE INTO users(id,username,first_name,lang,joined_at,last_seen) VALUES(?,?,?,?,?,?)",
                   u.id, u.username, u.first_name, _lang_from(u.language_code), now, now)
    elif (now - (row["last_seen"] or 0) > 60 or row["username"] != u.username
          or row["first_name"] != u.first_name or not row["active"]):
        await _run("UPDATE users SET username=?, first_name=?, last_seen=?, active=1 WHERE id=?",
                   u.username, u.first_name, now, u.id)
    else:
        return row
    return await get_user(u.id)


async def set_lang(uid: int, lang: str) -> None:
    await _run("UPDATE users SET lang=?, lang_set=1 WHERE id=?", lang, uid)


async def toggle_flag(uid: int, col: str) -> None:
    assert col in ("auto_circle", "auto_music")
    await _run(f"UPDATE users SET {col} = 1 - {col} WHERE id=?", uid)


async def set_banned(uid: int, banned: bool) -> None:
    await _run("UPDATE users SET banned=? WHERE id=?", int(banned), uid)


async def set_inactive(uid: int) -> None:
    await _run("UPDATE users SET active=0 WHERE id=?", uid)


async def bump_downloads(uid: int) -> None:
    await _run("UPDATE users SET downloads = downloads + 1 WHERE id=?", uid)


async def audience(lang: str | None = None) -> list[int]:
    if lang:
        rows = await _all("SELECT id FROM users WHERE active=1 AND banned=0 AND lang=?", lang)
    else:
        rows = await _all("SELECT id FROM users WHERE active=1 AND banned=0")
    return [r["id"] for r in rows]


async def all_users():
    return await _all("SELECT * FROM users ORDER BY joined_at")


# ---------- stats ----------
async def incr(key: str, n: int = 1) -> None:
    await _run("INSERT INTO stats(day,key,n) VALUES(?,?,?) ON CONFLICT(day,key) DO UPDATE SET n = n + excluded.n",
               _today(), key, n)


async def summary() -> dict:
    now = int(time.time())
    mid = _midnight()
    r = {}
    r["total"] = (await _one("SELECT COUNT(*) c FROM users"))["c"]
    r["active"] = (await _one("SELECT COUNT(*) c FROM users WHERE active=1"))["c"]
    r["banned"] = (await _one("SELECT COUNT(*) c FROM users WHERE banned=1"))["c"]
    r["new_today"] = (await _one("SELECT COUNT(*) c FROM users WHERE joined_at>=?", mid))["c"]
    r["new_7d"] = (await _one("SELECT COUNT(*) c FROM users WHERE joined_at>=?", now - 7 * 86400))["c"]
    r["online_24h"] = (await _one("SELECT COUNT(*) c FROM users WHERE last_seen>=?", now - 86400))["c"]
    r["langs"] = {x["lang"]: x["c"] for x in await _all("SELECT lang, COUNT(*) c FROM users GROUP BY lang")}
    r["today"] = {x["key"]: x["n"] for x in await _all("SELECT key, n FROM stats WHERE day=?", _today())}
    r["all"] = {x["key"]: x["n"] for x in await _all("SELECT key, SUM(n) n FROM stats GROUP BY key")}
    r["top"] = await _all("SELECT id, username, first_name, downloads FROM users ORDER BY downloads DESC LIMIT 5")
    return r


# ---------- daily usage ----------
async def usage_get(uid: int) -> int:
    row = await _one("SELECT n FROM usage WHERE uid=? AND day=?", uid, _today())
    return row["n"] if row else 0


async def usage_add(uid: int) -> None:
    await _run("INSERT INTO usage(uid,day,n) VALUES(?,?,1) ON CONFLICT(uid,day) DO UPDATE SET n = n + 1", uid, _today())
    await _run("DELETE FROM usage WHERE day < ?", _today())


# ---------- channels ----------
async def list_channels():
    return await _all("SELECT * FROM channels ORDER BY rowid")


async def add_channel(chat_id: int, title: str, link: str) -> None:
    await _run("INSERT OR REPLACE INTO channels(chat_id,title,link) VALUES(?,?,?)", chat_id, title, link)


async def del_channel(chat_id: int) -> None:
    await _run("DELETE FROM channels WHERE chat_id=?", chat_id)


# ---------- file_id cache ----------
async def cache_get(key: str):
    return await _one("SELECT * FROM media_cache WHERE key=?", key)


async def cache_put(key: str, items: list, title: str) -> None:
    await _run("INSERT OR REPLACE INTO media_cache(key,payload,title,ts) VALUES(?,?,?,?)",
               key, json.dumps(items), title or "", int(time.time()))


async def cache_del(key: str) -> None:
    await _run("DELETE FROM media_cache WHERE key=?", key)


# ---------- history ----------
async def history_add(uid: int, url: str, title: str) -> None:
    await _run("INSERT INTO history(uid,url,title,ts) VALUES(?,?,?,?)", uid, url, title or "", int(time.time()))
    await _run("DELETE FROM history WHERE uid=? AND id NOT IN "
               "(SELECT id FROM history WHERE uid=? ORDER BY id DESC LIMIT 20)", uid, uid)


async def history_list(uid: int, n: int = 10):
    return await _all("SELECT url, title FROM history WHERE uid=? ORDER BY id DESC LIMIT ?", uid, n)

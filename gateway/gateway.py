"""
WhatsApp for Claude: the gateway.

Claude talks to this gateway at https://<domain>/<MCP_SECRET>/mcp. The gateway:
  - passes every tool of the WhatsApp server (sealjay/mcp-whatsapp, next door in Docker)
    through, adding the password header that server wants;
  - adds what that server can't do: read_attachment (photos, PDFs, Word, Excel, text,
    and voice messages written out, Persian too) and scheduled messages;
  - keeps the WhatsApp data under STORAGE_LIMIT_MB by removing the oldest downloaded
    files first, then the oldest messages;
  - shows the WhatsApp pairing page at https://<domain>/<MCP_SECRET>/pair.

    python gateway.py             run the gateway
    python gateway.py --compact   shrink the message database (stop the WhatsApp server first)
"""

import asyncio
import io
import json
import logging
import os
import re
import secrets
import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
import uvicorn
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.server.fastmcp import FastMCP, Image
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import CallToolResult, TextContent, Tool, ToolAnnotations
from PIL import Image as PILImage, ImageOps
from starlette.requests import Request
from starlette.responses import Response

DATA_DIR = Path(os.environ.get("DATA_DIR", "/data"))
SCHEDULE_FILE = DATA_DIR / "scheduled.json"
SECRET = os.environ.get("MCP_SECRET", "").strip()
PORT = int(os.environ.get("PORT", "8000"))
UPSTREAM = os.environ.get("WHATSAPP_URL", "http://whatsapp:8765").rstrip("/")
UPSTREAM_TOKEN = os.environ.get("WHATSAPP_TOKEN", "").strip()
TZ = ZoneInfo(os.environ.get("TIMEZONE", "UTC"))
HIDE_TOOLS = {t.strip() for t in os.environ.get("HIDE_TOOLS", "").split(",") if t.strip()}
MAX_ATTACHMENT_MB = int(os.environ.get("MAX_ATTACHMENT_MB", "20"))
STORE_DIR = Path(os.environ.get("STORE_DIR", "/home/app/store"))  # the WhatsApp server's data
STORAGE_LIMIT_MB = int(os.environ.get("STORAGE_LIMIT_MB", "1024"))  # 0 = no limit
APP_UID = 10001           # the user the WhatsApp server runs as; the gateway runs as it too
MAX_SCHEDULED = 50        # waiting scheduled messages at most
SEND_GRACE = timedelta(hours=1)  # a scheduled message still goes out this late, not later
MAX_TEXT_CHARS = 60_000   # how much text one read_attachment call returns
IMAGE_SIDE = 1568         # pictures are shrunk to this many pixels on the long side
SCANNED_PAGES = 5         # pages shown as pictures when a PDF has no text in it
MB = 1024 * 1024

# Speech-to-text for voice messages (any OpenAI-compatible service).
TRANSCRIBE_API_KEY = os.environ.get("TRANSCRIBE_API_KEY", "").strip()
TRANSCRIBE_URL = os.environ.get("TRANSCRIBE_URL", "https://api.openai.com/v1/audio/transcriptions")
TRANSCRIBE_MODEL = os.environ.get("TRANSCRIBE_MODEL", "gpt-4o-transcribe")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("whatsapp-gateway")

if len(SECRET) < 32:
    raise SystemExit("MCP_SECRET is missing or too short. Run setup.sh.")
if not UPSTREAM_TOKEN:
    raise SystemExit("WHATSAPP_TOKEN is missing. Run setup.sh.")


# ---------------------------------------------------------------- the WhatsApp server
async def upstream_call(name: str, arguments: dict) -> CallToolResult:
    async with httpx.AsyncClient(headers={"Authorization": f"Bearer {UPSTREAM_TOKEN}"},
                                 timeout=httpx.Timeout(120, connect=10)) as http:
        async with streamable_http_client(f"{UPSTREAM}/mcp", http_client=http) as (r, w, _):
            async with ClientSession(r, w) as session:
                await session.initialize()
                return await session.call_tool(name, arguments)


_tools_cache: tuple[float, list[Tool]] = (0.0, [])


async def upstream_tools() -> list[Tool]:
    global _tools_cache
    loop = asyncio.get_running_loop()
    if _tools_cache[1] and loop.time() - _tools_cache[0] < 300:
        return _tools_cache[1]
    async with httpx.AsyncClient(headers={"Authorization": f"Bearer {UPSTREAM_TOKEN}"},
                                 timeout=httpx.Timeout(30, connect=10)) as http:
        async with streamable_http_client(f"{UPSTREAM}/mcp", http_client=http) as (r, w, _):
            async with ClientSession(r, w) as session:
                await session.initialize()
                tools = (await session.list_tools()).tools
    _tools_cache = (loop.time(), tools)
    return tools


def _first_json(result: CallToolResult) -> dict:
    for block in result.content:
        if isinstance(block, TextContent):
            try:
                return json.loads(block.text)
            except ValueError:
                return {"Success": False, "Message": block.text}
    return {}


def _upstream_error(e: Exception) -> str:
    while isinstance(e, BaseExceptionGroup) and len(e.exceptions) == 1:
        e = e.exceptions[0]  # the MCP client wraps errors in task groups
    if isinstance(e, httpx.ConnectError):
        return "The WhatsApp server isn't running. On the server: docker compose up -d"
    return f"The WhatsApp server didn't answer ({type(e).__name__}: {e})."


# ---------------------------------------------------------------- reading files
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".tif", ".tiff", ".heic"}
TEXT_EXTS = {
    ".txt", ".md", ".csv", ".tsv", ".json", ".xml", ".html", ".htm", ".css", ".js", ".ts",
    ".py", ".java", ".c", ".cpp", ".h", ".cs", ".go", ".rs", ".php", ".rb", ".sh", ".bat",
    ".ps1", ".sql", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".conf", ".env", ".log",
    ".srt", ".vtt", ".tex", ".asc", ".pem", ".svg",
}


def _cut(text: str) -> str:
    if len(text) <= MAX_TEXT_CHARS:
        return text
    return text[:MAX_TEXT_CHARS] + "\n\n[...cut off here: the file is longer than this]"


def _to_image(data: bytes) -> Image:
    """Shrink a picture so Claude can look at it, and send it as a JPEG."""
    with PILImage.open(io.BytesIO(data)) as im:
        im.seek(0)  # first frame of GIFs and animated stickers
        im = ImageOps.exif_transpose(im)
        im.thumbnail((IMAGE_SIDE, IMAGE_SIDE))
        if im.mode in ("RGBA", "LA", "P"):
            im = im.convert("RGBA")
            bg = PILImage.new("RGB", im.size, "white")
            bg.paste(im, mask=im.getchannel("A"))
            im = bg
        buf = io.BytesIO()
        im.convert("RGB").save(buf, "JPEG", quality=85)
    return Image(data=buf.getvalue(), format="jpeg")


def _read_pdf(data: bytes, start_page: int) -> list:
    import pymupdf

    doc = pymupdf.open(stream=data, filetype="pdf")
    pages = doc.page_count
    start = max(1, int(start_page))
    if start > pages:
        raise ValueError(f"This PDF has only {pages} pages.")
    parts, total, last, letters = [], 0, start - 1, 0
    for i in range(start - 1, pages):
        text = doc[i].get_text().strip()
        chunk = f"--- page {i + 1} ---\n{text}"
        if parts and total + len(chunk) > MAX_TEXT_CHARS:
            break
        parts.append(_cut(chunk))
        total += len(chunk)
        letters += len(text)
        last = i + 1
    out = [f"PDF with {pages} pages. Showing pages {start}-{last}."]
    if letters < 50 * (last - start + 1):
        # No real text inside: probably a scan or photos. Show the pages as pictures.
        last = min(pages, start + SCANNED_PAGES - 1)
        out[0] = (f"PDF with {pages} pages. It has almost no text inside (probably scanned), "
                  f"so here are pages {start}-{last} as pictures.")
        for i in range(start - 1, last):
            out.append(_to_image(doc[i].get_pixmap(dpi=110).tobytes("png")))
    else:
        out.append("\n\n".join(parts))
    if last < pages:
        out.append(f"More pages left: call read_attachment again with start_page={last + 1}.")
    return out


def _read_docx(data: bytes) -> list:
    import docx

    d = docx.Document(io.BytesIO(data))
    lines = [p.text for p in d.paragraphs if p.text.strip()]
    for n, table in enumerate(d.tables, 1):
        lines.append(f"\n--- table {n} ---")
        for row in table.rows:
            lines.append(" | ".join(c.text.strip() for c in row.cells))
    return [_cut("\n".join(lines)) or "The document is empty."]


def _read_xlsx(data: bytes) -> list:
    import openpyxl

    wb = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    lines = []
    for ws in wb.worksheets:
        lines.append(f"=== sheet: {ws.title} ===")
        for row in ws.iter_rows(values_only=True):
            if any(v is not None for v in row):
                lines.append(",".join("" if v is None else str(v) for v in row))
        if sum(map(len, lines)) > MAX_TEXT_CHARS:
            break
    return [_cut("\n".join(lines))]


def _as_text(data: bytes) -> str | None:
    if b"\x00" in data[:4096]:
        return None
    for enc in ("utf-8-sig", "cp1256"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            pass
    return None


def read_file(data: bytes, name: str, start_page: int = 1) -> list | None:
    """Turn a file into things Claude can read: text and/or pictures.
    Returns None for kinds of files it can't open (zip, apk, ...)."""
    ext = Path(name).suffix.lower()
    if ext in IMAGE_EXTS:
        try:
            return [_to_image(data)]
        except Exception:
            return None
    if ext == ".pdf" or data[:5] == b"%PDF-":
        return _read_pdf(data, start_page)
    if ext == ".docx":
        return _read_docx(data)
    if ext in (".xlsx", ".xlsm"):
        return _read_xlsx(data)
    if ext in TEXT_EXTS:
        text = _as_text(data)
        return [_cut(text) if text.strip() else "The file is empty."] if text is not None else None
    # Unknown kind: maybe a picture without the right name, or plain text.
    try:
        return [_to_image(data)]
    except Exception:
        pass
    text = _as_text(data) if len(data) < 2_000_000 else None
    if text and text.strip():
        sample = text[:5000]
        readable = sum(c.isprintable() or c in "\n\r\t" for c in sample)
        if readable >= 0.97 * len(sample):
            return [_cut(text)]
    return None


# ---------------------------------------------------------------- voice messages
AUDIO_SIGNATURES = [(b"OggS", ".ogg"), (b"\x1aE\xdf\xa3", ".webm"), (b"RIFF", ".wav"),
                    (b"ID3", ".mp3"), (b"\xff\xfb", ".mp3"), (b"\xff\xf3", ".mp3"),
                    (b"\xff\xf2", ".mp3"), (b"fLaC", ".flac")]


def _audio_name(data: bytes, name: str, kind: str) -> str:
    """WhatsApp calls every voice note .ogg, but ones recorded in WhatsApp Web are really
    WebM. Speech-to-text services go by the name, so give the file its real one."""
    stem = Path(name).stem
    for sig, ext in AUDIO_SIGNATURES:
        if data.startswith(sig):
            return stem + ext
    if data[4:8] == b"ftyp":
        return stem + (".mp4" if kind == "video" else ".m4a")
    return name


async def _speech_to_text(data: bytes, name: str, language: str | None) -> str:
    form = {"model": TRANSCRIBE_MODEL}
    if language:
        form["language"] = language
    async with httpx.AsyncClient(timeout=180) as http:
        r = await http.post(TRANSCRIBE_URL, data=form,
                            headers={"Authorization": f"Bearer {TRANSCRIBE_API_KEY}"},
                            files={"file": (name, data)})
    if r.status_code >= 400:
        raise RuntimeError(f"error {r.status_code}: {r.text[:200]}")
    return (r.json().get("text") or "").strip()


# ---------------------------------------------------------------- scheduled messages
_schedule_lock = asyncio.Lock()


def _when(dt: datetime) -> str:
    return dt.astimezone(TZ).strftime("%Y-%m-%d %H:%M")


def _load_jobs() -> list[dict]:
    try:
        return json.loads(SCHEDULE_FILE.read_text("utf-8"))
    except FileNotFoundError:
        return []


def _save_jobs(jobs: list[dict]) -> None:
    # Keep everything still waiting, plus the last 30 finished ones for the record.
    waiting = [j for j in jobs if j["status"] == "waiting"]
    done = [j for j in jobs if j["status"] != "waiting"][-30:]
    tmp = SCHEDULE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(waiting + done, ensure_ascii=False, indent=1), "utf-8")
    tmp.replace(SCHEDULE_FILE)


def _parse_send_time(value: str) -> datetime:
    raw = (value or "").strip()
    try:
        dt = datetime.fromisoformat(raw.replace("T", " "))
    except ValueError:
        raise ValueError(f"'{value}' isn't a time I understand. Use 2026-11-03 08:00 "
                         "(the user's local time).")
    dt = dt if dt.tzinfo else dt.replace(tzinfo=TZ)
    now = datetime.now(TZ)
    if dt < now + timedelta(seconds=30):
        raise ValueError(f"{_when(dt)} is not in the future (it's {_when(now)} now).")
    if dt > now + timedelta(days=365):
        raise ValueError("Messages can be scheduled at most a year ahead.")
    return dt


RECIPIENT = re.compile(r"^\+?\d{6,15}$|^[\d-]+@(s\.whatsapp\.net|g\.us|lid)$")


async def _send_due() -> None:
    async with _schedule_lock:
        jobs = _load_jobs()
        now = datetime.now(TZ)
        changed = False
        for job in jobs:
            if job["status"] != "waiting":
                continue
            at = datetime.fromisoformat(job["send_at"])
            if at > now:
                continue
            if now - at > SEND_GRACE:
                job.update(status="not sent", note="The connector was off or WhatsApp was "
                           "disconnected at the time, so it wasn't sent (too late now).")
                changed = True
                continue
            try:
                res = await upstream_call("send_message", {"recipient": job["recipient"],
                                                           "message": job["text"]})
                reply = _first_json(res)
                if not res.isError and reply.get("Success"):
                    job.update(status="sent", sent_at=now.isoformat(), message_id=reply.get("ID"))
                    job.pop("note", None)
                    log.info("scheduled message %s sent", job["id"])
                    changed = True
                elif not job.get("note"):
                    job["note"] = f"Trying again: {reply.get('Message') or 'WhatsApp refused it'}"
                    changed = True
            except Exception as e:  # server down for a moment: try again next round
                log.warning("scheduled message %s: %s", job["id"], e)
        if changed:
            _save_jobs(jobs)


async def scheduler() -> None:
    while True:
        try:
            await _send_due()
        except Exception:
            log.exception("scheduler")
        await asyncio.sleep(20)


# ---------------------------------------------------------------- storage limit
MESSAGES_DB = "messages.db"
KEEP_DIRS = {"uploads"}  # files put there on purpose are never removed


def _db_used(con: sqlite3.Connection) -> int:
    """Bytes the database really uses. Freed pages are reused, so they don't count."""
    page_size = con.execute("PRAGMA page_size").fetchone()[0]
    pages = con.execute("PRAGMA page_count").fetchone()[0]
    free = con.execute("PRAGMA freelist_count").fetchone()[0]
    return (pages - free) * page_size


def _storage(con: sqlite3.Connection | None) -> tuple[int, list[Path]]:
    """(bytes used by the WhatsApp data, downloaded media files)."""
    used, media = 0, []
    for p in STORE_DIR.rglob("*"):
        if not p.is_file():
            continue
        rel = p.relative_to(STORE_DIR).parts
        if rel[0].startswith(MESSAGES_DB):
            continue  # counted below, without its free pages and its journal
        used += p.stat().st_size
        if len(rel) >= 3 and rel[0] not in KEEP_DIRS:  # <chat>/<message>/<file>
            media.append(p)
    return used + (_db_used(con) if con else 0), media


def _open_messages_db() -> sqlite3.Connection | None:
    path = STORE_DIR / MESSAGES_DB
    if not path.exists():
        return None
    con = sqlite3.connect(path, timeout=30)
    con.execute("PRAGMA busy_timeout = 30000")
    return con


def prune_storage() -> str:
    """Keep the WhatsApp data under STORAGE_LIMIT_MB. Past 90% of it, remove the oldest
    downloaded files (they download again when needed), then the oldest messages, until
    it's at 75%."""
    limit = STORAGE_LIMIT_MB * MB
    con = _open_messages_db()
    try:
        used, media = _storage(con)
        if not limit or used <= 0.9 * limit:
            return f"WhatsApp data: {used // MB} MB of {STORAGE_LIMIT_MB or 'unlimited'} MB."
        goal, files, messages = int(0.75 * limit), 0, 0
        for f in sorted(media, key=lambda p: p.stat().st_mtime):
            if used <= goal:
                break
            used -= f.stat().st_size
            f.unlink()
            files += 1
            for d in (f.parent, f.parent.parent):
                if d != STORE_DIR and not any(d.iterdir()):
                    d.rmdir()
        if con and used - _db_used(con) > goal:
            # Not the messages' fault (the WhatsApp login data or kept files are that big):
            # deleting every message wouldn't help, so keep them.
            log.warning("WhatsApp data other than messages is %d MB; raise STORAGE_LIMIT_MB.",
                        (used - _db_used(con)) // MB)
            con.close()
            con = None
        while con and used > goal:
            total = con.execute("SELECT count(*) FROM messages").fetchone()[0]
            if not total:
                break
            db = _db_used(con)
            share = min(1.0, (used - goal) / max(db, 1) * 1.05)
            n = max(1000, int(total * share))
            cutoff = con.execute("SELECT timestamp FROM messages ORDER BY timestamp "
                                 "LIMIT 1 OFFSET ?", (min(n, total) - 1,)).fetchone()[0]
            removed = 0
            while True:  # small steps, so the WhatsApp server never waits long
                gone = con.execute("DELETE FROM messages WHERE rowid IN (SELECT rowid FROM "
                                   "messages WHERE timestamp IS NULL OR timestamp <= ? "
                                   "LIMIT 5000)", (cutoff,)).rowcount
                con.commit()
                removed += gone
                if gone < 5000:
                    break
            messages += removed
            used -= db - _db_used(con)
            if not removed:
                break
        if con:
            con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        return (f"WhatsApp data was over {int(0.9 * STORAGE_LIMIT_MB)} MB: removed {files} "
                f"downloaded files and {messages} old messages. Now {used // MB} MB.")
    finally:
        if con:
            con.close()


async def storage_keeper() -> None:
    await asyncio.sleep(60)
    while True:
        try:
            log.info(await asyncio.to_thread(prune_storage))
        except Exception:
            log.exception("storage limit")
        await asyncio.sleep(30 * 60)


def compact() -> None:
    """Prune, then rebuild the message database so the file really gets smaller."""
    print(prune_storage())
    con = _open_messages_db()
    if con:
        before = (STORE_DIR / MESSAGES_DB).stat().st_size
        con.execute("VACUUM")
        con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        con.close()
        after = (STORE_DIR / MESSAGES_DB).stat().st_size
        print(f"Message database: {before // MB} MB -> {after // MB} MB.")


def run_as_app_user() -> None:
    """Run as the same user as the WhatsApp server, so files it shares stay its own."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if not hasattr(os, "geteuid") or os.geteuid() != 0:
        return
    for p in [DATA_DIR, *DATA_DIR.rglob("*")]:
        os.chown(p, APP_UID, APP_UID)
    os.setgroups([])
    os.setgid(APP_UID)
    os.setuid(APP_UID)


# ---------------------------------------------------------------- MCP server
class Gateway(FastMCP):
    """FastMCP with the WhatsApp server's tools added next to its own."""

    async def list_tools(self) -> list[Tool]:
        own = await super().list_tools()
        names = {t.name for t in own}
        try:
            theirs = [t for t in await upstream_tools()
                      if t.name not in HIDE_TOOLS and t.name not in names]
        except Exception as e:
            log.warning("can't list WhatsApp tools: %s", e)
            theirs = []
        return theirs + own

    async def call_tool(self, name: str, arguments: dict):
        if name in self._tool_manager._tools:
            return await super().call_tool(name, arguments)
        if name in HIDE_TOOLS:
            raise ValueError(f"{name} is switched off in this connector.")
        try:
            result = await upstream_call(name, arguments)
        except Exception as e:
            return CallToolResult(content=[TextContent(type="text", text=_upstream_error(e))],
                                  isError=True)
        # Claude can't listen to audio; point it to read_attachment instead.
        if any(b.type == "audio" for b in result.content):
            result.content = [b for b in result.content if b.type != "audio"]
            result.content.append(TextContent(
                type="text", text="To know what is said in it, use read_attachment."))
        return result


mcp = Gateway(
    name="WhatsApp",
    instructions=(
        "These tools act as the user's OWN personal WhatsApp account. Anything sent appears "
        "as written by the user. Before anything that sends, replies, reacts, edits, deletes, "
        "votes, schedules, blocks, changes groups or changes settings, always show the user "
        "exactly what will happen (the chat, the exact text, which messages) and only go "
        "ahead after they clearly say yes. Never message people the user did not name. Only "
        "open attachments the user asked about: use read_attachment for photos, documents "
        "and voice messages. Times in list_messages are UTC; the user's own time zone is "
        f"{TZ.key}, and schedule_message takes times in the user's time zone."
    ),
    host="0.0.0.0",
    port=PORT,
    streamable_http_path=f"/{SECRET}/mcp",
    stateless_http=True,
    json_response=True,
    # Host checks are done by the HTTPS proxy in front; the secret path is the lock.
    transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
)

READ = ToolAnnotations(readOnlyHint=True, openWorldHint=True)
WRITE = ToolAnnotations(readOnlyHint=False, destructiveHint=False,
                        idempotentHint=False, openWorldHint=True)
DESTRUCTIVE = ToolAnnotations(readOnlyHint=False, destructiveHint=True,
                              idempotentHint=True, openWorldHint=False)


@mcp.tool(annotations=READ, structured_output=False)
async def read_attachment(chat_jid: str, message_id: str, start_page: int = 1,
                          language: str | None = None) -> list:
    """Open the photo, sticker, PDF, Word, Excel or text file in a WhatsApp message, or write
    out what is said in a voice message, audio or video. Get chat_jid and message_id from
    list_messages. For long PDFs, start_page reads further. language (like "fa" for Persian
    or "en") helps with voice messages when you know the language."""
    try:
        res = await upstream_call("download_media", {"chat_jid": chat_jid, "message_id": message_id})
    except Exception as e:
        raise RuntimeError(_upstream_error(e))
    info = _first_json(res)
    if res.isError or not info.get("Success"):
        raise ValueError(info.get("Message") or "WhatsApp couldn't download that file.")
    path = Path(info["Path"])
    kind = info.get("MediaType") or ""
    name = info.get("Filename") or path.name
    size = path.stat().st_size
    header = f"{kind or 'file'}: {name}, {max(1, size // 1024)} KB"

    if kind in ("audio", "video"):
        if not TRANSCRIBE_API_KEY:
            return [f"{header}. I can't listen to it myself. To fix this, put a "
                    "TRANSCRIBE_API_KEY in the .env file (see README)."]
        if size > 25 * MB:
            return [f"{header}. It's too long for the speech-to-text service (25 MB max)."]
        data = path.read_bytes()
        try:
            text = await _speech_to_text(data, _audio_name(data, path.name, kind), language)
        except (httpx.HTTPError, RuntimeError) as e:
            return [f"{header} (starts with {data[:8].hex()}). "
                    f"The speech-to-text service failed: {e}"]
        return [f"{header}. What is said (written out by speech-to-text):\n\n{text or '(nothing said)'}"]

    if size > MAX_ATTACHMENT_MB * MB:
        return [f"{header}. That's bigger than the {MAX_ATTACHMENT_MB} MB I open."]
    parts = await asyncio.to_thread(read_file, path.read_bytes(), name, start_page)
    if parts is None:
        return [f"{header}. I can't open this kind of file."]
    return [header, *parts]


@mcp.tool(annotations=WRITE)
async def schedule_message(recipient: str, text: str, send_at: str) -> dict:
    """Send a WhatsApp text message later. recipient is the phone number (digits, country
    code first) or the chat_jid from list_chats / search_contacts. send_at is the user's
    local time, like 2026-11-03 08:00. Works while the user's phone is off; the server
    sends it. Show the user the recipient, text and time and get a yes first."""
    recipient = recipient.strip()
    if not RECIPIENT.match(recipient):
        raise ValueError("recipient must be a phone number in digits (country code first) or "
                         "a chat_jid. Look it up with search_contacts or list_chats.")
    if not text.strip():
        raise ValueError("The message is empty.")
    at = _parse_send_time(send_at)
    async with _schedule_lock:
        jobs = _load_jobs()
        if sum(j["status"] == "waiting" for j in jobs) >= MAX_SCHEDULED:
            raise RuntimeError(f"There are already {MAX_SCHEDULED} messages waiting.")
        job = {"id": secrets.token_hex(3), "recipient": recipient.lstrip("+"), "text": text,
               "send_at": at.isoformat(), "status": "waiting"}
        jobs.append(job)
        _save_jobs(jobs)
    return {"scheduled": True, "id": job["id"], "recipient": job["recipient"],
            "send_at": _when(at), "time_zone": TZ.key}


@mcp.tool(annotations=READ)
async def list_scheduled() -> dict:
    """Show the scheduled WhatsApp messages: the ones waiting, and the last ones sent or not
    sent."""
    jobs = _load_jobs()
    return {
        "now": _when(datetime.now(TZ)),
        "time_zone": TZ.key,
        "messages": [{"id": j["id"], "recipient": j["recipient"], "text": j["text"],
                      "send_at": _when(datetime.fromisoformat(j["send_at"])),
                      "status": j["status"], **({"note": j["note"]} if j.get("note") else {})}
                     for j in jobs],
    }


@mcp.tool(annotations=DESTRUCTIVE)
async def cancel_scheduled(ids: list[str]) -> dict:
    """Cancel scheduled WhatsApp messages that haven't gone out yet (ids from list_scheduled)."""
    async with _schedule_lock:
        jobs = _load_jobs()
        cancelled = []
        for j in jobs:
            if j["id"] in ids and j["status"] == "waiting":
                j["status"] = "cancelled"
                cancelled.append(j["id"])
        _save_jobs(jobs)
    missing = [i for i in ids if i not in cancelled]
    return {"cancelled": cancelled,
            **({"not_waiting_or_unknown": missing} if missing else {})}


# ---------------------------------------------------------------- pairing page
PAIR = f"/{SECRET}/pair"


@mcp.custom_route(PAIR, methods=["GET"])
@mcp.custom_route(PAIR + "/qr.png", methods=["GET"])
@mcp.custom_route(PAIR + "/code", methods=["POST"])
@mcp.custom_route(PAIR + "/reset", methods=["POST"])
async def pair_page(request: Request) -> Response:
    """The WhatsApp server's own pairing page, reachable through the secret address."""
    path = "/pair" + request.url.path[len(PAIR):]
    async with httpx.AsyncClient(timeout=30, follow_redirects=False) as http:
        r = await http.request(
            request.method, UPSTREAM + path, content=await request.body(),
            headers={"Authorization": f"Bearer {UPSTREAM_TOKEN}",
                     "Content-Type": request.headers.get("content-type", "")})
    headers = {k: v for k, v in r.headers.items()
               if k.lower() in ("content-type", "cache-control", "retry-after")}
    if "location" in r.headers:
        headers["location"] = r.headers["location"].replace("/pair", PAIR, 1)
    body = r.content
    if r.headers.get("content-type", "").startswith("text/html"):
        body = body.replace(b'"/pair', f'"{PAIR}'.encode())
    headers["referrer-policy"] = "no-referrer"
    return Response(body, status_code=r.status_code, headers=headers)


async def main() -> None:
    run_as_app_user()
    asyncio.create_task(scheduler())
    asyncio.create_task(storage_keeper())
    server = uvicorn.Server(uvicorn.Config(mcp.streamable_http_app(), host="0.0.0.0", port=PORT,
                                           log_level="warning"))
    log.info("WhatsApp gateway ready")
    await server.serve()


if __name__ == "__main__":
    if sys.argv[1:] == ["--compact"]:
        run_as_app_user()
        compact()
    else:
        asyncio.run(main())

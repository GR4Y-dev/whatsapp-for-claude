"""
Offline tests for the gateway: file reading and the storage limit in-process, then the whole
gateway against a pretend WhatsApp server (tests/fake_whatsapp.py).

    pip install -r gateway/requirements.txt
    python tests/test_gateway.py
"""
import asyncio, io, json, os, sqlite3, subprocess, sys, tempfile, time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

HERE = Path(__file__).resolve().parent
WORK = Path(tempfile.mkdtemp(prefix="wa-test-"))
SECRET = "s" * 48
os.environ.update(MCP_SECRET=SECRET, WHATSAPP_TOKEN="tok-123", DATA_DIR=str(WORK / "data"),
                  STORE_DIR=str(WORK / "store"))
sys.path.insert(0, str(HERE.parent / "gateway"))
import gateway  # noqa: E402
import httpx  # noqa: E402
import pymupdf, docx, openpyxl  # noqa: E402
from mcp import ClientSession  # noqa: E402
from mcp.client.streamable_http import streamable_http_client  # noqa: E402
from PIL import Image as PILImage  # noqa: E402

ok = 0
def check(cond, what):
    global ok
    if not cond:
        raise SystemExit(f"FAIL: {what}")
    ok += 1
    print("ok -", what)

# ---------------------------------------------------------------- files
def pdf_bytes(words="Invoice number 4471 total 250 euros. " * 5):
    d = pymupdf.open(); d.new_page().insert_text((50, 72), words); return d.tobytes()

def docx_bytes():
    d = docx.Document(); d.add_paragraph("Meeting notes: buy milk"); t = d.add_table(rows=1, cols=2)
    t.rows[0].cells[0].text = "A"; t.rows[0].cells[1].text = "B"
    b = io.BytesIO(); d.save(b); return b.getvalue()

def xlsx_bytes():
    wb = openpyxl.Workbook(); ws = wb.active; ws.title = "Money"; ws.append(["item", "cost"]); ws.append(["tea", 3])
    b = io.BytesIO(); wb.save(b); return b.getvalue()

def image_bytes(fmt="JPEG", size=(3000, 2000), mode="RGB"):
    b = io.BytesIO(); PILImage.new(mode, size, "red").save(b, fmt); return b.getvalue()

r = gateway.read_file(image_bytes(), "photo.jpg")
check(max(PILImage.open(io.BytesIO(r[0].data)).size) == 1568, "big photo shrunk to 1568px")
check(isinstance(gateway.read_file(image_bytes("WEBP", (512, 512), "RGBA"), "sticker.webp")[0],
                 gateway.Image), "WebP sticker read as a picture")
check(isinstance(gateway.read_file(image_bytes("PNG", (50, 50)), "no-extension")[0], gateway.Image),
      "picture without a file extension still read")
check("Invoice number 4471" in gateway.read_file(pdf_bytes(), "a.pdf")[1], "PDF read")
check("buy milk" in gateway.read_file(docx_bytes(), "w.docx")[0], "Word file read")
check("tea,3" in gateway.read_file(xlsx_bytes(), "s.xlsx")[0], "Excel file read")
check("یادداشت" in gateway.read_file("یک یادداشت".encode(), "n.txt")[0], "Persian text read")
check(gateway.read_file(b"PK\x03\x04" + os.urandom(3000), "a.zip") is None, "zip refused")

# ---------------------------------------------------------------- voice note names
for data, name, kind, want in [
        (b"\x1aE\xdf\xa3....", "audio_x.ogg", "audio", "audio_x.webm"),
        (b"OggS....", "audio_x.ogg", "audio", "audio_x.ogg"),
        (b"\x00\x00\x00\x20ftypM4A ", "audio_x.ogg", "audio", "audio_x.m4a"),
        (b"\x00\x00\x00\x20ftypisom", "video_x.mp4", "video", "video_x.mp4"),
        (b"ID3\x04....", "a.ogg", "audio", "a.mp3"),
        (b"#!AMR\n..", "a.ogg", "audio", "a.ogg")]:
    check(gateway._audio_name(data, name, kind) == want, f"voice file {data[:4]!r} named {want}")

# ---------------------------------------------------------------- storage limit
MB = gateway.MB

def make_store(store: Path, messages=20000, media=20):
    import shutil
    if store.exists():
        shutil.rmtree(store)
    store.mkdir(parents=True)
    con = sqlite3.connect(store / "messages.db")
    con.execute("PRAGMA journal_mode=WAL")
    con.executescript("""
        CREATE TABLE chats (jid TEXT PRIMARY KEY, name TEXT, last_message_time TIMESTAMP);
        CREATE TABLE messages (id TEXT, chat_jid TEXT, sender TEXT, content TEXT, timestamp TIMESTAMP,
            is_from_me BOOLEAN, media_type TEXT, filename TEXT, url TEXT, direct_path TEXT,
            media_key BLOB, file_sha256 BLOB, file_enc_sha256 BLOB, file_length INTEGER,
            PRIMARY KEY (id, chat_jid), FOREIGN KEY (chat_jid) REFERENCES chats(jid));""")
    con.execute("INSERT INTO chats VALUES ('1@s.whatsapp.net', 'Mom', '2026-01-01')")
    start = datetime(2026, 1, 1)
    con.executemany("INSERT INTO messages (id, chat_jid, content, timestamp) VALUES (?, ?, ?, ?)",
                    [(f"m{i}", "1@s.whatsapp.net", "x" * 200,
                      (start + timedelta(minutes=i)).strftime("%Y-%m-%d %H:%M:%S+00:00"))
                     for i in range(messages)])
    con.commit(); con.close()
    for i in range(media):
        f = store / "1@s.whatsapp.net" / f"6d{i:02d}" / "photo.jpg"
        f.parent.mkdir(parents=True)
        f.write_bytes(os.urandom(100 * 1024))
        os.utime(f, (time.time() - 10000 + i, time.time() - 10000 + i))
    (store / "uploads").mkdir()
    (store / "uploads" / "keep.pdf").write_bytes(os.urandom(300 * 1024))
    (store / "whatsapp.db").write_bytes(os.urandom(200 * 1024))

store = gateway.STORE_DIR
make_store(store)
gateway.STORAGE_LIMIT_MB = 100
check("of 100 MB" in gateway.prune_storage(), "under the limit: nothing removed")
check(len(list(store.rglob("photo.jpg"))) == 20, "under the limit: files kept")

def count(where="1"):
    con = sqlite3.connect(store / "messages.db")
    try:
        return con.execute(f"SELECT count(*) FROM messages WHERE {where}").fetchone()[0]
    finally:
        con.close()

gateway.STORAGE_LIMIT_MB = 5
report = gateway.prune_storage()
con = sqlite3.connect(store / "messages.db")
used, media = gateway._storage(con)
con.close()
left = count()
check("removed 20 downloaded files" in report, "over the limit: downloaded files removed first")
check(0 < left < 20000 and count("id = 'm0'") == 0,
      f"then the oldest messages ({20000 - left} removed, {left} kept)")
check(count("id = 'm19999'") == 1, "newest message kept")
check(used <= 0.75 * 5 * MB + 64 * 1024, f"now under 75% of the limit ({used // 1024} KB)")
check((store / "uploads" / "keep.pdf").exists() and (store / "whatsapp.db").exists(),
      "WhatsApp login and uploads never removed")
check(not (store / "1@s.whatsapp.net").exists(), "empty media folders removed")
before = (store / "messages.db").stat().st_size
gateway.compact()
check((store / "messages.db").stat().st_size < before, "--compact makes the database file smaller")

make_store(store, messages=2000, media=0)
(store / "whatsapp.db").write_bytes(os.urandom(2 * MB))
gateway.STORAGE_LIMIT_MB = 2
gateway.prune_storage()
check(count() == 2000, "messages kept when other data alone is over the limit")

# ---------------------------------------------------------------- whole gateway, fake WhatsApp
files_dir = WORK / "store" / "media"; files_dir.mkdir()
media_files = {"m-pdf": ("document", "invoice.pdf", pdf_bytes()),
               "m-img": ("image", "photo.jpg", image_bytes()),
               "m-voice": ("audio", "voice.ogg", b"OggS" + os.urandom(2000)),
               "m-webvoice": ("audio", "web.ogg", b"\x1aE\xdf\xa3" + os.urandom(2000)),
               "m-zip": ("document", "big.zip", b"PK\x03\x04" + os.urandom(5000))}
fake = {}
for mid, (kind, name, data) in media_files.items():
    (files_dir / name).write_bytes(data)
    fake[mid] = [kind, str(files_dir / name)]
data_dir = WORK / "e2e-data"; data_dir.mkdir()
TZ = ZoneInfo("Europe/Istanbul")
now = datetime.now(TZ)
(data_dir / "scheduled.json").write_text(json.dumps([
    {"id": "due111", "recipient": "989121234567", "text": "سلام مامان",
     "send_at": (now - timedelta(minutes=2)).isoformat(), "status": "waiting"},
    {"id": "old222", "recipient": "989121234567", "text": "too late",
     "send_at": (now - timedelta(hours=3)).isoformat(), "status": "waiting"}], ensure_ascii=False), "utf-8")
env = dict(os.environ, WHATSAPP_TOKEN="tok-123", FAKE_FILES=json.dumps(fake), SENT_LOG=str(WORK / "sent.log"),
           MCP_SECRET=SECRET, WHATSAPP_URL="http://127.0.0.1:18765", PORT="18000", DATA_DIR=str(data_dir),
           STORE_DIR=str(WORK / "store"), TIMEZONE="Europe/Istanbul", HIDE_TOOLS="block_contact",
           TRANSCRIBE_API_KEY="sk-test", TRANSCRIBE_URL="http://127.0.0.1:18765/stt",
           PYTHONIOENCODING="utf-8")
up = subprocess.Popen([sys.executable, str(HERE / "fake_whatsapp.py"), "18765"], env=env)
gw = subprocess.Popen([sys.executable, str(HERE.parent / "gateway" / "gateway.py")], env=env)
URL = f"http://127.0.0.1:18000/{SECRET}/mcp"
BASE = "http://127.0.0.1:18000"


async def call(s, name, args):
    r = await s.call_tool(name, args)
    return r, [getattr(b, "text", None) or b.type for b in r.content]


async def session_do(fn):
    async with httpx.AsyncClient(timeout=60) as http:
        async with streamable_http_client(URL, http_client=http) as (r, w, _):
            async with ClientSession(r, w) as s:
                await s.initialize()
                return await fn(s)


async def wait_ready():
    async with httpx.AsyncClient() as http:
        for _ in range(60):
            try:
                await http.get(f"{BASE}/{SECRET}/pair")
                return
            except httpx.HTTPError:
                await asyncio.sleep(0.5)
    raise SystemExit("FAIL: gateway didn't start")


async def main():
    await wait_ready()

    async def tools(s):
        names = [t.name for t in (await s.list_tools()).tools]
        check({"list_chats", "send_message", "download_media", "read_attachment", "schedule_message",
               "list_scheduled", "cancel_scheduled"} <= set(names), "WhatsApp tools and own tools listed")
        check("block_contact" not in names, "HIDE_TOOLS hides a tool")
        res, c = await call(s, "list_chats", {})
        check("Mom" in c[0], "WhatsApp tool passed through")
        res, c = await call(s, "block_contact", {"jid": "x"})
        check(res.isError, "hidden tool refused")
        res, c = await call(s, "download_media", {"chat_jid": "1@s.whatsapp.net", "message_id": "m-voice"})
        check("audio" not in c and any("read_attachment" in x for x in c), "audio replaced by a pointer")
        jid = "1@s.whatsapp.net"
        res, c = await call(s, "read_attachment", {"chat_jid": jid, "message_id": "m-voice", "language": "fa"})
        check("سلام" in c[0] and "lang=fa" in c[0], "voice message written out, with the language hint")
        res, c = await call(s, "read_attachment", {"chat_jid": jid, "message_id": "m-webvoice"})
        check("سلام" in c[0] and "web.webm" in c[0], "WhatsApp Web voice note sent as WebM")
        res, c = await call(s, "read_attachment", {"chat_jid": jid, "message_id": "m-pdf"})
        check(any("Invoice number 4471" in x for x in c), "PDF attachment read")
        res, c = await call(s, "read_attachment", {"chat_jid": jid, "message_id": "m-img"})
        check("image" in c, "photo attachment shown")
        res, c = await call(s, "read_attachment", {"chat_jid": jid, "message_id": "m-zip"})
        check("can't open" in c[-1], "zip attachment refused politely")
        res, c = await call(s, "read_attachment", {"chat_jid": jid, "message_id": "nope"})
        check(res.isError and "no media" in c[0], "WhatsApp's error passed on")

        res, c = await call(s, "schedule_message", {"recipient": "Mom", "text": "hi", "send_at": "2030-01-01 08:00"})
        check(res.isError and "search_contacts" in c[0], "a name as recipient refused")
        res, c = await call(s, "schedule_message", {"recipient": "+989121234567", "text": "hi", "send_at": "2020-01-01 08:00"})
        check(res.isError and "not in the future" in c[0], "a past time refused")
        when = (datetime.now(TZ) + timedelta(days=1)).strftime("%Y-%m-%d 09:30")
        res, c = await call(s, "schedule_message", {"recipient": "+989121234567", "text": "صبح بخیر", "send_at": when})
        j = json.loads(c[0])
        check(j.get("scheduled") and j["send_at"] == when and j["recipient"] == "989121234567", "message scheduled")
        return j["id"]

    new_id = await session_do(tools)
    await asyncio.sleep(25)  # the scheduler runs every 20 seconds

    async def schedule(s):
        res, c = await call(s, "list_scheduled", {})
        lst = {m["id"]: m for m in json.loads(c[0])["messages"]}
        check(lst["due111"]["status"] == "sent", "due message sent")
        check(lst["old222"]["status"] == "not sent", "message more than an hour late not sent")
        check(lst[new_id]["status"] == "waiting", "future message waiting")
        res, c = await call(s, "cancel_scheduled", {"ids": [new_id, "zzz"]})
        j = json.loads(c[0])
        check(j["cancelled"] == [new_id] and j["not_waiting_or_unknown"] == ["zzz"], "cancel")

    await session_do(schedule)
    sent = (WORK / "sent.log").read_text("utf-8").splitlines()
    check(len(sent) == 1 and "سلام مامان" in sent[0], "WhatsApp got exactly the due message")

    async with httpx.AsyncClient(timeout=30) as http:
        check((await http.get(f"{BASE}/wrong/mcp")).status_code == 404, "wrong secret: Not Found")
        check((await http.get(f"{BASE}/pair")).status_code == 404, "pairing page needs the secret")
        r = await http.get(f"{BASE}/{SECRET}/pair")
        check(f'"/{SECRET}/pair/qr.png"' in r.text and f'action="/{SECRET}/pair/code"' in r.text,
              "pairing page links point through the secret address")
        r = await http.get(f"{BASE}/{SECRET}/pair/qr.png")
        check(r.status_code == 200 and r.content.startswith(b"\x89PNG"), "QR code image passed through")
        r = await http.post(f"{BASE}/{SECRET}/pair/code", data={"csrf_token": "t1", "phone": "+98912"})
        check(r.status_code == 303 and r.headers["location"] == f"/{SECRET}/pair", "pairing code form works")

    up.terminate(); up.wait()

    async def down(s):
        names = [t.name for t in (await s.list_tools()).tools]
        check("list_chats" in names, "tools still listed while WhatsApp server restarts")
        res, c = await call(s, "list_chats", {})
        check(res.isError and "isn't running" in c[0], "clear message when the WhatsApp server is down")

    await session_do(down)


try:
    asyncio.run(main())
finally:
    gw.terminate(); up.terminate()
    gw.wait(); up.wait()
    import shutil
    shutil.rmtree(WORK, ignore_errors=True)
print(f"\nAll {ok} checks passed.")

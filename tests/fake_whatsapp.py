"""
A pretend WhatsApp server for the tests: the same tool names, answers and pairing page as
sealjay/mcp-whatsapp, behind the same password header. Also a pretend speech-to-text service.

    python fake_whatsapp.py <port>     (settings come from environment variables, see the test)
"""
import base64, json, os, sys
import uvicorn
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import AudioContent, CallToolResult, TextContent
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response

TOKEN = os.environ["WHATSAPP_TOKEN"]
FILES = json.loads(os.environ["FAKE_FILES"])  # message_id -> [media_type, path]
SENT = os.environ["SENT_LOG"]                 # every sent message is written here

m = FastMCP("fake", stateless_http=True, json_response=True, streamable_http_path="/mcp",
            transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False))


@m.tool()
async def list_chats(limit: int = 20) -> str:
    """List chats."""
    return "Chat: Mom (989121234567@s.whatsapp.net)"


@m.tool()
async def block_contact(jid: str) -> str:
    """Block."""
    return "blocked"


@m.tool()
async def send_message(recipient: str, message: str):
    """Send."""
    with open(SENT, "a", encoding="utf-8") as f:
        f.write(json.dumps({"recipient": recipient, "message": message}, ensure_ascii=False) + "\n")
    return json.dumps({"Success": True, "Message": "sent", "ID": "ABC123"})


@m.tool(structured_output=False)
async def download_media(message_id: str, chat_jid: str, output_path: str = ""):
    """Download."""
    if message_id not in FILES:
        return CallToolResult(content=[TextContent(type="text", text=json.dumps(
            {"Success": False, "Message": "message has no media"}))])
    kind, path = FILES[message_id]
    out = [TextContent(type="text", text=json.dumps({
        "Success": True, "Message": "ok", "MediaType": kind,
        "Filename": os.path.basename(path), "Path": path}))]
    if kind == "audio":
        with open(path, "rb") as f:
            out.append(AudioContent(type="audio", data=base64.b64encode(f.read()).decode(),
                                    mimeType="audio/ogg"))
    return CallToolResult(content=out)


PAGE = ('<html><meta http-equiv="refresh" content="5"><img src="/pair/qr.png">'
        '<form method="post" action="/pair/code"><input name="csrf_token" value="t1"></form></html>')


@m.custom_route("/pair", methods=["GET"])
async def pair(r):
    return HTMLResponse(PAGE)


@m.custom_route("/pair/qr.png", methods=["GET"])
async def qr(r):
    return Response(b"\x89PNGfake", media_type="image/png", headers={"cache-control": "no-store"})


@m.custom_route("/pair/code", methods=["POST"])
async def code(r):
    form = await r.form()
    if form.get("csrf_token") != "t1":
        return PlainTextResponse("bad csrf", 403)
    return RedirectResponse("/pair", 303)


@m.custom_route("/stt", methods=["POST"])
async def stt(r: Request):
    if r.headers.get("authorization") != "Bearer sk-test":
        return JSONResponse({"error": "key"}, 401)
    form = await r.form()
    f = form["file"]
    data = await f.read()
    if f.filename.endswith(".ogg") and not data.startswith(b"OggS"):
        return JSONResponse({"error": {"message": "Audio file might be corrupted or unsupported"}}, 400)
    return JSONResponse({"text": f"سلام، این یک تست است ({f.filename}, "
                                 f"lang={form.get('language')}, model={form.get('model')})"})


app = m.streamable_http_app()


async def guarded(scope, receive, send):
    if scope["type"] == "http" and scope["path"] != "/stt":
        if dict(scope["headers"]).get(b"authorization", b"").decode() != f"Bearer {TOKEN}":
            return await PlainTextResponse("unauthorized", 401)(scope, receive, send)
    return await app(scope, receive, send)


uvicorn.run(guarded, host="127.0.0.1", port=int(sys.argv[1]), log_level="warning")

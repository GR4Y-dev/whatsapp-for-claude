"""Offline security regression tests. No real accounts or messages."""
import asyncio
import io
import json
import os
import sys
import tempfile
import unittest
import zipfile
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

os.environ["MCP_SECRET"] = "s" * 48

os.environ["WHATSAPP_TOKEN"] = "test-only"
os.environ["DATA_DIR"] = tempfile.gettempdir()
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "gateway"))
import gateway as module
from mcp.types import CallToolResult, TextContent
from starlette.requests import Request


class AttachmentLimits(unittest.TestCase):
    def test_large_image_rejected_before_decode(self):
        from PIL import Image as PILImage
        data = io.BytesIO()
        PILImage.new("1", (5001, 5000)).save(data, "PNG")
        with self.assertRaisesRegex(ValueError, "25 megapixels"):
            module._to_image(data.getvalue())

    def test_compressed_office_document_cannot_expand_without_bound(self):
        data = io.BytesIO()
        with zipfile.ZipFile(data, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("large.xml", b"0" * (33 * 1024 * 1024))
        self.assertLess(len(data.getvalue()), 100000)
        for reader in (module._read_docx, module._read_xlsx):
            with self.assertRaisesRegex(ValueError, "safe reading limit"):
                reader(data.getvalue())

    def test_sparse_spreadsheet_has_row_and_column_limits(self):
        import openpyxl
        wb = openpyxl.Workbook()
        ws = wb.active
        ws["A1"] = "visible"
        ws.cell(10001, 1, "beyond-row-limit")
        ws.cell(1, 101, "beyond-column-limit")
        data = io.BytesIO()
        wb.save(data)
        wb.close()
        text = module._read_xlsx(data.getvalue())[0]
        self.assertIn("visible", text)
        self.assertNotIn("beyond-row-limit", text)
        self.assertNotIn("beyond-column-limit", text)

    def test_pdf_extraction_stops_after_fifty_pages(self):
        import pymupdf
        with pymupdf.open() as doc:
            for _ in range(60):
                doc.new_page().insert_text((20, 20), "A page of readable text. " * 3)
            data = doc.tobytes()
        text = "\n".join(x for x in module._read_pdf(data, 1) if isinstance(x, str))
        self.assertIn("start_page=51", text)
        self.assertNotIn("--- page 51 ---", text)

    def test_huge_scanned_page_renders_at_bounded_size(self):
        import pymupdf
        from PIL import Image as PILImage
        with pymupdf.open() as doc:
            doc.new_page(width=100000, height=100000)
            data = doc.tobytes()
        parts = module._read_pdf(data, 1)
        picture = next(p for p in parts if isinstance(p, module.Image))
        with PILImage.open(io.BytesIO(picture.data)) as im:
            self.assertLessEqual(max(im.size), module.IMAGE_SIDE)

class GatewaySafety(unittest.IsolatedAsyncioTestCase):
    async def test_disabled_own_tool_is_neither_listed_nor_callable(self):
        with patch.object(module, "HIDE_TOOLS", {"schedule_message"}), \
             patch.object(module, "upstream_tools", AsyncMock(return_value=[])):
            self.assertNotIn("schedule_message", [t.name for t in await module.mcp.list_tools()])
            with self.assertRaisesRegex(ValueError, "switched off"):
                await module.mcp.call_tool("schedule_message", {})

    async def test_disabled_sending_cannot_be_scheduled_or_sent_by_worker(self):
        with patch.object(module, "HIDE_TOOLS", {"send_message"}), \
             patch.object(module, "upstream_call", AsyncMock()) as upstream:
            with self.assertRaisesRegex(ValueError, "switched off"):
                await module.schedule_message("123456789", "hello",
                    (datetime.now(module.TZ) + timedelta(hours=1)).isoformat())
            await module._send_due()
            upstream.assert_not_awaited()

    async def test_internal_upstream_calls_also_obey_disabled_tools(self):
        with patch.object(module, "HIDE_TOOLS", {"download_media"}), \
             patch.object(module.httpx, "AsyncClient") as http:
            with self.assertRaisesRegex(ValueError, "switched off"):
                await module.upstream_call("download_media", {})
            http.assert_not_called()

    async def test_attachment_cannot_escape_store_or_read_login_database(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            media = root / "store"
            media.mkdir()
            outside = root / "private.txt"
            outside.write_text("private")
            database = media / "whatsapp.db"
            database.write_text("login secrets")
            valid = media / "photo.txt"
            valid.write_text("ordinary attachment")
            with patch.object(module, "STORE_DIR", media):
                for path in (outside, database, valid):
                    result = CallToolResult(content=[TextContent(type="text", text=json.dumps({
                        "Success": True, "Path": str(path), "Filename": "photo.txt", "MediaType": "document"}))])
                    with patch.object(module, "upstream_call", AsyncMock(return_value=result)):
                        if path == valid:
                            parts = await module.read_attachment("chat", "message")
                            self.assertIn("ordinary attachment", "\n".join(parts))
                        else:
                            with self.assertRaises(ValueError):
                                await module.read_attachment("chat", "message")

    async def test_pairing_page_cannot_be_cached_or_framed(self):
        response = SimpleNamespace(content=b"<html>pair</html>", status_code=200,
                                   headers={"content-type": "text/html"})
        http = SimpleNamespace(request=AsyncMock(return_value=response))
        @asynccontextmanager
        async def client(**kwargs):
            yield http
        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}
        request = Request({"type": "http", "method": "GET", "scheme": "https",
                           "server": ("example.test", 443), "path": module.PAIR,
                           "query_string": b"", "headers": []}, receive)
        with patch.object(module.httpx, "AsyncClient", client):
            result = await module.pair_page(request)
        self.assertEqual(result.headers["cache-control"], "no-store")
        self.assertEqual(result.headers["x-frame-options"], "DENY")
        self.assertEqual(result.headers["referrer-policy"], "no-referrer")


if __name__ == "__main__":
    unittest.main()


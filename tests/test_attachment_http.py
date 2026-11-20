import sqlite3
import unittest

from fastapi import HTTPException

from attachment_http import serve_attachment


class AttachmentHTTPTests(unittest.TestCase):
    def setUp(self):
        self.con = sqlite3.connect(":memory:")
        self.addCleanup(self.con.close)
        self.con.executescript("""
            CREATE TABLE attachments(id INTEGER PRIMARY KEY, state TEXT, mime TEXT, body BLOB);
            INSERT INTO attachments VALUES (1, 'ready', 'audio/wav', X'00010203040506070809');
            INSERT INTO attachments VALUES (2, 'removed', NULL, NULL);
            INSERT INTO attachments VALUES (3, 'ready', 'audio/wav', X'');
            INSERT INTO attachments VALUES (4, 'ready', 'text/html', X'3C3E');
        """)
        self.body = bytes(range(10))

    def test_single_ranges(self):
        for value, expected, bounds in [
            ("bytes=0-1", self.body[:2], "0-1"),
            ("bytes=2-4", self.body[2:5], "2-4"),
            ("bytes=-3", self.body[-3:], "7-9"),
            ("bytes=7-", self.body[7:], "7-9"),
            ("bytes=7-90", self.body[7:], "7-9"),
            ("bytes=-100", self.body, "0-9"),
            ("Bytes=00000000000001-0002", self.body[1:3], "1-2"),
        ]:
            with self.subTest(range=value):
                statements = []
                self.con.set_trace_callback(statements.append)
                response = serve_attachment(self.con, 1, value)
                self.assertEqual(response.status_code, 206)
                self.assertEqual(response.body, expected)
                self.assertEqual(response.headers["content-range"], f"bytes {bounds}/10")
                self.assertEqual(response.headers["content-length"], str(len(expected)))
                self.assertEqual(response.headers["accept-ranges"], "bytes")
                self.assertEqual(response.headers["cache-control"], "no-store")
                self.assertEqual(response.headers["x-content-type-options"], "nosniff")
                self.assertEqual(response.headers["content-type"], "audio/wav")
                self.assertTrue(any("SELECT substr(body," in sql for sql in statements))
                self.assertFalse(any("SELECT body " in sql for sql in statements))

    def test_unsatisfiable_and_invalid_single_ranges(self):
        for value in ("bytes=10-", "bytes=5-3", "bytes=-0", "bytes=x-1", "bytes=-", "bytes=" + "9" * 5000 + "-"):
            with self.subTest(case=value[:80]):
                response = serve_attachment(self.con, 1, value)
                self.assertEqual(response.status_code, 416)
                self.assertEqual(response.body, b"")
                self.assertEqual(response.headers["content-range"], "bytes */10")
        self.assertEqual(serve_attachment(self.con, 3, "bytes=0-").headers["content-range"], "bytes */0")

    def test_full_and_unsupported_ranges(self):
        for value in (None, "items=0-2", "bytes=0-1,4-5"):
            with self.subTest(range=value):
                response = serve_attachment(self.con, 1, value)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.body, self.body)
                self.assertEqual(response.headers["content-length"], "10")
                self.assertNotIn("content-range", response.headers)

    def test_head_uses_metadata_only_and_ignores_range(self):
        statements = []
        self.con.set_trace_callback(statements.append)
        response = serve_attachment(self.con, 1, "bytes=0-1", head=True)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.body, b"")
        self.assertEqual(response.headers["content-length"], "10")
        self.assertEqual(response.headers["accept-ranges"], "bytes")
        self.assertNotIn("content-range", response.headers)
        self.assertEqual(len(statements), 1)
        self.assertIn("length(body)", statements[0])

    def test_unavailable_attachments_and_untrusted_mime(self):
        for attachment_id in (2, 5, 2**63):
            with self.subTest(attachment_id=attachment_id), self.assertRaises(HTTPException) as ctx:
                serve_attachment(self.con, attachment_id)
            self.assertEqual(ctx.exception.status_code, 404)
        response = serve_attachment(self.con, 4)
        self.assertEqual(response.headers["content-type"], "application/octet-stream")
        self.assertEqual(response.headers["x-content-type-options"], "nosniff")

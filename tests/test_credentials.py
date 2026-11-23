import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from app import create_app


class CredentialsTests(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.path=Path(temp.name)/"vault.sqlite3"
        self.master=self.path.with_name(self.path.name+".credentials.key")
        scope=patch.dict(os.environ,{"OPENAI_API_KEY":""})
        scope.start()
        self.addCleanup(scope.stop)

    def test_chat_and_service_persist_without_plaintext(self):
        chat="sk-demo-chat-fixture-not-a-real-key"
        image="sk-demo-image-fixture-not-a-real-key"
        with TestClient(create_app(self.path)) as client:
            current=client.get("/api/settings").json()
            res=client.put("/api/settings",json={**current,"api_key":chat})
            self.assertTrue(res.json()["saved_key"])
            media=client.get("/api/settings/media").json()
            media["services"]["image"]["api_key"]=image
            self.assertTrue(client.put("/api/settings/media",json=media).json()["services"]["image"]["saved_key"])
        if os.name == "posix":
            self.assertEqual(self.master.stat().st_mode & 0o777,0o600)
        with sqlite3.connect(self.path) as con:
            credentials=dict(con.execute("SELECT name,ciphertext FROM credential_values").fetchall())
            self.assertEqual(set(credentials),{"chat","image"})
            self.assertNotIn(chat.encode(),credentials["chat"])
            self.assertNotIn(image.encode(),credentials["image"])
        for f in self.path.parent.glob("*.sqlite3*"):
            self.assertNotIn(chat.encode(),f.read_bytes())
            self.assertNotIn(image.encode(),f.read_bytes())
        with TestClient(create_app(self.path)) as reopened:
            settings=reopened.get("/api/settings").json()
            self.assertTrue(settings["key_set"])
            self.assertNotIn("api_key",settings)
            services=reopened.get("/api/settings/media").json()
            self.assertTrue(services["services"]["stt"]["key_set"])
            self.assertTrue(services["services"]["image"]["override_key_set"])
            self.assertNotIn("api_key",services["services"]["image"])
            self.assertTrue(reopened.put("/api/settings",json={**settings,"api_key":None}).json()["saved_key"])
            self.assertFalse(reopened.put("/api/settings",json={**settings,"api_key":""}).json()["saved_key"])
        with TestClient(create_app(self.path)) as reopened:
            self.assertFalse(reopened.get("/api/settings").json()["key_set"])
            self.assertTrue(reopened.get("/api/settings/media").json()["services"]["image"]["override_key_set"])

    def test_master_missing_preserves_text_and_requires_explicit_reset(self):
        with TestClient(create_app(self.path)) as c:
            settings=c.get("/api/settings").json()
            self.assertEqual(c.put("/api/settings",json={**settings,"api_key":"fake-local-test"}).status_code,200)
        self.master.unlink()
        with TestClient(create_app(self.path)) as c:
            settings=c.get("/api/settings").json()
            self.assertFalse(settings["key_set"])
            self.assertTrue(settings["saved_key"])
            self.assertIn("加密文件",settings["key_store_error"])
            self.assertEqual(c.get("/api/chat").status_code,200)
            self.assertEqual(c.put("/api/settings",json={**settings,"api_key":"replacement"}).status_code,503)
            self.assertFalse(c.put("/api/settings",json={**settings,"api_key":""}).json()["saved_key"])
            self.assertTrue(c.put("/api/settings",json={**settings,"api_key":"replacement"}).json()["key_set"])


if __name__=="__main__":
    unittest.main()

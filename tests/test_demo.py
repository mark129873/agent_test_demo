import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from fastapi.testclient import TestClient

from app import create_app
from app.agent import ModelFailure, respond

START = datetime(2026,11,20,10,tzinfo=timezone.utc)

class DemoTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = Path(self.temp.name) / "demo.sqlite3"
        self.client = TestClient(create_app(self.db,clock=lambda:START))
        self.addCleanup(self.client.close)

    def send(self, text, key):
        return self.client.post("/api/chat/messages",json={"text":text,"request_id":key})

    def opens(self, client, day):
        return client.post("/api/chat/open",json={"demo_date":day}).json()

    def test_end_to_end_memory_reschedule_restart_and_followup(self):
        self.assertEqual(self.client.get("/").status_code,200)
        self.assertEqual(self.client.get("/static/app.js").status_code,200)
        self.assertEqual(self.send("我不吃香菜，我养了一只猫叫年糕，我住北京，下周三面试。","request-1").status_code,200)
        self.assertEqual(self.send("我从北京搬到上海了，面试改到下周五。","request-2").status_code,200)
        self.assertEqual(self.send("下周去北京出差。","request-3").status_code,200)
        with TestClient(create_app(self.db,clock=lambda:START)) as reopened:
            memory = reopened.get("/api/memories").json()
            got = {item["key"]:item["value"] for item in memory["facts"]}
            self.assertEqual(got,{"food:香菜":"不吃香菜","pet_name":"年糕","residence":"上海"})
            self.assertEqual(memory["events"][0]["event_date"],"2026-11-27")
            self.assertFalse(self.opens(reopened,"2026-11-26")["followup"])
            self.assertFalse(self.opens(reopened,"2026-11-27")["followup"])
            self.assertTrue(self.opens(reopened,"2026-11-28")["followup"])
            self.assertFalse(self.opens(reopened,"2026-11-28")["followup"])
            self.assertEqual(sum(m["role"]=="followup" for m in reopened.get("/api/chat").json()["messages"]),1)

    def test_failure_keeps_user_text_and_retry_dedup(self):
        tries = 0
        async def transient(config,key,context):
            nonlocal tries
            tries += 1
            if tries == 1: raise ModelFailure("模拟异常")
            return await respond(config,key,context)
        with TestClient(create_app(self.db,transient,lambda:START)) as c:
            body = {"text":"我住北京。","request_id":"retry-123"}
            self.assertEqual(c.post("/api/chat/messages",json=body).status_code,503)
            self.assertEqual(len(c.get("/api/chat").json()["messages"]),1)
            for _ in range(2): self.assertEqual(c.post("/api/chat/messages",json=body).status_code,200)
            self.assertEqual(tries,2)
            self.assertEqual(len(c.get("/api/chat").json()["messages"]),2)

    def test_event_result_suppresses_followup(self):
        self.send("下周三面试。","request-11")
        self.send("面试通过了！","request-12")
        self.assertFalse(self.opens(self.client,"2026-12-01")["followup"])
        self.assertEqual(self.client.get("/api/memories").json()["events"][0]["status"],"resolved")

    def test_unrelated_dates_and_outcomes_do_not_change_interview(self):
        for i, text in enumerate(["下周三面试。","我打算明天改简历。","考试通过了！","我想明天看电影。"]):
            self.assertEqual(self.send(text,f"unrelated-{i}").status_code,200)
        event = self.client.get("/api/memories").json()["events"][0]
        self.assertEqual((event["event_date"],event["status"]),("2026-11-25","pending"))

    def test_question_and_hypothesis_do_not_overwrite_facts(self):
        for i, text in enumerate(["我养了一只猫叫年糕。","我住上海。","你记得我家猫叫什么吗？",
                                  "如果我住在深圳，你会去哪玩？我爱吃豆腐。"]):
            self.assertEqual(self.send(text,f"fact-guard-{i}").status_code,200)
        got = {item["key"]:item["value"] for item in self.client.get("/api/memories").json()["facts"]}
        self.assertEqual(got,{"pet_name":"年糕","residence":"上海","food:豆腐":"爱吃豆腐"})

    def test_mock_replies_vary_and_old_event_does_not_interrupt(self):
        replies = [self.send(text,f"style-case-{i}").json()["messages"][-1]["text"]
                   for i, text in enumerate(["今天开心，老板夸我了！","你在干嘛？","下周三面试。"])]
        self.assertEqual(len(set(replies)),3)
        self.assertFalse(self.opens(self.client,"2027-02-01")["followup"])

    def test_reschedule_without_repeating_subject_after_interview_message(self):
        self.send("下周三面试。","subject-1")
        self.send("改到下周五了。","subject-2")
        self.assertEqual(self.client.get("/api/memories").json()["events"][0]["event_date"],"2026-11-27")

    def test_dated_cancellation_and_uncertain_result(self):
        self.send("下周三面试。","cancel-1")
        self.send("不知道面试能不能通过。","cancel-2")
        self.assertEqual(self.client.get("/api/memories").json()["events"][0]["status"],"pending")
        r = self.send("下周三面试取消了。","cancel-3")
        self.assertIn("取消",r.json()["messages"][-1]["text"])
        self.assertEqual(self.client.get("/api/memories").json()["events"][0]["status"],"resolved")
        self.assertFalse(self.opens(self.client,"2026-11-26")["followup"])

    def test_example_does_not_overwrite_actual_residence(self):
        self.send("我住上海。","example-1")
        self.send("比如说，我住北京。","example-2")
        self.assertEqual(self.client.get("/api/memories").json()["facts"][0]["value"],"上海")

    def test_guards_and_settings(self):
        setup = self.client.get("/api/settings").json()
        self.assertEqual(setup["model"], "gpt-6-luna")
        self.assertFalse(setup["key_set"])
        setup["api_key"] = "key-for-local-test"
        self.assertTrue(self.client.put("/api/settings",json=setup).json()["key_set"])
        self.assertNotIn("key-for-local-test",self.db.read_bytes().decode('utf-8',errors='ignore'))
        self.assertNotIn("api_key",self.client.get("/api/settings").json())
        self.assertTrue(self.client.put("/api/settings",json={**setup,"api_key":None}).json()["key_set"])
        self.assertFalse(self.client.put("/api/settings",json={**setup,"api_key":""}).json()["key_set"])
        self.assertEqual(self.client.post("/api/chat/open",json={},headers={"Origin":"https://example.com"}).status_code,403)
        self.assertEqual(self.client.put("/api/settings",json={**setup,"base_url":"https://example.com/v1"}).status_code,400)
        self.assertEqual(self.client.put("/api/settings",json={**setup,"mode":"openai"}).status_code,200)
        self.assertEqual(self.client.post("/api/chat/open",json={"demo_date":"2026-12-01"}).status_code,400)

if __name__=="__main__":
    unittest.main()

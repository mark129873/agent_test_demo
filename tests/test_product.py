"""Offline end-to-end checks for continuity, paging, and concurrent UI actions."""

import asyncio
import copy
import json
import os
import sqlite3
import stat
import tempfile
import unittest
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from httpx import ASGITransport, AsyncClient

from app.agent import ModelFailure, simulate
from app import create_app
from app.continuity import CONTEXT_BUDGET, RECENT_TURNS, SUMMARY_BATCH


START = datetime(2026, 11, 20, 10, tzinfo=timezone.utc)
INTERVIEW_DAY = "2026-11-26"
WAIT = 4


class ProductTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = Path(self.temp.name) / "product.sqlite3"

    async def client(self, model, app=None, clock=lambda: START):
        if app is None:
            app = create_app(self.db, model_call=model, clock=clock)
        client = AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver")
        await client.__aenter__()
        self.addAsyncCleanup(client.aclose)
        return client

    def saved(self):
        with closing(sqlite3.connect(self.db)) as con:
            epoch, floor, covered, summary = con.execute(
                "SELECT epoch, floor, covered, summary FROM continuity WHERE id=1"
            ).fetchone()
        return epoch, floor, covered, summary

    @staticmethod
    async def send(client, key, message, expected=200):
        result = await client.post("/api/chat/messages", json={"request_id": key, "text": message})
        if expected is not None and result.status_code != expected:
            raise AssertionError(f"{key}: expected HTTP {expected}, got {result.status_code}: {result.text}")
        return result

    async def get_json(self, client, path, **params):
        result = await client.get(path, params=params)
        result.raise_for_status()
        return result.json()

    async def eventually(self, predicate):
        async with asyncio.timeout(WAIT):
            while not predicate():
                await asyncio.sleep(0.01)

    async def test_110_rounds_compact_and_page_across_restart(self):
        contexts = []
        batches = []

        async def model(setup, key, context):
            if context["type"] == "summary":
                batches.append(copy.deepcopy(context))
                first = context["previous_summary"] or context["turns"][0]["user"][:35]
                return {"summary": first}
            contexts.append(copy.deepcopy(context))
            return {"reply": "answer:" + context["message"][:21], "patches": []}

        client = await self.client(model)
        for i in range(110):
            await self.send(client, f"long-round-{i}", f"round-{i:03d} " + "猫" * 730)

        await self.eventually(lambda: self.saved()[2] > 100)
        _, _, covered, summary = self.saved()
        self.assertIn("round-000", summary)
        self.assertGreaterEqual(len(batches), 2)
        self.assertTrue(any("round-000" in c.get("history_summary", "") for c in contexts))
        for context in [*contexts, *batches]:
            self.assertLessEqual(len(json.dumps(context, ensure_ascii=False).encode()), CONTEXT_BUDGET)
        for batch in batches:
            for turn in batch["turns"]:
                self.assertTrue(turn["assistant"].startswith("answer:" + turn["user"][:21]))

        latest = await self.get_json(client, "/api/chat", limit=17)
        self.assertEqual(len(latest["messages"]), 17)
        self.assertIsNotNone(latest["next_cursor"])
        restarted = await self.client(model)
        self.assertGreaterEqual(self.saved()[2], covered)
        self.assertEqual(self.saved()[3], summary)

        await self.send(restarted, "restart-probe", "new message after restart")
        self.assertIn("round-000", contexts[-1].get("history_summary", ""))

        pages = [latest["messages"]]
        cursor = latest["next_cursor"]
        for _ in range(20):
            if cursor is None:
                break
            page = await self.get_json(restarted, "/api/chat", limit=17, before=cursor)
            pages.append(page["messages"])
            cursor = page["next_cursor"]
        self.assertIsNone(cursor, "paging must terminate")
        history = [row for page in reversed(pages) for row in page]
        self.assertEqual([row["id"] for row in history], list(range(1, 221)))
        self.assertEqual(len({row["request_id"] for row in history if row["role"] == "user"}), 110)
        for user, assistant in zip(history[::2], history[1::2]):
            self.assertEqual(user["delivery_state"], "answered")
            self.assertEqual(assistant["reply_to"], user["id"])
        self.assertTrue(history[0]["text"].startswith("round-000"))

    async def test_forget_discards_paused_reply_and_summary_but_keeps_history_and_other_fact(self):
        summary_started, summary_late, reply_started, release_summary, release_reply = (asyncio.Event() for _ in range(5))
        contexts = []

        async def model(setup, key, context):
            if context["type"] == "summary":
                summary_started.set()
                try:
                    await release_summary.wait()
                except asyncio.CancelledError:
                    # Simulate a provider that finishes even when cancellation is requested.
                    await release_summary.wait()
                summary_late.set()
                return {"summary": "北京 and private old passages"}
            contexts.append(copy.deepcopy(context))
            if context["message"] == "我住北京。另一个旧私密片段。":
                reply_started.set()
                await release_reply.wait()
            return simulate(context)

        client = await self.client(model)
        await self.send(client, "private-origin", "我住北京，我养了一只猫叫年糕。")
        for i in range(RECENT_TURNS + SUMMARY_BATCH):
            await self.send(client, f"old-note-{i}", f"此前的便签第{i}张")
        await asyncio.wait_for(summary_started.wait(), WAIT)
        slow = asyncio.create_task(self.send(
            client, "old-paused-reply", "我住北京。另一个旧私密片段。", expected=None))
        try:
            await asyncio.wait_for(reply_started.wait(), WAIT)
            snapshot = await self.get_json(client, "/api/chat")
            pending = next(row for row in snapshot["messages"] if row["request_id"] == "old-paused-reply")
            self.assertEqual(pending["delivery_state"], "pending")
            response = await asyncio.wait_for(client.delete("/api/memories/residence"), WAIT)
            self.assertIn(response.status_code, (200, 204), response.text)
        finally:
            release_summary.set()
            release_reply.set()
            await asyncio.wait_for(slow, WAIT)
            await asyncio.wait_for(summary_late.wait(), WAIT)
        calls = len(contexts)
        retired = await self.send(client, "old-paused-reply",
                                  "我住北京。另一个旧私密片段。", expected=None)
        self.assertLess(retired.status_code, 500)
        self.assertEqual(len(contexts), calls, "retry must not replay retired content")
        await self.send(client, "after-forgetting", "你记得我原来住哪儿吗？我的猫叫什么？")
        context = json.dumps(contexts[-1], ensure_ascii=False)
        for raw in ("北京", "另一个旧私密片段", "此前的便签"):
            self.assertNotIn(raw, context)
        self.assertIn("年糕", context)
        facts = await self.get_json(client, "/api/memories")
        self.assertEqual({f["key"]: f["value"] for f in facts["facts"]}, {"pet_name": "年糕"})
        history = (await self.get_json(client, "/api/chat", limit=60))["messages"]
        self.assertTrue(any("北京" in row["text"] for row in history))
        self.assertFalse(any(row["reply_to"] == pending["id"] for row in history))
        self.assertEqual(self.saved()[3], "")

    async def test_edit_preserves_other_fact_and_cuts_old_context(self):
        contexts = []

        async def model(setup, key, context):
            contexts.append(copy.deepcopy(context))
            return simulate(context)

        client = await self.client(model)
        await self.send(client, "correction-origin", "我住北京，我养了一只猫叫年糕。旧谈话标记。")
        before = (await self.get_json(client, "/api/chat"))["messages"]
        response = await client.put("/api/memories/residence", json={"value": "南京"})
        response.raise_for_status()
        await self.send(client, "correction-probe", "我住哪里、猫叫什么？")
        payload = json.dumps(contexts[-1], ensure_ascii=False)
        self.assertIn("南京", payload)
        self.assertIn("年糕", payload)
        self.assertNotIn("北京", payload)
        self.assertNotIn("旧谈话标记", payload)
        after = (await self.get_json(client, "/api/chat"))["messages"]
        self.assertEqual(before, after[:len(before)])

    async def test_forgetting_linked_fact_hides_events_from_chat_and_followup(self):
        contexts = []

        async def model(setup,key,context):
            contexts.append(copy.deepcopy(context))
            return simulate(context)

        client = await self.client(model)
        await self.send(client,"city-origin","我住广州，明天有广州面试。")
        await self.send(client,"city-other","明天有乙公司面试。")
        (await client.delete("/api/memories/residence")).raise_for_status()
        reopened = await self.client(model)
        await self.send(reopened,"city-neutral-probe","晚上好。")
        neutral = json.dumps(contexts[-1],ensure_ascii=False)
        self.assertNotIn("广州",neutral)
        self.assertNotIn("乙公司",neutral)
        await self.send(reopened,"city-probe-2","接下来的面试是什么时候？")
        payload = json.dumps(contexts[-1],ensure_ascii=False)
        self.assertNotIn("广州",payload)
        self.assertIn("乙公司",payload)
        await self.send(reopened,"city-probe-related","想聊聊乙公司的面试。")
        self.assertIn("乙公司",json.dumps(contexts[-1],ensure_ascii=False))
        facts = await self.get_json(reopened,"/api/memories")
        self.assertEqual({e["label"]:e["status"] for e in facts["events"]},
                         {"广州面试":"muted","乙公司面试":"pending"})
        opened = (await reopened.post("/api/chat/open",json={"demo_date":"2026-11-22"})).json()
        asks = [m["text"] for m in opened["messages"] if m["role"]=="followup"]
        self.assertEqual(len(asks),1)
        self.assertIn("乙公司",asks[0])
        self.assertTrue(any("广州" in m["text"] for m in opened["messages"] if m["role"]=="user"))
        event = next(e for e in facts["events"] if e["label"]=="乙公司面试")
        (await reopened.delete(f"/api/memories/events/{event['event_id']}")).raise_for_status()
        await self.send(reopened,"city-probe-3","接下来还有面试吗？")
        self.assertNotIn("乙公司",json.dumps(contexts[-1],ensure_ascii=False))

    async def test_incomplete_head_blank_summary_and_late_reply(self):
        blank_attempt = asyncio.Event()
        successful_attempt = asyncio.Event()
        fail_head, use_blank = True, True
        batches = []

        async def model(setup, key, context):
            if context["type"] == "summary":
                batches.append(copy.deepcopy(context))
                if use_blank:
                    blank_attempt.set()
                    return {"summary": "  "}
                successful_attempt.set()
                return {"summary": "head and later turns"}
            if context["message"] == "delayed head" and fail_head:
                raise ModelFailure("offline planned failure")
            return {"reply": "paired:" + context["message"], "patches": []}

        client = await self.client(model)
        await self.send(client, "delayed-head", "delayed head", expected=503)
        head = (await self.get_json(client, "/api/chat"))["messages"][0]
        self.assertEqual(head["delivery_state"], "pending")
        for i in range(RECENT_TURNS + SUMMARY_BATCH + 1):
            await self.send(client, f"ahead-of-head-{i}", f"later turn {i}")
        self.assertEqual(self.saved()[2:], (0, ""))
        self.assertFalse(batches, "a summary must not cross an incomplete head")

        fail_head = False
        await self.send(client, "delayed-head", "delayed head")
        await asyncio.wait_for(blank_attempt.wait(), WAIT)
        self.assertEqual(self.saved()[2:], (0, ""))

        use_blank = False
        await self.send(client, "trigger-valid", "check the summary retry")
        await asyncio.wait_for(successful_attempt.wait(), WAIT)
        await self.eventually(lambda: self.saved()[2] > 0)
        self.assertEqual(batches[-1]["turns"][0]["user"], "delayed head")
        self.assertEqual(batches[-1]["turns"][0]["assistant"], "paired:delayed head")
        rows = (await self.get_json(client, "/api/chat", limit=60))["messages"]
        self.assertEqual(rows[0]["delivery_state"], "answered")
        self.assertEqual(sum(row["reply_to"] == head["id"] for row in rows), 1)
        head_page = await self.get_json(client, "/api/chat", before=head["id"] + 1, limit=1)
        self.assertEqual(head_page["messages"][0]["delivery_state"], "answered")
        self.assertIsNone(head_page["next_cursor"])

    async def test_two_tabs_open_dedup_and_slow_followup_does_not_block_send(self):
        open_started, release = asyncio.Event(), asyncio.Event()

        async def model(setup, key, context):
            if context["type"] == "session_open":
                open_started.set()
                await release.wait()
            return simulate(context)

        app = create_app(self.db, model_call=model, clock=lambda: START)
        first, second = await self.client(model, app), await self.client(model, app)
        await self.send(first, "first-interview", "下周三面试。")
        first_open = asyncio.create_task(first.post("/api/chat/open", json={"demo_date": INTERVIEW_DAY}))
        second_open = None
        try:
            await asyncio.wait_for(open_started.wait(), WAIT)
            second_open = asyncio.create_task(second.post("/api/chat/open", json={"demo_date": INTERVIEW_DAY}))
            await asyncio.wait_for(self.send(second, "chat-while-opening", "这条消息不必等回访。"), WAIT)
        finally:
            release.set()
            responses = await asyncio.wait_for(
                asyncio.gather(*(r for r in (first_open, second_open) if r is not None)), WAIT)
        for response in responses:
            response.raise_for_status()
        self.assertFalse(any(r.json()["followup"] for r in responses),
                         "a slow follow-up should not interrupt a new send")
        tabs = await asyncio.gather(
            first.post("/api/chat/open", json={"demo_date": INTERVIEW_DAY}),
            second.post("/api/chat/open", json={"demo_date": INTERVIEW_DAY}),
        )
        for response in tabs:
            response.raise_for_status()
        self.assertEqual(sum(r.json()["followup"] for r in tabs), 1)
        history = (await self.get_json(first, "/api/chat"))["messages"]
        self.assertEqual(sum(row["role"] == "followup" for row in history), 1)

    async def test_real_open_uses_local_template_without_calling_model(self):
        async def model(setup, key, context):
            if context["type"] != "user_message":
                self.fail("real-date follow-up must not invoke a model")
            return simulate(context)

        schedule = await self.client(model)
        await self.send(schedule, "real-interview", "下周三面试。")
        real_date = datetime(2026, 11, 26, 10, tzinfo=timezone.utc)
        client = await self.client(model, clock=lambda: real_date)
        settings = await self.get_json(client, "/api/settings")
        response = await client.put("/api/settings", json={**settings, "mode": "openai"})
        response.raise_for_status()
        opened = await client.post("/api/chat/open", json={})
        opened.raise_for_status()
        self.assertTrue(opened.json()["followup"])
        message = next(row for row in opened.json()["messages"] if row["role"] == "followup")
        self.assertEqual(message["text"], "昨天那场面试怎么样？")

    async def test_two_interviews_require_identity_for_updates(self):
        async def model(setup, key, context):
            return simulate(context)

        client = await self.client(model)
        await self.send(client, "interview-alpha", "下周三星河公司面试。")
        await self.send(client, "interview-beta", "下周五青木公司面试。")

        async def interviews():
            return {row["label"]: (row["event_date"], row["status"])
                    for row in (await self.get_json(client, "/api/memories"))["events"]}

        expected = {"星河公司面试": ("2026-11-25", "pending"),
                    "青木公司面试": ("2026-11-27", "pending")}
        self.assertEqual(await interviews(), expected)
        ask = await self.send(client, "ambiguous-interview", "面试改到下周四了。")
        self.assertIn("哪一场", ask.json()["messages"][-1]["text"])
        self.assertEqual(await interviews(), expected)
        await self.send(client, "resolved-interview", "星河公司面试取消了。")
        await self.send(client, "updated-interview", "青木公司面试改到下周四了。")
        self.assertEqual(await interviews(), {"星河公司面试": ("2026-11-25", "resolved"),
                                             "青木公司面试": ("2026-11-26", "pending")})
        follow = await client.post("/api/chat/open", json={"demo_date": "2026-11-27"})
        follow.raise_for_status()
        self.assertTrue(follow.json()["followup"])
        self.assertIn("青木公司面试", follow.json()["messages"][-1]["text"])

    async def test_abandon_active_head_discards_late_reply_and_unblocks_summary(self):
        active, release = asyncio.Event(), asyncio.Event()
        batches = []
        head_calls = 0

        async def model(setup, key, context):
            nonlocal head_calls
            if context["type"] == "summary":
                batches.append(copy.deepcopy(context))
                return {"summary": "compact after abandoned head"}
            if context["message"] == "retire an unanswered head":
                head_calls += 1
                if head_calls == 1:
                    raise ModelFailure("offline planned failure")
                active.set()
                await release.wait()
            return {"reply": "paired:" + context["message"], "patches": []}

        client = await self.client(model)
        text = "retire an unanswered head"
        key = "abandon-inflight-head"
        await self.send(client, key, text, expected=503)
        for i in range(RECENT_TURNS + SUMMARY_BATCH):
            await self.send(client, f"abandon-later-{i}", f"abandon test later turn {i}")
        self.assertEqual(self.saved()[2:], (0, ""))
        slow = asyncio.create_task(self.send(client, key, text, expected=None))
        try:
            await asyncio.wait_for(active.wait(), WAIT)
            response = await asyncio.wait_for(
                client.post("/api/chat/abandon", json={"request_id": key}), WAIT)
            response.raise_for_status()
            head = next(row for row in response.json()["messages"] if row["request_id"] == key)
            self.assertNotEqual(head["delivery_state"], "pending")
        finally:
            release.set()
            await asyncio.wait_for(slow, WAIT)
        await self.send(client, key, text, expected=409)
        self.assertEqual(head_calls, 2)
        await self.send(client, "abandon-compact-trigger", "compact now")
        await self.eventually(lambda: self.saved()[2] > head["id"])
        self.assertTrue(batches)
        self.assertNotIn(text, json.dumps(batches))
        history = (await self.get_json(client, "/api/chat"))["messages"]
        self.assertTrue(any(row["text"] == text for row in history))
        self.assertFalse(any(row["reply_to"] == head["id"] for row in history))

    async def test_online_summary_requires_explicit_opt_in(self):
        calls = []

        async def model(setup, key, context):
            calls.append((setup["mode"], context["type"]))
            return {"summary": "offline fake summary"} if context["type"] == "summary" else {
                "reply": "offline fake reply", "patches": []}

        client = await self.client(model)
        settings = await self.get_json(client, "/api/settings")
        response = await client.put("/api/settings", json={**settings, "mode": "openai"})
        response.raise_for_status()
        self.assertFalse(response.json()["online_summary"])
        for i in range(RECENT_TURNS + SUMMARY_BATCH):
            await self.send(client, f"online-no-summary-{i}", f"online test turn {i}")
        self.assertNotIn(("openai", "summary"), calls)
        self.assertEqual(self.saved()[2:], (0, ""))

        response = await client.put("/api/settings", json={**response.json(), "online_summary": True})
        response.raise_for_status()
        self.assertTrue(response.json()["online_summary"])
        await self.send(client, "online-summary-enabled", "summary opt-in now on")
        await self.eventually(lambda: self.saved()[2] > 0)
        self.assertIn(("openai", "summary"), calls)

    async def test_legacy_single_event_schema_survives_upgrade(self):
        with closing(sqlite3.connect(self.db)) as con, con:
            con.executescript("""
                CREATE TABLE messages(id INTEGER PRIMARY KEY, role TEXT NOT NULL,
                    text TEXT NOT NULL, created_at TEXT NOT NULL, request_id TEXT UNIQUE,
                    reply_to INTEGER UNIQUE REFERENCES messages(id));
                INSERT INTO messages VALUES(1, 'user', '下周三面试。',
                    '2026-11-20T10:00:00+00:00', 'legacy-event', NULL);
                INSERT INTO messages VALUES(2, 'assistant', '祝面试顺利！',
                    '2026-11-20T10:00:00+00:00', NULL, 1);
                CREATE TABLE events(key TEXT PRIMARY KEY, label TEXT NOT NULL,
                    event_date TEXT NOT NULL, status TEXT NOT NULL,
                    source_id INTEGER NOT NULL REFERENCES messages(id),
                    quote TEXT NOT NULL, updated_at TEXT NOT NULL,
                    followup_id INTEGER REFERENCES messages(id));
                INSERT INTO events VALUES('interview', '面试', '2026-11-25', 'pending',
                    1, '下周三面试。', '2026-11-20T10:00:00+00:00', NULL);
            """)

        async def model(setup, key, context):
            return simulate(context)

        restarted = await self.client(model)
        history = (await self.get_json(restarted, "/api/chat"))["messages"]
        self.assertEqual(history[0]["delivery_state"], "answered")
        self.assertEqual(history[1]["reply_to"], 1)
        memories = await self.get_json(restarted, "/api/memories")
        self.assertEqual(len(memories["events"]), 1)
        migrated = memories["events"][0]
        self.assertEqual((migrated["key"], migrated["event_date"], migrated["source_id"]),
                         ("interview", "2026-11-25", 1))
        self.assertIn("event_id", migrated)
        follow = await restarted.post("/api/chat/open", json={"demo_date": INTERVIEW_DAY})
        follow.raise_for_status()
        self.assertTrue(follow.json()["followup"])
        reopened_again = await self.client(model)
        follow = await reopened_again.post("/api/chat/open", json={"demo_date": INTERVIEW_DAY})
        follow.raise_for_status()
        self.assertFalse(follow.json()["followup"])

    async def test_summary_no_change_advances_retaining_prior_and_later_meaningful_batch(self):
        results = [
            {"summary": "prior significant context"},
            {"no_summary": "true"},  # Boolean required.
            {"summary": " \n "},
            {"no_summary": True},
            {"summary": "later significant context"},
        ]
        attempts = []
        queued = asyncio.Queue()

        async def model(setup, key, context):
            if context["type"] == "summary":
                attempts.append(copy.deepcopy(context))
                index = len(attempts) - 1
                queued.put_nowait(index)
                return results[index] if index < len(results) else {"no_summary": True}
            return {"reply":"recorded", "patches":[]}

        client = await self.client(model)
        next_id = 0

        async def send_turns(count):
            nonlocal next_id
            for _ in range(count):
                await self.send(client, f"no-summary-turn-{next_id}", f"turn {next_id}")
                next_id += 1

        async def wait_for_batch(index):
            self.assertEqual(await asyncio.wait_for(queued.get(), WAIT), index)
            return self.saved()[2:]

        await send_turns(RECENT_TURNS + SUMMARY_BATCH)
        covered, saved = await wait_for_batch(0)
        self.assertGreater(covered, 0)
        self.assertEqual(saved, results[0]["summary"])

        await send_turns(SUMMARY_BATCH)
        self.assertEqual(await wait_for_batch(1), (covered, saved))
        await send_turns(1)
        self.assertEqual(await wait_for_batch(2), (covered, saved))
        client = await self.client(model)
        await send_turns(1)
        next_covered, same_summary = await wait_for_batch(3)
        self.assertGreater(next_covered, covered)
        self.assertEqual(same_summary, saved)
        self.assertEqual(attempts[1]["turns"], attempts[2]["turns"])
        self.assertEqual(attempts[2]["turns"], attempts[3]["turns"])

        await send_turns(SUMMARY_BATCH - 2)
        final_covered, final_summary = await wait_for_batch(4)
        self.assertGreater(final_covered, next_covered)
        self.assertEqual(final_summary, results[4]["summary"])
        self.assertEqual(attempts[-1]["previous_summary"], saved)

    async def test_paid_summary_failure_retries_same_batch_once_then_waits_for_restart(self):
        calls = []
        loop = asyncio.get_running_loop()
        original_time = loop.time
        elapsed = 0
        recover = False

        async def model(setup, key, context):
            if context["type"] == "summary":
                calls.append(tuple(turn["source_id"] for turn in context["turns"]))
                if not recover:
                    raise ModelFailure("offline paid-provider simulation")
                return {"summary":"recovered after restart"}
            return {"reply":"recorded", "patches":[]}

        client = await self.client(model)
        setup = await self.get_json(client, "/api/settings")
        result = await client.put("/api/settings", json={**setup,"mode":"openai","online_summary":True})
        result.raise_for_status()
        idx = 0

        async def send_turn():
            nonlocal idx
            await self.send(client, f"paid-backoff-{idx}", f"paid summary test turn {idx}")
            idx += 1

        with (patch.object(loop, "time", lambda: original_time() + elapsed),
              patch.object(loop, "slow_callback_duration", float("inf"))):
            for _ in range(RECENT_TURNS + SUMMARY_BATCH):
                await send_turn()
            await self.eventually(lambda: len(calls) == 1)
            self.assertEqual(self.saved()[2:], (0, ""))
            elapsed += 119
            await send_turn()
            self.assertEqual(len(calls), 1, "must cool down for two minutes")
            elapsed += 2
            await send_turn()
            await self.eventually(lambda: len(calls) == 2)
            self.assertEqual(calls[0], calls[1])
            self.assertEqual(self.saved()[2:], (0, ""))
            elapsed += 600
            for _ in range(3):
                await send_turn()
            self.assertEqual(len(calls), 2, "the failed batch must stop retrying in this process")

            recover = True
            client = await self.client(model)
            await send_turn()
            await self.eventually(lambda: self.saved()[2] > 0)
            self.assertEqual(calls, [calls[0]] * 3)
            self.assertEqual(self.saved()[3], "recovered after restart")

    async def test_two_tabs_queue_before_model_and_retry_pending_across_restart(self):
        started, release, second_started = (asyncio.Event() for _ in range(3))
        invocations = []
        first_message, second_message = "first tab text", "second tab text"

        async def model(setup, key, context):
            invocations.append(context["message"])
            if context["message"] == first_message:
                started.set()
                await release.wait()
            if context["message"] == second_message:
                self.assertEqual(context["recent"], [
                    {"role":"user", "text":first_message},
                    {"role":"assistant", "text":"answered"},
                ])
                second_started.set()
                raise ModelFailure("leave second text pending for restart")
            return {"reply": "answered", "patches": []}

        app = create_app(self.db, model_call=model, clock=lambda: START)
        first, second = await self.client(model, app), await self.client(model, app)
        first_task = asyncio.create_task(self.send(first, "queue-first-tab", first_message, expected=None))
        second_task = None
        duplicate_task = None
        try:
            await asyncio.wait_for(started.wait(), WAIT)
            second_task = asyncio.create_task(self.send(second, "queue-second-tab", second_message, expected=None))
            async with asyncio.timeout(WAIT):
                while True:
                    rows = (await self.get_json(second, "/api/chat"))["messages"]
                    if len(rows) == 2:
                        break
                    await asyncio.sleep(0.01)
            self.assertEqual([(m["text"],m["delivery_state"]) for m in rows],
                             [(first_message,"pending"),(second_message,"pending")])
            self.assertEqual(invocations, [first_message])
            read_after_restart = await self.client(model)
            self.assertEqual(rows, (await self.get_json(read_after_restart, "/api/chat"))["messages"])
            await asyncio.wait_for(self.send(second, "queue-second-tab", "different text", expected=409), WAIT)
            duplicate_task = asyncio.create_task(self.send(second, "queue-second-tab", second_message, expected=None))
            self.assertEqual(len((await self.get_json(first, "/api/chat"))["messages"]), 2)
            self.assertFalse(second_started.is_set())
        finally:
            release.set()
            await asyncio.wait_for(asyncio.gather(*(t for t in (first_task,second_task,duplicate_task)
                                                    if t is not None)), WAIT)
        await asyncio.wait_for(second_started.wait(), WAIT)
        self.assertEqual(invocations, [first_message, second_message])
        self.assertEqual(first_task.result().status_code, 200)
        self.assertEqual(second_task.result().status_code, 503)
        self.assertEqual(duplicate_task.result().status_code, 503)

        async def recovered_model(setup, key, context):
            self.assertEqual(context["message"], second_message)
            return {"reply":"recovered", "patches":[]}

        restarted = await self.client(recovered_model)
        await self.send(restarted, "queue-second-tab", second_message)
        await self.send(restarted, "queue-second-tab", second_message)
        all_rows = (await self.get_json(restarted, "/api/chat"))["messages"]
        self.assertEqual(len(all_rows), 4)
        self.assertEqual([m["id"] for m in all_rows if m["role"]=="user"], [r["id"] for r in rows])
        self.assertEqual(sum(m["reply_to"]==rows[-1]["id"] for m in all_rows), 1)

    async def test_hallucinated_facts_cannot_overwrite_literal_evidence(self):
        async def model(setup, key, context):
            source = context["message"]
            if "上海" in source or "北京" in source:
                patches=[{"kind":"fact","key":"residence","value":"上海" if "上海" in source else "北京",
                          "quote":next(clause for clause in ("住在上海","搬到上海","住在北京") if clause in source)}]
            else:
                patches=[{"kind":"fact","key":"food:香菜","value":"不吃香菜","quote":"不吃香菜"}]
            return {"reply":"收到","patches":patches}

        client = await self.client(model)

        async def facts():
            return {f["key"]:f["value"] for f in (await self.get_json(client,"/api/memories"))["facts"]}

        await self.send(client,"fact-hallucinated-empty","我可能住在上海。")
        self.assertEqual(await facts(), {})
        await self.send(client,"fact-actual-beijing","我住在北京。")
        self.assertEqual(await facts(), {"residence":"北京"})
        for i, statement in enumerate(("我可能住在上海。","我朋友住在上海。","我准备搬到上海。")):
            await self.send(client,f"fact-hallucinated-{i}",statement)
            self.assertEqual(await facts(), {"residence":"北京"})
        await self.send(client,"fact-actual-move","我已经搬到上海了。")
        await self.send(client,"fact-literal-food","我不吃香菜。")
        self.assertEqual(await facts(), {"residence":"上海","food:香菜":"不吃香菜"})

    async def test_private_posix_permissions_for_existing_database_and_wal(self):
        if os.name != "posix":
            self.skipTest("POSIX file-mode check")

        async def model(setup,key,context):
            return {"reply":"saved","patches":[]}

        with closing(sqlite3.connect(self.db)) as con, con:
            pass
        os.chmod(self.db, 0o644)
        self.assertEqual(stat.S_IMODE(self.db.stat().st_mode), 0o644)
        client = await self.client(model)
        with closing(sqlite3.connect(self.db)) as observer, observer:
            observer.execute("BEGIN")
            observer.execute("SELECT * FROM settings").fetchall()
            await self.send(client,"database-private","sensitive text")
            for suffix in ("","-wal","-shm"):
                path=Path(str(self.db)+suffix)
                self.assertTrue(path.exists(), str(path))
                self.assertEqual(stat.S_IMODE(path.stat().st_mode) & 0o077, 0, str(path))

    async def test_opted_in_morning_meeting_followup_is_local_noon_and_one_shot(self):
        # November 20 20:00 in Asia/Shanghai, then the next local morning/noon.
        current = datetime(2026, 11, 20, 12, tzinfo=timezone.utc)
        model_calls = []
        user_text = "明早开完会我来找你，可以问我一句讲得怎样，但今晚不要再提同事那件事了"

        async def model(setup, key, context):
            model_calls.append(context["type"])
            return {"reply":"我今晚就主动提醒你同事那件事，并且到点发消息。", "patches":[]}

        client = await self.client(model, clock=lambda: current)
        response = await self.send(client, "consented-early-meeting", user_text)
        duplicate = await self.send(client, "consented-early-meeting", user_text)
        self.assertEqual(response.json(), duplicate.json())
        user, ack = response.json()["messages"]
        self.assertEqual((ack["role"], ack["reply_to"]), ("assistant", user["id"]))
        self.assertNotIn("同事", ack["text"])
        self.assertNotIn("主动提醒", ack["text"])
        self.assertNotIn("到点发消息", ack["text"])
        self.assertRegex(ack["text"], r"回来|来找|打开|再来|来聊")
        self.assertEqual(model_calls, ["user_message"])

        entries = (await self.get_json(client, "/api/memories"))["events"]
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["status"], "pending")
        self.assertEqual(entries[0]["event_date"], "2026-11-21")
        self.assertEqual(entries[0]["not_before"], "2026-11-21T12:00")
        self.assertNotIn("同事", entries[0]["quote"])

        async def open_chat(tab):
            response = await tab.post("/api/chat/open", json={})
            response.raise_for_status()
            return response.json()

        current = datetime(2026, 11, 20, 13, tzinfo=timezone.utc)  # 21:00, request date.
        self.assertFalse((await open_chat(client))["followup"])
        current = datetime(2026, 11, 21, 3, 59, tzinfo=timezone.utc)
        self.assertFalse((await open_chat(client))["followup"])
        current = datetime(2026, 11, 21, 4, tzinfo=timezone.utc)
        asked = await open_chat(client)
        self.assertTrue(asked["followup"])
        questions = [m["text"] for m in asked["messages"] if m["role"] == "followup"]
        self.assertEqual(len(questions), 1)
        self.assertIn("讲得怎样", questions[0].replace("怎么样", "怎样"))
        self.assertNotIn("同事", questions[0])
        self.assertLessEqual(len(questions[0]), 30)
        self.assertEqual(sum(questions[0].count(mark) for mark in ("？", "?")), 1)
        self.assertFalse((await open_chat(client))["followup"])
        reopened = await self.client(model, clock=lambda: current)
        after_restart = await open_chat(reopened)
        self.assertFalse(after_restart["followup"])
        self.assertEqual(sum(m["role"] == "followup" for m in after_restart["messages"]), 1)
        self.assertEqual(model_calls, ["user_message"])

    async def test_followup_unclear_clarifies_and_no_negative_or_hypothetical_opt_in(self):
        async def model(setup, key, context):
            return {"reply":"已经安排了，明早会主动提醒你。", "patches":[]}

        client = await self.client(model)
        for i, source in enumerate((
            "下次我回来时，可以问我一句结果怎么样吗？",
            "开完会我回来时，可以问我一句讲得怎样吗？",
            "明天我回来时，可以问我一句结果怎么样吗？",
            "明天等我回来，可以问我一句吃得怎样？",
        )):
            response = await self.send(client, f"unclear-followup-{i}", source)
            reply = response.json()["messages"][-1]["text"]
            with self.subTest(source=source):
                self.assertRegex(reply, r"什么时候|哪天|哪件事|什么事|具体")
                self.assertNotIn("已经安排", reply)
                self.assertNotIn("提醒", reply)
        for i, source in enumerate((
            "明早开完会我来找你，不要问我讲得怎样。",
            "如果我明早开完会来找你，你可以问我一句讲得怎样吗？",
        )):
            await self.send(client, f"no-followup-opt-in-{i}", source)
        self.assertEqual((await self.get_json(client, "/api/memories"))["events"], [])

    async def test_courteous_followup_request_is_saved_before_ack(self):
        async def model(setup,key,context):
            return {"reply":"我不能说已经登记。","patches":[]}

        client=await self.client(model)
        for i,permission in enumerate(("能不能","可不可以")):
            r=await self.send(client,f"polite-open-request-{i}",f"明早开完会我来找你，{permission}问我一句讲得怎样？")
            self.assertIn("明天中午后你回来",r.json()["messages"][-1]["text"])
            data=await self.get_json(client,"/api/memories")
            self.assertEqual(len(data["events"]),1)
            self.assertEqual(data["events"][0]["not_before"],"2026-11-21T12:00")

    async def test_generic_events_remain_day_after_through_day_fourteen(self):
        async def model(setup, key, context):
            return simulate(context)

        client = await self.client(model)
        for i, text in enumerate(("明天有面试。", "明天有考试。", "明天有会议。")):
            await self.send(client, f"generic-due-event-{i}", text)

        async def opens(day):
            response = await client.post("/api/chat/open", json={"demo_date":day})
            response.raise_for_status()
            return response.json()["followup"]

        entries = (await self.get_json(client, "/api/memories"))["events"]
        self.assertEqual(len(entries), 3)
        self.assertTrue(all(e["event_date"] == "2026-11-21" and not e.get("not_before") for e in entries))
        self.assertFalse(await opens("2026-11-21"))
        self.assertTrue(await opens("2026-11-22"))
        self.assertTrue(await opens("2026-12-05"))
        self.assertFalse(await opens("2026-12-06"))
        after = (await self.get_json(client, "/api/memories"))["events"]
        self.assertEqual(sorted(row["status"] for row in after), ["asked","asked","pending"])

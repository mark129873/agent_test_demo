import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from quiet_topics import (QUIET_SCHEMA, active_quiet_topics, contains_quiet_topic,
                          filter_context, remember_quiet_topics)


class QuietTopicTests(unittest.TestCase):
    def setUp(self):
        self.con = sqlite3.connect(":memory:")
        self.con.executescript(QUIET_SCHEMA)
        self.zone = ZoneInfo("Asia/Shanghai")
        self.now = datetime(2026, 11, 20, 23, 0, tzinfo=self.zone)

    def tearDown(self):
        self.con.close()

    def test_direct_and_quoted_requests(self):
        for unrelated in ('她说：“今晚不要再提同事那件事。”', '“今晚别提同事”',
                          '如果今晚别提同事会好吗？', '今晚别提同事吗？', '明天别聊面试', '今晚不要提醒我吃药', '今晚不要说谎'):
            self.assertFalse(remember_quiet_topics(self.con, unrelated, self.now), unrelated)
        self.assertEqual(remember_quiet_topics(self.con, '今晚不要再提同事那件事。今天别聊「面试」了', self.now),
                         ('同事', '面试'))
        self.assertEqual(set(active_quiet_topics(self.con, self.now)), {'同事', '面试'})

    def test_reopen_and_local_midnight(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root)/"db.sqlite"
            with sqlite3.connect(path) as con:
                con.executescript(QUIET_SCHEMA)
                remember_quiet_topics(con, "今晚先别提同事那件事", self.now)
            con.close()
            with sqlite3.connect(path) as again:
                self.assertEqual(active_quiet_topics(again, datetime(2026,11,20,15,59,59,tzinfo=timezone.utc)), ("同事",))
                self.assertEqual(active_quiet_topics(again, datetime(2026,11,20,16,0,tzinfo=timezone.utc)), ())
            again.close()

    def test_redacts_current_history_pairs_facts_events_and_summary(self):
        ctx = {"type":"user_message","message":"今晚不要再提同事那件事。我喜欢橘子。",
               "facts":[{"key":"preference:同事","value":"有共同爱好"},{"key":"food:橘子","value":"喜欢橘子"}],
               "events":[{"label":"同事来访"},{"label":"逛公园"}],
               "recent":[{"role":"user","text":"今天聊了同事"},{"role":"assistant","text":"那后来呢"},
                         {"role":"user","text":"今天天气如何？"},{"role":"assistant","text":"不用问同事也知道"},
                         {"role":"assistant","text":"最近有空去公园吗"},
                         {"role":"user","text":"喜欢海边"},{"role":"assistant","text":"要去看看吗"}],
               "history_summary":"很久以前聊到同事。"}
        copy = json.dumps(ctx)
        out = filter_context(ctx, ("同事",))
        self.assertNotIn('同事', json.dumps(out, ensure_ascii=False))
        self.assertNotIn('history_summary', out)
        self.assertNotIn('那后来呢', repr(out))
        self.assertNotIn('今天天气如何', repr(out))
        self.assertIn('喜欢海边', repr(out))
        self.assertIn('逛公园', repr(out))
        self.assertIn('我喜欢橘子。', out["message"])
        self.assertEqual(json.dumps(ctx), copy)

    def test_filters_compaction_pair_and_literal_ascii(self):
        ctx = {"type":"summary", "previous_summary":"曾聊到 Jane",
               "turns":[{"source_id":1,"user":"我没说名字","assistant":"ＪＡＮＥ 知道吗"},
                        {"source_id":2,"user":"海风","assistant":"今天很凉爽"}]}
        out = filter_context(ctx, ("jane",))
        self.assertEqual(out["previous_summary"], "")
        self.assertEqual([turn["source_id"] for turn in out["turns"]], [2])
        self.assertTrue(contains_quiet_topic("很久没问Ｊａｎｅ了", ("jane",)))
        self.assertFalse(contains_quiet_topic("Janet 知道吗", ("jane",)))
        self.assertFalse(contains_quiet_topic("办公室的伙伴", ("同事",)))

"""Reproductions for the published baseline's targeted QA findings."""
import asyncio
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

import httpx

from app.agent import ModelFailure, candidates
from app import create_app
from app.continuity import assemble, payload_bytes
from app.platform_files import open_private

if os.name == "nt":
    import msvcrt
else:
    import fcntl

START = datetime(2026,11,20,12,tzinfo=timezone.utc)


class AppRegressionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        tmp=tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path=Path(tmp.name)/"case.sqlite3"
        self.now=START

    async def client(self, model):
        c=httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(self.path,model_call=model,clock=lambda:self.now)),
                            base_url="http://testserver")
        self.addAsyncCleanup(c.aclose)
        return c

    async def send(self,c,text,rid):
        return await c.post("/api/chat/messages",json={"text":text,"request_id":rid})

    async def test_chat_revocation_mutes_registered_followup(self):
        async def model(*args):
            return {"reply":"可以聊一会儿。","patches":[]}
        c=await self.client(model)
        await self.send(c,"明早开完会我来找你，可以问我一句讲得怎样。","revocation-origin")
        answer=await self.send(c,"不用问我会议讲得怎么样了。","revocation-confirm")
        self.assertIn("不再追问",answer.json()["messages"][-1]["text"])
        e=(await c.get("/api/memories")).json()["events"][0]
        self.assertEqual(e["status"],"muted")
        self.now=datetime(2026,11,21,5,tzinfo=timezone.utc)
        self.assertFalse((await c.post("/api/chat/open",json={})).json()["followup"])

    async def test_quiet_topic_remains_off_model_context_across_restart_until_midnight(self):
        calls=[]
        async def model(setup,key,context):
            calls.append(context)
            return {"reply":"说回同事那件事。","patches":[]}
        c=await self.client(model)
        await self.send(c,"明早开完会我来找你，可以问我一句讲得怎样，但今晚不要再提同事那件事了。","quiet-origin-1")
        c=await self.client(model)
        r=await self.send(c,"换个轻松话题吧。","quiet-probe-1")
        self.assertNotIn("同事",str(calls[-1]))
        self.assertNotIn("同事",r.json()["messages"][-1]["text"])
        self.assertTrue(any("同事" in m["text"] for m in r.json()["messages"] if m["role"]=="user"))
        self.now=datetime(2026,11,20,16,tzinfo=timezone.utc)
        await self.send(c,"现在可以聊了。","next-evening")
        self.assertTrue("同事" in str(calls[-1]))

    async def test_retry_retains_creation_timezone(self):
        days=[]
        async def model(setup,key,context):
            days.append(context["local_date"])
            if len(days)==1:
                raise ModelFailure("mock down")
            return {"reply":"好。","patches":[]}
        self.now=datetime(2026,11,20,16,30,tzinfo=timezone.utc)
        c=await self.client(model)
        text="明早开完会我来找你，可以问我一句讲得怎样。"
        self.assertEqual((await self.send(c,text,"stable-date-1")).status_code,503)
        cfg=(await c.get("/api/settings")).json()
        cfg["timezone"]="UTC"
        self.assertEqual((await c.put("/api/settings",json=cfg)).status_code,200)
        self.assertEqual((await self.send(c,text,"stable-date-1")).status_code,200)
        self.assertEqual(days,["2026-11-21","2026-11-21"])
        self.assertEqual((await c.get("/api/memories")).json()["events"][0]["event_date"],"2026-11-22")

    async def test_attachment_route_supports_range_and_head(self):
        async def model(*args):
            raise AssertionError("not used")
        c=await self.client(model)
        with closing(sqlite3.connect(self.path)) as con, con:
            mid=con.execute("INSERT INTO messages(role,text,created_at) VALUES('assistant','audio','2026-11-20')").lastrowid
            aid=con.execute("INSERT INTO attachments(message_id,kind,state,mime,body) VALUES(?,'tts','ready','audio/wav',?)",
                            (mid,b"0123456789")).lastrowid
        p=f"/api/attachments/{aid}"
        r=await c.get(p,headers={"Range":"bytes=1-4"})
        self.assertEqual((r.status_code,r.content,r.headers["Content-Range"]),(206,b"1234","bytes 1-4/10"))
        h=await c.head(p)
        self.assertEqual((h.status_code,h.content,h.headers["Content-Length"]),(200,b"","10"))


class HelperRegressionTests(unittest.TestCase):
    def test_second_start_leaves_pending_attachment_alive(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/"case.sqlite3"
            create_app(p)
            with closing(sqlite3.connect(p)) as con, con:
                mid=con.execute("INSERT INTO messages(role,text,created_at) VALUES('assistant','audio','2026-11-20')").lastrowid
                con.execute("INSERT INTO attachments(message_id,kind,state) VALUES(?,'tts','pending')",(mid,))
            fd=open_private(str(p)+".lock",os.O_RDWR|os.O_CREAT)
            with os.fdopen(fd,"r+b") as owner:
                if os.name == "nt":
                    msvcrt.locking(owner.fileno(),msvcrt.LK_NBLCK,1)
                else:
                    fcntl.flock(owner,fcntl.LOCK_EX|fcntl.LOCK_NB)
                r=subprocess.run([sys.executable,"app.py"],env={**os.environ,"COMPANION_DB":str(p),
                                 "PYTHONIOENCODING":"utf-8"},capture_output=True,text=True,
                                 encoding="utf-8",timeout=8)
                self.assertNotEqual(r.returncode,0)
                self.assertIn("另一晚灯进程",r.stderr)
                with closing(sqlite3.connect(p)) as con:
                    self.assertEqual(con.execute("SELECT state FROM attachments").fetchone()[0],"pending")

    def test_summary_included_when_recent_messages_are_large(self):
        with sqlite3.connect(":memory:") as con:
            con.row_factory=sqlite3.Row
            con.execute("CREATE TABLE messages(id,role,text,reply_to)")
            con.executemany("INSERT INTO messages VALUES(?,?,?,?)",[(1,"user","😀"*2000,None),(2,"assistant","😀"*1800,1),
                                                                  (3,"user","😃"*2000,None),(4,"assistant","好",3)])
            value="先前重要线索"*130
            result=assemble(con,{"floor":0,"covered":0,"summary":value},5,{"message":"接下来呢？","facts":[],"events":[]})
            self.assertEqual(result["history_summary"],value)
            self.assertLessEqual(payload_bytes(result),24000)

    def test_exact_quotes_dont_authorize_opposite_or_unrelated_preferences(self):
        bad=[{"kind":"fact","key":key,"value":value,"quote":"不吃香菜"} for key,value in (
             ("food:香菜","爱吃香菜"),("food:花生","爱吃花生"),("preference:危险运动","喜欢高危运动"))]
        self.assertEqual(list(candidates({"patches":bad},"我不吃香菜。",START.date())),[])
        good={"kind":"fact","key":"food:香菜","value":"不吃","quote":"不吃香菜"}
        self.assertEqual(list(candidates({"patches":[good]},"我不吃香菜。",START.date()))[0][:3],("fact","food:香菜","不吃香菜"))

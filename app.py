import asyncio
import os
import sqlite3
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from typing import Literal

from agent import EVENT_NAMES, ModelFailure, candidates, recall, respond

DIR = Path(__file__).resolve().parent
SCHEMA = """
CREATE TABLE IF NOT EXISTS messages(id INTEGER PRIMARY KEY, role TEXT NOT NULL, text TEXT NOT NULL,
 created_at TEXT NOT NULL, request_id TEXT UNIQUE, reply_to INTEGER UNIQUE REFERENCES messages(id));
CREATE TABLE IF NOT EXISTS memories(key TEXT PRIMARY KEY, value TEXT NOT NULL,
 source_id INTEGER NOT NULL REFERENCES messages(id), quote TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS events(key TEXT PRIMARY KEY, label TEXT NOT NULL, event_date TEXT NOT NULL,
 status TEXT NOT NULL, source_id INTEGER NOT NULL REFERENCES messages(id), quote TEXT NOT NULL,
 updated_at TEXT NOT NULL, followup_id INTEGER REFERENCES messages(id));
CREATE TABLE IF NOT EXISTS settings(name TEXT PRIMARY KEY, value TEXT NOT NULL);
"""
DEFAULTS = {"mode":"mock", "base_url":"https://api.openai.com/v1", "model":"gpt-4o-mini", "timezone":"Asia/Shanghai"}

class Send(BaseModel):
    text: str = Field(min_length=1, max_length=2400)
    request_id: str = Field(min_length=8, max_length=80)

class Config(BaseModel):
    mode: Literal["mock","openai"]
    base_url: str = Field(max_length=300)
    model: str = Field(min_length=1, max_length=120)
    timezone: str = Field(min_length=1, max_length=80)
    api_key: str | None = Field(default=None, max_length=500)

class OpenChat(BaseModel):
    demo_date: date | None = None

def create_app(db_path=None, model_call=respond, clock=None):
    db_path = Path(db_path or os.getenv("COMPANION_DB", DIR / "companion.sqlite3"))
    app = FastAPI(title="晚灯 · 文字陪伴 Demo", docs_url=None, redoc_url=None)
    turn_lock = asyncio.Lock()
    runtime_key = os.getenv("OPENAI_API_KEY", "")
    clock = clock or (lambda: datetime.now(timezone.utc))

    @contextmanager
    def db():
        con = sqlite3.connect(db_path, timeout=5)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA foreign_keys=ON")
        try:
            with con:
                yield con
        finally:
            con.close()

    with db() as con:
        con.execute("PRAGMA journal_mode=WAL")
        con.executescript(SCHEMA)

    def cfg(con):
        return {**DEFAULTS, **{r["name"]:r["value"] for r in con.execute("SELECT * FROM settings")}}

    def records(con, sql, args=()):
        return [dict(x) for x in con.execute(sql, args)]

    def history(con, limit=100):
        return records(con, """SELECT id,role,text,created_at,request_id,reply_to
            FROM (SELECT * FROM messages ORDER BY id DESC LIMIT ?) ORDER BY id""", (limit,))

    @app.middleware("http")
    async def guard(request: Request, call_next):
        host = request.url.hostname
        if host not in {"127.0.0.1","localhost","::1","testserver","test"}:
            return JSONResponse({"detail":"此 Demo 仅供本机访问。"}, status_code=400)
        if request.method not in {"GET","HEAD","OPTIONS"}:
            origin = request.headers.get("origin")
            if origin and origin != f"{request.url.scheme}://{request.url.netloc}":
                return JSONResponse({"detail":"不接受跨站写入。"}, status_code=403)
            if request.headers.get("sec-fetch-site") == "cross-site":
                return JSONResponse({"detail":"不接受跨站请求。"}, status_code=403)
        return await call_next(request)

    @app.get("/")
    def index():
        return FileResponse(DIR / "static" / "index.html")

    @app.get("/api/chat")
    def get_chat():
        with db() as con:
            return {"messages":history(con)}

    @app.get("/api/memories")
    def get_memories():
        with db() as con:
            return {"facts":records(con,"SELECT * FROM memories ORDER BY updated_at DESC, key"),
                    "events":records(con,"SELECT * FROM events ORDER BY event_date DESC, key")}

    @app.get("/api/settings")
    def get_settings():
        with db() as con:
            return {**cfg(con), "key_set":bool(runtime_key)}

    @app.put("/api/settings")
    async def put_settings(body: Config):
        nonlocal runtime_key
        try:
            url = urlsplit(body.base_url.strip())
            if (url.scheme != "https" or url.hostname != "api.openai.com" or url.port not in (None,443)
                    or url.path.rstrip("/") != "/v1" or url.username or url.password or url.query or url.fragment):
                raise ValueError
            ZoneInfo(body.timezone)
        except (ValueError, ZoneInfoNotFoundError):
            raise HTTPException(400,"接口地址必须是 OpenAI 官方 HTTPS /v1，时区请填 IANA 名称，例如 Asia/Shanghai。")
        if not body.model.strip():
            raise HTTPException(400,"模型名不能为空。")
        async with turn_lock:
            with db() as con:
                data = body.model_dump(exclude={"api_key"})
                data.update(base_url=body.base_url.strip().rstrip("/"),model=body.model.strip())
                con.executemany("""INSERT INTO settings(name,value) VALUES (?,?)
                    ON CONFLICT(name) DO UPDATE SET value=excluded.value""", data.items())
            if body.api_key is not None:
                runtime_key = body.api_key.strip()
        return get_settings()

    @app.post("/api/chat/messages")
    async def send(body: Send):
        source = body.text.strip()
        if not source:
            raise HTTPException(400,"先写点什么吧。")
        async with turn_lock:
            with db() as con:
                setup = cfg(con)
                saved = con.execute("SELECT * FROM messages WHERE request_id=?", (body.request_id,)).fetchone()
                if saved and saved["text"] != source:
                    raise HTTPException(409,"消息编号已用于另一条消息。")
                if saved:
                    old = con.execute("SELECT id FROM messages WHERE reply_to=?", (saved["id"],)).fetchone()
                    if old:
                        return {"messages":history(con)}
                    user_id, sent = saved["id"], saved["created_at"]
                else:
                    sent = clock().isoformat()
                    user_id = con.execute("INSERT INTO messages(role,text,created_at,request_id) VALUES('user',?,?,?)",
                                          (source,sent,body.request_id)).lastrowid
                facts = records(con,"SELECT key,value FROM memories ORDER BY updated_at DESC")
                events = records(con,"SELECT key,label,event_date,status FROM events WHERE status IN ('pending','asked') ORDER BY event_date DESC LIMIT 12")
                recent = records(con,"""SELECT role,text FROM
                    (SELECT * FROM messages WHERE id < ? ORDER BY id DESC LIMIT 14) ORDER BY id""",(user_id,))
            day = datetime.fromisoformat(sent).astimezone(ZoneInfo(setup["timezone"])).date()
            try:
                result = await model_call(setup,runtime_key,{"type":"user_message","message":source,
                           "local_date":day.isoformat(),"facts":recall(source,facts),"events":events,"recent":recent})
            except ModelFailure as exc:
                raise HTTPException(503,str(exc)) from None
            now = clock().isoformat()
            with db() as con:
                for kind,key,value,quote in candidates(result,source,day,recent):
                    if kind == "fact":
                        con.execute("""INSERT INTO memories VALUES (?,?,?,?,?)
                            ON CONFLICT(key) DO UPDATE SET value=excluded.value,source_id=excluded.source_id,
                            quote=excluded.quote,updated_at=excluded.updated_at
                            WHERE excluded.source_id >= memories.source_id""",(key,value,user_id,quote,now))
                    elif kind == "event":
                        label = EVENT_NAMES.get(key,key)
                        con.execute("""INSERT INTO events VALUES (?,?,?,'pending',?,?,?,NULL)
                         ON CONFLICT(key) DO UPDATE SET event_date=excluded.event_date,
                         status=CASE WHEN events.event_date=excluded.event_date AND events.status='asked' THEN 'asked' ELSE 'pending' END,
                         followup_id=CASE WHEN events.event_date=excluded.event_date AND events.status='asked' THEN events.followup_id ELSE NULL END,
                         source_id=excluded.source_id, quote=excluded.quote, updated_at=excluded.updated_at
                         WHERE excluded.source_id >= events.source_id""",(key,label,value,user_id,quote,now))
                    else:
                        con.execute("""UPDATE events SET status='resolved',source_id=?,quote=?,updated_at=?
                                    WHERE key=? AND source_id<=?""",(user_id,quote,now,key,user_id))
                con.execute("INSERT INTO messages(role,text,created_at,reply_to) VALUES('assistant',?,?,?)",
                            (result["reply"],now,user_id))
                return {"messages":history(con)}

    @app.post("/api/chat/open")
    async def open_chat(body: OpenChat):
        async with turn_lock:
            with db() as con:
                setup = cfg(con)
                if body.demo_date and setup["mode"] != "mock":
                    raise HTTPException(400,"演示日期仅用于模拟模式。")
                day = body.demo_date or clock().astimezone(ZoneInfo(setup["timezone"])).date()
                event = con.execute("""SELECT * FROM events WHERE status='pending' AND event_date < ? AND event_date >= ?
                                      ORDER BY event_date DESC,key LIMIT 1""",(day.isoformat(),(day-timedelta(days=14)).isoformat())).fetchone()
                if not event:
                    return {"messages":history(con),"followup":False}
            try:
                result = await model_call(setup,runtime_key,{"type":"session_open","local_date":day.isoformat(),
                               "event":{"label":event["label"],"event_date":event["event_date"]}})
            except ModelFailure as exc:
                raise HTTPException(503,str(exc)) from None
            now = clock().isoformat()
            with db() as con:
                updated = con.execute("""UPDATE events SET status='asked',updated_at=?
                     WHERE key=? AND status='pending' AND source_id=?""",(now,event["key"],event["source_id"])).rowcount
                if updated:
                    message_id = con.execute("INSERT INTO messages(role,text,created_at) VALUES('followup',?,?)",
                                             (result["reply"],now)).lastrowid
                    con.execute("UPDATE events SET followup_id=? WHERE key=?",(message_id,event["key"]))
                return {"messages":history(con),"followup":bool(updated)}

    app.mount("/static", StaticFiles(directory=DIR / "static"), name="static")
    return app

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(create_app(), host="127.0.0.1", port=8765)

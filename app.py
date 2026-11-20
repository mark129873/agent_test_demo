import asyncio
import os
import sqlite3
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from typing import Literal

from agent import EVENT_NAMES, ModelFailure, candidates, recall, respond
from media import (AUDIO_TYPES, MAX_ASSET, MAX_INPUT_AUDIO, OPENAI_URL, SCENES, SERVICES,
                   MediaFailure, intent, openai_media, photo_detail, scene_hint, tone_for)

DIR = Path(__file__).resolve().parent
SCHEMA = """
CREATE TABLE IF NOT EXISTS messages(id INTEGER PRIMARY KEY, role TEXT NOT NULL, text TEXT NOT NULL,
 created_at TEXT NOT NULL, request_id TEXT UNIQUE, reply_to INTEGER UNIQUE REFERENCES messages(id));
CREATE TABLE IF NOT EXISTS memories(key TEXT PRIMARY KEY, value TEXT NOT NULL,
 source_id INTEGER NOT NULL REFERENCES messages(id), quote TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS events(key TEXT PRIMARY KEY, label TEXT NOT NULL, event_date TEXT NOT NULL,
 status TEXT NOT NULL, source_id INTEGER NOT NULL REFERENCES messages(id), quote TEXT NOT NULL,
 updated_at TEXT NOT NULL, followup_id INTEGER REFERENCES messages(id));
CREATE TABLE IF NOT EXISTS attachments(
 id INTEGER PRIMARY KEY, message_id INTEGER NOT NULL REFERENCES messages(id),
 kind TEXT NOT NULL, state TEXT NOT NULL, mime TEXT, body BLOB, error TEXT,
 presentation TEXT NOT NULL DEFAULT 'both', UNIQUE(message_id,kind)
);
CREATE TABLE IF NOT EXISTS settings(name TEXT PRIMARY KEY, value TEXT NOT NULL);
"""
DEFAULTS = {"mode":"mock", "base_url":"https://api.openai.com/v1", "model":"gpt-6-luna", "timezone":"Asia/Shanghai"}

class Send(BaseModel):
    text: str = Field(min_length=1, max_length=2400)
    request_id: str = Field(min_length=8, max_length=80)

class Config(BaseModel):
    mode: Literal["mock","openai"]
    base_url: str = Field(max_length=300)
    model: str = Field(min_length=1, max_length=120)
    timezone: str = Field(min_length=1, max_length=80)
    api_key: str | None = Field(default=None, max_length=500)

class ServiceConfig(BaseModel):
    enabled: bool = False
    base_url: str = Field(max_length=300)
    model: str = Field(default="",max_length=120)
    api_key: str | None = Field(default=None,max_length=500)
    inherit: bool = True
    voice: str = Field(default="marin",max_length=40)

class MediaConfig(BaseModel):
    reply_mode: Literal["text","voice","both"] = "text"
    proactive_images: bool = False
    services: dict[str, ServiceConfig]

class OpenChat(BaseModel):
    demo_date: date | None = None

def create_app(db_path=None, model_call=respond, clock=None, media_call=openai_media):
    db_path = Path(db_path or os.getenv("COMPANION_DB", DIR / "companion.sqlite3"))
    app = FastAPI(title="晚灯 · 文字陪伴 Demo", docs_url=None, redoc_url=None)
    turn_lock = asyncio.Lock()
    runtime_key = os.getenv("OPENAI_API_KEY", "")
    service_keys = {}
    tasks = set()
    audio_lock = asyncio.Lock()
    media_limit = asyncio.Semaphore(2)
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
        con.execute("UPDATE attachments SET state='failed',error='上次运行中断，媒体未生成。' WHERE state='pending'")

    def cfg(con):
        return {**DEFAULTS, **{r["name"]:r["value"] for r in con.execute("SELECT * FROM settings")}}

    def records(con, sql, args=()):
        return [dict(x) for x in con.execute(sql, args)]

    def history(con, limit=100):
        messages = records(con, """SELECT id,role,text,created_at,request_id,reply_to
            FROM (SELECT * FROM messages ORDER BY id DESC LIMIT ?) ORDER BY id""", (limit,))
        by_id = {m["id"]:m for m in messages}
        for m in messages:
            m["attachments"] = []
        if by_id:
            for asset in records(con,"SELECT id,message_id,kind,state,mime,error,presentation FROM attachments WHERE message_id BETWEEN ? AND ?",
                                 (min(by_id),max(by_id))):
                if asset["message_id"] in by_id:
                    by_id[asset["message_id"]]["attachments"].append(asset)
        return messages

    def media_settings(con):
        values = cfg(con)
        output = {"reply_mode":values.get("media.reply_mode","text"),
                  "proactive_images":values.get("media.proactive_images")=="1","services":{}}
        for service,(_,model) in SERVICES.items():
            pre = "media."+service+"."
            settings = {"model":values.get(pre+"model",model),"base_url":values.get(pre+"base_url",OPENAI_URL if service!="video" else ""),
                        "voice":values.get(pre+"voice","marin"),"enabled":values.get(pre+"enabled")=="1",
                        "inherit":values.get(pre+"inherit","1" if service!="video" else "0")=="1",
                        "override_key_set":bool(service_keys.get(service))}
            settings["key_set"] = bool(service_key(service,settings))
            output["services"][service] = settings
        return output

    def service_key(service, setup):
        return service_keys.get(service) or (runtime_key if setup["inherit"] and service!="video" and setup["base_url"]==OPENAI_URL else "")

    def can_call(service, options, config):
        setting = options["services"][service]
        return config["mode"]=="openai" and setting["enabled"] and setting["key_set"] and setting["model"]

    def unavailable(service, config):
        name=SERVICES[service][0]
        if service=="video":
            return "视频生成未接入服务；可以继续文字、语音或照片聊天。"
        if config["mode"]=="mock":
            return f"离线模式不调用{name}，可在设置中切到 OpenAI。"
        return f"{name}未启用或缺少 Key／模型，请检查设置。"

    def attachment(con, mid, kind, state, error=None, presentation="both"):
        return con.execute("INSERT INTO attachments(message_id,kind,state,error,presentation) VALUES(?,?,?,?,?)",
                           (mid,kind,state,error,presentation)).lastrowid

    def plan_media(con, mid, source, result, config, followup=False):
        options = media_settings(con)
        jobs = []
        picture = intent(source)
        branches = []
        if options["reply_mode"] in ("voice","both"):
            branches.append(("tts",{"text":result["reply"],"tone":tone_for(source)},options["reply_mode"]))
        if picture["video"] and not followup:
            branches.append(("video",{},"both"))
        elif not followup and not picture["negated"]:
            scene = scene_hint(source)
            last = con.execute("SELECT max(message_id) FROM attachments WHERE kind='image'").fetchone()[0]
            lately = last is not None and mid-last < 12
            images_ready = can_call("image",options,config)
            # Bounded proactive sends: an applicable scene and model approval, never every turn.
            if picture["photo"] or (images_ready and options["proactive_images"] and not lately
                and scene in ("meal","walk") and result.get("photo_scene")==scene):
                branches.append(("image",{"scene":scene,"detail":photo_detail(scene,source,result["reply"])},"both"))
        for service,payload,presentation in branches:
            enabled=service!="video" and bool(can_call(service,options,config))
            mid_asset=attachment(con,mid,service,"pending" if enabled else "failed",
                                 None if enabled else unavailable(service,config),presentation)
            if enabled:
                setup=options["services"][service]
                jobs.append((mid_asset,service,setup,service_key(service,setup),payload))
        return jobs

    def launch(jobs):
        async def run(mid_asset, service, setup, key, payload):
            try:
                async with asyncio.timeout(100):
                    async with media_limit:
                        mime, content = await media_call(service,setup,key,payload)
                allowed = {"tts":{"audio/wav","audio/mpeg"},"image":{"image/jpeg","image/png","image/webp"}}
                if mime not in allowed[service] or not isinstance(content,bytes) or not 0<len(content)<=MAX_ASSET:
                    raise MediaFailure("媒体格式无效或过大。")
                with db() as con:
                    con.execute("UPDATE attachments SET state='ready',mime=?,body=? WHERE id=?",(mime,content,mid_asset))
            except Exception as exc:
                message = str(exc) if isinstance(exc,MediaFailure) else "媒体服务暂时不可用，文字回复已保留。"
                with db() as con:
                    con.execute("UPDATE attachments SET state='failed',error=? WHERE id=?",(message,mid_asset))
        for job in jobs:
            task = asyncio.create_task(run(*job))
            tasks.add(task)
            task.add_done_callback(tasks.discard)

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

    @app.get("/api/settings/media")
    def get_media_settings():
        with db() as con:
            return media_settings(con)

    @app.put("/api/settings/media")
    async def put_media_settings(body: MediaConfig):
        if set(body.services)!=set(SERVICES):
            raise HTTPException(400,"请提交四项服务设置。")
        data={"media.reply_mode":body.reply_mode,"media.proactive_images":str(int(body.proactive_images))}
        for service,setting in body.services.items():
            name=SERVICES[service][0]
            url=setting.base_url.strip().rstrip("/")
            if service!="video" and url != OPENAI_URL and not (not url and not setting.enabled):
                raise HTTPException(400,f"{name}目前仅支持 OpenAI 官方接口。")
            if service=="video":
                try:
                    parts=urlsplit(url)
                    if url and (parts.scheme!="https" or not parts.hostname or parts.username or parts.password or parts.query or parts.fragment):
                        raise ValueError
                except ValueError:
                    raise HTTPException(400,"视频地址需要有效的 HTTPS URL。") from None
                if setting.enabled:
                    raise HTTPException(400,"视频还未接入供应商，不能启用。")
            if setting.enabled and not setting.model.strip():
                raise HTTPException(400,f"{name}模型名不能为空。")
            pre="media."+service+"."
            data.update({pre+"model":setting.model.strip(),pre+"base_url":url,pre+"enabled":str(int(setting.enabled)),
                         pre+"inherit":str(int(setting.inherit and service!="video")),pre+"voice":setting.voice.strip() or "marin"})
        async with turn_lock:
            with db() as con:
                con.executemany("""INSERT INTO settings(name,value) VALUES (?,?)
                    ON CONFLICT(name) DO UPDATE SET value=excluded.value""",data.items())
            for service,setting in body.services.items():
                if setting.api_key is not None:
                    service_keys[service]=setting.api_key.strip()
        return get_media_settings()

    @app.get("/api/attachments/{attachment_id}")
    def fetch_attachment(attachment_id: int):
        with db() as con:
            asset=con.execute("SELECT mime,body FROM attachments WHERE id=? AND state='ready'",(attachment_id,)).fetchone()
            if not asset:
                raise HTTPException(404,"该附件还不可用。")
            return Response(content=asset["body"],media_type=asset["mime"],headers={"X-Content-Type-Options":"nosniff","Cache-Control":"no-store"})

    @app.post("/api/chat/audio")
    async def send_audio(request: Request):
        mime=request.headers.get("content-type","").split(";")[0]
        rid=request.headers.get("x-request-id","")
        if mime not in AUDIO_TYPES or not 8<=len(rid)<=80:
            raise HTTPException(400,"这段录音格式或编号不受支持。")
        async with audio_lock:
            with db() as con:
                old=con.execute("SELECT text FROM messages WHERE request_id=?",(rid,)).fetchone()
                config=cfg(con)
                options=media_settings(con)
            if old:
                return await run_turn(Send(text=old["text"],request_id=rid))
            if not can_call("stt",options,config):
                raise HTTPException(503,unavailable("stt",config))
            raw=bytearray()
            async for chunk in request.stream():
                if len(raw)+len(chunk)>MAX_INPUT_AUDIO:
                    raise HTTPException(413,"录音过大，请重录一段更短的。")
                raw.extend(chunk)
            if len(raw)<16:
                raise HTTPException(400,"录音为空，请重试。")
            setup=options["services"]["stt"]
            try:
                async with asyncio.timeout(45):
                    transcript=await media_call("stt",setup,service_key("stt",setup),{"mime":mime,"data":bytes(raw)})
                body=Send(text=transcript,request_id=rid)
            except (MediaFailure,ValueError,TimeoutError) as exc:
                raise HTTPException(503,str(exc) if isinstance(exc,MediaFailure) else "未收到有效转写。") from None
            return await run_turn(body,(mime,bytes(raw)))

    @app.post("/api/chat/messages")
    async def send(body: Send):
        return await run_turn(body)


    async def run_turn(body: Send, original_audio=None):
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
                if original_audio and not saved:
                    asset=attachment(con,user_id,"recording","ready")
                    con.execute("UPDATE attachments SET mime=?,body=? WHERE id=?",(*original_audio,asset))
                media_context = intent(source)
                media_context["proactive"] = media_settings(con)["proactive_images"]
                facts = records(con,"SELECT key,value FROM memories ORDER BY updated_at DESC")
                events = records(con,"SELECT key,label,event_date,status FROM events WHERE status IN ('pending','asked') ORDER BY event_date DESC LIMIT 12")
                recent = records(con,"""SELECT role,text FROM
                    (SELECT * FROM messages WHERE id < ? ORDER BY id DESC LIMIT 14) ORDER BY id""",(user_id,))
            day = datetime.fromisoformat(sent).astimezone(ZoneInfo(setup["timezone"])).date()
            try:
                result = await model_call(setup,runtime_key,{"type":"user_message","message":source,
                           "local_date":day.isoformat(),"facts":recall(source,facts),"events":events,"recent":recent,"media":media_context})
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
                mid = con.execute("INSERT INTO messages(role,text,created_at,reply_to) VALUES('assistant',?,?,?)",
                            (result["reply"],now,user_id)).lastrowid
                jobs = plan_media(con,mid,source,result,setup)
                response = {"messages":history(con)}
            launch(jobs)
            return response

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
                jobs=[]
                if updated:
                    message_id = con.execute("INSERT INTO messages(role,text,created_at) VALUES('followup',?,?)",
                                             (result["reply"],now)).lastrowid
                    con.execute("UPDATE events SET followup_id=? WHERE key=?",(message_id,event["key"]))
                    jobs=plan_media(con,message_id,"",result,setup,followup=True)
                response={"messages":history(con),"followup":bool(updated)}
            launch(jobs)
            return response

    app.mount("/static", StaticFiles(directory=DIR / "static"), name="static")
    return app

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(create_app(), host="127.0.0.1", port=8765)

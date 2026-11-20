import asyncio
import os
import sqlite3
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from typing import Literal

from agent import ModelFailure, candidates, recall, respond, simulate
from continuity import (SCHEMA as CONTINUITY_SCHEMA, assemble, commit_summary,
                        compaction_input, reset_summary, retire_context, state)
from credentials import Credentials, CredentialStoreError
from events import migrate_events, apply_event
from media import (AUDIO_TYPES, MAX_ASSET, MAX_INPUT_AUDIO, OPENAI_URL, SCENES, SERVICES,
                   MediaFailure, intent, openai_media, photo_detail, scene_hint, timeout_message, tone_for)

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
CREATE TABLE IF NOT EXISTS credential_values(name TEXT PRIMARY KEY, ciphertext BLOB NOT NULL);
CREATE TABLE IF NOT EXISTS settings(name TEXT PRIMARY KEY, value TEXT NOT NULL);
"""
DEFAULTS = {"mode":"mock", "base_url":"https://api.openai.com/v1", "model":"gpt-6-luna", "timezone":"Asia/Shanghai", "online_summary":False}

class Send(BaseModel):
    text: str = Field(min_length=1, max_length=2400)
    request_id: str = Field(min_length=8, max_length=80)

class Config(BaseModel):
    mode: Literal["mock","openai"]
    base_url: str = Field(max_length=300)
    model: str = Field(min_length=1, max_length=120)
    timezone: str = Field(min_length=1, max_length=80)
    api_key: str | None = Field(default=None, max_length=500)
    online_summary: bool = False

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

class MemoryEdit(BaseModel):
    value: str = Field(min_length=1,max_length=100)

class Abandon(BaseModel):
    request_id: str = Field(min_length=8,max_length=80)

def create_app(db_path=None, model_call=respond, clock=None, media_call=openai_media):
    db_path = Path(db_path or os.getenv("COMPANION_DB", DIR / "companion.sqlite3"))
    app = FastAPI(title="晚灯", docs_url=None, redoc_url=None)
    turn_lock = asyncio.Lock()
    credential_store = Credentials(db_path)
    runtime_key = os.getenv("OPENAI_API_KEY", "")
    service_keys = {}
    credential_error = None
    tasks = set()
    summary_task = None
    summary_retry_at = 0.0
    opening = set()
    activity = 0
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
        con.executescript(SCHEMA + CONTINUITY_SCHEMA)
        migrate_events(con)
        try:
            restored=credential_store.load(con)
            runtime_key=restored.pop("chat",runtime_key)
            service_keys.update(restored)
        except CredentialStoreError as exc:
            credential_error=str(exc)
        con.execute("UPDATE attachments SET state='failed',error='上次运行中断，媒体未生成。' WHERE state='pending'")

    def cfg(con):
        values = {**DEFAULTS, **{r["name"]:r["value"] for r in con.execute("SELECT * FROM settings")}}
        values["online_summary"] = values["online_summary"]=="1"
        return values

    def records(con, sql, args=()):
        return [dict(x) for x in con.execute(sql, args)]

    def chat_page(con, before=None, limit=100):
        messages = records(con, """SELECT m.id,m.role,m.text,m.created_at,m.request_id,m.reply_to,
            CASE WHEN m.role='user' THEN CASE WHEN EXISTS(
                SELECT 1 FROM messages r WHERE r.reply_to=m.id)
                THEN 'answered' WHEN EXISTS(SELECT 1 FROM abandoned_turns d WHERE d.message_id=m.id)
                THEN 'archived' ELSE 'pending' END END AS delivery_state
            FROM (SELECT * FROM messages WHERE id<? ORDER BY id DESC LIMIT ?) m ORDER BY m.id""",
            (before or 2**63-1,limit+1))
        cursor = messages[1]["id"] if len(messages)>limit else None
        if cursor is not None:
            messages = messages[1:]
        floor = state(con)["floor"]
        by_id = {m["id"]:m for m in messages}
        for m in messages:
            m["attachments"] = []
            if m["delivery_state"]=="pending" and m["id"]<=floor:
                m["delivery_state"]="archived"
        if by_id:
            for asset in records(con,"SELECT id,message_id,kind,state,mime,error,presentation FROM attachments WHERE message_id BETWEEN ? AND ?",
                                 (min(by_id),max(by_id))):
                if asset["message_id"] in by_id:
                    by_id[asset["message_id"]]["attachments"].append(asset)
        return {"messages":messages,"next_cursor":cursor}

    def history(con):
        return chat_page(con)["messages"]

    def maybe_summarize(setup, key):
        nonlocal summary_task
        if setup["mode"]!="mock" and not setup["online_summary"]:
            return
        if setup["mode"]!="mock" and asyncio.get_running_loop().time()<summary_retry_at:
            return
        if summary_task is not None and not summary_task.done():
            return
        with db() as con:
            item = compaction_input(con)
        if item is None:
            return
        snapshot, end, context = item

        async def compact():
            nonlocal summary_retry_at
            try:
                async with asyncio.timeout(15):
                    result = await model_call(setup,key,context)
            except (ModelFailure,TimeoutError):
                if setup["mode"]!="mock":
                    summary_retry_at=asyncio.get_running_loop().time()+120
                return
            with db() as con:
                current=cfg(con)
                if current["mode"]==setup["mode"] and (
                    setup["mode"]=="mock" or current["online_summary"]):
                    committed=commit_summary(con,snapshot,end,result)
                    if not committed and setup["mode"]!="mock":
                        summary_retry_at=asyncio.get_running_loop().time()+120

        summary_task = asyncio.create_task(compact())

    def invalidate_context(con):
        retire_context(con)
        if summary_task is not None and not summary_task.done():
            summary_task.cancel()

    def mute_linked_events(con, key, old_value, now):
        # Event labels and keys are another route into both chat and the
        # opening template. Cover existing literal references across sources.
        fragments = {key.partition(":")[2].casefold()} if key.startswith("food:") else {old_value.strip().casefold()}
        fragments.discard("")
        for item in con.execute("""SELECT event_id,key,label,quote FROM events
                                    WHERE status IN ('pending','asked')""").fetchall():
            event_text = "\n".join(item[k] for k in ("key","label","quote")).casefold()
            if any(f in event_text for f in fragments):
                con.execute("""UPDATE events SET status='muted',version=version+1,updated_at=?
                                WHERE event_id=?""",(now,item["event_id"]))

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
            settings["saved_key"]=bool(con.execute("SELECT 1 FROM credential_values WHERE name=?",(service,)).fetchone())
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
                async with asyncio.timeout(165 if service=="image" else 65):
                    async with media_limit:
                        mime, content = await media_call(service,setup,key,payload)
                allowed = {"tts":{"audio/wav","audio/mpeg"},"image":{"image/jpeg","image/png","image/webp"}}
                if mime not in allowed[service] or not isinstance(content,bytes) or not 0<len(content)<=MAX_ASSET:
                    raise MediaFailure("媒体格式无效或过大。")
                with db() as con:
                    con.execute("UPDATE attachments SET state='ready',mime=?,body=? WHERE id=?",(mime,content,mid_asset))
            except Exception as exc:
                message = (timeout_message(service) if isinstance(exc,TimeoutError)
                           else str(exc) if isinstance(exc,MediaFailure) else "媒体服务暂时不可用，文字回复已保留。")
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
    def get_chat(before: int | None = Query(default=None,gt=0,lt=2**63), limit: int = Query(default=100,ge=1,le=100)):
        with db() as con:
            return chat_page(con,before,limit)

    @app.get("/api/memories")
    def get_memories():
        with db() as con:
            return {"facts":records(con,"SELECT * FROM memories ORDER BY updated_at DESC, key"),
                    "events":records(con,"SELECT * FROM events ORDER BY event_date DESC, event_id")}

    @app.put("/api/memories/{key}")
    async def correct_memory(key: str, body: MemoryEdit):
        if not body.value.strip():
            raise HTTPException(400,"请填写新的记忆内容。")
        with db() as con:
            existing=con.execute("SELECT value FROM memories WHERE key=?",(key,)).fetchone()
            if not existing:
                raise HTTPException(404,"这条记忆已经不在了。")
            if existing["value"]!=body.value.strip():
                now=clock().isoformat()
                con.execute("""UPDATE memories SET value=?,quote=?,updated_at=? WHERE key=?""",
                            (body.value.strip(),"你在记忆页手动更正",now,key))
                mute_linked_events(con,key,existing["value"],now)
                invalidate_context(con)
        return get_memories()

    @app.delete("/api/memories/{key}")
    async def forget_memory(key: str):
        with db() as con:
            existing=con.execute("SELECT value FROM memories WHERE key=?",(key,)).fetchone()
            if existing:
                con.execute("DELETE FROM memories WHERE key=?",(key,))
                mute_linked_events(con,key,existing["value"],clock().isoformat())
                invalidate_context(con)
        return get_memories()

    @app.delete("/api/memories/events/{event_id}")
    async def mute_event(event_id: int):
        with db() as con:
            if con.execute("""UPDATE events SET status='muted',version=version+1,updated_at=?
                WHERE event_id=? AND status!='muted'""",(clock().isoformat(),event_id)).rowcount:
                invalidate_context(con)
        return get_memories()

    @app.get("/api/settings")
    def get_settings():
        with db() as con:
            return {**cfg(con), "key_set":bool(runtime_key),
                    "saved_key":bool(con.execute("SELECT 1 FROM credential_values WHERE name='chat'").fetchone()),
                    "key_store_error":credential_error}

    @app.put("/api/settings")
    async def put_settings(body: Config):
        nonlocal runtime_key, credential_error
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
                data.update(base_url=body.base_url.strip().rstrip("/"),model=body.model.strip(),
                            online_summary=str(int(body.online_summary)))
                previous = cfg(con)
                if (previous["mode"],previous["online_summary"])!=(body.mode,body.online_summary):
                    reset_summary(con)
                    if summary_task is not None and not summary_task.done():
                        summary_task.cancel()
                try:
                    credential_store.save(con,{"chat":body.api_key})
                except CredentialStoreError as exc:
                    raise HTTPException(503,str(exc)) from None
                con.executemany("""INSERT INTO settings(name,value) VALUES (?,?)
                    ON CONFLICT(name) DO UPDATE SET value=excluded.value""", data.items())
                if body.api_key is not None:
                    runtime_key = body.api_key.strip()
                    try:
                        credential_store.load(con)
                        credential_error=None
                    except CredentialStoreError as exc:
                        credential_error=str(exc)
        return get_settings()

    @app.get("/api/settings/media")
    def get_media_settings():
        with db() as con:
            return media_settings(con)

    @app.put("/api/settings/media")
    async def put_media_settings(body: MediaConfig):
        nonlocal credential_error
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
                try:
                    credential_store.save(con,{service:setting.api_key for service,setting in body.services.items()})
                except CredentialStoreError as exc:
                    raise HTTPException(503,str(exc)) from None
                con.executemany("""INSERT INTO settings(name,value) VALUES (?,?)
                    ON CONFLICT(name) DO UPDATE SET value=excluded.value""",data.items())
                try:
                    credential_store.load(con)
                    credential_error=None
                except CredentialStoreError as exc:
                    credential_error=str(exc)
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

    @app.post("/api/chat/abandon")
    async def abandon(body: Abandon):
        with db() as con:
            found=con.execute("SELECT id FROM messages WHERE request_id=?",(body.request_id,)).fetchone()
            if not found:
                raise HTTPException(404,"这条消息未找到。")
            con.execute("""INSERT OR IGNORE INTO abandoned_turns(message_id)
                SELECT ? WHERE NOT EXISTS(SELECT 1 FROM messages WHERE reply_to=?)""",(found["id"],found["id"]))
            archived=con.execute("SELECT 1 FROM abandoned_turns WHERE message_id=?",(found["id"],)).fetchone()
            return {**chat_page(con),"abandoned":bool(archived)}


    async def run_turn(body: Send, original_audio=None):
        nonlocal activity
        source = body.text.strip()
        if not source:
            raise HTTPException(400,"先写点什么吧。")
        activity += 1
        async with turn_lock:
            with db() as con:
                setup, chat_key, snap = cfg(con), runtime_key, state(con)
                saved = con.execute("SELECT * FROM messages WHERE request_id=?", (body.request_id,)).fetchone()
                if saved and saved["text"] != source:
                    raise HTTPException(409,"消息编号已用于另一条消息。")
                if saved:
                    old = con.execute("SELECT id FROM messages WHERE reply_to=?", (saved["id"],)).fetchone()
                    if old:
                        return chat_page(con)
                    if saved["id"] <= snap["floor"] or con.execute(
                        "SELECT 1 FROM abandoned_turns WHERE message_id=?",(saved["id"],)).fetchone():
                        raise HTTPException(409,"记忆范围已经更新，这条旧消息不会再发给模型。可以另发一条新消息。")
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
                events = records(con,"""SELECT event_id,key,label,event_date,status FROM events
                    WHERE status IN ('pending','asked') ORDER BY updated_at DESC,event_id DESC LIMIT 12""")
                day = datetime.fromisoformat(sent).astimezone(ZoneInfo(setup["timezone"])).date()
                context = assemble(con,snap,user_id,{"type":"user_message","message":source,
                           "local_date":day.isoformat(),"facts":recall(source,facts),"events":events,"media":media_context})
            try:
                async with asyncio.timeout(35):
                    result = await model_call(setup,chat_key,context)
            except (ModelFailure,TimeoutError) as exc:
                raise HTTPException(503,str(exc) or "这次回复超时，可以重试。") from None
            now = clock().isoformat()
            with db() as con:
                if state(con)["epoch"]!=snap["epoch"]:
                    raise HTTPException(409,"对话期间修改了记忆，旧回复已丢弃。你可以重新发一条消息。")
                if con.execute("SELECT 1 FROM abandoned_turns WHERE message_id=?",(user_id,)).fetchone():
                    raise HTTPException(409,"已停止等待这条回复。")
                reset = False
                for kind,key,value,quote in candidates(result,source,day,context["recent"]):
                    if kind == "fact":
                        previous=con.execute("SELECT value FROM memories WHERE key=?",(key,)).fetchone()
                        written=con.execute("""INSERT INTO memories VALUES (?,?,?,?,?)
                            ON CONFLICT(key) DO UPDATE SET value=excluded.value,source_id=excluded.source_id,
                            quote=excluded.quote,updated_at=excluded.updated_at
                            WHERE excluded.source_id >= memories.source_id""",(key,value,user_id,quote,now)).rowcount
                        if written and previous and previous["value"]!=value:
                            mute_linked_events(con,key,previous["value"],now)
                            reset = True
                    else:
                        clarification=apply_event(con,kind,key,value,quote,user_id,now)
                        if clarification:
                            result={**result,"reply":clarification}
                mid = con.execute("INSERT INTO messages(role,text,created_at,reply_to) VALUES('assistant',?,?,?)",
                            (result["reply"],now,user_id)).lastrowid
                if reset:
                    invalidate_context(con)
                jobs = plan_media(con,mid,source,result,setup)
                response = chat_page(con)
            launch(jobs)
            maybe_summarize(setup,chat_key)
            return response

    @app.post("/api/chat/open")
    async def open_chat(body: OpenChat):
        # The UI requests history independently. This path never holds the
        # send lock while preparing a follow-up.
        with db() as con:
            setup = cfg(con)
            if body.demo_date and setup["mode"] != "mock":
                raise HTTPException(400,"演示日期仅用于模拟模式。")
            day = body.demo_date or clock().astimezone(ZoneInfo(setup["timezone"])).date()
            event = con.execute("""SELECT * FROM events WHERE status='pending' AND event_date < ? AND event_date >= ?
                                  ORDER BY event_date DESC,event_id LIMIT 1""",(day.isoformat(),(day-timedelta(days=14)).isoformat())).fetchone()
            if not event or opening or turn_lock.locked():
                return {**chat_page(con),"followup":False}
            watch_activity, watch_epoch = activity, state(con)["epoch"]
        event_id, version = event["event_id"], event["version"]
        opening.add(event_id)
        try:
            payload={"type":"session_open","local_date":day.isoformat(),
                     "event":{"label":event["label"],"event_date":event["event_date"]}}
            # Paid-provider configuration uses a local template here. A mock
            # may deliberately block to exercise interleavings.
            try:
                if setup["mode"]=="mock":
                    async with asyncio.timeout(5):
                        result = await model_call(setup,"",payload)
                else:
                    await asyncio.sleep(0)
                    result = simulate(payload)
            except (ModelFailure,TimeoutError):
                with db() as con:
                    return {**chat_page(con),"followup":False}
            with db() as con:
                if activity!=watch_activity or state(con)["epoch"]!=watch_epoch:
                    return {**chat_page(con),"followup":False}
                now = clock().isoformat()
                updated = con.execute("""UPDATE events SET status='asked',updated_at=?,version=version+1
                    WHERE event_id=? AND version=? AND status='pending'""",(now,event_id,version)).rowcount
                jobs=[]
                if updated:
                    message_id = con.execute("INSERT INTO messages(role,text,created_at) VALUES('followup',?,?)",
                                             (result["reply"],now)).lastrowid
                    con.execute("UPDATE events SET followup_id=? WHERE event_id=?",(message_id,event_id))
                    jobs=plan_media(con,message_id,"",result,setup,followup=True)
                response={**chat_page(con),"followup":bool(updated)}
            launch(jobs)
            return response
        finally:
            opening.discard(event_id)

    app.mount("/static", StaticFiles(directory=DIR / "static"), name="static")
    return app

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(create_app(), host="127.0.0.1", port=8765)

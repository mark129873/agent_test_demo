import asyncio
import base64
import io
import json
import tempfile
import unittest
import wave
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import aiohttp
import httpx
from PIL import Image

from app import create_app
from app.agent import respond
from app.media import (MediaFailure, align_photo_reply, audio_reply, image_prompt,
                          intent, openai_media, pcm_wav, text_only_turn, tone_for)

SAMPLE_AUDIO=pcm_wav(b"\x00\x00"*1000)


def make_jpeg_bytes(size=(16,16)):
    buffer = io.BytesIO()
    with Image.new("RGB",size,color=(86,77,61)) as image:
        image.save(buffer,format="JPEG")
    return buffer.getvalue()


SAMPLE_JPEG=make_jpeg_bytes()


class PhotoReplyTests(unittest.TestCase):
    def test_normalizes_common_unverified_photo_statuses_but_keeps_conversation(self):
        for claim in ("我不能发照片。", "我发不出照片。", "我不能拍照给你。", "照片已发送。",
                      "已经发给你了。", "我保证这张照片一定能送到。"):
            with self.subTest(claim=claim):
                self.assertEqual(align_photo_reply(claim+"窗外雨声好轻。",True),"我试试看。窗外雨声好轻。")
        chat="你说的那扇窗好像我以前见过的。"
        self.assertEqual(align_photo_reply(chat,True),chat)


class ImageTransportTests(unittest.IsolatedAsyncioTestCase):
    async def render(self, jpeg):
        def response(request):
            self.assertEqual(request.url.path,"/v1/images/generations")
            return httpx.Response(200,json={"data":[{"b64_json":base64.b64encode(jpeg).decode()}]})

        transport = httpx.MockTransport(response)
        async_client = httpx.AsyncClient(transport=transport)
        with patch("app.media.httpx.AsyncClient",return_value=async_client):
            return await openai_media("image",{"base_url":"https://api.openai.com/v1","model":"synthetic-img"},
                                      "synthetic-key",{"scene":"home","detail":"雨窗饺子"})

    async def test_accepts_decodable_jpeg(self):
        self.assertEqual(await self.render(SAMPLE_JPEG),("image/jpeg",SAMPLE_JPEG))

    async def test_image_request_contains_the_requested_visual_details(self):
        def response(request):
            payload=json.loads(request.content)
            self.assertIn("蓝色鲸鱼",payload["prompt"])
            self.assertIn("紫色雪山",payload["prompt"])
            self.assertNotIn("预约人李小周",payload["prompt"])
            return httpx.Response(200,json={"data":[{"b64_json":base64.b64encode(SAMPLE_JPEG).decode()}]})
        transport=httpx.MockTransport(response)
        client=httpx.AsyncClient(transport=transport)
        with patch("app.media.httpx.AsyncClient",return_value=client):
            await openai_media("image",{"base_url":"https://api.openai.com/v1","model":"synthetic-img"},
                "synthetic-key",{"scene":"home","detail":"","visual_request":
                "发一张蓝色鲸鱼飞过紫色雪山的照片，不要提及预约人李小周"})

    async def test_rejects_header_only_truncated_or_oversized_jpeg(self):
        for jpeg in (b"\xff\xd8\xff\xdbTEST\xff\xd9", SAMPLE_JPEG[:-2],
                     SAMPLE_JPEG[:200]+b"\xff\xd9",make_jpeg_bytes((4097,1))):
            with self.subTest(length=len(jpeg)), self.assertRaisesRegex(MediaFailure,"没有返回有效结果"):
                await self.render(jpeg)


class ImageChatModelTests(unittest.IsolatedAsyncioTestCase):
    async def test_chat_model_description_is_preserved(self):
        def response(request):
            request_body=json.loads(request.content)
            self.assertIn("photo_description",request_body["messages"][0]["content"])
            content={"reply":"夜空真美。","patches":[],"photo_scene":"share",
                     "photo_description":"湖面上空的紫色极光。"}
            return httpx.Response(200,json={"choices":[{"message":{"content":json.dumps(content,ensure_ascii=False)}}]})
        client=httpx.AsyncClient(transport=httpx.MockTransport(response))
        with patch("app.agent.httpx.AsyncClient",return_value=client):
            result=await respond({"mode":"openai","base_url":"https://api.openai.com/v1","model":"test"},
                                 "synthetic-key",{"type":"user_message","message":"湖面上有极光"})
        self.assertEqual(result["photo_description"],"湖面上空的紫色极光。")


class AudioTransportTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.setup = {"base_url":"https://api.openai.com/v1","model":"gpt-realtime-2.1-mini","voice":"marin"}
        self.sent = []
        self.attempts = []
        self.sessions = []
        self.events = [
            {"type":"session.updated"},
            {"type":"response.output_audio.delta","delta":base64.b64encode(b"\x00\x00"*20).decode()},
            {"type":"response.done","response":{"status":"completed"}},
        ]
        self.handshake_error = None
        self.handshake_seconds = 0
        driver = self

        class Socket:
            async def __aenter__(self):
                if driver.handshake_error:
                    raise driver.handshake_error
                if driver.handshake_seconds>driver.sessions[-1]["timeout"].total:
                    raise TimeoutError
                return self

            async def __aexit__(self, *args):
                pass

            async def send_json(self, message):
                driver.sent.append(message)

            async def __aiter__(self):
                for event in driver.events:
                    yield SimpleNamespace(type=aiohttp.WSMsgType.TEXT,data=json.dumps(event))

        class Session:
            def __init__(self, **kwargs):
                driver.sessions.append(kwargs)

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                pass

            def ws_connect(self, url, **kwargs):
                driver.attempts.append((url,kwargs))
                return Socket()

        self.enterContext(patch("app.media.aiohttp.ClientSession", Session))
        self.proxies = self.enterContext(patch("app.media.getproxies", return_value={"https":"http://proxy.test:3128"}))
        self.bypass = self.enterContext(patch("app.media.proxy_bypass", return_value=False))

    async def speak(self):
        return await audio_reply(self.setup, "synthetic-token", "饺子今晚又赢了。", "bright")

    async def test_realtime_protocol_wav_and_https_proxy_fallback(self):
        mime, wav = await self.speak()
        self.assertEqual(mime, "audio/wav")
        with wave.open(io.BytesIO(wav)) as audio:
            self.assertEqual((audio.getframerate(),audio.getnframes()),(24000,20))
        self.assertEqual(len(self.attempts),1)
        url, kwargs = self.attempts[0]
        self.assertEqual(url,"wss://api.openai.com/v1/realtime?model=gpt-realtime-2.1-mini")
        self.assertEqual(kwargs["proxy"], "http://proxy.test:3128")
        self.assertEqual(kwargs["headers"]["Authorization"], "Bearer synthetic-token")
        self.assertEqual([event["type"] for event in self.sent],
                         ["session.update","conversation.item.create","response.create"])
        self.assertEqual(self.sent[0]["session"]["audio"]["output"]["voice"],"marin")
        self.assertEqual(self.sent[1]["item"]["content"][0]["text"],"饺子今晚又赢了。")
        with self.assertRaisesRegex(MediaFailure,"被重定向"):
            trace = self.sessions[0]["trace_configs"][0]
            await next(iter(trace.on_request_redirect))(None,None,SimpleNamespace(response=SimpleNamespace(close=lambda:None)))

    async def test_failing_proxy_does_not_send_or_retry(self):
        self.proxies.return_value["wss"] = "http://websocket-proxy.test:3128"
        self.handshake_error = aiohttp.ClientConnectionError("synthetic-token leaked from proxy")
        with self.assertRaisesRegex(MediaFailure,"无法建立连接") as failure:
            await self.speak()
        self.assertNotIn("synthetic-token", str(failure.exception))
        self.assertEqual(len(self.attempts),1)
        self.assertEqual(self.attempts[0][1]["proxy"],"http://websocket-proxy.test:3128")
        self.assertEqual(self.sent,[])

    async def test_slow_handshake_has_bounded_budget_without_retry(self):
        self.handshake_seconds=15
        mime,_=await self.speak()
        self.assertEqual(mime,"audio/wav")
        self.assertEqual(len(self.attempts),1)
        self.attempts.clear()
        self.sent.clear()
        self.handshake_seconds=21
        with self.assertRaisesRegex(MediaFailure,"连接超时"):
            await self.speak()
        self.assertEqual(len(self.attempts),1)
        self.assertEqual(self.sent,[])

    async def test_unsupported_proxy_fails_closed_instead_of_connecting(self):
        self.proxies.return_value["socks"] = "socks5://proxy.test:1080"
        with self.assertRaisesRegex(MediaFailure,"无法使用当前网络代理"):
            await self.speak()
        self.assertEqual(self.attempts,[])
        self.assertEqual(self.sent,[])

    async def test_server_failure_is_sanitized_and_not_retried(self):
        self.events = [{"type":"error","error":{"message":"synthetic-token leaked from provider"}}]
        with self.assertRaisesRegex(MediaFailure,"被服务拒绝") as failure:
            await self.speak()
        self.assertNotIn("synthetic-token", str(failure.exception))
        self.assertEqual(len(self.attempts),1)
        self.assertEqual(len(self.sent),3)

    async def test_invalid_audio_is_not_returned(self):
        self.events[1]["delta"] = "###"
        with self.assertRaisesRegex(MediaFailure,"没有返回有效结果"):
            await self.speak()
        self.assertEqual(len(self.attempts),1)


class ActualProxyTests(unittest.IsolatedAsyncioTestCase):
    async def test_local_connect_eof_once_without_auth_or_loop_errors(self):
        received = []
        async def close_on_connect(reader, writer):
            try:
                received.append(await reader.readuntil(b"\r\n\r\n"))
            finally:
                writer.close()
                await writer.wait_closed()

        try:
            proxy = await asyncio.start_server(close_on_connect,"127.0.0.1",0)
        except PermissionError:
            self.skipTest("loopback server is blocked in this environment")
        loop = asyncio.get_running_loop()
        failures = []
        self.addCleanup(loop.set_exception_handler, loop.get_exception_handler())
        loop.set_exception_handler(lambda _loop, context: failures.append(context))
        async with proxy:
            port = proxy.sockets[0].getsockname()[1]
            with patch("app.media.getproxies",return_value={"https":f"http://127.0.0.1:{port}"}), patch("app.media.proxy_bypass",return_value=False):
                with self.assertRaisesRegex(MediaFailure,"无法建立连接"):
                    await audio_reply({"base_url":"https://api.openai.com/v1","model":"gpt-realtime-2.1-mini"},
                                      "synthetic-token","synthetic text","warm")
        self.assertEqual(len(received),1)
        self.assertTrue(received[0].startswith(b"CONNECT api.openai.com:443 HTTP/1.1\r\n"))
        self.assertNotIn(b"synthetic-token",received[0])
        self.assertEqual(failures,[])


class MediaTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        temp=tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.db=Path(temp.name)/"media.sqlite3"
        self.calls=[]
        self.failed=set()
        self.timed_out=set()
        self.release=asyncio.Event()
        self.release.set()

        async def fake_media(service,setup,key,payload):
            self.calls.append((service,key,payload))
            if service!="stt":
                await self.release.wait()
            if service in self.timed_out:
                raise TimeoutError
            if service in self.failed:
                raise MediaFailure(f"{service} 模拟服务失败")
            if service=="stt":
                return "我今天有点难过。"
            if service=="tts":
                return "audio/wav",SAMPLE_AUDIO
            return "image/jpeg",SAMPLE_JPEG

        async def fake_chat(config,key,context):
            text=context.get("message","")
            self.chat_context=context
            return {"reply":getattr(self,"chat_reply","我陪你坐一会儿。"),"patches":[],
                    "photo_scene":getattr(self,"chat_photo_scene","meal" if "晚饭" in text else None),
                    "photo_description":getattr(self,"chat_photo_description","")}

        self.app=create_app(self.db,model_call=fake_chat,media_call=fake_media)
        self.client=httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app),base_url="http://test",trust_env=False)
        self.addAsyncCleanup(self.client.aclose)
        config=(await self.client.get("/api/settings")).json()
        config.update(mode="openai",api_key="synthetic-local-secret")
        self.assertEqual((await self.client.put("/api/settings",json=config)).status_code,200)

    async def configure(self, mode="text", proactive=False):
        cfg=(await self.client.get("/api/settings/media")).json()
        cfg.update(reply_mode=mode,proactive_images=proactive)
        for kind,service in cfg["services"].items():
            service["enabled"]=True
        r=await self.client.put("/api/settings/media",json=cfg)
        self.assertEqual(r.status_code,200,r.text)
        return r.json()

    async def message(self, text, rid):
        r=await self.client.post("/api/chat/messages",json={"text":text,"request_id":rid})
        self.assertEqual(r.status_code,200,r.text)
        return r.json()["messages"][-1]

    async def attached(self):
        for _ in range(40):
            rows=(await self.client.get("/api/chat")).json()["messages"]
            if not any(a["state"]=="pending" for m in rows for a in m["attachments"]):
                return rows
            await asyncio.sleep(.01)
        self.fail("media job did not settle")

    async def test_audio_recording_transcript_voice_and_retry(self):
        await self.configure("voice")
        headers={"Content-Type":"audio/wav","X-Request-Id":"media-test-001"}
        for _ in range(2):
            r=await self.client.post("/api/chat/audio",content=SAMPLE_AUDIO,headers=headers)
            self.assertEqual(r.status_code,200,r.text)
        user,assistant=await self.attached()
        self.assertEqual(user["text"],"我今天有点难过。")
        original=user["attachments"][0]
        voice=assistant["attachments"][0]
        self.assertEqual((original["kind"],voice["state"],voice["presentation"]),("recording","ready","voice"))
        self.assertEqual([c[0] for c in self.calls],["stt","tts"])
        self.assertEqual(self.calls[1][2]["tone"],"comfort")
        for a in (original,voice):
            content=await self.client.get(f"/api/attachments/{a['id']}")
            with wave.open(io.BytesIO(content.content)) as audio:
                self.assertGreater(audio.getnframes(),0)

    async def test_text_only_and_both(self):
        await self.configure("text")
        msg=await self.message("你在干嘛？","media-text-only")
        self.assertEqual(msg["attachments"],[])
        self.assertEqual(self.calls,[])
        await self.configure("both")
        await self.message("今天超开心，面试通过了！","media-both-mode")
        rows=await self.attached()
        self.assertEqual(rows[-1]["attachments"][0]["presentation"],"both")
        self.assertEqual(self.calls[-1][2]["tone"],"bright")

    async def test_images_explicit_proactive_cooldown_and_excluded_video(self):
        await self.configure(proactive=True)
        self.release.clear()
        msg=await self.message("我在吃晚饭，你呢？","media-proactive")
        self.assertEqual(msg["attachments"][0]["state"],"pending")
        self.assertEqual(msg["text"],"我陪你坐一会儿。")
        self.release.set()
        self.assertEqual((await self.attached())[-1]["attachments"][0]["state"],"ready")
        self.assertEqual(self.calls[-1][2]["scene"],"meal")
        self.assertEqual((await self.message("咱继续聊聊晚饭。","media-not-again"))["attachments"],[])
        await self.message("发张照片看看","media-explicit-1")
        rows=await self.attached()
        self.assertEqual(self.calls[-1][2]["scene"],"selfie")
        photo=rows[-1]["attachments"][0]
        r=await self.client.get(f"/api/attachments/{photo['id']}")
        self.assertEqual((r.headers["content-type"],r.content),("image/jpeg",SAMPLE_JPEG))
        self.assertEqual((await self.message("不用发照片。","media-negative"))["attachments"],[])
        result=await self.message("你在干嘛，拍个视频。","media-video-excluded")
        self.assertEqual(result["attachments"],[])
        self.assertTrue(result["text"])
        self.assertNotIn("video",(await self.client.get("/api/settings/media")).json()["services"])

    async def test_explicit_photo_reply_agrees_with_attachment_lifecycle(self):
        self.chat_reply="我不能发照片。窗外下着雨呢。"
        wanted="发张雨天的窗边照片给我看看。"
        missing=await self.message(wanted,"photo-contract-missing")
        self.assertEqual(missing["attachments"][0]["state"],"failed")
        self.assertNotIn("不能发照片",missing["text"])
        self.assertFalse(self.chat_context["media"]["photo_available"])
        self.assertIn("窗外下着雨呢",missing["text"])
        self.assertEqual(self.calls,[])

        await self.configure()
        self.release.clear()
        pending=await self.message(wanted,"photo-contract-pending")
        self.assertTrue(self.chat_context["media"]["photo_available"])
        self.assertEqual(pending["attachments"][0]["state"],"pending")
        self.assertNotIn("不能发照片",pending["text"])
        self.release.set()
        ready=(await self.attached())[-1]
        self.assertEqual((ready["text"],ready["attachments"][0]["state"]),(pending["text"],"ready"))
        asset=await self.client.get(f"/api/attachments/{ready['attachments'][0]['id']}")
        self.assertEqual(asset.content,SAMPLE_JPEG)

        self.chat_reply="照片已发送。"
        self.failed={"image"}
        scheduled=await self.message(wanted,"photo-contract-provider-failure")
        self.assertNotIn("已发送",scheduled["text"])
        failed=(await self.attached())[-1]
        self.assertEqual((failed["text"],failed["attachments"][0]["state"]),(scheduled["text"],"failed"))
        self.assertEqual([call[0] for call in self.calls],["image","image"])

    async def test_full_local_storage_preserves_attachments_and_text_chat(self):
        await self.configure()
        with patch("app.server.MAX_MEDIA_STORAGE",len(SAMPLE_JPEG)):
            await self.message("给我发一张照片。","storage-photo-1")
            first=(await self.attached())[-1]
            aid=first["attachments"][0]["id"]
            self.assertEqual(first["attachments"][0]["state"],"ready")
            blocked=await self.message("再发一张照片。","storage-photo-2")
            self.assertIn("媒体空间已满",blocked["attachments"][0]["error"])
            self.assertTrue(blocked["text"])
            self.assertEqual([c[0] for c in self.calls],["image"])
            path=f"/api/attachments/{aid}"
            self.assertEqual((await self.client.delete(path)).status_code,405)
            asset=await self.client.get(path)
            self.assertEqual((asset.status_code,asset.content),(200,SAMPLE_JPEG))
            chat=await self.message("我们接着聊吧。","storage-text-3")
            self.assertTrue(chat["text"])
            self.assertEqual(chat["attachments"],[])

    async def test_failure_isolation_and_no_key_leak(self):
        cfg=await self.configure("both")
        self.failed={"tts","image","stt"}
        result=await self.message("发张照片给我","media-failures")
        rows=await self.attached()
        self.assertEqual({a["state"] for a in rows[-1]["attachments"]},{"failed"})
        self.assertEqual(rows[-1]["text"],result["text"])
        r=await self.client.post("/api/chat/audio",content=SAMPLE_AUDIO,
                  headers={"Content-Type":"audio/wav","X-Request-Id":"stt-error-1"})
        self.assertEqual(r.status_code,503)
        self.assertEqual(len((await self.client.get("/api/chat")).json()["messages"]),2)
        self.assertNotIn(b"synthetic-local-secret",self.db.read_bytes())
        self.assertTrue(cfg["services"]["stt"]["key_set"])
        self.assertNotIn("api_key",cfg["services"]["stt"])
        cfg["services"]["image"]["base_url"]="https://example.com/v1"
        self.assertEqual((await self.client.put("/api/settings/media",json=cfg)).status_code,400)

    async def test_photo_timeout_keeps_text_and_does_not_retry_provider(self):
        await self.configure()
        self.timed_out={"image"}
        wanted="能发一张你做的番茄鸡蛋面的照片吗？"
        first=await self.message(wanted,"photo-timeout")
        rows=await self.attached()
        self.assertEqual(rows[-1]["text"],first["text"])
        self.assertEqual(rows[-1]["attachments"][0]["state"],"failed")
        self.assertIn("照片生成等待超时",rows[-1]["attachments"][0]["error"])
        await self.message(wanted,"photo-timeout")
        await self.message("那我们接着聊天吧。","after-photo-timeout")
        self.assertEqual([call[0] for call in self.calls],["image"])

    async def test_offline_and_unconfigured_never_call_provider(self):
        cfg=(await self.client.get("/api/settings")).json()
        cfg.update(mode="mock")
        await self.client.put("/api/settings",json=cfg)
        await self.configure("both")
        msg=await self.message("发张照片看看","media-mock-only")
        self.assertTrue(msg["text"])
        self.assertEqual({a["state"] for a in msg["attachments"]},{"failed"})
        self.assertEqual(self.calls,[])

    async def test_abandoned_media_is_not_reissued_after_restart(self):
        await self.configure()
        self.release.clear()
        await self.message("发张照片看看","media-restart")
        other=create_app(self.db)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=other),base_url="http://test",trust_env=False) as client:
            rows=(await client.get("/api/chat")).json()["messages"]
            self.assertEqual(rows[-1]["attachments"][0]["state"],"failed")
        self.release.set()
        await asyncio.sleep(.02)

    async def test_reports_and_first_person_uploads_do_not_bill_generation(self):
        await self.configure()
        for i,text in enumerate(("我昨天给妈妈发了一张照片。","我发张照片给你看看。","刚给朋友发了视频。","我不想你发照片。","昨天我和小周拍了一个视频。","假如有一天我说发张照片看看，你会回我吗？","你发完照片了吗？","你发照片了吗？")):
            self.assertEqual((await self.message(text,f"media-report-{i}"))["attachments"],[])
        self.assertEqual(self.calls,[])
        await self.message("不用发视频，能发一张你做的番茄牛肉饭的照片吗？","media-mixed-command")
        rows=await self.attached()
        self.assertEqual(rows[-1]["attachments"][0]["kind"],"image")
        payload=self.calls[-1][2]
        self.assertEqual((payload["scene"],payload["detail"]),("meal","番茄牛肉饭"))
        self.assertIn("番茄牛肉饭",image_prompt(payload["scene"],payload["detail"]))

    async def test_window_photo_uses_requested_details_without_private_info(self):
        await self.configure()
        wanted = "可以发张你想象中的窗边给我看看吗？就素净一点，雨窗和一小盘饺子，照片不要加字"
        cases = (
            (wanted + "，不要提及预约人李小周。", "雨窗饺子"),
            ("可以发张你想象中的窗边给我看看吗？", "窗边"),
            ("发张照片看看，雨窗和窗台上的一小盘饺子，照片不要加字，不要人像。", "雨窗饺子"),
        )
        for i, (request, detail) in enumerate(cases):
            for _ in range(2):
                await self.message(request, f"window-photo-{i}")
            last = (await self.attached())[-1]
            self.assertEqual([(a["kind"], a["state"]) for a in last["attachments"]], [("image", "ready")])
            self.assertEqual(len(self.calls), i + 1)
            payload = self.calls[-1][2]
            self.assertEqual((payload["scene"],payload["detail"]), ("home",detail))
            prompt = image_prompt(**payload)
            self.assertNotIn("李小周", prompt)
            self.assertIn("窗", prompt)
            self.assertIn("不写文字", prompt)
            self.assertIn("不出现人物或人像", prompt)
            if detail == "雨窗饺子":
                self.assertIn("雨滴", prompt)
                self.assertIn("一小盘饺子", prompt)
        self.assertNotIn("李小周", image_prompt("home", "把李小周画进去"))

    async def test_explicit_image_uses_arbitrary_current_scene(self):
        await self.configure()
        await self.message("发一张蓝色鲸鱼飞过紫色雪山的照片。", "photo-unusual-scene")
        await self.attached()
        payload=self.calls[-1][2]
        prompt=image_prompt(**payload)
        self.assertIn("蓝色鲸鱼",prompt)
        self.assertIn("紫色雪山",prompt)
        self.assertNotIn("温暖的小台灯",prompt)

    async def test_selfie_at_window_does_not_forbid_the_character(self):
        await self.configure()
        await self.message("发一张你在窗边看书的自拍。", "photo-window-selfie")
        await self.attached()
        prompt=image_prompt(**self.calls[-1][2])
        self.assertIn("窗边",prompt)
        self.assertIn("自拍",prompt)
        self.assertIn("虚构角色的外观",prompt)
        self.assertNotIn("不出现人物或人像",prompt)

    async def test_generic_image_request_uses_recent_filtered_conversation(self):
        await self.configure()
        await self.message("我刚看到湖面上有紫色极光。", "photo-recent-scene")
        await self.message("发张照片看看。", "photo-recent-request")
        await self.attached()
        prompt=image_prompt(**self.calls[-1][2])
        self.assertIn("紫色极光",prompt)
        self.assertIn("湖面",prompt)

    async def test_new_image_request_takes_priority_over_previous_scene(self):
        await self.configure()
        await self.message("我刚才在森林里散步。", "photo-old-scene")
        await self.message("发张月球上金色风筝的照片。", "photo-new-scene")
        await self.attached()
        prompt=image_prompt(**self.calls[-1][2])
        self.assertIn("月球",prompt)
        self.assertIn("金色风筝",prompt)
        self.assertNotIn("森林",prompt)

    async def test_muted_topic_is_not_used_in_image_context(self):
        await self.configure()
        await self.message("刚看到海边的蓝色浪花。", "photo-quiet-safe")
        await self.message("今晚不要再提同事那件事。", "photo-quiet-request")
        self.chat_photo_description="同事在办公室里。"
        self.chat_reply="我试试看。"
        await self.message("发张照片看看。", "photo-quiet-image")
        await self.attached()
        prompt=image_prompt(**self.calls[-1][2])
        self.assertNotIn("同事",prompt)
        self.assertIn("蓝色浪花",prompt)

    async def test_proactive_image_can_match_a_new_kind_of_scene(self):
        await self.configure(proactive=True)
        self.chat_photo_scene="share"
        self.chat_photo_description="湖面上方的紫色极光。"
        self.chat_reply="湖边的极光真美。"
        await self.message("我在湖边看紫色极光。", "photo-new-proactive")
        self.assertEqual((await self.attached())[-1]["attachments"][0]["state"],"ready")
        self.assertIn("紫色极光",image_prompt(**self.calls[-1][2]))

    async def test_proactive_image_requires_a_related_subject(self):
        await self.configure(proactive=True)
        self.chat_photo_scene="share"
        self.chat_photo_description="海边的橙色晚霞。"
        self.chat_reply="你好呀。"
        self.assertEqual((await self.message("你好。", "photo-unrelated-proactive"))["attachments"],[])
        self.assertEqual(self.calls,[])

    async def test_window_photo_nonrequests_do_not_bill_generation(self):
        await self.configure(proactive=True)
        for i, request in enumerate((
            "别发照片。", "我发张照片给你看看。", "先聊聊你想象中的窗边。",
            "我发张晚饭照片给你看看。", "不用再发图，聊聊晚饭。",
            "她对我说：“可以发张你想象中的窗边给我看看吗？”",
        )):
            self.assertEqual((await self.message(request, f"window-negative-{i}"))["attachments"], [])
        self.assertEqual(self.calls, [])

    async def test_text_only_override_never_calls_tts_and_preserves_mode(self):
        await self.configure("both", proactive=True)
        for i, text in enumerate((
            "不用再发图。这次就回一小句文字，不要语音：饺子今晚又赢了",
            "我在地铁上，给我发文字就好。",
            "这次请用文字回复我，我不方便听语音。",
            "车上太吵啦，我不方便听语音。",
        )):
            reply = await self.message(text, f"media-turn-text-override-{i}")
            self.assertTrue(reply["text"])
            self.assertEqual(reply["attachments"], [])
            self.assertEqual(self.calls, [])
        cfg = (await self.client.get("/api/settings/media")).json()
        self.assertEqual(cfg["reply_mode"], "both")
        await self.message("好啦，我们继续聊天。", "media-turn-after-override")
        self.assertEqual((await self.attached())[-1]["attachments"][0]["presentation"], "both")
        self.assertEqual([c[0] for c in self.calls], ["tts"])

    async def test_current_photo_opt_out_blocks_proactive_then_resumes(self):
        await self.configure("text", proactive=True)
        for i, request in enumerate((
            "晚饭吃了饺子，今天不想看照片。",
            "晚饭刚吃完，这次别给我发图。",
            "我在做晚饭，我今晚不想看照片。",
        )):
            reply = await self.message(request, f"media-turn-photo-override-{i}")
            self.assertTrue(reply["text"])
            self.assertEqual(reply["attachments"], [])
            self.assertEqual(self.calls, [])
        await self.message("晚饭吃了饺子，你呢？", "media-turn-after-photo-override")
        self.assertEqual((await self.attached())[-1]["attachments"][0]["kind"], "image")
        self.assertEqual([c[0] for c in self.calls], ["image"])

    def test_text_only_intents_distinguish_directives_from_mentions(self):
        for text in ("这次只用文字回复。", "不要语音。", "这次不用发语音", "就回一小句文字"):
            with self.subTest(text=text):
                self.assertTrue(text_only_turn(text))
        for text in ("文字", "我不常发语音。", "你可以发语音。", "不要语音识别出错。", "他不方便听语音。",
                     "例如，不要语音可以写成「禁用语音」。", "例如：不要语音。", "她说，不用语音。", "她说“不用语音”。",
                     "如果只用文字回复，会是什么感觉？"):
            with self.subTest(text=text):
                self.assertFalse(text_only_turn(text))

    def test_photo_intents_ignore_negations_in_quotes_hypotheticals_and_reports(self):
        for text in ("晚饭吃了饺子。她说：今天不想看照片。", "比如：今天不想看照片。",
                     "假如我在地铁上，今天不想看照片。", "她说“今天不想看照片”。",
                     "不要发照片失败。"):
            with self.subTest(text=text):
                self.assertFalse(intent(text)["negated"])
                self.assertFalse(intent(text)["photo"])
        for text in ("比如：发张照片看看。", "她昨天说，发张照片看看。"):
            with self.subTest(text=text):
                self.assertFalse(intent(text)["photo"])

    def test_tone_honors_negation_and_a_confirmed_outcome(self):
        self.assertEqual(tone_for("终于通过了，刚才还紧张得不行"), "bright")
        self.assertEqual(tone_for("刚才紧张得不行，不过终于通过了！"), "bright")
        for text in ("我一点也不开心", "我并没有开心", "我没通过了", "我没有成功通过了"):
            with self.subTest(text=text):
                self.assertNotEqual(tone_for(text), "bright")

    def test_photo_video_intents(self):
        self.assertTrue(intent("你在干嘛，拍个视频")["video"])
        self.assertTrue(intent("能发张照片看看吗？")["photo"])
        self.assertTrue(intent("照片发我看看。")["photo"])
        self.assertFalse(intent("你发完照片了吗？")["photo"])
        self.assertFalse(intent("你发照片了吗？")["photo"])
        self.assertFalse(intent("不要发照片了")["photo"])

if __name__=="__main__":
    unittest.main()

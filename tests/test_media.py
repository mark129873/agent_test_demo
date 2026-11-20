import asyncio
import io
import tempfile
import unittest
import wave
from pathlib import Path

import httpx

from app import create_app
from media import MediaFailure, image_prompt, intent, pcm_wav

SAMPLE_AUDIO=pcm_wav(b"\x00\x00"*1000)
SAMPLE_JPEG=b"\xff\xd8\xff\xdbTEST\xff\xd9"

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
            return {"reply":"我陪你坐一会儿。","patches":[],
                    "photo_scene":"meal" if "晚饭" in text else None}

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
            if kind!="video":service["enabled"]=True
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

    async def test_images_explicit_proactive_cooldown_and_video(self):
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
        result=await self.message("你在干嘛，拍个视频。","media-video-pending")
        self.assertIn("未接入",result["attachments"][0]["error"])
        self.assertTrue(result["text"])

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

    def test_photo_video_intents(self):
        self.assertTrue(intent("你在干嘛，拍个视频")["video"])
        self.assertTrue(intent("能发张照片看看吗？")["photo"])
        self.assertTrue(intent("照片发我看看。")["photo"])
        self.assertFalse(intent("你发完照片了吗？")["photo"])
        self.assertFalse(intent("你发照片了吗？")["photo"])
        self.assertFalse(intent("不要发照片了")["photo"])

if __name__=="__main__":
    unittest.main()

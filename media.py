"""Provider adapters and bounded decisions for chat attachments."""
import asyncio
import base64
import io
import json
import re
import wave
from urllib.parse import urlencode

import httpx
from websockets.asyncio.client import connect
from websockets.exceptions import WebSocketException

OPENAI_URL = "https://api.openai.com/v1"
SERVICES = {
    "stt": ("语音识别", "gpt-transcribe"),
    "tts": ("语音合成", "gpt-realtime-2.1-mini"),
    "image": ("照片生成", "gpt-image-2.6-flare"),
    "video": ("视频生成", ""),
}
VOICE_TONES = {
    "warm": "温柔自然，像跟熟人说话，不要主播腔。句间留一点停顿。",
    "comfort": "放慢一点，轻声、克制，不强行振作，用句间停顿表达关心。",
    "bright": "语气带笑，稍轻快，有自然起伏，不尖叫、不夸张。",
    "quiet": "像夜里轻声说话，语速慢一点，声音柔和，不用气声表演。",
}
SCENES = {
    "meal": ("晚饭", "一碗家里刚做好的热汤面，普通陶瓷碗，木桌有用过的餐具"),
    "walk": ("散步", "刚路过的安静街角，树影、暖色路灯，构图略偏"),
    "home": ("家里", "桌边沏好的茶与一本翻开的书，窗外柔和光线"),
    "selfie": ("自拍", "一位虚构成年东亚女性，深棕发与酒红色针织衫，松弛自然的居家随手自拍"),
}
AUDIO_TYPES = {
    "audio/webm": "webm", "audio/mp4": "m4a", "audio/mpeg": "mp3",
    "audio/ogg": "ogg", "audio/wav": "wav", "audio/x-wav": "wav",
}
MAX_INPUT_AUDIO = 6 * 1024 * 1024
MAX_ASSET = 8 * 1024 * 1024


class MediaFailure(Exception):
    pass


def timeout_message(service):
    label=SERVICES[service][0]
    return f"{label}等待超时。这次没有自动重试，聊天仍可继续。"


def intent(text):
    """Only direct requests may bypass the separate proactive-image opt-in."""
    photo=video=negated=False
    nouns=r"照片|自拍|张图|图片|视频|短片"
    for fragment in re.split(r"[，。！？；、,.!?;\n]+",text):
        clause=fragment.strip(" \t：:～~")
        if not clause or not re.search(nouns,clause) or re.search(r"假设|假如|如果|比如|引用|举例",clause):
            continue
        if re.search(r"(?:发完|发出|发好了|发了|拍完|拍好了|拍了|传完|传好了|传了|什么时候发|怎么还没发)|(?:了(?:吗|没|没有)|没(?:有)?发)[呀啊呢吗]*$",clause):
            negated=True
            continue
        if re.match(r"^(?:(?:你|请你)\s*)?(?:别|不要|不用|不想)(?:再)?\s*(?:给我\s*)?(?:发|拍|看)",clause):
            negated=True
            continue
        action=r"(?:发(?:给我)?|拍(?:给我)?|给我(?:发|拍|看)|分享|传)(?!了|过)"
        command=bool(re.match(r"(?:(?:阿晚|晚晚)\s*)?(?:(?:你|请你?|麻烦你?|能不能|可不可以|可以|能|帮我|给我|来|想请你|我想请你)\s*){0,5}"+action,clause))
        desire=bool(re.match(r"^(?:(?:我)?(?:想|要)(?:看看?|瞧瞧)|(?:让|给)我看看?|(?:可以|能不能)让我看)",clause))
        noun_first=bool(re.match(r"^(?:照片|自拍|图片|视频|短片)(?:请)?(?:发|传)给?我(?:看看?|瞧瞧|一下|吧|好吗)?$",clause))
        if not (command or desire or noun_first):
            continue
        if re.search(r"视频|短片",clause):
            video=True
        elif re.search(r"照片|自拍|张图|图片",clause):
            photo=True
    return {"video":video,"photo":photo and not video,"negated":negated and not (photo or video)}


def tone_for(text):
    if re.search(r"难受|累|难过|心烦|失眠|委屈|紧张|不想听建议", text):
        return "comfort"
    if re.search(r"开心|太好了|通过了|庆祝|恭喜|好消息", text):
        return "bright"
    if re.search(r"晚安|要睡|睡了|夜里", text):
        return "quiet"
    return "warm"


def scene_hint(text):
    if re.search(r"你长什么|自拍|你的照片|发张照片看看", text):
        return "selfie"
    if re.search(r"吃|晚饭|午饭|做饭|下厨|面条|牛肉饭", text) or any(word in text for word in MEAL_DETAILS):
        return "meal"
    if re.search(r"散步|外面|街上|路灯|夜景", text):
        return "walk"
    return "home"


MEAL_DETAILS=("番茄牛肉饭","番茄鸡蛋面","葱油拌面","鸡蛋面","三明治","煎饺","炒饭","牛肉面",
              "番茄炒蛋","麻婆豆腐","粥","寿司","披萨","沙拉","咖喱","饺子","米饭")
WALK_DETAILS=("雨后的路面","雨后","江边","河边","公园","街角","小巷","咖啡店","落叶","路灯")

def photo_detail(scene, source, reply):
    vocabulary = MEAL_DETAILS if scene=="meal" else WALK_DETAILS if scene=="walk" else ()
    return next((word for text in (source,reply) for word in vocabulary if word in text), "")


def image_prompt(scene, detail=""):
    if detail and detail in (MEAL_DETAILS if scene=="meal" else WALK_DETAILS if scene=="walk" else ()):
        scene_text = ("普通陶瓷盘碗里、刚做好的"+detail+"，旁边有家用餐具"
                      if scene=="meal" else "画面明显切合"+detail+"，随手拍到的街景")
    else:
        scene_text=SCENES[scene][1]
    return ("虚构角色阿晚的日常随手拍，非真实人照片。内容：" + scene_text +
            "。手机拍摄的自然光、略有颗粒、非对称构图，普通生活痕迹，避免广告大片和棚拍。"
            "像朋友私聊发来的一张照片。不写文字、品牌、水印，不描绘真实人物。")


def pcm_wav(data, rate=24000):
    out = io.BytesIO()
    with wave.open(out,"wb") as f:
        f.setnchannels(1)
        f.setsampwidth(2)
        f.setframerate(rate)
        f.writeframes(data)
    return out.getvalue()


async def audio_reply(setup, key, text, tone):
    url = setup["base_url"].replace("https://","wss://",1) + "/realtime?" + urlencode({"model":setup["model"]})
    instructions = ("用普通话念输入文本，不改写、不加开场词。语气像虚构角色阿晚。"
                    + VOICE_TONES.get(tone,VOICE_TONES["warm"]))
    async def send(ws, payload):
        await ws.send(json.dumps(payload,ensure_ascii=False))
    try:
        async with asyncio.timeout(45):
            async with connect(url,additional_headers={"Authorization":f"Bearer {key}"},
                               open_timeout=12,max_size=4*1024*1024) as ws:
                await send(ws,{"type":"session.update","session":{"type":"realtime",
                    "instructions":instructions,"output_modalities":["audio"],
                    "audio":{"output":{"voice":setup.get("voice","marin"),"format":{"type":"audio/pcm","rate":24000}}}}})
                await send(ws,{"type":"conversation.item.create","item":{"type":"message","role":"user",
                    "content":[{"type":"input_text","text":text}]}})
                await send(ws,{"type":"response.create","response":{"output_modalities":["audio"]}})
                audio = bytearray()
                async for payload in ws:
                    event = json.loads(payload)
                    if event.get("type") == "error":
                        raise MediaFailure("语音合成被服务拒绝，请检查模型、音色和接口权限。")
                    if event.get("type") == "response.output_audio.delta":
                        audio.extend(base64.b64decode(event["delta"],validate=True))
                        if len(audio) > MAX_ASSET-44:
                            raise MediaFailure("语音结果过大。")
                    if event.get("type") == "response.done":
                        if event["response"].get("status") == "completed" and audio and not len(audio)%2:
                            return "audio/wav",pcm_wav(audio)
                        break
    except (OSError,TimeoutError,WebSocketException,ValueError,KeyError,ImportError):
        raise MediaFailure("语音合成暂时不可用，请检查接口或网络。") from None
    raise MediaFailure("未收到完整语音。")


async def openai_media(service, setup, key, payload):
    headers = {"Authorization": f"Bearer {key}"}
    if service == "tts":
        return await audio_reply(setup,key,payload["text"],payload["tone"])
    try:
        async with httpx.AsyncClient(timeout=150 if service=="image" else 35,follow_redirects=False) as client:
            if service == "stt":
                r = await client.post(setup["base_url"] + "/audio/transcriptions", headers=headers,
                    data={"model":setup["model"]},files={"file":("voice."+AUDIO_TYPES[payload["mime"]],payload["data"],payload["mime"])})
            else:
                r = await client.post(setup["base_url"] + "/images/generations", headers=headers,
                    json={"model":setup["model"],"prompt":image_prompt(payload["scene"],payload.get("detail","")),"n":1,"size":"1024x1024","output_format":"jpeg"})
        if r.status_code in (401,403,404):
            raise MediaFailure(f"{SERVICES[service][0]}不可用：请检查 Key、模型权限或组织验证。")
        if r.status_code==429:
            raise MediaFailure(f"{SERVICES[service][0]}暂时不可用：额度或频率受限。")
        r.raise_for_status()
        data = r.json()
        if service == "stt":
            text = data["text"].strip()
            if not text or len(text)>2400:
                raise MediaFailure("这段没听清，或者内容过长。可以重录或改用文字。")
            return text
        image = base64.b64decode(data["data"][0]["b64_json"], validate=True)
        if not image.startswith(b"\xff\xd8\xff") or len(image)>MAX_ASSET:
            raise ValueError("invalid image")
        return "image/jpeg",image
    except httpx.TimeoutException:
        raise MediaFailure(timeout_message(service)) from None
    except (httpx.HTTPError,ValueError,KeyError,IndexError,TypeError,AttributeError,ImportError):
        raise MediaFailure(f"{SERVICES[service][0]}没有返回有效结果，请检查设置或稍后再试。") from None

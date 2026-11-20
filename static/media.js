"use strict";
window.mediaUI=(()=>{
  let options, timer=null, recorder=null, recordTimer=null, draft=null, draftURL=null;
  const titles={stt:"语音识别",tts:"语音合成",image:"照片生成",video:"视频生成（暂未接入）"};
  const clips=new Map();
  const rid=()=>globalThis.crypto?.randomUUID?.()||Date.now()+"-"+Math.random().toString(36).slice(2);
  function display(row){
    const block=el("div","message-content"), assets=row.attachments||[];
    const voice=assets.find(a=>a.kind==="tts"&&a.presentation==="voice");
    if(voice?.state==="ready"){
      const details=el("details","transcript");
      details.append(el("summary",null,"查看文字"),el("div","bubble",row.text));block.append(details);
    }else if(voice?.state!=="pending")block.append(el("div","bubble",row.text));
    for(const a of assets){
      const card=el("div","attachment "+a.state);
      if(a.state==="pending")card.append(el("span",null,a.kind==="tts"?"正在生成语音…":"正在准备照片…"));
      if(a.state==="failed")card.append(el("span",null,a.error||"这项媒体暂不可用。"));
      if(a.state==="ready"){
        const url="/api/attachments/"+a.id;
        if(a.kind==="image"){
          const image=el("img","photo");image.src=url;image.alt="阿晚分享的虚构日常照片";image.loading="lazy";
          card.append(image,el("small",null,"AI 生成照片"));
        }else{
          const player=clips.get(a.id)||el("audio","voice");
          if(!player.getAttribute("src"))player.src=url;
          player.controls=true;player.preload="metadata";
          player.setAttribute("aria-label",a.kind==="recording"?"你的录音":"阿晚的语音");
          clips.set(a.id,player);if(clips.size>105)clips.delete(clips.keys().next().value);
          card.append(player);
          if(a.kind==="tts")card.append(el("small",null,"AI 合成语音"));
        }
      }
      block.append(card);
    }
    return block;
  }
  function poll(){
    if(timer||!shown.some(m=>(m.attachments||[]).some(a=>a.state==="pending")))return;
    timer=setTimeout(async()=>{
      timer=null;
      try{
        const {messages}=await api("/chat");
        if(!busy&&JSON.stringify(messages)!==JSON.stringify(shown))renderMessages(messages);
      }catch{}
      poll();
    },950);
  }
  function formField(d,label,kind,name,type,value,hidden=false){
    const l=el("label",type==="checkbox"?"check":null),input=el("input");
    input.id=kind+"-"+name;input.type=type;
    if(type==="checkbox"){input.checked=!!value;l.append(input,document.createTextNode(label));}
    else {input.value=value||"";l.append(document.createTextNode(label),input);}
    if(hidden)l.hidden=true;d.append(l);return input;
  }
  function fill(cfg){
    options=cfg;
    $("reply-mode").value=cfg.reply_mode;
    $("proactive-images").checked=cfg.proactive_images;
    const panel=$("media-services");
    const open=new Set([...panel.querySelectorAll('details[open]')].map(n=>n.dataset.service));
    panel.replaceChildren();
    for(const [kind,m] of Object.entries(cfg.services)){
      const d=el("details","memory-card");d.dataset.service=kind;d.open=open.has(kind);
      d.append(el("summary",null,titles[kind]+(m.enabled?" · 已启用":"")));
      formField(d,"启用此服务",kind,"enabled","checkbox",m.enabled,kind==="video");
      formField(d,"接口地址",kind,"base","url",m.base_url);
      formField(d,"模型名称",kind,"model","text",m.model);
      const key=formField(d,"API Key",kind,"key","password","");
      key.autocomplete="off";key.placeholder=m.override_key_set?"••••••••":"输入此服务的 Key";
      formField(d,"清除此服务单独保存的 Key",kind,"clear","checkbox",false,!m.override_key_set);
      formField(d,"没有单独 Key 时继承文字服务的 OpenAI Key",kind,"inherit","checkbox",m.inherit,kind==="video");
      formField(d,"音色名称",kind,"voice","text",m.voice,kind!=="tts");
      d.append(el("p","muted",kind==="video"?"OpenAI 已停用视频接口。这里可预填其他供应商信息，本版不会发视频请求。":
        m.key_set?(m.override_key_set?"使用服务专用 Key。":"继承文字服务的 OpenAI Key。"):"Key 未配置。"));
      panel.append(d);
    }
  }
  async function load(render=true){const cfg=await api("/settings/media");if(render)fill(cfg);else options=cfg;}
  $("media-config").onsubmit=async e=>{
    e.preventDefault();if(!options)return;
    const services={};
    for(const kind of Object.keys(titles))services[kind]={
      enabled:$(kind+"-enabled").checked,base_url:$(kind+"-base").value,model:$(kind+"-model").value,
      inherit:$(kind+"-inherit").checked,voice:$(kind+"-voice").value,
      api_key:$(kind+"-clear").checked?"":($(kind+"-key").value.trim()||null)
    };
    try{fill(await api("/settings/media","PUT",{services,reply_mode:$("reply-mode").value,
      proactive_images:$("proactive-images").checked}));notice("语音与媒体设置已保存。",true);}
    catch(err){notice(err.message);}
  };
  function clearDraft(){
    if(draftURL)URL.revokeObjectURL(draftURL);
    draftURL=null;draft=null;$("draft-audio").removeAttribute("src");$("rec-preview").hidden=true;
  }
  function recordNote(value){$("rec-status").textContent=value;$("rec-status").hidden=!value;}
  $("discard-audio").onclick=()=>{if(!busy)clearDraft();};
  $("send-audio").onclick=()=>{
    if(!draft||busy||pending)return;
    pending={audio:draft,request_id:rid()};send();
  };
  async function submitAudio(pending){
    const r=await fetch("/api/chat/audio",{method:"POST",headers:{"Content-Type":pending.audio.type,
      "X-Request-Id":pending.request_id},body:pending.audio});
    const result=await r.json();
    if(!r.ok)throw Error(typeof result.detail==="string"?result.detail:"这段语音没能发送。");
    return result;
  }
  $("mic").onclick=async()=>{
    if(recorder?.state==="recording"){recorder.stop();return;}
    if(busy||pending)return;
    if(setup?.mode!=="openai"||!options?.services?.stt?.enabled||!options.services.stt.key_set){
      notice("录音转写未开启。到设置启用语音识别并配置 Key，或继续打字。");return;
    }
    if(!navigator.mediaDevices?.getUserMedia||!window.MediaRecorder){
      notice("当前浏览器不支持录音。请在 Mac 浏览器打开本地页面。");return;
    }
    let mic;
    try{
      $("mic").disabled=true;mic=await navigator.mediaDevices.getUserMedia({audio:true});
      if(tab!=="chat"){mic.getTracks().forEach(t=>t.stop());return;}
      const type=["audio/webm;codecs=opus","audio/mp4","audio/ogg;codecs=opus"].find(t=>MediaRecorder.isTypeSupported(t));
      const chunks=[];let failed=false;const active=type?new MediaRecorder(mic,{mimeType:type}):new MediaRecorder(mic);
      recorder=active;
      active.ondataavailable=e=>{if(e.data.size)chunks.push(e.data);};
      active.onerror=()=>{failed=true;mic.getTracks().forEach(t=>t.stop());clearTimeout(recordTimer);recordNote("");$("mic").textContent="🎙";$("mic").setAttribute("aria-label","录音");recorder=null;notice("录音失败，请重试。");};
      active.onstop=()=>{
        clearTimeout(recordTimer);mic.getTracks().forEach(t=>t.stop());recordNote("");$("mic").textContent="🎙";$("mic").setAttribute("aria-label","录音");
        const mime=(active.mimeType||chunks[0]?.type||"audio/webm").split(";")[0];recorder=null;
        if(failed)return;
        clearDraft();const next=new Blob(chunks,{type:mime});
        if(next.size<16){notice("这段没有录到声音，请重试。");return;}
        draft=next;draftURL=URL.createObjectURL(draft);$("draft-audio").src=draftURL;$("rec-preview").hidden=false;
      };
      active.start();$("mic").textContent="■";$("mic").setAttribute("aria-label","停止录音");
      recordNote("正在录音，点方块停止。最多 30 秒。");recordTimer=setTimeout(()=>{if(active.state==="recording")active.stop();},30000);
    }catch(err){mic?.getTracks().forEach(t=>t.stop());notice("未能使用麦克风。检查浏览器权限，或继续打字。");}
    finally{$("mic").disabled=false;}
  };
  return {display,poll,load,clearDraft,submitAudio,leaveChat:()=>{if(recorder?.state==="recording")recorder.stop();}};
})();

"use strict";
window.mediaUI=(()=>{
  let options, timer=null, recorder=null, recordTimer=null, draft=null, draftURL=null, draftRequest=null, attemptedDraft=false;
  const titles={stt:"语音识别",tts:"语音合成",image:"照片生成"};
  const clips=new Map();
  const chatAssets=row=>(row.attachments||[]).filter(asset=>["recording","tts","image"].includes(asset.kind));
  function display(row,{transcriptOpen=false}={}){
    const block=el("div","message-content"), assets=chatAssets(row);
    const voice=assets.find(a=>a.kind==="tts"&&a.presentation==="voice");
    if(voice&&(voice.state==="ready"||voice.state==="pending")){
      const details=el("details","transcript");
      details.open=transcriptOpen;
      details.append(el("summary",null,"查看文字"),el("div","bubble",row.text));block.append(details);
    }else block.append(el("div","bubble",row.text));
    for(const a of assets){
      const card=el("div","attachment "+a.state);
      if(a.state==="pending")card.append(el("span",null,a.kind==="tts"?"正在生成语音…":"正在准备照片…"));
      if(a.state==="failed")card.append(el("span",null,a.error||"这项媒体暂不可用。"));
      if(a.state==="removed"){
        const clip=clips.get(a.id);
        if(clip){clip.pause();clip.removeAttribute("src");clips.delete(a.id);}
        const marker=el("span","attachment-removed-marker","已移除本地附件");
        marker.dataset.removedAttachmentId=String(a.id);marker.tabIndex=-1;
        card.append(marker);
      }
      if(a.state==="ready"){
        const url="/api/attachments/"+a.id;
        if(a.kind==="image"){
          const image=el("img","photo");image.src=url;image.alt="阿晚分享的照片";image.loading="lazy";
          card.append(image);
        }else{
          const player=clips.get(a.id)||el("audio","voice");
          if(!player.getAttribute("src"))player.src=url;
          player.controls=true;player.preload="metadata";
          player.setAttribute("aria-label",a.kind==="recording"?"你的录音":"阿晚的语音");
          clips.set(a.id,player);if(clips.size>105)clips.delete(clips.keys().next().value);
          card.append(player);
        }
        const remove=button("移除本地附件","attachment-remove",()=>removeAttachment(a,remove));
        const kindLabel={recording:"录音",tts:"语音",image:"照片"}[a.kind];
        remove.dataset.removeAttachmentId=String(a.id);
        remove.setAttribute("aria-label","移除本地"+kindLabel+"附件");
        card.append(remove);
      }
      block.append(card);
    }
    return block;
  }
  async function removeAttachment(asset,control){
    if(control.getAttribute("aria-disabled")==="true"||
      !confirm("移除这份本地附件？附件将无法播放或查看，原消息中的文字和聊天记录会保留。"))return;
    control.setAttribute("aria-disabled","true");
    try{
      const result=await api("/attachments/"+encodeURIComponent(asset.id),"DELETE");
      mergeMessages(result.messages);
      if(tab==="chat")$("chat-update").textContent="已移除本地附件。";
    }catch(error){if(tab==="chat")notice(error.message);diagnostic(error.message);}
    finally{control.removeAttribute("aria-disabled");}
  }
  function poll(){
    if(timer||!shown.some(m=>chatAssets(m).some(a=>a.state==="pending")))return;
    timer=setTimeout(async()=>{
      try{
        const {messages}=await api("/chat");
        mergeMessages(messages);
        const oldestPending=shown.find(row=>row.id&&!messages.some(item=>item.id===row.id)&&chatAssets(row).some(asset=>asset.state==="pending"));
        if(oldestPending){
          const older=await api("/chat?before="+encodeURIComponent(oldestPending.id+1));
          mergeMessages(older.messages,{preserve:true});
        }
      }catch{}
      finally{timer=null;poll();}
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
      if(!Object.hasOwn(titles,kind))continue;
      const d=el("details","memory-card");d.dataset.service=kind;d.open=open.has(kind);
      d.append(el("summary",null,titles[kind]+(m.enabled?" · 已启用":"")));
      formField(d,"启用此服务",kind,"enabled","checkbox",m.enabled);
      formField(d,"接口地址",kind,"base","url",m.base_url);
      formField(d,"模型名称",kind,"model","text",m.model);
      const key=formField(d,"API Key",kind,"key","password","");
      key.autocomplete="off";key.placeholder=m.override_key_set?"••••••••":"输入此服务的 Key";
      formField(d,"清除此服务单独保存的 Key",kind,"clear","checkbox",false,!m.override_key_set&&!m.saved_key);
      formField(d,"没有单独 Key 时继承文字服务的 OpenAI Key",kind,"inherit","checkbox",m.inherit);
      formField(d,"音色名称",kind,"voice","text",m.voice,kind!=="tts");
      const keyStatus=el("p","muted",statusText(kind,m));
      keyStatus.id=kind+"-key-status";d.append(keyStatus);
      panel.append(d);
    }
  }
  function statusText(kind,m){
    return m.key_set?(m.override_key_set?"使用已加密保存的服务专用 Key。":"继承文字服务的 OpenAI Key。"):"Key 未配置。";
  }
  async function refreshKeys(){
    options=await api("/settings/media");
    for(const [kind,m] of Object.entries(options.services)){
      const label=$(kind+"-key-status");if(label)label.textContent=statusText(kind,m);
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
  function clearDraft(expected){
    if(expected&&expected!==draft)return;
    if(draftURL)URL.revokeObjectURL(draftURL);
    draftURL=null;draft=null;draftRequest=null;attemptedDraft=false;
    $("draft-audio").removeAttribute("src");$("rec-preview").hidden=true;
    $("send-audio").textContent="发送语音";
  }
  function recordNote(value){$("rec-status").textContent=value;$("rec-status").hidden=!value;}
  $("discard-audio").onclick=()=>{if(!busy)clearDraft();};
  $("send-audio").onclick=()=>{
    if(!draft||busy)return;
    draftRequest??=requestId();
    const expected=draft;
    send({audio:draft,request_id:draftRequest}).finally(()=>{
      if(draft===expected){attemptedDraft=true;$("send-audio").textContent="重试语音";}
    });
  };
  async function submitAudio(pending){
    const r=await fetch("/api/chat/audio",{method:"POST",headers:{"Content-Type":pending.audio.type,
      "X-Request-Id":pending.request_id},body:pending.audio});
    const result=await r.json().catch(()=>({}));
    if(!r.ok)throw Error(typeof result.detail==="string"?result.detail:"这段语音没能发送。");
    return result;
  }
  $("mic").onclick=async()=>{
    if(recorder?.state==="recording"){recorder.stop();return;}
    if(busy)return;
    if(draft){notice(attemptedDraft?"这段录音还在，可以重试或先丢弃。":"请先发送或丢弃这段录音。");return;}
    if(setup?.mode!=="openai"||!options?.services?.stt?.enabled||!options.services.stt.key_set){
      notice("录音转写未开启。到设置启用语音识别并配置 Key，或继续打字。");return;
    }
    if(!navigator.mediaDevices?.getUserMedia||!window.MediaRecorder){
      notice("当前浏览器不支持录音。请用支持麦克风的浏览器打开晚灯。");return;
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
  return {display,poll,load,refreshKeys,clearDraft,submitAudio,leaveChat:()=>{if(recorder?.state==="recording")recorder.stop();}};
})();

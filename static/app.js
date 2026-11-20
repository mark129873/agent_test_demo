"use strict";
const $=id=>document.getElementById(id);
const requestId=()=>globalThis.crypto?.randomUUID?.()||Date.now()+"-"+Math.random().toString(36).slice(2);
let setup=null, busy=false, tab="chat", shown=[], activeRequest=null, nextCursor=null;
let historyReady=false, historyLoading=false, historyError=false, loadingOlder=false, openRequested=false;
let memoryLoad=0, memoryBusy=false, settingsLoad=0, newMessages=false;
const serverMessages=new Map(), localMessages=new Map(), answeredRequests=new Set(), archivedRequests=new Set(), renderedRows=new Map();
async function api(path, method="GET", data){
  let response;
  try{
    response=await fetch("/api"+path,{method,cache:"no-store",headers:data?{"Content-Type":"application/json"}:{},body:data?JSON.stringify(data):undefined});
  }catch{throw Error("暂时连不上晚灯，请检查服务是否正在运行。");}
  const body=await response.json().catch(()=>({}));
  if(!response.ok){
    const error=Error(typeof body.detail==="string"?body.detail:"请求没有成功，请再试一次。");
    error.status=response.status;throw error;
  }
  return body;
}
function notice(message="", success=false){
  $("notice").textContent=message;
  $("notice").hidden=!message;
  $("notice").classList.toggle("success",success);
}
function diagnostic(message){$("diagnostics").textContent=message;}
function el(tag,cls,text){
  const node=document.createElement(tag);
  if(cls)node.className=cls;
  if(text!==undefined&&text!==null)node.textContent=text;
  return node;
}
function button(label,cls,click){
  const node=el("button",cls,label);node.type="button";node.onclick=click;return node;
}
function keyFor(row){return row.id!=null?"id:"+row.id:"request:"+row.request_id;}
function nearBottom(content){return content.scrollHeight-content.scrollTop-content.clientHeight<100;}
function newest(){const content=$("messages");content.scrollTop=content.scrollHeight;newMessages=false;$("new-messages").hidden=true;}
$("to-latest").onclick=newest;
$("messages").addEventListener("scroll",()=>{if(tab==="chat"&&nearBottom($("messages"))){newMessages=false;$("new-messages").hidden=true;}});
function welcome(content){
  const hero=el("div","welcome");
  hero.append(el("div","welcome-mark","✦"),el("h2",null,"给自己留一盏灯。"),el("p",null,"你和阿晚的聊天，从这里开始。"));
  content.append(hero);
  const opener=el("div","row assistant");
  const av=el("span","bubble-avatar","晚");av.setAttribute("aria-hidden","true");
  opener.append(av,el("div","bubble","你来啦。今天过得怎么样？不急，想到哪儿就说到哪儿。"));content.append(opener);
  const examples=el("div","examples");examples.append(el("p",null,"不知道怎么开口？"));
  for(const sample of ["今天有点累","我养了一只猫叫年糕","下周三有个面试"]){
    examples.append(button(sample,"chip",()=>{const input=$("message");if(!input.value.trim())input.value=sample;input.focus();}));
  }
  content.append(examples);
}
function renderMessages({stick=false, preserve=false, arrived=false}={}){
  if(tab!=="chat") {if(arrived)newMessages=true;return;}
  const content=$("messages");
  const atBottom=!preserve&&nearBottom(content);
  const oldBounds=content.getBoundingClientRect();
  const oldTop=content.scrollTop;
  const focused=document.activeElement;
  let anchor=null;
  if(!atBottom&&!stick){
    for(const row of content.querySelectorAll("[data-message-key]")){
      const rect=row.getBoundingClientRect();
      if(rect.bottom>oldBounds.top&&rect.top<oldBounds.bottom){
        anchor={key:row.dataset.messageKey,offset:rect.top-oldBounds.top};break;
      }
    }
  }
  const desired=[];
  const output={append:(...nodes)=>desired.push(...nodes)};
  if(nextCursor!==null){
    const previous=button(loadingOlder?"正在加载…":"加载更早的消息","chip history-button",loadOlder);
    previous.id="older-messages";previous.setAttribute("aria-disabled",String(loadingOlder));
    output.append(previous);
  }
  if(!historyReady&&!shown.length){
    const state=el("div","history-state");
    state.append(el("p","muted",historyError?"聊天记录暂时没加载出来。":"正在打开聊天…"));
    if(historyError)state.append(button("重新加载","secondary",loadHistory));
    output.append(state);
  }else if(!shown.length)welcome(output);
  let day=null;
  const keep=new Set();
  for(const row of shown){
    const date=row.created_at?new Date(row.created_at):null;
    if(date&&!Number.isNaN(date.getTime())&&day!==date.toDateString()){
      day=date.toDateString();
      output.append(el("div","day-note",date.toLocaleDateString("zh-CN",{month:"long",day:"numeric"})));
    }
    if(row.role==="followup")output.append(el("div","day-note proactive","✦ 阿晚想起了这件事"));
    const key=keyFor(row);keep.add(key);
    let entry=renderedRows.get(key);
    const signature=JSON.stringify(row);
    if(!entry||entry.signature!==signature){
      const line=el("div","row "+row.role);line.dataset.messageKey=key;
      if(row.role!=="user"){
        const av=el("span","bubble-avatar","晚");av.setAttribute("aria-hidden","true");line.append(av);
      }
      const block=window.mediaUI?.display(row)||el("div","message-content");line.append(block);
      if(!window.mediaUI)block.append(el("div","bubble",row.text));
      entry={line,block,signature};renderedRows.set(key,entry);
    }
    if(row.role==="user"){
      const archived=row.delivery_state==="archived"||archivedRequests.has(row.request_id);
      const pending=!archived&&row.delivery_state==="pending"&&!answeredRequests.has(row.request_id);
      if((pending||archived)&&!entry.delivery){
        const retry=button("重试","retry-message",()=>send({text:row.text,request_id:row.request_id}));
        retry.setAttribute("aria-label","重试这条消息的回复");
        const abandon=button("不再等待","retry-message",()=>abandonMessage(row));
        abandon.setAttribute("aria-label","不再等待这条消息的回复");
        entry.delivery=el("div","delivery");entry.label=el("span");entry.retry=retry;entry.abandon=abandon;
        entry.delivery.append(entry.label,entry.retry,entry.abandon);entry.block.append(entry.delivery);
      }
      if(entry.delivery){
        const sending=activeRequest===row.request_id;
        entry.delivery.hidden=!pending&&!archived;
        entry.retry.hidden=entry.abandon.hidden=sending||archived;
        entry.retry.disabled=entry.abandon.disabled=busy;
        entry.label.textContent=archived?"已停止等待":sending?"正在等回复…":"还未收到回复";
      }
    }
    output.append(entry.line);
  }
  for(const key of renderedRows.keys())if(!keep.has(key))renderedRows.delete(key);
  // Keep unchanged message nodes mounted, including an audio player in use.
  const liveNodes=new Set(desired);
  for(const item of [...content.children])if(!liveNodes.has(item))item.remove();
  let cursor=content.firstElementChild;
  for(const item of desired){
    if(item===cursor)cursor=cursor.nextElementSibling;
    else content.insertBefore(item,cursor);
  }
  if(stick||atBottom)newest();
  else{
    const target=anchor&&renderedRows.get(anchor.key)?.line;
    content.scrollTop=target?content.scrollTop+target.getBoundingClientRect().top-oldBounds.top-anchor.offset:oldTop;
    if(arrived)newMessages=true;
  }
  $("new-messages").hidden=!newMessages;
  if(focused?.isConnected&&focused!==document.body)focused.focus({preventScroll:true});
  else if(focused?.id==="older-messages")($("older-messages")||content).focus({preventScroll:true});
  window.mediaUI?.poll();
}
function mergeMessages(messages, options={}){
  const rows=Array.isArray(messages)?messages:[];
  let previousMaximum=0;
  for(const id of serverMessages.keys())previousMaximum=Math.max(previousMaximum,id);
  const keysByUser=new Map();
  for(const row of rows){
    if(row.id==null)continue;
    const old=serverMessages.get(row.id);
    const priorAssets=new Map((old?.attachments||[]).map(a=>[a.id,a]));
    for(const next of row.attachments||[]){
      const prior=priorAssets.get(next.id);
      if(!prior||prior.state==="pending"||next.state!=="pending")priorAssets.set(next.id,next);
    }
    const entry={...old,...row,attachments:[...priorAssets.values()]};
    if(old?.delivery_state==="archived"||archivedRequests.has(row.request_id))entry.delivery_state="archived";
    else if(old?.delivery_state==="answered")entry.delivery_state="answered";
    serverMessages.set(row.id,entry);
    if(entry.request_id){
      keysByUser.set(entry.id,entry.request_id);
      localMessages.delete(entry.request_id);
      if(entry.delivery_state==="answered")answeredRequests.add(entry.request_id);
      if(entry.delivery_state==="archived")archivedRequests.add(entry.request_id);
    }
  }
  for(const row of rows){
    const rid=keysByUser.get(row.reply_to)||serverMessages.get(row.reply_to)?.request_id;
    if(rid)answeredRequests.add(rid);
  }
  const nextShown=[...serverMessages.values()].sort((a,b)=>a.id-b.id).concat([...localMessages.values()]);
  const changed=nextShown.length!==shown.length||nextShown.some((row,i)=>JSON.stringify(row)!==JSON.stringify(shown[i]));
  shown=nextShown;
  const arrived=!!rows.some(row=>row.id>previousMaximum&&row.role!=="user");
  if(arrived&&historyReady&&!options.initial)$("chat-update").textContent="阿晚有新消息。";
  if(changed||options.force)renderMessages({...options,arrived:arrived&&!options.initial});
}
async function loadOlder(){
  if(loadingOlder||nextCursor===null)return;
  loadingOlder=true;renderMessages({preserve:true});notice();
  try{
    const {messages,next_cursor}=await api("/chat?before="+encodeURIComponent(nextCursor));
    nextCursor=next_cursor??null;
    loadingOlder=false;mergeMessages(messages,{preserve:true,force:true});
    $("chat-update").textContent="更早的聊天记录已加载。";
  }catch(error){notice(error.message);diagnostic(error.message);}
  finally{if(loadingOlder){loadingOlder=false;renderMessages({preserve:true});}}
}
async function loadHistory(){
  if(historyReady||historyLoading)return;
  historyLoading=true;historyError=false;renderMessages();
  try{
    const {messages,next_cursor}=await api("/chat");
    nextCursor=next_cursor??null;historyReady=true;
    mergeMessages(messages,{force:true,initial:true,stick:true});
    if(!openRequested){
      openRequested=true;
      openChat().catch(error=>diagnostic("回访检查："+error.message));
    }
  }catch(error){historyError=true;diagnostic(error.message);renderMessages();}
  finally{historyLoading=false;}
}
function rememberedTitle(key){
  return {residence:"现居地",pet_name:"宠物名字",name:"称呼"}[key]||(key.startsWith("food:")?"饮食偏好":key.replace(/^preference:/,""));
}
function factDisplay(fact){
  const prefix="food:";
  if(!fact.key.startsWith(prefix))return fact.value;
  const subject=fact.key.slice(prefix.length);
  if(fact.value.includes(subject))return fact.value;
  if(/^(不吃|爱吃|喜欢吃|讨厌|喜欢|不喜欢|忌口|过敏)$/.test(fact.value)){
    if(fact.value==="过敏")return "对"+subject+"过敏";
    if(fact.value==="忌口")return subject+"：忌口";
    return fact.value+subject;
  }
  return subject+"："+fact.value;
}
function card(title,quote,key){
  const d=el("details","memory-card");d.dataset.key=key||"";
  d.append(el("summary",null,title));
  if(quote)d.append(el("p","quote","来源：「"+quote+"」"));
  return d;
}
async function changeMemory(fact,value=null){
  if(memoryBusy)return;
  memoryBusy=true;notice();
  try{
    const path="/memories/"+encodeURIComponent(fact.key);
    if(value===null)await api(path,"DELETE");
    else await api(path,"PUT",{value});
    if(value===null||value!==fact.value)refreshPending().catch(error=>diagnostic(error.message));
    if(tab==="memory"){
      await loadMemory(fact.key);
      notice(value===null?"已忘记这条记忆。原聊天记录仍可查看。":"这条记忆已更正。",true);
    }
  }catch(error){notice(error.message);}
  finally{memoryBusy=false;}
}
function factCard(fact){
  const d=card(rememberedTitle(fact.key)+" · "+factDisplay(fact),fact.quote,fact.key);
  const actions=el("div","actions memory-actions");
  const editor=el("form","memory-editor");editor.hidden=true;
  const field=el("input");field.value=fact.value;field.required=true;field.maxLength=100;
  const label=el("label",null,"改成什么？");label.append(field);
  const controls=el("div","actions");
  const save=el("button",null,"保存更正");
  const edit=button("更正","secondary",()=>{editor.hidden=false;confirmation.hidden=true;field.focus();});
  controls.append(save,button("取消","secondary",()=>{editor.hidden=true;edit.focus();}));
  editor.append(label,controls);
  editor.onsubmit=async event=>{
    event.preventDefault();
    const value=field.value.trim();
    if(value){
      save.disabled=true;
      await changeMemory(fact,value);
      save.disabled=false;
    }
  };
  const confirmation=el("div","forget-confirm");confirmation.hidden=true;
  const forget=button("忘记","secondary",()=>{confirmation.hidden=false;editor.hidden=true;cancelForget.focus();});
  const confirmForget=button("确认忘记",null,async()=>{confirmForget.disabled=true;await changeMemory(fact);confirmForget.disabled=false;});
  const cancelForget=button("取消","secondary",()=>{confirmation.hidden=true;forget.focus();});
  confirmation.append(el("p","muted","忘记后，阿晚不会再用这条记忆、之前的聊天摘要或含有旧值的关联事件来回复。其他独立记忆会保留，原聊天记录仍可查看。"));
  const confirmActions=el("div","actions");confirmActions.append(cancelForget,confirmForget);confirmation.append(confirmActions);
  actions.append(edit,forget);d.append(actions,editor,confirmation);
  return d;
}
async function muteEvent(event){
  if(memoryBusy)return false;
  memoryBusy=true;notice();
  try{
    const result=await api("/memories/events/"+encodeURIComponent(event.event_id),"DELETE");
    refreshPending().catch(error=>diagnostic(error.message));
    if(tab==="memory"){
      await loadMemory({eventId:String(event.event_id)},result);
      notice("已停用这条事件，不会再用它聊天或追问。原聊天记录仍可查看。",true);
    }
    return true;
  }catch(error){if(tab==="memory")notice(error.message);return false;}
  finally{memoryBusy=false;}
}
function eventCard(event){
  const statuses={pending:"待回访",asked:"已问候",resolved:"已聊完或取消",muted:"已停用"};
  const item=card(event.label,event.quote);
  const meta=el("span","event-meta");
  const date=el("time",null,event.event_date);date.dateTime=event.event_date;
  meta.append(date,el("span","event-status",statuses[event.status]||event.status));
  item.querySelector("summary").append(meta);
  if(event.event_id==null)return item;
  item.dataset.eventId=String(event.event_id);
  if(event.status==="muted")return item;
  const actionRow=el("div","actions memory-actions");
  const confirmation=el("div","forget-confirm");confirmation.hidden=true;
  const actions=el("div","actions");
  const stop=button("停止用于聊天","secondary",()=>{confirmation.hidden=false;cancel.focus();});
  const cancel=button("取消","secondary",()=>{confirmation.hidden=true;stop.focus();});
  const accept=button("确认停用",null,async()=>{
    accept.disabled=cancel.disabled=stop.disabled=true;
    const done=await muteEvent(event);
    if(!done){
      accept.disabled=cancel.disabled=stop.disabled=false;
      if(accept.isConnected)accept.focus();
    }
  });
  actionRow.append(stop);actions.append(cancel,accept);
  confirmation.append(el("p","muted","停用后，阿晚不会用这条事件来聊天，也不会再主动追问。事件会标为已停用，原聊天记录不会删除。"),actions);
  item.append(actionRow,confirmation);
  return item;
}
async function loadMemory(focusTarget,result){
  const version=++memoryLoad;
  const list=$("memory-list");
  if(!focusTarget)list.replaceChildren(el("p","muted","正在读取记忆…"));
  const {facts,events}=result||await api("/memories");
  if(tab!=="memory"||version!==memoryLoad)return;
  list.replaceChildren(el("h3",null,"偏好与事实"));
  if(!facts.length)list.append(el("p","muted","还没有记下长期偏好。"));
  for(const fact of facts)list.append(factCard(fact));
  list.append(el("h3",null,"回访事件"));
  if(!events.length)list.append(el("p","muted","还没有记下需要回访的日子。"));
  for(const event of events)list.append(eventCard(event));
  if(focusTarget){
    const match=[...list.querySelectorAll(".memory-card")].find(item=>typeof focusTarget==="string"?
      item.dataset.key===focusTarget:item.dataset.eventId===focusTarget.eventId);
    if(match){match.open=true;match.querySelector("summary").focus();}
    else $("memory-title").focus();
  }
}
function fill(s){
  setup=s;$("provider").value=s.mode;$("base").value=s.base_url;$("model").value=s.model;$("timezone").value=s.timezone;
  $("online-summary").checked=!!s.online_summary;
  $("key").value="";$("key").placeholder=s.key_set?"••••••••":"输入 OpenAI API Key";
  $("demo").hidden=s.mode!=="mock";$("clear-key").disabled=!s.saved_key;
  $("key-info").textContent=s.key_store_error||(s.saved_key?
    "已加密保存到本机，重启可用。留空保存会保留现有 Key。":s.key_set?
    "正在使用环境变量中的 Key。":"未配置 API Key。填写后将加密保存在本机。");
  $("chat-setup").hidden=s.mode==="openai"&&s.key_set;
  $("chat-setup-text").textContent=s.mode==="mock"?"现在可以直接打字体验，无需 API Key。语音和照片可在设置中开启。":"接入模型还需要 API Key。也可以切回无需 Key 的文字体验。";
}
async function openChat(date=null){
  const result=await api("/chat/open","POST",date?{demo_date:date}:{});
  mergeMessages(result.messages);
  return result;
}
async function navigate(next){
  if(tab===next)return;
  if(next!=="chat")window.mediaUI?.leaveChat();
  tab=next;notice();
  for(const item of document.querySelectorAll("[data-tab]")){
    if(item.dataset.tab===next)item.setAttribute("aria-current","page");
    else item.removeAttribute("aria-current");
  }
  for(const name of ["chat","memory","settings"])$(name).hidden=name!==next;
  if(next==="chat"){
    renderMessages({preserve:true});$("message").focus({preventScroll:true});
    if(!historyReady)loadHistory();
    return;
  }
  $(next+"-title").focus();
  try{
    if(next==="memory")await loadMemory();
    if(next==="settings"){
      const version=++settingsLoad;
      const settings=await api("/settings");
      if(tab!=="settings"||version!==settingsLoad)return;
      fill(settings);await window.mediaUI?.load();
    }
  }catch(error){if(tab===next)notice(error.message);diagnostic(error.message);}
}
for(const item of document.querySelectorAll("[data-tab]"))item.onclick=()=>navigate(item.dataset.tab);
for(const item of document.querySelectorAll(".back-link"))item.onclick=()=>navigate("chat");
$("go-settings").onclick=()=>navigate("settings");
function syncSending(){
  $("send").disabled=busy;
  $("send").textContent=busy?"等回复…":"发送 ↑";
  $("send-audio").disabled=busy;$("discard-audio").disabled=busy;
  renderMessages();
}
async function refreshPending(){
  const latest=await api("/chat");
  mergeMessages(latest.messages);
  const updated=new Set(latest.messages.map(row=>row.id));
  for(const row of shown){
    if(row.id==null||row.delivery_state!=="pending"||updated.has(row.id))continue;
    const older=await api("/chat?before="+encodeURIComponent(row.id+1));
    for(const item of older.messages)updated.add(item.id);
    mergeMessages(older.messages,{preserve:true});
  }
}
async function abandonMessage(row){
  if(busy||!row.request_id)return;
  if(!confirm("停止等待这条消息？原消息仍可查看，但阿晚不会再回复这条，也不会在之后的回复中使用它。"))return;
  busy=true;syncSending();notice();
  try{
    let result;
    try{result=await api("/chat/abandon","POST",{request_id:row.request_id});}
    catch(error){
      if(row.id!=null||error.status!==404)throw error;
      // The server never saved this message; mark the local bubble only.
      result={messages:[]};
    }
    if(result.abandoned===false||result.messages.some(item=>item.request_id===row.request_id&&item.delivery_state==="answered")){
      answeredRequests.add(row.request_id);
      $("chat-update").textContent="这条消息已经收到回复。";
    }else{
      archivedRequests.add(row.request_id);
      const local=localMessages.get(row.request_id);
      if(local)localMessages.set(row.request_id,{...local,delivery_state:"archived"});
      else if(row.id!=null)serverMessages.set(row.id,{...serverMessages.get(row.id),delivery_state:"archived"});
      $("chat-update").textContent="已停止等待这条消息的回复。";
    }
    mergeMessages(result.messages,{force:true});
  }catch(error){notice(error.message);diagnostic(error.message);}
  finally{busy=false;syncSending();}
}
async function send(payload,{fresh=false}={}){
  if(busy||!payload?.request_id)return;
  busy=true;activeRequest=payload.request_id;
  notice();
  if(fresh&&payload.text){
    localMessages.set(payload.request_id,{role:"user",text:payload.text,request_id:payload.request_id,delivery_state:"pending",created_at:new Date().toISOString()});
    mergeMessages([],{stick:true});
  }
  syncSending();
  try{
    const result=payload.audio?await window.mediaUI.submitAudio(payload):await api("/chat/messages","POST",payload);
    // A successful response may omit an old retried row from its latest window.
    answeredRequests.add(payload.request_id);
    localMessages.delete(payload.request_id);
    if(payload.audio)window.mediaUI?.clearDraft(payload.audio);
    mergeMessages(result.messages,{force:true});
  }catch(error){
    try{mergeMessages((await api("/chat")).messages);}catch{}
    if(payload.audio&&[...serverMessages.values()].some(row=>row.request_id===payload.request_id))window.mediaUI?.clearDraft(payload.audio);
    if(!answeredRequests.has(payload.request_id))notice(error.message);
    diagnostic(error.message);
  }finally{
    busy=false;activeRequest=null;syncSending();
  }
}

$("composer").onsubmit=e=>{
  e.preventDefault();if(busy)return;
  const text=$("message").value.trim();if(!text)return;
  $("message").value="";
  send({text,request_id:requestId()},{fresh:true});
};
$("message").addEventListener("keydown",e=>{
  if(e.key==="Enter"&&!e.shiftKey&&!e.isComposing&&e.keyCode!==229){e.preventDefault();$("composer").requestSubmit();}
});
async function store(clear=false){
  notice();
  try{
    const mode=$("provider").value,key=$("key").value.trim();
    if(mode==="openai"&&setup?.mode!=="openai"&&!confirm("接入模型会把对话和相关记忆发送给配置的服务，调用可能产生费用。确定切换？"))return;
    const s=await api("/settings","PUT",{mode,base_url:$("base").value,model:$("model").value,timezone:$("timezone").value,online_summary:$("online-summary").checked,api_key:clear?"":(key||null)});
    fill(s);notice(clear?"已清除本机保存的 Key。":"聊天设置已保存。",true);
    window.mediaUI?.refreshKeys().catch(()=>{});
  }catch(error){notice(error.message);diagnostic(error.message);}
}
$("config").onsubmit=e=>{e.preventDefault();store();};
$("config").addEventListener("invalid",event=>{const detail=event.target.closest("details");if(detail)detail.open=true;},true);
$("clear-key").onclick=()=>store(true);
$("clock").onsubmit=async e=>{
  e.preventDefault();const date=$("demo-date").value;if(!date)return;
  const check=e.currentTarget.querySelector("button");check.disabled=true;
  try{
    const result=await openChat(date);
    await navigate("chat");
    notice(result.followup?"已按所选日期添加一条回访。":"所选日期没有新的到期事件。",true);
  }catch(error){notice(error.message);}
  finally{check.disabled=false;}
};
window.addEventListener("DOMContentLoaded",()=>{
  loadHistory();
  api("/settings").then(s=>{if(!setup&&tab!=="settings")fill(s);}).catch(error=>diagnostic(error.message));
  window.mediaUI?.load(false).catch(error=>diagnostic(error.message));
});

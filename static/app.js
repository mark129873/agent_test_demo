const $=id=>document.getElementById(id);
let setup=null, pending=null, busy=false, tab="chat", shown=[], retryFollowup=false;
async function api(path, method="GET", data){
  let response;
  try{response=await fetch("/api"+path,{method,cache:"no-store",headers:data?{"Content-Type":"application/json"}:{},body:data?JSON.stringify(data):undefined});}
  catch{throw Error("没连上服务，请确认 Demo 还在运行。");}
  const body=await response.json().catch(()=>({}));
  if(!response.ok)throw Error(typeof body.detail==="string"?body.detail:"保存或请求失败，请检查输入。");
  return body;
}
function notice(message="", success=false){
  $("notice").textContent=message;$("notice").hidden=!message;$("notice").classList.toggle("success",success);
}
function el(tag,cls,text){
  const node=document.createElement(tag);if(cls)node.className=cls;if(text)node.textContent=text;return node;
}
function renderMessages(messages){
  shown=messages;
  const content=$("messages");content.replaceChildren();
  if(!messages.length){
    const welcome=el("div","welcome");
    welcome.append(el("div","welcome-mark","✦"),el("h2",null,"给自己留一盏灯。"),el("p",null,"你和阿晚的聊天，从这里开始。"));
    content.append(welcome);
    const opener=el("div","row assistant");
    const av=el("span","bubble-avatar","晚");av.setAttribute("aria-hidden","true");
    opener.append(av,el("div","bubble","你来啦。今天过得怎么样？不急，想到哪儿就说到哪儿。"));
    content.append(opener);
    const examples=el("div","examples");examples.append(el("p",null,"不知道怎么开口？"));
    for(const sample of ["今天有点累","我养了一只猫叫年糕","下周三有个面试"]){
      const chip=el("button","chip",sample);chip.type="button";
      chip.onclick=()=>{const input=$("message");if(!input.value.trim())input.value=sample;input.focus();};examples.append(chip);
    }
    content.append(examples);
  }
  let day=null;
  for(const row of messages){
    const date=row.created_at?new Date(row.created_at):null;
    if(date&&day!==date.toDateString()){
      day=date.toDateString();
      content.append(el("div","day-note",date.toLocaleDateString("zh-CN",{month:"long",day:"numeric"})));
    }
    if(row.role==="followup")content.append(el("div","day-note proactive","✦ 阿晚想起了这件事"));
    const line=el("div","row "+row.role);
    if(row.role!=="user"){const av=el("span","bubble-avatar","晚");av.setAttribute("aria-hidden","true");line.append(av);}
    line.append(el("div","bubble",row.text));content.append(line);
  }
  content.scrollTop=content.scrollHeight;
}
function rememberedTitle(key){
  return {residence:"现居地",pet_name:"宠物名字",name:"称呼"}[key]||(key.startsWith("food:")?"饮食偏好":key.replace(/^preference:/,""));
}
function card(title,quote){
  const d=el("details","memory-card");d.append(el("summary",null,title),el("p","quote","来源：「"+quote+"」"));return d;
}
async function loadMemory(){
  const {facts,events}=await api("/memories");
  const list=$("memory-list");list.replaceChildren(el("h3",null,"偏好与事实"));
  if(!facts.length)list.append(el("p","muted","还没有记下长期偏好。"));
  for(const f of facts)list.append(card(rememberedTitle(f.key)+" · "+f.value,f.quote));
  list.append(el("h3",null,"回访事件"));
  if(!events.length)list.append(el("p","muted","还没有待关注的日子。"));
  for(const e of events)list.append(card(e.label+" · "+e.event_date+" · "+({pending:"待回访",asked:"已问候",resolved:"已聊完或取消"}[e.status]||e.status),e.quote));
}
function fill(s){
  setup=s;$("mode").textContent=s.mode==="mock"?"离线模拟":"OpenAI";$("provider").value=s.mode;$("base").value=s.base_url;$("model").value=s.model;$("timezone").value=s.timezone;$("key").value="";$("demo").hidden=s.mode!=="mock";$("clear-key").disabled=!s.key_set;
  $("key-info").textContent=(s.key_set?"本次运行已配置。":"本次运行没有配置 Key。")+" Key 不写入数据库；重启后可用环境变量或重新填写。";
}
async function openChat(demo_date=null){
  const result=await api("/chat/open","POST",demo_date?{demo_date}:{});
  renderMessages(result.messages);retryFollowup=false;return result;
}
async function navigate(next){
  tab=next;notice();
  for(const button of document.querySelectorAll("[data-tab]")){
    if(button.dataset.tab===next)button.setAttribute("aria-current","page");
    else button.removeAttribute("aria-current");
  }
  for(const name of ["chat","memory","settings"])$(name).hidden=name!==next;
  try{
    if(next==="memory")await loadMemory();
    if(next==="settings")fill(await api("/settings"));
    if(next==="chat"&&retryFollowup)await openChat();
  }catch(e){notice(e.message);}
}
for(const b of document.querySelectorAll("[data-tab]"))b.onclick=()=>navigate(b.dataset.tab);
function syncRetry(){ $("retry").hidden=!pending;$("retry").disabled=busy; }
async function send(){
  if(!pending||busy)return;
  busy=true;$("send").disabled=true;$("send").textContent="等回复…";syncRetry();notice();
  try{const r=await api("/chat/messages","POST",pending);renderMessages(r.messages);pending=null;}
  catch(e){
    notice(e.message+" 可以重试刚才那条消息。");
    try{renderMessages((await api("/chat")).messages);}catch{}
  }finally{busy=false;$("send").disabled=false;$("send").textContent="发送 ↑";syncRetry();}
}
$("composer").onsubmit=e=>{
  e.preventDefault();if(busy)return;
  if(pending){notice("上一条还没有回复，先点「重试回复」。");return;}
  const text=$("message").value.trim();if(!text)return;
  pending={text,request_id:globalThis.crypto?.randomUUID?.()||Date.now()+"-"+Math.random().toString(36).slice(2)};
  $("message").value="";
  renderMessages([...shown,{role:"user",text}]);send();
};
$("retry").onclick=send;
$("message").addEventListener("keydown",e=>{if(e.key==="Enter"&&!e.shiftKey&&!e.isComposing&&e.keyCode!==229){e.preventDefault();$("composer").requestSubmit();}});
async function store(clear=false){
  notice();
  try{
    const mode=$("provider").value, key=$("key").value.trim();
    if(mode==="openai"&&setup?.mode!=="openai"&&!confirm("打开 OpenAI 将把对话发送给 OpenAI，真实请求可能产生费用。确定切换？"))return;
    const s=await api("/settings","PUT",{mode,base_url:$("base").value,model:$("model").value,timezone:$("timezone").value,
      api_key:clear?"":(key||null)});
    fill(s);notice(clear?"本次 Key 已清除。":"设置已保存。",true);retryFollowup=true;
  }catch(e){notice(e.message);}
}
$("config").onsubmit=e=>{e.preventDefault();store();};
$("clear-key").onclick=()=>store(true);
$("clock").onsubmit=async e=>{
  e.preventDefault();const d=$("demo-date").value;if(!d)return;
  try{const result=await openChat(d);await navigate("chat");notice(result.followup?"演示日期 "+d+"：已触发一次回访。":"演示日期 "+d+"：没有新的到期事件。",true);}
  catch(error){notice(error.message);}
};
(async()=>{
 try{
   fill(await api("/settings"));
   renderMessages((await api("/chat")).messages);
   const last=shown.at(-1);
   if(last?.role==="user"&&last.request_id){pending={text:last.text,request_id:last.request_id};syncRetry();}
   try{await openChat();}catch(e){retryFollowup=true;notice(e.message);}
 }catch(e){notice(e.message);}
})();

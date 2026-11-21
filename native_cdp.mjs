// Only attaches to the explicitly selected local debugging port and Codex app target.
import {readFileSync} from 'node:fs';
import {createInterface} from 'node:readline';

const port=Number(process.argv[2]);
if(!Number.isInteger(port)||port<1024||port>65535)throw Error('Invalid local debugging port');
const script=readFileSync(new URL('./native.js',import.meta.url),'utf8');
const connections=new Map();
function emit(value){process.stdout.write(JSON.stringify(value)+'\n');}
class Connection{
  constructor(target){this.target=target;this.waiters=new Map();this.counter=0;this.ready=false;this.closed=false;}
  async connect(){
    const endpoint=new URL(this.target.webSocketDebuggerUrl);
    if(!['127.0.0.1','localhost'].includes(endpoint.hostname)||Number(endpoint.port)!==port)throw Error('Unexpected debugger endpoint');
    this.ws=new WebSocket(endpoint);
    await new Promise((resolve,reject)=>{this.ws.addEventListener('open',resolve,{once:true});this.ws.addEventListener('error',reject,{once:true});});
    this.ws.addEventListener('close',()=>{this.closed=true;for(const p of this.waiters.values())p.reject(Error('Debugger disconnected'));this.waiters.clear();});
    this.ws.addEventListener('message',event=>{
      let message;try{message=JSON.parse(event.data);}catch{return;}
      if(message.id){const p=this.waiters.get(message.id);if(p){this.waiters.delete(message.id);clearTimeout(p.timer);message.error?p.reject(Error(message.error.message)):p.resolve(message.result);}return;}
      if(message.method==='Runtime.bindingCalled'&&message.params.name==='__codexRewindNative'){
        try{const request=JSON.parse(message.params.payload);if(!request||typeof request.id!=='string')return;
          emit({type:'request',target:this.target.id,context:message.params.executionContextId,...request});
        }catch{}
      }
    });
    await this.call('Runtime.enable');
    await this.call('Page.enable');
    await this.call('Runtime.addBinding',{name:'__codexRewindNative'});
    this.scriptId=(await this.call('Page.addScriptToEvaluateOnNewDocument',{source:script})).identifier;
    const injected=await this.call('Runtime.evaluate',{expression:script,awaitPromise:true,returnByValue:true});
    if(injected.exceptionDetails)throw Error(injected.exceptionDetails.text);
    this.ready=true;emit({type:'injected',target:this.target.id,url:this.target.url});
  }
  call(method,params={}){
    return new Promise((resolve,reject)=>{
      const id=++this.counter;
      const timer=setTimeout(()=>{this.waiters.delete(id);reject(Error('CDP timeout: '+method));},20000);
      this.waiters.set(id,{resolve,reject,timer});this.ws.send(JSON.stringify({id,method,params}));
    });
  }
}

const input=createInterface({input:process.stdin});
input.on('line',async line=>{
  let message;try{message=JSON.parse(line);}catch{return;}
  const c=connections.get(message.target);if(!c?.ready)return;
  if(message.type==='response'){
    try{await c.call('Runtime.evaluate',{expression:`window.__codexRewindResolve?.(${JSON.stringify(message.id)},${JSON.stringify(message.result??null)},${JSON.stringify(message.error??null)})`,contextId:message.context});}catch(e){emit({type:'error',message:String(e)});}
  }else if(message.type==='inspect'){
    try{const result=await c.call('Runtime.evaluate',{expression:'JSON.stringify(window.__codexRewindDiagnostics||{})',returnByValue:true});emit({type:'diagnostics',target:message.target,value:result.result?.value});}catch(e){emit({type:'error',message:String(e)});}
  }else if(message.type==='cleanup'){
    try{await c.call('Runtime.evaluate',{expression:'window.__codexRewindCleanup?.()'});}catch{}
  }
});
let stopping=false;
async function stop(){
  if(stopping)return;stopping=true;
  await Promise.allSettled([...connections.values()].map(async c=>{
    if(!c.closed){
      await c.call('Runtime.evaluate',{expression:'window.__codexRewindCleanup?.()'});
      if(c.scriptId)await c.call('Page.removeScriptToEvaluateOnNewDocument',{identifier:c.scriptId});
      await c.call('Runtime.removeBinding',{name:'__codexRewindNative'});
    }
    c.ws?.close();
  }));
  process.exit(0);
}
input.on('close',stop);
process.on('SIGINT',stop);process.on('SIGTERM',stop);

let polling=false;
async function poll(){
  if(polling||stopping)return;polling=true;
  try{
    const response=await fetch(`http://127.0.0.1:${port}/json/list`,{signal:AbortSignal.timeout(3000)});
    if(!response.ok)throw Error('Debugger target listing failed');
    const targets=(await response.json()).filter(t=>t.type==='page'&&/^app:\/\/-\/index\.html(?:[?#]|$)/.test(t.url));
    for(const target of targets){
      const existing=connections.get(target.id);if(existing&&!existing.closed)continue;
      const connection=new Connection(target);connections.set(target.id,connection);
      try{await connection.connect();}catch(e){connection.closed=true;connection.ws?.close();emit({type:'error',message:String(e)});}
    }
  }catch(e){emit({type:'waiting',message:String(e)});}finally{polling=false;}
}
await poll();setInterval(poll,2000);

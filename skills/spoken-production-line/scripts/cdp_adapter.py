"""Portable CDP primitives; requires websocket-client, never persists credentials."""
import json,re,time
import websocket

class CDP:
    def __init__(self,url,timeout=30):
        self.ws=websocket.create_connection(url,timeout=timeout,suppress_origin=True);self.next_id=0
    def call(self,method,params=None,timeout=30):
        self.next_id+=1;mid=self.next_id;end=time.monotonic()+timeout
        self.ws.send(json.dumps({'id':mid,'method':method,'params':params or {}}))
        while time.monotonic()<end:
            self.ws.settimeout(max(.1,end-time.monotonic()));msg=json.loads(self.ws.recv())
            if msg.get('id')!=mid:continue
            if 'error' in msg:raise RuntimeError(f'CDP {method}: {msg["error"]}')
            return msg.get('result') or {}
        raise TimeoutError('CDP command deadline: '+method)
    def evaluate(self,expression,await_promise=True,timeout=30):
        r=self.call('Runtime.evaluate',{'expression':expression,'returnByValue':True,'awaitPromise':await_promise},timeout=timeout)
        if r.get('exceptionDetails'):raise RuntimeError('JavaScript exception: '+r['exceptionDetails'].get('text','unknown'))
        return r.get('result',{}).get('value')
    def close(self):
        try:self.ws.close()
        except Exception:pass

def visible_state(page):
    return json.loads(page.evaluate("""JSON.stringify({url:location.href,ready:document.readyState,
    editors:[...document.querySelectorAll('[contenteditable=true]')].map(e=>({aria:e.getAttribute('aria-label')||'',rect:e.getBoundingClientRect().toJSON(),text:e.innerText||''})).filter(x=>x.rect.width>0&&x.rect.height>0),
    removers:[...document.querySelectorAll('button')].map(b=>({label:b.getAttribute('aria-label')||'',title:b.title||'',rect:b.getBoundingClientRect().toJSON()})).filter(x=>/移除|remove/i.test(x.label+' '+x.title)&&x.rect.width>0&&x.rect.height>0),
    quotaNotice:(()=>{const t=(document.body?.innerText||'').replace(/\\s+/g,' ');const m=t.match(/你已达到文件附件数量限制|已达到上传上限|上传次数已达上限|upload limit reached|too many files/i);return m?t.slice(m.index,m.index+180):''})(),
    send:[...document.querySelectorAll('button[aria-label="发送"],button[aria-label="Send prompt"]')].map(b=>({disabled:b.disabled,rect:b.getBoundingClientRect().toJSON()})).filter(x=>x.rect.width>0&&x.rect.height>0)})"""))

def wait_for_removers(page,expected_stems,timeout=180):
    end=time.monotonic()+timeout;last=[]
    while time.monotonic()<end:
        s=visible_state(page)
        if s['quotaNotice']:raise RuntimeError('Official attachment limit: '+s['quotaNotice'])
        last=[r['label'] or r['title'] for r in s['removers']]
        if len(last)==len(expected_stems) and all(any(stem in x for x in last) for stem in expected_stems):return last
        if len(last)>len(expected_stems):raise RuntimeError('Unexpected extra attachments')
        time.sleep(2)
    raise TimeoutError('Attachment UI did not confirm exact set: '+repr(last))

def api_snapshot(page):
    url=page.evaluate('location.href');m=re.search(r'/c/([A-Za-z0-9-]+)',url or '')
    if not m:return None
    js="""(async()=>{const s=await fetch('/api/auth/session').then(r=>r.json());const r=await fetch('/backend-api/conversation/'+CID,{headers:{Authorization:'Bearer '+s.accessToken}});if(!r.ok)return JSON.stringify({ok:false,status:r.status,retryAfter:r.headers.get('Retry-After')});const d=await r.json();let n=d.current_node;const out=[];while(n&&d.mapping[n]){const x=d.mapping[n],m=x.message;if(m)out.push({id:m.id,role:m.author?.role,status:m.status,text:(m.content?.parts||[]).filter(x=>typeof x==='string').join(' '),attachments:(m.metadata?.attachments||[]).map(x=>x.name||x.filename||'')});n=x.parent}return JSON.stringify({ok:true,id:d.conversation_id,title:d.title,messages:out.reverse()})})()""".replace('CID',json.dumps(m.group(1)))
    return json.loads(page.evaluate(js,timeout=45))

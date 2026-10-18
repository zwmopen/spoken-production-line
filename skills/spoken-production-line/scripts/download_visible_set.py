"""Save the current generated image set without navigating or altering its chat."""
import argparse,sys,json,pathlib,urllib.request,base64,hashlib,io,time
from PIL import Image
import cdp_adapter as c
def main():
    ap=argparse.ArgumentParser();ap.add_argument('--port',type=int,required=True);ap.add_argument('--out',required=True);ap.add_argument('--expected',type=int,required=True);ap.add_argument('--conversation-id',required=True);a=ap.parse_args();sys.stdout.reconfigure(encoding='utf-8')
    ts=json.load(urllib.request.urlopen(f'http://127.0.0.1:{a.port}/json/list',timeout=5));t=next(t for t in ts if t['type']=='page' and a.conversation_id in t['url']);p=c.CDP(t['webSocketDebuggerUrl']);out=pathlib.Path(a.out);out.mkdir(parents=True,exist_ok=True);rows=[]
    try:
        urls=json.loads(p.evaluate("JSON.stringify([...new Set([...document.images].filter(i=>i.naturalWidth>=500&&i.alt!=='用户附件').map(i=>i.currentSrc||i.src))])"))
        if len(urls)!=a.expected:raise RuntimeError(f'Visible unique generated images {len(urls)} != expected {a.expected}; select proper reply before download')
        for i,url in enumerate(urls,1):
            dest=out/f'P{i}.png'
            if dest.exists():
                with Image.open(dest) as im:im.verify()
                rows.append({'page':i,'path':str(dest),'sha256':hashlib.sha256(dest.read_bytes()).hexdigest(),'resumed':True});continue
            js="""(async()=>{const r=await fetch(IMAGE_SOURCE);if(!r.ok)throw Error('Image fetch '+r.status);const b=await r.blob();return await new Promise((resolve,reject)=>{const f=new FileReader();f.onload=()=>resolve(f.result);f.onerror=reject;f.readAsDataURL(b)})})()""".replace('IMAGE_SOURCE',json.dumps(url))
            data=p.evaluate(js,timeout=90);raw=base64.b64decode(data.split(',',1)[1]);im=Image.open(io.BytesIO(raw));im.load()
            if im.format!='PNG':raise RuntimeError('Non-PNG output requires matching extension; refusing silent conversion')
            dest.write_bytes(raw);rows.append({'page':i,'path':str(dest),'sha256':hashlib.sha256(raw).hexdigest(),'width':im.width,'height':im.height,'bytes':len(raw),'downloadedAt':time.time()})
            (out/'download-receipts.json').write_text(json.dumps(rows,ensure_ascii=False,indent=2),encoding='utf-8');print(json.dumps({'page':i,'bytes':len(raw),'width':im.width,'height':im.height}),flush=True)
    finally:p.close()
if __name__=='__main__':main()

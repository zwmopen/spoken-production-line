"""Download verified generated asset IDs from URLs already loaded by the chat.
Signed URLs and credentials remain in the browser; only image bytes leave CDP.
"""
import argparse,sys,json,urllib.request,base64,hashlib,io,time,re
from pathlib import Path
from PIL import Image
from cdp_adapter import CDP


def verify_terminal_asset_set(page, conversation_id, assistant_id, expected_assets, start_message_id=None, context_assets=()):
 js = """(async()=>{const s=await fetch('/api/auth/session').then(r=>r.json());
 const r=await fetch('/backend-api/conversation/'+CID,{headers:{Authorization:'Bearer '+s.accessToken}});
 if(!r.ok)return JSON.stringify({ok:false,status:r.status});const d=await r.json();
 let n=d.current_node,out=[];while(n&&d.mapping[n]){const m=d.mapping[n].message;if(m)out.push(m);n=d.mapping[n].parent}
 out.reverse();const stop=START;const k=out.findIndex(m=>m.id===stop&&m.author?.role==='user');
 const latest=[...out].reverse().find(m=>m.author?.role==='assistant')||{};
 const raw=JSON.stringify(k>=0?out.slice(k+1).map(m=>m.content):[]);
 const pointers=[...new Set(raw.match(/sediment:\/\/[A-Za-z0-9_-]+/g)||[])];
 return JSON.stringify({ok:true,id:d.conversation_id,title:d.title,startFound:k>=0,
 latestId:latest.id,latestStatus:latest.status,pointers})})()""".replace(
  'CID', json.dumps(conversation_id)
 ).replace('START', json.dumps(start_message_id))
 snapshot = json.loads(page.evaluate(js, timeout=45))
 if not snapshot or not snapshot.get('ok') or snapshot.get('id') != conversation_id:
  raise RuntimeError('Exact server conversation unavailable; no download')
 if (assistant_id and snapshot.get('latestId') != assistant_id) or snapshot.get('latestStatus') != 'finished_successfully':
  raise RuntimeError('Latest reply identity or terminal state changed')
 if not snapshot.get('startFound'):
  raise RuntimeError('Generation start message not found in exact server conversation')
 pointers = set(snapshot.get('pointers') or [])
 wanted = {'sediment://' + asset.replace('sediment://', '') for asset in expected_assets}
 context = {'sediment://' + asset.replace('sediment://', '') for asset in context_assets}
 if not wanted or len(wanted) != len(expected_assets) or wanted & context:
  raise RuntimeError('Expected assets must be unique and disjoint from previously accepted context assets')
 permitted = wanted | context
 if pointers != permitted:
  raise RuntimeError(f'Generation turn asset set mismatch: expected {len(wanted)} new + {len(context)} accepted context assets, found {len(pointers)}')
 return {'id': snapshot.get('latestId'), 'status': snapshot.get('latestStatus')}

def download_latest_terminal_blob(page, conversation_id, assistant_id, expected_assets, start_message_id=None, context_assets=(), image_index=0):
 verify_terminal_asset_set(page, conversation_id, assistant_id, expected_assets, start_message_id, context_assets)
 js = """(async()=>{const galleries=[...document.querySelectorAll('[data-testid="generated-image-gallery"]')];const gallery=galleries.at(-1);if(!gallery)throw Error('No generated gallery');const imgs=[...gallery.querySelectorAll('img')].filter(i=>(i.currentSrc||i.src).startsWith('blob:')&&i.naturalWidth>=500);const urls=[...new Set(imgs.map(i=>i.currentSrc||i.src))];if(urls.length!==EXPECTED_COUNT)throw Error('Latest gallery candidate count mismatch');const index=IMAGE_INDEX;if(index<0||index>=urls.length)throw Error('Candidate index out of range');const r=await fetch(urls[index]);if(!r.ok)throw Error('Blob download failed');const blob=await r.blob();return await new Promise((resolve,reject)=>{const reader=new FileReader();reader.onload=()=>resolve(reader.result);reader.onerror=reject;reader.readAsDataURL(blob)})})()""".replace('EXPECTED_COUNT',str(len(expected_assets))).replace('IMAGE_INDEX',str(image_index))
 return base64.b64decode(page.evaluate(js, timeout=60).split(',', 1)[1])


def download_exact_file(page, asset_id):
 # Resolve only an asset already verified against this terminal generation turn.
 # Session token and temporary signed URL remain inside the authenticated page.
 js = """(async()=>{const s=await fetch('/api/auth/session').then(r=>r.json());const r=await fetch('/backend-api/files/'+FILE_ID+'/download',{headers:{Authorization:'Bearer '+s.accessToken}});if(!r.ok)throw Error('Exact file lookup HTTP '+r.status);const d=await r.json();if(!d.download_url)throw Error('Exact file has no download URL');const b=await fetch(d.download_url);if(!b.ok)throw Error('Exact file bytes HTTP '+b.status);const blob=await b.blob();return await new Promise((resolve,reject)=>{const f=new FileReader();f.onload=()=>resolve(f.result);f.onerror=reject;f.readAsDataURL(blob)})})()""".replace('FILE_ID',json.dumps(asset_id.replace('sediment://','')))
 return base64.b64decode(page.evaluate(js,timeout=90).split(',',1)[1])

def connect_authenticated_page(port, conversation_id):
 targets=json.load(urllib.request.urlopen(f'http://127.0.0.1:{port}/json/list',timeout=5))
 pages=[t for t in targets if t.get('type')=='page' and t.get('url','').startswith('https://chatgpt.com/')]
 exact=[t for t in pages if conversation_id in t.get('url','')]
 target=(exact or pages)
 if not target: raise RuntimeError('No authenticated ChatGPT page for this account profile')
 return CDP(target[0]['webSocketDebuggerUrl'],timeout=10)

def main():
 ap=argparse.ArgumentParser();ap.add_argument('--port',type=int,required=True);ap.add_argument('--assets',required=True);ap.add_argument('--known-context-assets',help='JSON list of previously accepted asset pointers explicitly referenced after this generation start');ap.add_argument('--out',required=True);ap.add_argument('--conversation-id',required=True);ap.add_argument('--start-message-id');a=ap.parse_args();sys.stdout.reconfigure(encoding='utf-8')
 assets=json.loads(Path(a.assets).read_text(encoding='utf-8-sig'));context_assets=json.loads(Path(a.known_context_assets).read_text(encoding='utf-8-sig')) if a.known_context_assets else [];assert len(assets)==len(set(assets)) and assets and len(context_assets)==len(set(context_assets)) and not set(assets)&set(context_assets)
 out=Path(a.out);out.mkdir(parents=True,exist_ok=True);receipt_path=out/'download-receipts.json';old_receipts=[]
 if receipt_path.exists():
  try:old_receipts=json.loads(receipt_path.read_text(encoding='utf-8-sig'))
  except Exception:old_receipts=[]
 old_by_page={int(r.get('page',0)):r for r in old_receipts if isinstance(r,dict)}
 receipts=[];p=None;stage_started=time.time()
 try:
  latest=None;initial_error=None
  for initial_attempt in range(3):
   try:
    p=connect_authenticated_page(a.port,a.conversation_id)
    latest=verify_terminal_asset_set(p,a.conversation_id,None,assets,a.start_message_id,context_assets)
    break
   except Exception as exc:
    initial_error=exc
   finally:
    if p:
     try:p.close()
     except Exception:pass
     p=None
   if initial_attempt<2:time.sleep(1)
  if latest is None:raise RuntimeError(f'Could not verify terminal generation after bounded reconnects: {type(initial_error).__name__}: {initial_error}')
  seen_hashes=set()
  for i,asset in enumerate(assets,1):
   prior=old_by_page.get(i)
   if prior and prior.get('assetId')==asset and prior.get('path') and Path(prior['path']).is_file():
    existing=Path(prior['path']);existing_sha=hashlib.sha256(existing.read_bytes()).hexdigest()
    if existing_sha==prior.get('sha256'):
     if existing_sha in seen_hashes:raise RuntimeError('Two generated pages have identical bytes; refusing duplicate-page acceptance')
     seen_hashes.add(existing_sha);receipts.append(prior);print(json.dumps({'page':i,'resumedVerified':True}),flush=True);continue
   raw=None;last_error=None
   for connection_attempt in range(3):
    try:
     p=connect_authenticated_page(a.port,a.conversation_id)
     verify_terminal_asset_set(p,a.conversation_id,latest.get('id'),assets,a.start_message_id,context_assets)
     raw=download_exact_file(p,asset)
     last_error=None
     break
    except Exception as exc:
     last_error=exc
    finally:
     if p:
      try:p.close()
      except Exception:pass
      p=None
    if connection_attempt<2:time.sleep(1)
   if raw is None:raise RuntimeError(f'Exact verified download failed for page {i}: {type(last_error).__name__}: {last_error}')
   method='verified_exact_file_id_download'
   im=Image.open(io.BytesIO(raw));im.load();ext={'PNG':'.png','JPEG':'.jpg','WEBP':'.webp'}.get(im.format)
   if not ext or min(im.size)<500:raise RuntimeError('Not full output image')
   dest=out/f'P{i}{ext}'
   sha=hashlib.sha256(raw).hexdigest()
   if sha in seen_hashes:raise RuntimeError('Two generated pages have identical bytes; refusing duplicate-page acceptance')
   seen_hashes.add(sha)
   if dest.exists() and hashlib.sha256(dest.read_bytes()).hexdigest()!=sha:raise RuntimeError('Existing page differs; refusing overwrite')
   dest.write_bytes(raw);receipts.append({'page':i,'path':str(dest),'assetId':asset,'sha256':sha,'width':im.width,'height':im.height,'bytes':len(raw),'downloadedAt':time.time(),'bindingMethod':method})
   receipt_path.write_text(json.dumps(receipts,ensure_ascii=False,indent=2),encoding='utf-8');print(json.dumps({'page':i,'bytes':len(raw),'width':im.width,'height':im.height}),flush=True)
 finally:
  (out/'download-stage.json').write_text(json.dumps({'stage':'download','startedAtEpoch':stage_started,'endedAtEpoch':time.time(),'durationSeconds':round(time.time()-stage_started,3),'expectedImages':len(assets),'downloadedImages':len(receipts),'outcome':'completed' if len(receipts)==len(assets) else 'failed'},ensure_ascii=False,indent=2),encoding='utf-8')
  if p:
   try:p.close()
   except Exception:pass
if __name__=='__main__':main()

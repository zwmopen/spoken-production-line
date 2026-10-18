import pathlib,json,datetime,requests,hashlib,base64,sys
import os
R=pathlib.Path(os.environ.get('SPL_TASK_ROOT','./data/tasks')).resolve()
R.mkdir(parents=True,exist_ok=True)
STATE=R/'Cockpit-生产断点.json'
def now():return datetime.datetime.now().astimezone().isoformat()
def save(d):STATE.write_text(json.dumps(d,ensure_ascii=False,indent=2),encoding='utf8')
def run():
 d=json.loads(STATE.read_text(encoding='utf8'))
 if d['stage']=='QUOTA_WAIT':
  if datetime.datetime.now().astimezone()<datetime.datetime.fromisoformat(d['nextRunAt']):print('QUOTA_WAIT until '+d['nextRunAt']);return
  d['stage']='RETRY_DUE'
 if d['stage'] not in ['READY','RETRY_DUE']:print(d['stage']);return
 cfg={'apiKey':os.environ['COCKPIT_API_KEY']}
 paths=[pathlib.Path(x) for x in d['references']]
 files=[('image[]',(p.name,p.open('rb'),'image/jpeg')) for p in paths]
 d.update(stage='SUBMITTING',submittedAt=now());save(d)
 try:
  content=[{'type':'input_text','text':d.get('activePrompt',d['prompt'])}]+[{'type':'input_image','image_url':'data:image/jpeg;base64,'+base64.b64encode(p.read_bytes()).decode()} for p in paths]
  res=requests.post(os.environ.get('COCKPIT_BASE_URL','http://localhost:51612/v1').rstrip('/')+'/responses',headers={'Authorization':'Bearer '+cfg['apiKey']},json={'model':os.environ['COCKPIT_IMAGE_MODEL'],'input':[{'role':'user','content':content}],'tools':[{'type':'image_generation','output_format':'jpeg','output_compression':65,'size':'1024x1536'}],'tool_choice':{'type':'image_generation'},'stream':False},timeout=(15,900))
  try:body=res.json()
  except ValueError:
   (R/(d['workId']+'-response-recovery.txt')).write_text(res.text,encoding='utf8')
   raise RuntimeError('Response was not JSON; raw response retained for recovery; do not resend')
  info={'httpStatus':res.status_code,'observedAt':now(),'headers':{k:v for k,v in res.headers.items() if k.lower() in ['retry-after','x-request-id']}}
  if res.status_code!=200:
   info['error']=body
   text=json.dumps(body,ensure_ascii=False).lower()
   quota=res.status_code==429 or any(x in text for x in ['quota_exceeded','usage_limit','rate_limit_exceeded','insufficient_quota','rate limit','usage limit','image generation limit','quota exhausted'])
   stage='QUOTA_WAIT' if quota else 'AUTH_OR_PERMISSION_ERROR' if res.status_code in [401,403] else 'HTTP_ERROR'
   d.update(stage=stage,lastResponse=info)
   if quota:
    step=min(d.get('cooldownStep',0),2);hours=[3,6,12][step];d['cooldownStep']=step+1
    retry=res.headers.get('Retry-After'); seconds=int(retry) if retry and retry.isdigit() else hours*3600
    d['nextRunAt']=(datetime.datetime.now().astimezone()+datetime.timedelta(seconds=seconds)).isoformat();d['resetSource']='Retry-After' if retry and retry.isdigit() else 'user_authorized_backoff'
   save(d);print(json.dumps(info,ensure_ascii=False));return
  out=R/d['workId'];out.mkdir(exist_ok=True);assets=d.get('assets',[])
  for i,a in enumerate(body.get('data') or [{'b64_json':x['result']} for x in body.get('output',[]) if x.get('type')=='image_generation_call' and x.get('result')],1):
   raw=base64.b64decode(a['b64_json']) if a.get('b64_json') else requests.get(a['url'],timeout=90).content
   p=out/f"P{d.get('activePage',1)+i-1}.jpg";p.write_bytes(raw);assets.append({'path':str(p),'sha256':hashlib.sha256(raw).hexdigest(),'bytes':len(raw)})
  d.update(stage='IMAGES_DOWNLOADED' if assets else 'NO_IMAGE_RESULT',assets=assets,completedAt=now(),lastResponse=info,responseId=body.get('id'),responseSummary=[{k:v for k,v in x.items() if k not in ['result','partial_image_b64']} for x in body.get('output',[])]);save(d);print(json.dumps({'stage':d['stage'],'count':len(assets),'status':res.status_code}))
 except Exception as e:d.update(stage='SUBMISSION_UNKNOWN',error=type(e).__name__+': '+str(e)[:300]);save(d);print(d['stage'])
 finally:
  for _,(_,f,_) in files:f.close()
if __name__=='__main__':
 import msvcrt
 lock=open(R/'Cockpit-writer.lock','a+b');lock.seek(0);lock.write(b'1');lock.flush();lock.seek(0)
 try:msvcrt.locking(lock.fileno(),msvcrt.LK_NBLCK,1)
 except OSError:print('WRITER_ALREADY_ACTIVE');sys.exit(0)
 try:run()
 finally:lock.seek(0);msvcrt.locking(lock.fileno(),msvcrt.LK_UNLCK,1);lock.close()

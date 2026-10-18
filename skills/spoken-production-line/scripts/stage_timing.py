"""Per-work stage spans; unknown/unpaired time is never attributed to a phase."""
import argparse,datetime,json,os,statistics,time,uuid
from pathlib import Path

class StageTimer:
    def __init__(self,root,work_id,account,phase,category='active'):
        self.root=Path(root);self.work=work_id;self.account=account;self.phase=phase;self.category=category;self.span=str(uuid.uuid4())
    def emit(self,event,**extra):
        folder=self.root/'阶段耗时事件';folder.mkdir(parents=True,exist_ok=True)
        safe=uuid.uuid5(uuid.NAMESPACE_URL,self.work).hex
        payload={'workId':self.work,'account':self.account,'phase':self.phase,'category':self.category,'spanId':self.span,'event':event,'at':datetime.datetime.now(datetime.timezone.utc).isoformat(),**extra}
        fd=os.open(folder/(safe+'.jsonl'),os.O_WRONLY|os.O_CREAT|os.O_APPEND)
        try:os.write(fd,(json.dumps(payload,ensure_ascii=False)+'\n').encode('utf-8'))
        finally:os.close(fd)
        return payload
    def __enter__(self):self.started=time.perf_counter();self.emit('start');return self
    def __exit__(self,kind,value,tb):self.emit('end',seconds=round(time.perf_counter()-self.started,3),outcome='failed' if kind else 'completed',errorType=kind.__name__ if kind else None)


def analyze(root):
    root=Path(root);spans={};rows=[]
    for p in (root/'阶段耗时事件').glob('*.jsonl'):
        for line in p.read_text(encoding='utf-8').splitlines():
            try:e=json.loads(line)
            except ValueError:continue
            spans.setdefault(e['spanId'],{})[e['event']]=e
    for span in spans.values():
        start=span.get('start');end=span.get('end')
        if not start:continue
        row={k:start[k] for k in ['workId','account','phase','category','spanId']}
        row['startedAt']=start['at'];row['endedAt']=end['at'] if end else None
        row['seconds']=end.get('seconds') if end else None
        if end and row['seconds'] is None:row['seconds']=(datetime.datetime.fromisoformat(end['at'])-datetime.datetime.fromisoformat(start['at'])).total_seconds()
        row['outcome']=end.get('outcome','completed') if end else 'in_progress';rows.append(row)
    summary={}
    for phase in {r['phase'] for r in rows}:
        sample=[r['seconds'] for r in rows if r['phase']==phase and r['outcome']=='completed' and isinstance(r['seconds'],(float,int))]
        vals=sorted(sample);n=len(vals)
        summary[phase]={'n':n,'medianSeconds':statistics.median(vals) if n else None,'p90Seconds':vals[min(n-1,int((n-1)*.9))] if n else None,'baselineReady':n>=10}
    result={'updatedAt':datetime.datetime.now(datetime.timezone.utc).isoformat(),'scope':'仅明确记录的阶段跨度；不把未知间隔分摊给下载/验收','phases':summary,'records':rows,'investigate':[]}
    now=datetime.datetime.now(datetime.timezone.utc)
    for row in rows:
        base=summary.get(row['phase'],{})
        if row['outcome']=='in_progress' and base.get('baselineReady'):
            age=(now-datetime.datetime.fromisoformat(row['startedAt'])).total_seconds()
            if age>max(300,base['p90Seconds']*1.5):result['investigate'].append({'spanId':row['spanId'],'workId':row['workId'],'phase':row['phase'],'ageSeconds':int(age),'reason':'超过同阶段实测p90的1.5倍；需查现场，不自动判故障'})
    (root/'阶段耗时统计.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    return result

if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--task-root',required=True);p.add_argument('--action',choices=['start','end','analyze'],required=True)
    p.add_argument('--work-id');p.add_argument('--account');p.add_argument('--phase');p.add_argument('--span-id');p.add_argument('--category',default='active')
    a=p.parse_args()
    if a.action=='analyze':print(json.dumps({'records':len(analyze(a.task_root)['records'])}))
    else:
        if not all([a.work_id,a.account,a.phase]):p.error('record requires work-id/account/phase')
        t=StageTimer(a.task_root,a.work_id,a.account,a.phase,a.category)
        if a.action=='end':
            if not a.span_id:p.error('end requires span-id from matching start')
            t.span=a.span_id
        print(json.dumps(t.emit(a.action),ensure_ascii=True))

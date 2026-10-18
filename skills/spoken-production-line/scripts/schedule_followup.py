"""Persist one delayed eligibility check; does not probe quota or submit generation."""
import argparse, datetime, json, random
from pathlib import Path

def schedule(root, account, stage, event_id, eligible_at):
    root=Path(root); path=root/'复查安排.json'
    d=json.loads(path.read_text(encoding='utf-8-sig')) if path.exists() else {'records':[]}
    key=f'{account}:{stage}:{event_id}'
    old=next((r for r in d['records'] if r['key']==key and r['eligibleAt']==eligible_at),None)
    if old:return old
    at=datetime.datetime.fromisoformat(eligible_at.replace('Z','+00:00'))
    if at.tzinfo is None:raise ValueError('eligibleAt requires explicit timezone')
    delay=random.SystemRandom().randint(5,15)
    result={'key':key,'account':account,'stage':stage,'eventId':event_id,'eligibleAt':eligible_at,
            'delayMinutes':delay,'checkAt':(at+datetime.timedelta(minutes=delay)).isoformat(),
            'createdAt':datetime.datetime.now(datetime.timezone.utc).isoformat(), 'automationScheduled':False,
            'meaning':'到点复查，不保证额度已恢复；不自动发送试探请求'}
    d['records']=[r for r in d['records'] if r['key']!=key]+[result]
    path.write_text(json.dumps(d,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    return result

if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    for key in ('task-root','account','stage','event-id','eligible-at'):p.add_argument('--'+key,required=True)
    a=p.parse_args(); print(json.dumps(schedule(a.task_root,a.account,a.stage,a.event_id,a.eligible_at),ensure_ascii=True))

"""Reconcile unknown start receipts without sending any production command."""
import argparse,json,time,urllib.request,sys
from pathlib import Path
import cdp_adapter as c
def main():
    ap=argparse.ArgumentParser();ap.add_argument('--queue',required=True);a=ap.parse_args();sys.stdout.reconfigure(encoding='utf-8')
    path=Path(a.queue);q=json.loads(path.read_text(encoding='utf-8-sig'));pending=[r for r in q['tasks'] if r.get('generationStatus')=='START_CLICKED_NOT_VERIFIED']
    if not pending:print('No pending start receipts');return
    port=q.get('cdpPort',9431 if q['account']=='A' else 9432);ts=json.load(urllib.request.urlopen(f'http://127.0.0.1:{port}/json/list',timeout=5));t=next(t for t in ts if t['type']=='page' and (t['url'].startswith('https://chatgpt.com/') or t['url']=='about:blank'));page=c.CDP(t['webSocketDebuggerUrl'])
    try:
        for r in pending:
            if time.time()<r.get('readbackRetryAt',0):print('Readback cooldown still active');break
            page.call('Page.navigate',{'url':r['conversationUrl']});time.sleep(4);snap=c.api_snapshot(page)
            if not snap or not snap.get('ok'):
                delay=(snap or {}).get('retryAfter');delay=int(delay) if str(delay).isdigit() else 60
                r.update(readbackRetryAt=time.time()+max(5,delay),readbackHttpStatus=(snap or {}).get('status'));path.write_text(json.dumps(q,ensure_ascii=False,indent=2),encoding='utf-8');print(json.dumps({'id':r['id'],'retryAt':r['readbackRetryAt'],'status':r.get('readbackHttpStatus')}),flush=True);break
            ms=snap['messages'];idx=next((i for i,m in enumerate(ms) if m['id']==r['planMessageId']),None);sent=[m for m in ms[idx+1:] if m['role']=='user' and m['text'].strip()=='1'] if idx is not None else []
            if sent:r.update(generationStatus='START_SUBMITTED',startMessageId=sent[0]['id'],readbackConfirmedAt=time.time())
            path.write_text(json.dumps(q,ensure_ascii=False,indent=2),encoding='utf-8');print(json.dumps({'id':r['id'],'confirmed':bool(sent)}),flush=True)
            if not sent:break
            # Preserve the completed conversation page; never navigate to blank for cleanup.
    finally:page.close()
if __name__=='__main__':main()

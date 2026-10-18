"""Restart only an explicitly verified idle workbench, keeping its own profile."""
import argparse,json,urllib.request,datetime
from cdp_adapter import CDP

def main():
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--port',type=int,required=True)
    ap.add_argument('--evidence',required=True)
    ap.add_argument('--execute',action='store_true')
    args=ap.parse_args()
    evidence=json.load(open(args.evidence,encoding='utf-8-sig'))
    for key in ('exclusiveOwnerVerified','serverTerminalVerified','draftClearVerified','noPendingSubmissionVerified'):
        if evidence.get(key) is not True: raise RuntimeError('Missing verified prerequisite: '+key)
    if evidence.get('cdpPort')!=args.port: raise RuntimeError('Account port mismatch')
    observed=datetime.datetime.fromisoformat(evidence.get('observedAt','').replace('Z','+00:00'))
    if observed.tzinfo is None: raise RuntimeError('Evidence timestamp must include timezone')
    age=(datetime.datetime.now(datetime.timezone.utc)-observed).total_seconds()
    if age < 0 or age > 300: raise RuntimeError('Idle evidence older than five minutes; recheck first')
    if not evidence.get('workId') or not evidence.get('owner'): raise RuntimeError('Missing task/owner identity')
    targets=json.load(urllib.request.urlopen(f'http://127.0.0.1:{args.port}/json/list',timeout=5))
    candidates=[t for t in targets if t.get('type')=='page' and 'chatgpt.com' not in t.get('url','') and '/?' in t.get('url','')]
    result=None
    for target in candidates:
        page=CDP(target['webSocketDebuggerUrl'],timeout=5)
        try:
            bridge=page.evaluate("typeof window.gptWorkbench?.restartApp === 'function'",timeout=5)
            if not bridge: continue
            if args.execute:
                result=page.evaluate("window.gptWorkbench.restartApp({source:'spoken-verified-idle-recovery',interactive:false})",timeout=5)
            else: result={'available':True,'executed':False}
            break
        finally:page.close()
    if result is None: raise RuntimeError('No responsive supported restart bridge; use bounded host recovery')
    print(json.dumps(result,ensure_ascii=False))

if __name__=='__main__': main()

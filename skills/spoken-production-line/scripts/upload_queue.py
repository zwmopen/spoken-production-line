"""Resume an upload-plan queue through the configured, existing CDP adapter."""
import argparse,json,sys,time,urllib.request,hashlib,random,re
from collections import Counter
from pathlib import Path
from urllib.parse import urlparse
from upload_budget import reserve,mark,default_db,get_event

MAX_FILES_PER_SELECTION=10

def canonical_filename(value):
    value=re.sub(r'^(?:移除|Remove)\s*','',value,flags=re.I).strip('“”" ')
    p=Path(value)
    return re.sub(r'\((?:\d+|\d{8}-\d{6})\)$','',p.stem)+p.suffix.lower()

def split_upload_batches(files,max_files=MAX_FILES_PER_SELECTION):
    if max_files<1:raise ValueError('max_files must be positive')
    return [files[i:i+max_files] for i in range(0,len(files),max_files)]

def exact_attachment_set(labels,expected_files):
    return Counter(map(canonical_filename,labels))==Counter(canonical_filename(Path(f).name) for f in expected_files)

def normalize_prompt(value):
    """Normalize editor-only whitespace without changing words or punctuation."""
    text=' '.join((value or '').split())
    return re.sub(r'([：:])\s+(?=https?://)',r'\1',text)

def server_prompt_matches(expected,actual):
    """Accept exact user text plus platform-added file metadata around it."""
    expected=normalize_prompt(expected);actual=normalize_prompt(actual)
    return bool(expected) and (actual==expected or expected in actual)

def validate_resumable_draft(row,state,url,reservation):
    """Validate an existing unsent composer without touching its page or files."""
    if row.get('status')!='DRAFT_READY_NOT_SUBMITTED':
        raise RuntimeError('Queue row is not marked as a resumable unsent draft')
    verify_template_target(row,url)
    if state.get('quotaNotice'):
        raise RuntimeError('Official attachment limit: '+state['quotaNotice'])
    expected_aria=row.get('expectedComposerAria')
    editors=[e for e in state.get('editors',[]) if e.get('aria')==expected_aria]
    if len(editors)!=1:
        raise RuntimeError('Expected composer is not the unique visible target')
    labels=[r.get('label') or r.get('title') or '' for r in state.get('removers',[])]
    if not exact_attachment_set(labels,row.get('files',[])):
        raise RuntimeError('Resumable draft attachment set does not match the queue')
    if normalize_prompt(editors[0].get('text'))!=normalize_prompt(row.get('prompt')):
        raise RuntimeError('Resumable draft prompt does not match the queue')
    send=state.get('send') or []
    if len(send)!=1 or send[0].get('disabled'):
        raise RuntimeError('Resumable draft has no unique enabled Send control')
    if not reservation or reservation.get('count')!=len(row.get('files',[])):
        raise RuntimeError('Resumable draft has no matching upload reservation')
    if reservation.get('status') not in {'reserved','failed_or_unknown'}:
        raise RuntimeError('Resumable draft upload reservation has an unexpected state')
    attempt_id=row.get('currentAttemptId')
    if not attempt_id:
        raise RuntimeError('Resumable draft has no existing attempt ID')
    return attempt_id

def wait_for_exact_attachments(ui,page,expected_files,timeout=180):
    expected=Counter(canonical_filename(Path(f).name) for f in expected_files)
    end=time.monotonic()+timeout;last=[]
    while time.monotonic()<end:
        state=ui.visible_state(page)
        if state['quotaNotice']:raise RuntimeError('Official attachment limit: '+state['quotaNotice'])
        last=[r['label'] or r['title'] for r in state['removers']]
        actual=Counter(map(canonical_filename,last))
        if actual==expected:return last
        if any(actual[name]>expected[name] for name in actual):raise RuntimeError('Unexpected extra attachments: '+repr(last))
        time.sleep(2)
    raise TimeoutError('Attachment UI did not confirm exact set: '+repr(last))

def next_attempt_id(work_id,history):
    suffix=re.compile(re.escape(work_id)+r'-attempt-(\d+)$')
    numbers=[int(match.group(1)) for item in history if (match:=suffix.fullmatch(item.get('attemptId','')))]
    return f"{work_id}-attempt-{max(numbers,default=1)+1:02d}"

def reserve_upload_attempt(ledger,account,row,budget):
    history=row.setdefault('attemptHistory',[])
    attempt_id=row.get('currentAttemptId') or row['id']
    quota=reserve(ledger,account,attempt_id,len(row['files']),budget)
    if not quota['allowed'] and quota.get('reason')=='existing_attempt_reconcile':
        if not any(item.get('attemptId')==attempt_id for item in history):
            history.append({'attemptId':attempt_id,'uploadStartedAt':row.get('uploadStartedAt'),
                            'status':row.get('status'),'stage':row.get('stage'),
                            'error':row.get('error'),'mountedCount':row.get('mountedCount')})
        attempt_id=next_attempt_id(row['id'],history)
        quota=reserve(ledger,account,attempt_id,len(row['files']),budget)
    row['currentAttemptId']=attempt_id
    row['uploadBudget']=quota
    return attempt_id,quota

def custom_gpt_route(url):
    """Return the exact custom-GPT route segment, excluding /c/<conversation>."""
    parts=[part for part in urlparse(url or '').path.split('/') if part]
    try:
        index=parts.index('g')
    except ValueError:
        return None
    if index+1>=len(parts) or not parts[index+1].startswith('g-p-'):
        return None
    return parts[index+1]

def conversation_id(url):
    parts=[part for part in urlparse(url or '').path.split('/') if part]
    try:
        index=parts.index('c')
    except ValueError:
        return None
    if index+1>=len(parts) or not re.fullmatch(r'[A-Za-z0-9-]+',parts[index+1]):
        return None
    return parts[index+1]

def verify_template_target(row,url,after_send=False):
    """Bind uploads to either one exact existing chat or a fresh custom-GPT chat."""
    if row.get('newConversation'):
        expected=row.get('expectedGptRoute') or custom_gpt_route(row.get('templateUrl'))
        if not expected or custom_gpt_route(url)!=expected:
            raise RuntimeError('Wrong custom GPT route for new conversation')
        if after_send and not conversation_id(url):
            raise RuntimeError('New conversation URL not created; submission remains unverified')
        return
    expected=conversation_id(row.get('templateUrl'))
    if not expected or conversation_id(url)!=expected:
        raise RuntimeError('Wrong template conversation')

def main():
    ap=argparse.ArgumentParser();ap.add_argument('--queue',required=True);ap.add_argument('--adapter');args=ap.parse_args()
    sys.stdout.reconfigure(encoding='utf-8')
    if args.adapter:
        sys.path.insert(0,args.adapter)
        import sync_batch as core
        import sync_batch_direct_ui as ui
    else:
        import cdp_adapter as core
        import cdp_adapter as ui
    # Continuous browser events must not extend a command timeout indefinitely.
    def bounded_call(self,method,params=None,timeout=30):
        self.next_id+=1;mid=self.next_id;deadline=time.monotonic()+timeout
        self.ws.send(json.dumps({'id':mid,'method':method,'params':params or {}}))
        while time.monotonic()<deadline:
            self.ws.settimeout(max(.1,deadline-time.monotonic()))
            msg=json.loads(self.ws.recv())
            if msg.get('id')!=mid:continue
            if 'error' in msg:raise RuntimeError(f'CDP {method}: {msg["error"]}')
            return msg.get('result') or {}
        raise TimeoutError('CDP command deadline: '+method)
    core.CDP.call=bounded_call
    path=Path(args.queue);data=json.loads(path.read_text(encoding='utf-8-sig'))
    budget=min(80,int(data.get('rollingUploadThreshold',80)))
    ledger=Path(data.get('uploadLedgerPath') or default_db())
    account_key=str(data.get('accountIdentity') or data['account'])
    def save():
        tmp=path.with_suffix('.tmp');tmp.write_text(json.dumps(data,ensure_ascii=False,indent=2),encoding='utf-8');tmp.replace(path)
    def targets(port):
        return json.load(urllib.request.urlopen(f'http://127.0.0.1:{port}/json/list',timeout=10))
    def click(page,rect):
        x=rect['x']+rect['width']/2;y=rect['y']+rect['height']/2
        for typ in ['mousePressed','mouseReleased']:
            page.call('Input.dispatchMouseEvent',{'type':typ,'x':x,'y':y,'button':'left','clickCount':1})
    # Seed known attempts from this queue; other queues/devices must use the same
    # accountIdentity and ledger or register their attempts explicitly.
    for previous in data['tasks']:
        attempts=list(previous.get('attemptHistory') or [])
        if previous.get('uploadStartedAt'):
            attempts.append({'attemptId':previous.get('currentAttemptId') or previous['id'],'uploadStartedAt':previous['uploadStartedAt']})
        for attempt in attempts:
            stamp=attempt.get('uploadStartedAt')
            if stamp and stamp>time.time()-10920:
                reserve(ledger,account_key,attempt.get('attemptId') or previous['id'],len(previous['files']),10**9,now=stamp)
    prior=int(data.get('priorWindowAttachments',0))
    if prior and data.get('priorWindowStartedAt'):
        reserve(ledger,account_key,'prior:'+str(data['priorWindowStartedAt']),prior,10**9,now=float(data['priorWindowStartedAt']))
    elif prior:
        # Unknown-time historic count must be reconciled before live upload.
        raise RuntimeError('priorWindowAttachments has no timestamp; reconcile shared ledger before upload')
    blocked=set()
    for row in data['tasks']:
        if row['status']=='WAITING_UPLOAD_BUDGET' and (row.get('nextUploadCheckAt') or float('inf'))<=time.time():row['status']='PREPARED_NOT_UPLOADED'
        resume_draft=row['status']=='DRAFT_READY_NOT_SUBMITTED'
        if row['status'] not in {'PREPARED_NOT_UPLOADED','DRAFT_READY_NOT_SUBMITTED'} or row['account'] in blocked:continue
        port=int(data.get('cdpPort',9431 if row['account']=='A' else 9432))
        page=None;newtarget=None;active_attempt_id=None
        try:
            if [hashlib.sha256(Path(f).read_bytes()).hexdigest() for f in row['files']]!=row['sha256']:raise RuntimeError('Source changed after preparation')
            ts=targets(port);base=next(t for t in ts if t['type']=='page' and (t['url'].startswith('https://chatgpt.com/') or t['url']=='about:blank'))
            page=core.CDP(base['webSocketDebuggerUrl'])
            if page.evaluate("[...document.querySelectorAll('button')].some(b=>/^(停止|Stop|Stop generating)$/.test(b.getAttribute('aria-label')||'')&&b.getBoundingClientRect().width>0)"):
                raise RuntimeError('Account page responding; preserve page and schedule uploads after completion')
            existing=ui.visible_state(page)
            if resume_draft:
                # Resume the exact mounted draft in place: no navigation, re-upload,
                # prompt reinjection, or new quota reservation.
                snap=ui.api_snapshot(page) or {}
                users=[m for m in snap.get('messages',[]) if m.get('role')=='user' and server_prompt_matches(row.get('prompt'),m.get('text'))]
                if users:
                    msg=users[-1]
                    if not exact_attachment_set(msg.get('attachments',[]),row['files']):
                        raise RuntimeError('Existing server message has a mismatched attachment set')
                    verify_template_target(row,existing['url'],after_send=bool(row.get('newConversation')))
                    row['status']='SUBMITTED';row['conversationUrl']=existing['url'];row['conversationId']=conversation_id(existing['url']);row['serverMessageId']=msg.get('id');row['serverAttachments']=msg.get('attachments',[]);row['stage']='server_submission_reconciled';mark(ledger,account_key,row['currentAttemptId'],'confirmed');save()
                    print(json.dumps({'id':row['id'],'account':row['account'],'status':row['status'],'serverMessageId':row['serverMessageId'],'reconciledExistingMessage':True},ensure_ascii=False),flush=True)
                    page.close();page=None
                    continue
                if conversation_id(existing['url']):
                    raise RuntimeError('Fresh conversation exists but the queued user message is absent; reconcile without resend')
                reservation=get_event(ledger,account_key,row.get('currentAttemptId'))
                attempt_id=validate_resumable_draft(row,existing,existing['url'],reservation)
                active_attempt_id=attempt_id
                state=existing
                row['stage']='verified_exact_unsent_draft_ready_to_submit';save()
            else:
                if existing['removers'] or any(e['text'].strip() for e in existing['editors']):raise RuntimeError('Existing task page has an unsent draft; resume it before navigating')
                # Reuse one account page, unloading each submitted conversation before the next.
                page.call('Page.bringToFront')
                page.call('Page.navigate',{'url':row['templateUrl']})
                expected_aria=json.dumps(row.get('expectedComposerAria'))
                deadline=time.monotonic()+90
                while time.monotonic()<deadline:
                    state=ui.visible_state(page)
                    input_ready=page.evaluate(f"""(()=>{{const expected={expected_aria};const editors=[...document.querySelectorAll('[contenteditable=true]')].filter(e=>{{const r=e.getBoundingClientRect();return r.width>0&&r.height>0&&(!expected||e.getAttribute('aria-label')===expected)}});return editors.length===1&&[...document.querySelectorAll('input[type=file][aria-label=\"附加文件\"]')].length>0}})()""")
                    if state['ready']=='complete' and state['editors'] and input_ready:break
                    time.sleep(2)
                verify_template_target(row,state['url'])
                if row.get('expectedComposerAria') and not any(e.get('aria')==row['expectedComposerAria'] for e in state['editors']):
                    raise RuntimeError('Expected composer is not the active visible target')
                if state['removers'] or any(e['text'].strip() for e in state['editors']):raise RuntimeError('Template has an unsent draft')
                # A newly opened GPT route may have a conversation ID already. If so,
                # verify that it is empty before attaching anything to it.
                if row.get('newConversation') and conversation_id(state['url']):
                    snapshot=ui.api_snapshot(page)
                    if snapshot and snapshot.get('messages'):
                        raise RuntimeError('New-conversation route resolved to a non-empty chat')
                attempt_id,quota=reserve_upload_attempt(ledger,account_key,row,budget)
                if not quota['allowed']:
                    row['status']='WAITING_UPLOAD_BUDGET' if quota['reason']=='rolling_budget_wait' else 'UPLOAD_REQUIRES_RECONCILIATION'
                    row['nextUploadCheckAt']=quota.get('nextCheckAt');save()
                    print(json.dumps({'id':row['id'],'uploadBudget':quota}),flush=True)
                    continue
                active_attempt_id=attempt_id
                row['uploadStartedAt']=time.time();row['status']='UPLOADING';save()
                batches=split_upload_batches(row['files'])
                mounted=[]
                for batch_index,batch in enumerate(batches,1):
                    row['stage']=f'set_input_files_batch_{batch_index}_of_{len(batches)}';save()
                    selected=page.call('Runtime.evaluate',{'expression':f"(()=>{{const expected={expected_aria};const editors=[...document.querySelectorAll('[contenteditable=true]')].filter(e=>{{const r=e.getBoundingClientRect();return r.width>0&&r.height>0&&(!expected||e.getAttribute('aria-label')===expected)}});const e=editors[0];if(!e)return null;const inputs=[...document.querySelectorAll('input[type=file][aria-label=\\\"附加文件\\\"]')].filter(x=>x.parentElement.contains(e));return inputs.length===1?inputs[0]:null}})()",'returnByValue':False})
                    obj=selected.get('result',{}).get('objectId')
                    if not obj:raise RuntimeError('No visible-composer file input')
                    backend=page.call('DOM.describeNode',{'objectId':obj})['node']['backendNodeId']
                    page.call('DOM.setFileInputFiles',{'backendNodeId':backend,'files':batch},timeout=60)
                    mounted.extend(batch)
                    row['stage']=f'wait_attachment_mount_batch_{batch_index}_of_{len(batches)}';save()
                    labels=wait_for_exact_attachments(ui,page,mounted,timeout=180)
                    if not exact_attachment_set(labels,mounted):raise RuntimeError('Exact attachment filename set mismatch')
                    row['mountedLabels']=labels;row['mountedCount']=len(mounted);save()
                labels=wait_for_exact_attachments(ui,page,row['files'],timeout=1)
                page.call('Page.bringToFront')
                page.evaluate("[...document.querySelectorAll('[contenteditable=true]')].find(e=>e.getBoundingClientRect().width>0).focus();true")
                state=ui.visible_state(page)
                page.call('Input.insertText',{'text':row['prompt']})
                deadline=time.monotonic()+180
                while time.monotonic()<deadline:
                    state=ui.visible_state(page)
                    if state['quotaNotice']:raise RuntimeError(state['quotaNotice'])
                    if len(state['removers'])==len(row['files']) and state['send'] and not state['send'][0]['disabled']:break
                    time.sleep(2)
                else:raise RuntimeError('Attachments did not finish processing')
            if not any(normalize_prompt(e.get('text'))==normalize_prompt(row['prompt']) for e in state['editors']):raise RuntimeError('Prompt mismatch')
            # Persist the one-shot intent before clicking Send. If the process or
            # CDP disconnects at this boundary, the next run must reconcile and
            # may not click again automatically.
            row['submissionIntentId']=row.get('submissionIntentId') or hashlib.sha256((row['id']+'|'+str(time.time_ns())).encode()).hexdigest()
            row['submissionIntentCreatedAt']=time.time();row['status']='SUBMISSION_INTENT_DURABLE';save()
            page.evaluate("(()=>{const b=[...document.querySelectorAll('button[aria-label=\"发送\"]')].find(b=>b.getBoundingClientRect().width>0&&!b.disabled);if(!b)throw Error('Send not ready');b.click();return true})()")
            row['status']='SUBMIT_CLICKED_NOT_VERIFIED';row['submittedAt']=time.time();save()
            deadline=time.monotonic()+90
            while time.monotonic()<deadline:
                state=ui.visible_state(page)
                if row.get('newConversation') and not conversation_id(state['url']):
                    time.sleep(3)
                    continue
                verify_template_target(row,state['url'],after_send=bool(row.get('newConversation')))
                snap=ui.api_snapshot(page)
                messages=(snap or {}).get('messages',[])
                users=[i for i,m in enumerate(messages) if m['role']=='user' and server_prompt_matches(row.get('prompt'),m.get('text'))]
                if users:
                    i=users[-1];msg=messages[i]
                    if len(msg['attachments'])!=len(row['files']):raise RuntimeError('Server attachment count mismatch')
                    if not exact_attachment_set(msg['attachments'],row['files']):raise RuntimeError('Server attachment filename set mismatch')
                    row['status']='SUBMITTED';row['conversationUrl']=state['url'];row['conversationId']=conversation_id(state['url']);row['serverMessageId']=msg['id'];row['serverAttachments']=msg['attachments'];mark(ledger,account_key,attempt_id,'confirmed');save()
                    # Plans are read back later as a batch; do not block subsequent uploads.
                    break
                time.sleep(5)
            else:raise RuntimeError('Plan receipt not complete; do not resend')
            print(json.dumps({'id':row['id'],'account':row['account'],'status':row['status'],'files':len(row['files']),'url':row['conversationUrl']},ensure_ascii=False),flush=True)
            page.close();page=None  # Disconnect CDP only; preserve the actual conversation tab.
            time.sleep(2)  # brief UI settling only; no per-suite quota pacing
        except Exception as error:
            row['error']=str(error);row['failedAt']=time.time()
            if active_attempt_id:mark(ledger,account_key,active_attempt_id,'failed_or_unknown')
            if row['status'] in ['PREPARED_NOT_UPLOADED','UPLOADING']:row['status']='UPLOAD_BLOCKED_NOT_SUBMITTED'
            elif row['status']=='SUBMISSION_INTENT_DURABLE':row['status']='SUBMISSION_UNKNOWN_NOT_VERIFIED'
            blocked.add(row['account']);save()
            print(json.dumps({'account':row['account'],'id':row['id'],'status':row['status'],'error':str(error)},ensure_ascii=False),flush=True)
        finally:
            if page:page.close()
    print(json.dumps({'finalStatuses':{s:sum(r['status']==s for r in data['tasks']) for s in {r['status'] for r in data['tasks']}}},ensure_ascii=False),flush=True)

if __name__=='__main__':main()

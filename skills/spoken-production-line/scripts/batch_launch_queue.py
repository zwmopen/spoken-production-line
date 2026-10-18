"""Start reviewed successor batches after verified closure; never retry unknown sends.

This is a submission scheduler, not a completion or visual acceptance engine.
All account browser writes share production_supervisor.account_lock.
"""
import argparse
import datetime as dt
import hashlib
import json
from pathlib import Path
import time
import urllib.request

import cdp_adapter as cdp
from production_supervisor import account_lock, save
from start_ready import _safe_page_target, _insert_prompt, atomic_save_queue


def now():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def read(path):
    return json.loads(Path(path).read_text(encoding='utf-8-sig'))


def closed(manifest):
    notice = manifest.get('notification') or {}
    verified = manifest.get('notificationReadbackVerified') or notice.get('readbackVerified')
    message = manifest.get('notificationMessageId') or notice.get('messageId')
    return (manifest.get('status') in ('accepted_archived_and_notified', 'accepted_archived')
            and bool(verified and message)
            and isinstance(manifest.get('plannedPageCount'), int)
            and manifest['plannedPageCount'] > 0
            and manifest.get('acceptedPageCount') == manifest.get('plannedPageCount')
            and bool(manifest.get('usageRecorded')))


def require_safe_page(page):
    state = cdp.visible_state(page)
    active = page.evaluate("[...document.querySelectorAll('button')].some(b=>"
                           "b.getBoundingClientRect().width>0&&"
                           "/^(停止|Stop|Stop generating)$/.test(b.getAttribute('aria-label')||''))")
    if active or state.get('removers') or any(e['text'].strip() for e in state.get('editors', [])):
        raise RuntimeError('Active generation, draft or attachments; preserve page')
    if len(state.get('editors', [])) != 1:
        raise RuntimeError('Exactly one mounted visible editor required')


def exact_page_target(port, conversation_url):
    with urllib.request.urlopen(f'http://127.0.0.1:{port}/json/list', timeout=5) as response:
        targets = json.load(response)
    expected = conversation_url.split('?', 1)[0]
    matches = [t for t in targets if t.get('type') == 'page'
               and t.get('url', '').split('?', 1)[0] == expected]
    return matches[0] if len(matches) == 1 else None


def transient_cdp_error(error):
    text = str(error).lower()
    return any(token in text for token in ('navigated or closed', 'websockettimeout',
                                           'connection timed out', 'connection is already closed'))


def conversation_snapshot(page, conversation_id):
    """Read an exact thread from the authenticated profile without relying on location.href."""
    js = """(async()=>{const s=await fetch('/api/auth/session').then(r=>r.json());
    const r=await fetch('/backend-api/conversation/'+CID,{headers:{Authorization:'Bearer '+s.accessToken}});
    if(!r.ok)return JSON.stringify({ok:false,status:r.status});const d=await r.json();let n=d.current_node;const out=[];
    while(n&&d.mapping[n]){const x=d.mapping[n],m=x.message;if(m)out.push({id:m.id,role:m.author?.role,status:m.status,
    text:(m.content?.parts||[]).filter(x=>typeof x==='string').join(' '),
    attachments:(m.metadata?.attachments||[]).map(x=>x.name||x.filename||'')});n=x.parent}
    return JSON.stringify({ok:true,id:d.conversation_id,title:d.title,messages:out.reverse()})})()""".replace(
        'CID', json.dumps(conversation_id)
    )
    return json.loads(page.evaluate(js, timeout=45))


def tick(root, account):
    root = Path(root)
    path = root / '自动批量接续.json'
    with account_lock(root / 'supervisor' / (account + '.lock')):
        config = read(path)
        if config.get('enabled') is False:
            return {'account': account, 'status': 'DISABLED_BY_CONFIGURATION'}
        jobs = [j for j in config.get('jobs', []) if j.get('account') == account
                and j.get('status') not in ('CLOSED', 'CANCELLED')]
        if not jobs:
            return {'account': account, 'status': 'NO_REVIEWED_JOB'}
        job = jobs[0]
        if job.get('controllerReviewed') is not True:
            return {'account': account, 'status': 'REVIEW_REQUIRED'}
        predecessor = read(job['predecessorManifestPath'])
        if predecessor.get('workId') != job['predecessorWorkId'] or not closed(predecessor):
            return {'account': account, 'status': 'WAIT_PREDECESSOR_CLOSURE'}
        queue_path = root / ('queue-' + account + '.json')
        queue = read(queue_path)
        rows = [x for x in queue['tasks'] if (x.get('workId') or x.get('id')) == job['workId']]
        if len(rows) != 1 or queue.get('account') != account:
            raise ValueError('Exact work/account binding missing')
        row = rows[0]
        if row.get('manifestPath') and Path(row['manifestPath']).exists():
            if closed(read(row['manifestPath'])):
                job.update(status='CLOSED', closedObservedAt=now())
                save(path, config)
                return {'account': account, 'status': 'CLOSED', 'workId': job['workId']}
        if job.get('status') == 'SUBMITTED_CONFIRMED':
            return {'account': account, 'status': 'AWAIT_CURRENT_WORK_CLOSURE',
                    'workId': job['workId'], 'messageId': job.get('messageId')}
        prompt = Path(job['promptPath']).read_text(encoding='utf-8-sig').strip()
        if hashlib.sha256(prompt.encode()).hexdigest() != job['promptSha256']:
            raise ValueError('Reviewed prompt changed')
        if not 1 <= job.get('pageCount', 0) <= 10 or job['workId'] not in prompt:
            raise ValueError('Invalid native batch binding')
        if row.get('templateId') != job['templateId'] or row.get('sourceIdentity') != job['sourceIdentity']:
            raise ValueError('Reviewed source/template changed')
        files, hashes = row.get('files', []), row.get('sha256', [])
        if not files or len(files) != len(hashes):
            raise ValueError('Source file hashes missing')
        for filename, expected in zip(files, hashes):
            if hashlib.sha256(Path(filename).read_bytes()).hexdigest() != expected:
                raise ValueError('Source file changed: ' + filename)
        if not job.get('templateFiles'):
            raise ValueError('Reviewed physical template references missing')
        for item in job['templateFiles']:
            if hashlib.sha256(Path(item['path']).read_bytes()).hexdigest() != item['sha256']:
                raise ValueError('Template reference changed')
        port = 9431 if account == 'A' else 9432
        unknown = job.get('intent') or row.get('submissionIntent') or row.get('startMessageId')
        previous_intent = job.get('intent') or row.get('submissionIntent') or {}
        # A PRE_CLICK_ABORTED intent proves the composer rejected insertion
        # before the send button was clicked. It is safe to rebind the exact
        # conversation after checking every existing tab for active work or a
        # draft. A click-issued/ambiguous intent remains read-only and must
        # never navigate or submit again.
        pre_click_retry = (previous_intent.get('status') == 'PRE_CLICK_ABORTED'
                           and not previous_intent.get('clickIssuedAt')
                           and previous_intent.get('insertionAttempts', 0) < 5)
        read_only_reconcile = bool(unknown and not pre_click_retry)
        if unknown and not pre_click_retry:
            with urllib.request.urlopen(f'http://127.0.0.1:{port}/json/list', timeout=5) as stream:
                targets = json.load(stream)
            matches = [t for t in targets if t.get('type') == 'page'
                       and t.get('url', '').split('?', 1)[0] == job['conversationUrl']]
            if len(matches) == 1:
                target, exact = matches[0], True
            else:
                # Reconcile an ambiguous send with the authenticated profile's
                # read-only conversation API. Requiring the exact URL to remain
                # the selected tab deadlocked recovery after normal navigation,
                # even though the server-side message could be fetched safely.
                # Never navigate or submit from this fallback path.
                candidates = [t for t in targets if t.get('type') == 'page'
                              and t.get('url', '').startswith('https://chatgpt.com/')]
                if not candidates:
                    return {'account': account, 'status': 'UNKNOWN_SEND_READ_ONLY_NO_AUTH_PAGE'}
                target, exact = candidates[0], False
        else:
            target, exact = _safe_page_target(cdp, cdp, port, job['conversationUrl'])
        page = cdp.CDP(target['webSocketDebuggerUrl'], timeout=10)
        try:
            if not exact and not read_only_reconcile:
                try:
                    page.call('Page.navigate', {'url': job['conversationUrl']})
                except Exception as error:
                    # Chromium may replace the inspected target during a
                    # navigation. Only the exact conversation can resume this
                    # pre-click intent, so reconnect below rather than treating
                    # a closed CDP socket as a failed send.
                    if not transient_cdp_error(error):
                        raise
                deadline = time.monotonic() + 25
                while time.monotonic() < deadline:
                    target = exact_page_target(port, job['conversationUrl'])
                    if target:
                        page.close()
                        page = cdp.CDP(target['webSocketDebuggerUrl'], timeout=10)
                        try:
                            state = cdp.visible_state(page)
                            if (state.get('url', '').split('?', 1)[0] == job['conversationUrl'].split('?', 1)[0]
                                    and len(state.get('editors', [])) == 1):
                                break
                        except Exception as error:
                            if not transient_cdp_error(error):
                                raise
                    time.sleep(1)
                else:
                    raise RuntimeError('Exact task conversation did not stabilize after safe navigation')
            conversation_id = job['conversationUrl'].split('/c/')[-1].split('?', 1)[0]
            snapshot = None
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                try:
                    snapshot = conversation_snapshot(page, conversation_id)
                    if snapshot and snapshot.get('ok'):
                        break
                    raise RuntimeError('Authoritative conversation read failed')
                except Exception as error:
                    if not transient_cdp_error(error):
                        raise
                    target = exact_page_target(port, job['conversationUrl'])
                    if target:
                        page.close()
                        page = cdp.CDP(target['webSocketDebuggerUrl'], timeout=10)
                    time.sleep(1)
            if not snapshot or not snapshot.get('ok'):
                raise RuntimeError('Exact conversation API read did not stabilize')
            if not snapshot or not snapshot.get('ok') or snapshot.get('id') != job['conversationUrl'].rsplit('/c/', 1)[1]:
                raise ValueError('Authoritative conversation read failed')
            matches = [m for m in snapshot['messages'] if m.get('role') == 'user'
                       and ' '.join(m.get('text', '').split()) == ' '.join(prompt.split())]
            if matches:
                row.update(workId=job['workId'], startMessageId=matches[-1]['id'],
                           status='GENERATION_SUBMITTED', generationStatus='START_SUBMITTED',
                           awaitingGenerationCompletion=True)
                job.update(status='SUBMITTED_CONFIRMED', messageId=matches[-1]['id'], confirmedAt=now())
                atomic_save_queue(queue_path, queue)
                save(path, config)
                return {'account': account, 'workId': job['workId'], 'status': job['status'],
                        'messageId': job['messageId']}
            previous_intent = job.get('intent') or row.get('submissionIntent') or {}
            pre_click_retry = (previous_intent.get('status') == 'PRE_CLICK_ABORTED'
                               and not previous_intent.get('clickIssuedAt')
                               and previous_intent.get('insertionAttempts', 0) < 5)
            if (job.get('intent') or row.get('submissionIntent') or row.get('startMessageId')) and not pre_click_retry:
                return {'account': account, 'status': 'SUBMISSION_UNKNOWN_READ_ONLY', 'workId': job['workId']}
            require_safe_page(page)
            latest = [m for m in snapshot['messages'] if m.get('text', '').strip()][-1]
            if latest['id'] != job['planMessageId'] or latest['status'] != 'finished_successfully':
                raise ValueError('Reviewed terminal plan is no longer latest')
            intent = previous_intent or {'id': job['workId'] + ':batch:' + job['promptSha256'][:12],
                      'createdAt': now(), 'status': 'SUBMISSION_UNKNOWN',
                      'promptSha256': job['promptSha256'], 'reconcileOnly': True}
            job.update(status='SUBMISSION_UNKNOWN', intent=intent)
            row.update(workId=job['workId'], submissionIntent=intent.copy(),
                       status='SUBMISSION_UNKNOWN', generationStatus='SUBMISSION_UNKNOWN')
            save(path, config)
            atomic_save_queue(queue_path, queue)
            intent['insertionAttempts'] = intent.get('insertionAttempts', 0) + 1
            try:
                _insert_prompt(page, prompt, cdp)
            except Exception:
                # We have not reached the click call. Retry only if a later
                # authoritative read still finds the exact plan and empty draft.
                intent['status'] = 'PRE_CLICK_ABORTED'
                intent['abortObservedAt'] = now()
                job['intent'] = intent
                row['submissionIntent'] = intent.copy()
                save(path, config)
                atomic_save_queue(queue_path, queue)
                raise
            intent.update(status='CLICK_ISSUED_RECONCILE_ONLY', clickIssuedAt=now())
            job['intent'] = intent
            row['submissionIntent'] = intent.copy()
            save(path, config)
            atomic_save_queue(queue_path, queue)
            page.evaluate("(()=>{const bs=[...document.querySelectorAll('button')].filter(b=>"
                          "b.getBoundingClientRect().width>0&&!b.disabled&&"
                          "(/send-button/.test(b.getAttribute('data-testid')||'')||"
                          "/^(发送|发送提示词|Send prompt)$/.test(b.getAttribute('aria-label')||'')));"
                          "if(bs.length!==1)throw Error('Send control ambiguous');bs[0].click();return true})()")
            return {'account': account, 'workId': job['workId'], 'status': 'CLICK_ISSUED_RECONCILE_ONLY'}
        finally:
            page.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--task-root', required=True)
    parser.add_argument('--account', choices=('A', 'B'), required=True)
    parser.add_argument('--daemon', action='store_true')
    args = parser.parse_args()
    root = Path(args.task_root)
    while True:
        try:
            result = tick(root, args.account)
        except Exception as error:
            result = {'account': args.account, 'status': 'RECOVERY_REQUIRED',
                      'error': type(error).__name__ + ': ' + str(error)[:300]}
        result.update(checkedAt=now(), nextRunAt=(dt.datetime.now(dt.timezone.utc)
                      + dt.timedelta(seconds=30)).isoformat())
        save(root / 'supervisor' / ('batch-launch-' + args.account + '.json'), result)
        print(json.dumps(result, ensure_ascii=True), flush=True)
        if not args.daemon:
            return
        time.sleep(30)


if __name__ == '__main__':
    main()

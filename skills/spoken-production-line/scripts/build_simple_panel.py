"""Read-only completion checks and a small evidence-based production panel."""
import argparse, datetime, html, json, os, tempfile, urllib.error, urllib.request
from pathlib import Path
from quota_panel_view import account_view
from panel_commands import render_commands
from check_visual_acceptance import check as check_visual_receipt


def read(path):
    try:
        return json.loads(Path(path).read_text(encoding='utf-8-sig'))
    except (OSError, ValueError):
        return {}


def read_live_runtime():
    """Read the local workbench control-plane state without mutating queues."""
    snapshots = {}
    for account, port in (('A', 4331), ('B', 4332)):
        observed = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec='seconds')
        state = None
        source = None
        api_error = None
        try:
            request = urllib.request.Request(
                f'http://127.0.0.1:{port}/api/gpt-production/runtime-state',
                headers={'Accept': 'application/json'}, method='GET')
            with urllib.request.urlopen(request, timeout=2.5) as response:
                payload = json.loads(response.read(4 * 1024 * 1024).decode('utf-8'))
            state = payload.get('state') if isinstance(payload, dict) else None
            source = 'runtime_api'
        except (OSError, ValueError, urllib.error.URLError, TimeoutError) as exc:
            api_error = f'{type(exc).__name__}: {exc}'[:240]
            # The API can stall while Electron's event loop is busy even though
            # the app keeps writing its compact runtime snapshot to disk. Read
            # that same source of truth directly so the panel stays fresh and
            # reports the API failure instead of freezing on an older render.
            runtime_file = Path(
                f'./data/browser-runtime/instance-{account}/gpt-production-runtime.json')
            try:
                state = json.loads(runtime_file.read_text(encoding='utf-8-sig'))
                observed = datetime.datetime.fromtimestamp(
                    runtime_file.stat().st_mtime, datetime.timezone.utc).isoformat(timespec='seconds')
                source = 'direct_local_runtime_file'
            except (OSError, ValueError) as file_exc:
                snapshots[account] = {
                    'available': False, 'port': port, 'observedAt': observed,
                    'source': 'unavailable', 'apiError': api_error,
                    'fileError': f'{type(file_exc).__name__}: {file_exc}'[:240]
                }
                continue
        queue = (state or {}).get('queue') or {}
        tasks = queue.get('tasks') if isinstance(queue.get('tasks'), list) else []
        snapshots[account] = {
            'available': isinstance(state, dict), 'port': port, 'observedAt': observed,
            'source': source, 'apiError': api_error,
            'runtimeUpdatedAt': (state or {}).get('updatedAt'),
            'revision': (state or {}).get('revision'),
            'queueRunning': queue.get('running'), 'paused': queue.get('paused'),
            'activeAccountId': queue.get('activeAccountId'), 'taskCount': len(tasks),
            'tasks': [{
                'requestId': task.get('requestId'), 'name': task.get('name'),
                'status': task.get('_status'), 'stage': task.get('_stage'),
                'percent': task.get('_percent'), 'submittedToGpt': task.get('_submittedToGpt'),
                'startedAt': task.get('_startedAt'), 'workId': task.get('workId'),
                'expectedImages': task.get('expectedImages'),
                'generatedImages': (task.get('workflow') or {}).get('generatedImageActualCount')
            } for task in tasks]
        }
    return snapshots


def closure(root, row):
    work = row.get('workId')
    # Prefer the exact receipt paths registered on this workId. Historical
    # accepted suites commonly use names such as `*-归档回执.json`, which the
    # old `*交付验收.json` glob silently missed and then hid from the latest
    # formal-closure metric.
    receipt_paths = [
        row.get('deliveryAcceptancePath'), row.get('acceptanceReceiptPath'),
        row.get('archiveReceiptPath'), row.get('evidencePath'), row.get('receiptPath')
    ]
    manifest_path = Path(row.get('manifestPath') or Path(row.get('productFolderPath') or '__missing__')/'manifest.json')
    manifest_hint = read(manifest_path)
    receipt_paths.extend([
        manifest_hint.get('deliveryAcceptancePath'), manifest_hint.get('acceptanceReceiptPath'),
        manifest_hint.get('archiveReceiptPath'), manifest_hint.get('acceptance', {}).get('receiptPath')
    ])
    receipt = {}
    receipt_path = None
    for candidate in receipt_paths:
        if candidate and Path(candidate).is_file():
            value = read(candidate)
            if work and value.get('workId') == work:
                receipt, receipt_path = value, str(candidate)
                break
    if not receipt and work:
        for pattern in ('*交付验收.json', '*归档回执.json', '*验收回执.json'):
            for candidate_path in root.glob(pattern):
                value = read(candidate_path)
                if value.get('workId') == work:
                    receipt, receipt_path = value, str(candidate_path)
                    break
            if receipt:
                break
    product = Path(receipt.get('productPath') or row.get('productFolderPath') or '__missing__')
    manifest = read(product / 'manifest.json')
    visual_candidates = [
        receipt.get('visualAcceptancePath'), receipt.get('acceptancePath'),
        receipt.get('visualAcceptanceEvidencePath'), receipt.get('visualAcceptanceReceiptPath'),
        row.get('visualAcceptanceEvidencePath'), row.get('visualAcceptancePath'),
        manifest.get('visualAcceptancePath'), manifest.get('acceptance', {}).get('visualAcceptancePath'),
        manifest.get('visualAcceptanceEvidencePath'),
        product / '逐页视觉验收V3.json', product / 'visual-acceptance.json'
    ]
    visual = next((read(candidate) for candidate in visual_candidates
                   if candidate and Path(candidate).is_file() and read(candidate).get('workId') == work), {})
    planned = receipt.get('plannedPages') or receipt.get('plannedPageCount') or row.get('plannedPageCount')
    accepted = receipt.get('acceptedPages') or receipt.get('acceptedPageCount') or row.get('acceptedPageCount') or row.get('actualPageCount')
    images = list(product.glob('P*.png')) + list(product.glob('P*.jpg')) if product.is_dir() else []
    copy = product / '文案.txt'
    tags = read(Path(row.get('sourceFolderPath') or '__missing__') / '.tags.json')
    notice_path = receipt.get('notificationReceipt') or receipt.get('notificationReceiptPath') or row.get('notificationReceiptPath')
    notice = read(notice_path) if notice_path else {}
    def visual_clean(value):
        if isinstance(value, dict):
            if value.get('unresolvedDefects') or value.get('result') in ('fail', 'failed'):
                return False
            return all(visual_clean(v) for v in value.values())
        return all(visual_clean(v) for v in value) if isinstance(value, list) else True
    gates = {
        '验收回执': receipt.get('workId') == work and (receipt.get('validation', {}).get('accepted') is True or receipt.get('accepted') is True),
        '计划与图片数量': isinstance(planned, int) and planned > 0 and planned == accepted == len(images),
        '文案文件': copy.is_file() and copy.stat().st_size > 0,
        '元数据': manifest.get('workId') == work,
        '逐页视觉记录': visual.get('workId') == work and len(visual.get('pages', [])) == planned and visual_clean(visual) and check_visual_receipt(visual)['accepted'],
        '使用记录': any(e.get('workId') == work for e in tags.get('usageEvents', []) if isinstance(e, dict)),
        '通知回读': notice.get('workId') == work and (notice.get('readbackVerified') is True or (notice.get('readbackStatus') == 'verified' and notice.get('readbackApiCode') == 0 and notice.get('readbackText') == notice.get('text'))),
    }
    ready = all(gates.values())
    return {'workId': work, 'conversationUrl': row.get('productionConversationUrl'),
            'channel': row.get('executionChannel'), 'templateId': row.get('templateId'),
            'closureComplete': ready, 'reusableAfterPageIdleCheck': ready,
            'receiptPath': receipt_path, 'gates': gates,
            'state': '可再次投料（先核窗口空闲）' if ready else '本套仍占用',
            'missing': [k for k, v in gates.items() if not v]}


def build(root):
    root = Path(root)
    ledger = read(root / '生产台账.json')
    rows = ledger.get('records', [])
    batch = read(root / '网页备料本轮汇总.json')
    config = read(root / '循环生产配置.json')
    runtime = read(root / '当前执行通路核查.json')
    live_runtime = read_live_runtime()
    sync = read(root / '面板同步器状态.json')
    bridge = read(root / '宿主自启动回执.json')
    active_channels = config.get('activeChannels', ['GPT A', 'GPT B'])
    now = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec='seconds')
    now_local = datetime.datetime.now().astimezone().isoformat(timespec='seconds')
    sync_state = str(sync.get('state') or 'unknown')
    sync_at = sync.get('lastSuccessfulCheckAt') or ''
    sync_age_minutes = None
    if sync_at:
        try:
            parsed_sync_at = datetime.datetime.fromisoformat(str(sync_at).replace('Z', '+00:00'))
            if parsed_sync_at.tzinfo is None:
                parsed_sync_at = parsed_sync_at.astimezone()
            sync_age_minutes = max(0, int((datetime.datetime.now(parsed_sync_at.tzinfo) - parsed_sync_at).total_seconds() / 60))
        except ValueError:
            pass
    if sync_state == 'degraded':
        sync_text = f"后台同步器异常；最近成功 {sync_at or '未取得'}；错误 {sync.get('lastErrorSummary') or '详情未取得'}"
    elif sync_state == 'running' and sync_age_minutes is not None:
        sync_text = f"后台同步器运行中；最近成功 {sync_at}（{sync_age_minutes} 分钟前）"
    else:
        sync_text = f"后台同步器状态未确认；最近成功 {sync_at or '未取得'}"
    bridge_state = str(bridge.get('libraryBridge') or '未取得')
    bridge_checked = str(bridge.get('checkedAt') or '未取得')
    bridge_connected = bridge.get('libraryBrowserConnected')
    if bridge_connected is True:
        bridge_text = f'本机Bridge {bridge_state}；Library页面扩展已回报连接，最近宿主检查 {bridge_checked}'
    elif bridge_connected is False:
        bridge_text = f'本机Bridge {bridge_state}；未收到Library页面扩展心跳，最近宿主检查 {bridge_checked}'
    else:
        bridge_text = f'Library Bridge/页面连接状态未取得；最近宿主检查 {bridge_checked}'
    host_interval = str(bridge.get('hostCheckInterval') or '未取得')
    host_wake = bridge.get('hostWakeToRun')
    host_login = bridge.get('hostInteractiveLoginRequired')
    host_available = bridge.get('hostStartWhenAvailable')
    host_boundaries = []
    if host_wake is False:
        host_boundaries.append('电脑睡眠时不会唤醒')
    elif host_wake is True:
        host_boundaries.append('电脑睡眠时允许唤醒')
    if host_login is True:
        host_boundaries.append('需要Windows交互登录会话')
    if host_available is True:
        host_boundaries.append('错过的触发会在条件恢复后补跑')
    host_schedule_text = f'宿主任务间隔 {host_interval}；' + ('；'.join(host_boundaries) if host_boundaries else '运行边界未完整取得')
    checks = [closure(root, r) for r in rows]
    # A conversation with another unfinished work remains occupied.
    busy = {c['conversationUrl'] for c in checks if c['conversationUrl'] and not c['reusableAfterPageIdleCheck']}
    for c in checks:
        if c['conversationUrl'] in busy and c['reusableAfterPageIdleCheck']:
            c['reusableAfterPageIdleCheck'] = False
            c['state'] = '同会话仍有其他在途任务'
    (root / '会话可复用状态.json').write_text(json.dumps({'observedAt':now, 'sourceUpdatedAt':ledger.get('updatedAt'),
        'scope':'本地文件与已存回执检查；不是实时浏览器检测', 'records':checks}, ensure_ascii=False, indent=2), encoding='utf-8')
    esc = lambda v: html.escape(str(v if v not in (None, '') else '未取得'), quote=True)
    # Formal delivery is independent of whether its conversation is currently
    # occupied by another unfinished workId. Keep both figures separate.
    done = sum(c.get('closureComplete', c['reusableAfterPageIdleCheck']) for c in checks)
    recent_times = []
    for r,c in zip(rows,checks):
        if c.get('closureComplete', c['reusableAfterPageIdleCheck']):
            manifest = read(Path(r.get('manifestPath') or Path(r.get('productFolderPath') or '__missing__')/'manifest.json'))
            receipt = read(c.get('receiptPath') or '__missing__')
            value = (manifest.get('timing',{}).get('archivedAt') or manifest.get('archivedAt')
                     or receipt.get('archivedAt') or r.get('archivedAt'))
            if value:
                try:
                    archived = datetime.datetime.fromisoformat(str(value).replace('Z','+00:00'))
                except (TypeError, ValueError):
                    try:
                        archived = datetime.datetime.strptime(str(value), '%Y/%m/%d %H:%M:%S')
                    except (TypeError, ValueError):
                        continue
                if archived.tzinfo is None:
                    archived = archived.replace(tzinfo=datetime.timezone(datetime.timedelta(hours=8)))
                recent_times.append(archived)
    if recent_times:
        last = max(recent_times)
        elapsed = max(0, int((datetime.datetime.now(datetime.timezone.utc)-last).total_seconds()/60))
        last_text = last.astimezone(datetime.timezone(datetime.timedelta(hours=8))).strftime('%m月%d日 %H:%M') + f'（距今约{elapsed}分钟）'
    else:
        last_text = '暂无具备完整时间证据的已闭环成品'
    # Keep suite archival time distinct from page-level image activity. A suite may
    # be in progress for hours while valid native pages continue to be generated.
    page_events = []
    for account in ('A', 'B'):
        quota = read(root / f'{account}生图滚动账.json')
        for event in quota.get('events', []):
            try:
                at = datetime.datetime.fromisoformat(str(event.get('at', '')).replace('Z', '+00:00'))
                outputs = int(event.get('uniqueOutputs') or 0)
                if at.tzinfo is None:
                    at = at.replace(tzinfo=datetime.timezone(datetime.timedelta(hours=8)))
                if outputs > 0:
                    page_events.append((at, account, event))
            except (TypeError, ValueError):
                continue
    # A Codex page can be materially generated and staged before the writer
    # appends its acceptance/quota receipt. Show that real candidate as the
    # latest generation while keeping it out of accepted-page totals.
    current_codex = (runtime.get('latestRecheck') or {}).get('Codex') or {}
    codex_native = Path(str(current_codex.get('nativePath') or ''))
    codex_work_id = current_codex.get('workId')
    if codex_work_id and codex_native.is_file():
        codex_manifest = read(codex_native.parent.parent / 'manifest.json')
        native_root = Path(str(codex_manifest.get('nativeRoot') or codex_native.parent))
        page_files = []
        for candidate in native_root.glob('P*.png'):
            try:
                page_files.append((int(candidate.stem[1:]), candidate))
            except ValueError:
                continue
        if codex_manifest.get('workId') == codex_work_id and page_files:
            for accepted in (codex_manifest.get('acceptedOutputs') or []):
                accepted_path = Path(str(accepted.get('outputPath') or ''))
                accepted_page = accepted.get('page')
                if (accepted.get('qaStatus') != 'PASS' or not accepted_page
                        or not accepted_path.is_file()):
                    continue
                if any(item[2].get('workId') == codex_work_id
                       and item[2].get('page') == accepted_page for item in page_events):
                    continue
                accepted_at = datetime.datetime.fromtimestamp(
                    accepted_path.stat().st_mtime,
                    datetime.timezone(datetime.timedelta(hours=8)))
                page_events.append((accepted_at, 'Codex', {
                    'workId': codex_work_id, 'page': accepted_page, 'uniqueOutputs': 1,
                    'acceptedOutputs': 1, 'candidateOnly': False,
                }))
            page_number, latest_native = max(page_files, key=lambda item: item[0])
            page_name = f'P{page_number}'
            accepted_row = next((item for item in (codex_manifest.get('acceptedOutputs') or [])
                                 if item.get('page') == page_name
                                 and item.get('qaStatus') == 'PASS'
                                 and item.get('outputPath') == str(latest_native)), None)
            already_recorded = any(
                item[2].get('workId') == codex_work_id and item[2].get('page') == page_name
                for item in page_events)
            if not already_recorded:
                codex_at = datetime.datetime.fromtimestamp(
                    latest_native.stat().st_mtime,
                    datetime.timezone(datetime.timedelta(hours=8)))
                page_events.append((codex_at, 'Codex', {
                    'workId': codex_work_id, 'page': page_name, 'uniqueOutputs': 1,
                    'acceptedOutputs': 1 if accepted_row else 0,
                    'candidateOnly': accepted_row is None,
                }))
    if page_events:
        at, account, event = max(page_events, key=lambda item: item[0])
        page_time = at.astimezone(datetime.timezone(datetime.timedelta(hours=8))).strftime('%m月%d日 %H:%M')
        channel_name = 'Codex' if account == 'Codex' else f'GPT {account}'
        if event.get('candidateOnly'):
            latest_page_text = f'{page_time} · {channel_name} · {event.get("workId", "workId未取得")} {event.get("page", "页码未取得")} · 原生候选{event.get("uniqueOutputs")}张（待验收，不计通过）'
        else:
            latest_page_text = f'{page_time} · {channel_name} · {event.get("workId", "workId未取得")} {event.get("page", "页码未取得")} · 实际输出{event.get("uniqueOutputs")}张'
    else:
        latest_page_text = '暂无可核实的单页生图事件'
    accepted_page_events = [item for item in page_events if int(item[2].get('acceptedOutputs', item[2].get('acceptedPages', 0)) or 0) > 0]
    if accepted_page_events:
        at, account, event = max(accepted_page_events, key=lambda item: item[0])
        channel_name = 'Codex' if account == 'Codex' else f'GPT {account}'
        accepted_page_text = f'{at.astimezone(datetime.timezone(datetime.timedelta(hours=8))).strftime("%m月%d日 %H:%M")} · {channel_name} · {event.get("workId", "workId未取得")} {event.get("page", "页码未取得")}'
    else:
        accepted_page_text = '暂无可核实的单页验收事件'
    next_pages = []
    for account in ('A', 'B'):
        queue = read(root / f'queue-{account}.json')
        task = next((x for x in queue.get('tasks', []) if x.get('status') == 'PRODUCTION_IN_PROGRESS'), None)
        if task:
            progress = f'{task.get("acceptedPageCount", 0)}/{task.get("plannedPageCount", task.get("actualPageCount", "?"))}'
            page = task.get('nextPage')
            next_pages.append(f'{account} {task.get("workId") or task.get("id")} 已验收{progress}，队列下一页P{page}（待提交）' if page else f'{account} {task.get("workId") or task.get("id")} 已验收{progress}，无待续页')
    next_page_text = '；'.join(next_pages) if next_pages else '队列中未找到待续页'
    runtime_summaries = []
    runtime_observations = []
    for account in ('A', 'B'):
        item = live_runtime.get(account, {})
        source_queue = read(root / f'queue-{account}.json')
        current_plan = next((task for task in source_queue.get('tasks', [])
                             if task.get('status') == 'PRODUCTION_IN_PROGRESS'), {})
        current_work_id = current_plan.get('workId') or current_plan.get('id')
        running_tasks = [task for task in item.get('tasks', []) if task.get('status') == 'running']
        bound_running = [task for task in running_tasks if current_work_id and task.get('workId') == current_work_id]
        if item.get('observedAt'):
            runtime_observations.append(str(item['observedAt']))
        if not item.get('available'):
            runtime_summaries.append(f'{account} runtime未读到')
        elif item.get('paused'):
            suffix = f'，另有{len(running_tasks)}个未绑定running标记' if running_tasks and not bound_running else ''
            runtime_summaries.append(f'{account}队列暂停{suffix}')
        elif item.get('queueRunning') and bound_running:
            submitted = all(task.get('submittedToGpt') is True for task in bound_running)
            runtime_summaries.append(f'{account}有workId匹配的调度任务，网页提交标记{"已置位" if submitted else "未置位"}')
        elif running_tasks:
            runtime_summaries.append(f'{account}队列无匹配运行任务，但存在{len(running_tasks)}个未绑定running标记')
        elif item.get('queueRunning'):
            runtime_summaries.append(f'{account}队列运行标记无当前workId匹配任务')
        else:
            runtime_summaries.append(f'{account}队列空闲，无当前workId匹配运行任务')
    live_observed = max(runtime_observations) if runtime_observations else '未取得'
    execution_text = f"只读runtime观察 {live_observed}：{'；'.join(runtime_summaries)}。runtime不证明GPT网页已提交或正在生成，需核网页/服务端回执。"
    codex_writer = runtime.get('codexWriter', {})
    # The long-lived Codex thread can advance after the last full thread
    # readback.  A newer, workId-bound local recheck may already contain the
    # actual native candidate while codexWriter still points at yesterday's
    # queue item.  Prefer that newer evidence for the *current* panel card,
    # but only when it belongs to the same sole writer and the native file is
    # physically present.  Keep acceptance/archive explicitly false until the
    # current workId has its own receipts.
    latest_codex = (runtime.get('latestRecheck') or {}).get('Codex') or {}
    try:
        writer_revision = int(str(codex_writer.get('latestThreadCursor', '')).split('revision', 1)[1].split(';', 1)[0].strip())
    except (IndexError, ValueError):
        writer_revision = -1
    try:
        recheck_revision = int(latest_codex.get('revision', -1))
    except (TypeError, ValueError):
        recheck_revision = -1
    latest_native = Path(str(latest_codex.get('nativePath') or ''))
    latest_manifest = read(latest_native.parent.parent / 'manifest.json') if latest_native.is_file() else {}
    try:
        manifest_read_at = datetime.datetime.fromisoformat(
            str(latest_manifest.get('updatedAtUtc') or latest_manifest.get('updatedAt') or '').replace('Z', '+00:00'))
    except ValueError:
        manifest_read_at = None
    try:
        thread_read_at = datetime.datetime.fromisoformat(
            str(codex_writer.get('latestThreadReadbackAt') or '').replace('Z', '+00:00'))
    except ValueError:
        thread_read_at = None
    manifest_is_newer = bool(manifest_read_at and
                             (thread_read_at is None or manifest_read_at > thread_read_at))
    if (latest_codex.get('threadId')
            and latest_codex.get('threadId') == codex_writer.get('threadId')
            and latest_codex.get('workId')
            and latest_codex.get('threadStatus') == 'active'
            and latest_native.is_file()
            and latest_manifest.get('workId') == latest_codex.get('workId')
            and (recheck_revision > writer_revision or manifest_is_newer
                 or latest_codex.get('workId') != codex_writer.get('activeWorkId'))):
        work_id = str(latest_codex['workId'])
        queue_id = work_id[:-5] if work_id.endswith('-WORK') else work_id
        native_root = Path(str(latest_manifest.get('nativeRoot') or latest_native.parent))
        page_numbers = []
        for candidate in native_root.glob('P*.png'):
            try:
                page_numbers.append(int(candidate.stem[1:]))
            except ValueError:
                continue
        latest_page_number = max(page_numbers) if page_numbers else 1
        latest_page = f'P{latest_page_number}'
        current_page_path = native_root / f'{latest_page}.png'
        accepted_rows = [item for item in (latest_manifest.get('acceptedOutputs') or [])
                         if item.get('page') == latest_page and item.get('qaStatus') == 'PASS'
                         and item.get('outputPath') == str(current_page_path)]
        accepted_count = len(latest_manifest.get('acceptedOutputs') or [])
        generated_count = int(latest_manifest.get('generatedPageCount') or latest_manifest.get('generatedPages') or 0)
        planned_count = int((latest_manifest.get('productionPlan') or {}).get('plannedPages') or 0)
        next_action = latest_manifest.get('nextAction') or latest_codex.get('nextAction') or '继续核对当前页面'
        generation_text = (f'原生图已生成{generated_count}页，验收通过{accepted_count}页；'
                           f'最新 {latest_page} {"已验收" if accepted_rows else "待验收"}；下一步 {next_action}')
        codex_writer = {
            **codex_writer,
            'status': latest_codex.get('threadStatus'),
            'latestThreadStatus': latest_codex.get('threadStatus'),
            'latestThreadCursor': f"latestRecheck revision {recheck_revision}; live same-workId manifest",
            'activeQueueId': queue_id,
            'activeWorkId': work_id,
            'activeManifestPath': str(latest_native.parent.parent / 'manifest.json'),
            'activeTemplateId': latest_codex.get('templateId'),
            'activeTemplateState': '已绑定；来自同一workId回执',
            'activeGenerationState': generation_text,
            'formalArchiveVerified': False,
            'acceptanceVerified': bool(planned_count and accepted_count >= planned_count),
            'candidateOutputObserved': True,
            'candidateOutputPath': str(current_page_path if current_page_path.is_file() else latest_native),
            'candidateWorkIdBinding': 'verified',
        }
    if codex_writer.get('activeWorkId'):
        codex_writer_text = (f"唯一writer状态 {codex_writer.get('latestThreadStatus') or codex_writer.get('status') or '未取得'}；"
                             f"当前任务 {codex_writer.get('activeQueueId') or '未取得'} / {codex_writer.get('activeWorkId')}；"
                             f"模板 {codex_writer.get('activeTemplateId') or '待回读'}（{codex_writer.get('activeTemplateState') or '状态未取得'}）；"
                             f"生图回执 {codex_writer.get('activeGenerationState') or '未取得'}；正式归档 {codex_writer.get('formalArchiveVerified')}；验收 {codex_writer.get('acceptanceVerified')}。")
    elif codex_writer.get('candidateOutputObserved'):
        candidate_name = Path(str(codex_writer.get('candidateOutputPath') or '')).name or '文件名未取得'
        codex_writer_text = (f"唯一writer状态 {codex_writer.get('latestThreadStatus') or codex_writer.get('status') or '未取得'}；"
                             f"{codex_writer.get('latestToolMarkerType') or '生成'}工具 {codex_writer.get('latestToolMarkerStatus') or '状态未取得'}；报告页 {codex_writer.get('candidateReportedPage') or '未取得'}；候选 {candidate_name}；"
                             f"workId绑定 {codex_writer.get('candidateWorkIdBinding') or '未取得'}；正式归档 {codex_writer.get('formalArchiveVerified')}；验收 {codex_writer.get('acceptanceVerified')}。")
    else:
        codex_writer_text = f"唯一writer状态 {codex_writer.get('status') or '未取得'}；当前候选和落盘回执未取得。"
    runtime_cards = []
    for account in ('A', 'B'):
        item = live_runtime.get(account, {})
        source_queue = read(root / f'queue-{account}.json')
        current_plan = next((task for task in source_queue.get('tasks', [])
                             if task.get('status') == 'PRODUCTION_IN_PROGRESS'), {})
        current_work_id = current_plan.get('workId') or current_plan.get('id') or '未取得'
        if not item.get('available'):
            runtime_cards.append(f'<article><h3>GPT {account} 工作台 runtime</h3><p>当前计划 workId：{esc(current_work_id)}</p><p>API和本地runtime文件均不可读（端口 {item.get("port")}）；API：{esc(item.get("apiError", "未取得错误详情"))}；文件：{esc(item.get("fileError", "未取得错误详情"))}</p><small>这表示控制面状态未核实，不代表账号额度耗尽。</small></article>')
            continue
        linked_tasks = [task for task in item.get('tasks', []) if task.get('workId') == current_work_id]
        linked_text = '有' if linked_tasks else '无'
        running_tasks = [task for task in item.get('tasks', []) if task.get('status') == 'running']
        if item.get('queueRunning') and running_tasks:
            queue_label = '调度队列运行中；仍需生成消息/图片回执证明网页侧已提交'
        elif running_tasks:
            queue_label = '队列全局未运行，但有任务标记 running，后台状态冲突；不能当作生成中'
        elif item.get('paused'):
            queue_label = '队列暂停、无 running 任务'
        else:
            queue_label = '队列空闲、无 running 任务'
        task_lines = []
        for task in item.get('tasks', []):
            task_lines.append(f'{task.get("status", "状态未知")} · {task.get("stage") or "阶段未填"} · {task.get("name") or task.get("requestId") or "任务名未取得"} · 已向网页提交标记={task.get("submittedToGpt")} · runtime workId={task.get("workId") or "未绑定"}')
        details = '<br>'.join(esc(x) for x in task_lines) if task_lines else '无 runtime 任务'
        source_label = '本机runtime API' if item.get('source') == 'runtime_api' else '本机runtime JSON文件回退'
        api_note = f'；API请求失败后回退：{item.get("apiError")}' if item.get('apiError') else ''
        runtime_cards.append(f'<article><h3>GPT {account} 工作台 runtime</h3><b>{esc(queue_label)}</b><p>当前计划 workId：{esc(current_work_id)} · runtime 精确绑定：{linked_text}</p><p>观察：{esc(item.get("observedAt"))} · revision {esc(item.get("revision"))} · 账号槽 {esc(item.get("activeAccountId"))} · 任务 {item.get("taskCount", 0)} 条</p><p>{details}</p><small>来源：{esc(source_label)}{esc(api_note)}。本地运行态只读快照不等于网页正在生成；提交与成图仍以网页/服务端真实回执为准。</small></article>')
    cards = []
    for channel in active_channels:
        group = [(r,c) for r,c in zip(rows,checks) if r.get('executionChannel') == channel]
        complete = sum(c['reusableAfterPageIdleCheck'] for r,c in group)
        pending = [(r,c) for r,c in group if not c['reusableAfterPageIdleCheck']]
        if channel == 'Codex':
            codex = read(root / 'Codex当前账号与额度绑定.json')
            uploads = '独立本地生产通道，不与网页上传混账'
            budget = f"当前账号 {codex.get('currentAccountLabel','未核实')}；官方生图余量未知；旧账号限额不沿用"
        else:
            view = account_view(root, channel[-1])
            upload_coverage = '近3小时有本地登记回执' if view['uploadEvidenceFresh'] else '本地回执未覆盖完整近3小时'
            ledger_coverage = '事件账覆盖当前窗口' if view['ledgerFresh'] else f"事件账已 {view['ledgerAgeMinutes'] if view['ledgerAgeMinutes'] is not None else '未知'} 分钟未更新"
            uploads = f"近3小时已登记上传 {view['uploads']} 个附件 / 约{view['uploadBudget']}（排产估值）；{view['uploadTimingNote']}；{upload_coverage}，官方/外端用量未知"
            budget = f"近3小时事件账登记原生候选 {view['images']} 张，另预留 {view['reserved']} 张 / 约{view['imageBudget']}；账更新 {view['ledgerUpdatedAt']}（{ledger_coverage}）。{view['coverageNote']}"
        cards.append(f'<article><h2>{esc(channel)}</h2><b>{complete} 套已闭环 · {len(pending)} 项未闭环（是否运行需实时核对）</b><p>📤 {esc(uploads)}</p><p>🎨 {esc(budget)}</p><p>官方剩余额度：未提供</p><small>上传与生图独立；手机用量可能未入账。未闭环数量不代表当前正在执行数量。</small></article>')
    body = []
    for row, c in zip(rows, checks):
        url = row.get('productionConversationUrl') or ''
        link = f'<a href="{esc(url)}">打开对话</a>' if url else '链接未取得'
        raw = str(row.get('status','')).lower()
        actual, planned = row.get('actualPageCount'), row.get('plannedPageCount')
        live_codex_work = bool(
            row.get('executionChannel') == 'Codex'
            and row.get('workId')
            and row.get('workId') == codex_writer.get('activeWorkId')
            and codex_writer.get('latestThreadStatus') == 'active'
            and row.get('linkVerification') == 'active_thread_and_workId_manifest_verified'
        )
        if c['reusableAfterPageIdleCheck']:
            label = c['state']
        elif live_codex_work and isinstance(actual, int) and isinstance(planned, int):
            accepted = row.get('acceptedPageCount') if isinstance(row.get('acceptedPageCount'), int) else 0
            latest_state = codex_writer.get('activeGenerationState') or '当前页状态待回读'
            label = f'🔵 原writer活跃；已生成 {actual}/{planned} 页、验收 {accepted}/{planned} 页；{latest_state}'
        elif 'dispatch' in raw or 'unverified' in raw:
            label = '🔎 已派发，执行结果未核实'
        elif isinstance(actual, int) and isinstance(planned, int) and 0 < actual < planned:
            label = f'🟡 已生成 {actual}/{planned} 页；待续接（非实时运行状态）'
        elif 'quota' in raw:
            label = '⏳ 回执记载额度受限；需核当前账号'
        elif 'copy_pending' in raw:
            label = '✍️ 图片齐，文案待归档'
        elif 'start_submitted' in raw:
            label = '📨 生图指令已提交；待回读'
        elif 'plan_ready' in raw:
            label = '📝 计划待生产'
        elif 'awaiting_visual' in raw:
            label = '🔎 图片待验收'
        elif any(k in raw for k in ('archived','accepted','complete','completed')):
            label = '✅ 已验收归档'
        elif 'generating' in raw or 'in_progress' in raw or raw == 'running':
            label = '🟠 回执记为处理中；须实时回读页面'
        elif 'priority_repair' in raw:
            label = '🔧 待整套重做'
        else:
            label = '🔎 待核实'
        stage = label
        body.append('<tr>'+''.join(f'<td>{v}</td>' for v in [esc(row.get('executionChannel')),esc(row.get('templateId')),esc(row.get('title')),esc(stage),esc(str(row.get('actualPageCount') or '?')+'/'+str(row.get('plannedPageCount') or '?')),esc('已闭环' if c['reusableAfterPageIdleCheck'] else '、'.join(c['missing'])),link])+'</tr>')
    page = '''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta http-equiv="refresh" content="60"><meta name="viewport" content="width=device-width,initial-scale=1"><title>口述生产线 · 状态</title><style>body{font:15px system-ui;background:#f4f6fa;color:#243249;max-width:1200px;margin:24px auto;padding:16px}article,section{background:white;padding:20px;border-radius:14px;margin:16px 0}.cards{display:grid;grid-template-columns:repeat(2,1fr);gap:14px}small{color:#64748b}a{color:#1769b1}table{width:100%;border-collapse:collapse}td,th{text-align:left;padding:12px 8px;border-bottom:1px solid #e4e9f0}td:nth-child(3){max-width:260px}details{margin:12px 0}.scroll{overflow:auto}.flow{font-size:17px;line-height:2}@media(max-width:800px){.cards{grid-template-columns:1fr}}</style>'''
    page += '''<style>:root{color-scheme:light dark;--bg:#f4f6fa;--surface:#fff;--fg:#243249;--muted:#64748b;--line:#e4e9f0;--link:#1769b1}@media(prefers-color-scheme:dark){:root{--bg:#10151d;--surface:#1b2330;--fg:#e4eaf3;--muted:#a3b2c6;--line:#344155;--link:#8bc3ff}}body{background:var(--bg);color:var(--fg)}article,section{background:var(--surface)}small{color:var(--muted)}a{color:var(--link)}td,th{border-color:var(--line)}#freshness{display:block;margin-top:8px}.stale{color:#d98b20}</style>'''
    page += f'<h1>口述生产线</h1><small>页面生成：{esc(now_local)} · 台账更新时间：{esc(ledger.get("updatedAt"))} · 指标来自本地已落盘数据</small><small id="freshness" data-updated="{esc(ledger.get("updatedAt"))}"></small><small id="sync-freshness" data-updated="{esc(sync_at)}" data-state="{esc(sync_state)}">{esc(sync_text)}</small>'
    page += f'<section><b>{len(active_channels)} 条产线参与调度 · {done} 套完成全部闭环检查</b><p>最近整套正式闭环（图片、文案、验收和通知均完成；不是最近单页生图）：{esc(last_text)}</p><p>最近单页原生生图：{esc(latest_page_text)}</p><p>最近单页验收：{esc(accepted_page_text)}</p><p>队列待续页：{esc(next_page_text)}</p><p>A/B实时控制面：{esc(execution_text)}</p><p>Codex唯一writer：{esc(codex_writer_text)}</p><p>Library辅助通路：{esc(bridge_text)}。Bridge在线不等于账号身份已确认或A/B Library扩展已连通。</p><p>宿主定时边界：{esc(host_schedule_text)}</p><p>素材覆盖 {len({r.get("sourceFolderPath") for r in rows if r.get("sourceFolderPath")})} 套 · 均为本批统计，全库总量未接入。</p><div class="flow">📂 选料 → 📤 上传 → 📝 计划 → 🎨 生图 → ✍️ 文案 → 📥 验收落地 → 🔔 通知 → 🟢 再次投料</div></section>'
    runtime_sources = {item.get('source') for item in live_runtime.values()}
    if runtime_sources == {'runtime_api'}:
        runtime_source_label = '来自本机 4331/4332 只读 runtime API'
    elif runtime_sources == {'direct_local_runtime_file'}:
        runtime_source_label = 'API超时时自动回退到本机 A/B runtime JSON文件；时间以文件实际修改时间为准'
    else:
        runtime_source_label = '按账号展示本机 API或runtime JSON文件来源与各自观察时间'
    page += f'<section><h2>A/B 工作台实时运行面</h2><small>{esc(runtime_source_label)}；用于发现队列空闲、暂停或状态冲突，不替代当前workId会话回读和生图回执。</small><div class="cards">'+''.join(runtime_cards)+'</div></section>'
    scheduled = read(root/'复查安排.json').get('records',[])
    now_local = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=8)))
    scheduled = [x for x in scheduled if x.get('automationScheduled') is True and x.get('status') in ('scheduled', 'active', 'pending')]
    future = []
    for item in scheduled:
        try:
            check_at = datetime.datetime.fromisoformat(str(item.get('checkAt', '')).replace('Z', '+00:00'))
            if check_at.tzinfo is None:
                check_at = check_at.replace(tzinfo=now_local.tzinfo)
            if check_at.astimezone(now_local.tzinfo) > now_local:
                future.append(item)
        except ValueError:
            continue
    schedule_text = '；'.join(f"{x.get('account')} {x.get('stage')}：{x.get('checkAt')}（已安排唤醒）" for x in future)
    if not schedule_text:
        schedule_text = f'无有效冷却等待；待续页：{next_page_text}。兜底巡检每15分钟；此巡检时间不代表页面正在生成。'
    page += f'<section><b>下次复查</b><p>{esc(schedule_text)}</p><small>实际平台时间+5～15分钟；上传与生成分别计算。</small></section>'
    page += '<div class="cards">'+''.join(cards)+'</div>'
    page += f'<p><small>台账有 {len(ledger.get("warnings",[]))} 条证据身份提醒，未绑定到同一workId的历史验收不会自动当成本套完成。</small></p>'
    page += render_commands()
    page += '<section><b>怎么用</b><p>说“开始/继续”：续当前待办；“这个路径整套重做”：找原素材返工；“补料”：只给可用会话上传并出计划；“看看状态”：检查整条A/B产线、最近交付间隔、上传/生图额度与下一步。</p><p>A、B各保留一个生产页面，每套图、文案、验收、落地全部完成后才下一套。生成中保留页面，定点回读即可。冷却到点后延后5～15分钟复查，实际选定时间保存，不反复改约。下载、文件检查、台账、通知可以代码执行；视觉验收仍须真实看图。全部闭环后会话才可复用，上传前再核无生成、无草稿、无其他写入者。</p><details><summary>路径与资料入口</summary><p><a href="生产台账.csv">台账CSV</a> · <a href="生产台账.json">台账JSON</a> · <a href="闭环清单.md">闭环清单</a> · <a href="阶段耗时统计.json">阶段耗时与异常候选</a> · <a href="会话可复用状态.json">会话释放检查</a></p></details></section>'
    page += '<section><h2>每套现在到哪了</h2><div class="scroll"><table><tr><th>产线</th><th>模板</th><th>作品</th><th>阶段</th><th>图片</th><th>闭环待补</th><th>入口</th></tr>'+''.join(body)+'</table></div></section><p><small>后台同步器每15秒检查本地队列、台账源、回执和已登记成品文件变化；变化后重算派生台账/CSV/HTML，页面每60秒重载。页面展示本地已存证据，不直接读取平台官方余额；浏览器真实运行须由执行者回读。任务未闭环不等于正在生成。</small></p><script>function freshness(){const e=document.getElementById("freshness"),t=Date.parse(e.dataset.updated);if(Number.isFinite(t)){const n=Math.max(0,Math.floor((Date.now()-t)/60000));e.textContent="台账数据更新于 "+new Date(t).toLocaleString("zh-CN")+" · 距今 "+n+" 分钟";e.classList.toggle("stale",n>=5)}else e.textContent="台账更新时间未取得";const s=document.getElementById("sync-freshness"),u=Date.parse(s.dataset.updated),state=s.dataset.state;if(state!=="running"||!Number.isFinite(u)){s.classList.add("stale");return}const age=Math.max(0,Math.floor((Date.now()-u)/60000));if(age>=3){s.textContent="后台同步滞后 · 最近成功于 "+new Date(u).toLocaleString("zh-CN")+" · 距今 "+age+" 分钟";s.classList.add("stale")}else{s.textContent="后台同步器运行中 · 最近成功 "+new Date(u).toLocaleString("zh-CN")+" · "+age+" 分钟前";s.classList.remove("stale")}}freshness();setInterval(freshness,10000);</script></html>'
    target = root/'口述生产线状态总览.html'
    with tempfile.NamedTemporaryFile('w', encoding='utf-8', dir=root, delete=False) as handle:
        temporary = handle.name
        handle.write(page)
    try:
        os.replace(temporary, target)
    except PermissionError:
        target.write_text(page, encoding='utf-8')
        Path(temporary).unlink(missing_ok=True)
    return target

if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__); p.add_argument('--task-root',type=Path,required=True)
    print(build(p.parse_args().task_root))


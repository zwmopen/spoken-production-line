"""Derived production snapshots. Import refresh(task_root, template_root) after receipts persist."""
from pathlib import Path
import argparse
import csv
import datetime
import json
import os
import tempfile


def render_board(snapshot, root):
    """Human view of the same snapshot; never interpret dispatch as active generation."""
    def cell(value):
        return str(value or '未取得').replace('|', '｜').replace('\n', ' ')

    def link(label, value):
        return f'[{label}](<{value}>)' if value else f'{label}未取得'

    def state(row):
        status = str(row.get('status', '')).lower()
        if row.get('recordKind') == 'repair_pages':
            if status in ('repair_notified', 'accepted_repaired_notified', 'accepted_archived_and_notified'):
                return '🛠️ 页级返修已闭环', '返修并入父作品；不另计作品套数', 2
            return '🛠️ 页级返修进行中', row.get('nextAction') or '按返修workId补齐验收与通知', 0
        if status == 'priority_repair_pending':
            return '🔴 插队待重做', row.get('nextAction') or '按真实母版重做并验收', -1
        if status in ('archived', 'complete', 'completed', 'accepted', 'accepted_notified', 'accepted_local', 'accepted_and_notified', 'accepted_archived_and_notified', 'accepted_repaired_notified'):
            return '✅ 已验收归档', '核对通知回执与使用次数；齐全后结束', 2
        if 'copy_pending' in status:
            return '✍️ 图片已齐，文案待归档', '保存本套三版文案并完成归档验收', 1
        if 'awaiting_visual' in status:
            return '🔎 图片已保存，待验收', '逐页验收图片，再完成通知与使用计数', 1
        if 'quota' in status:
            return '⏳ 生图受限', '按该账号最新限额记录续接，先处理已有文案和文件', 1
        if 'plan_ready' in status:
            return '📝 已有计划，待生产', '核实素材与计划；额度允许且窗口空闲后生成', 1
        if 'dispatch' in status:
            return '🔎 已分发，交付未核实', '读取执行会话和文件回执，确认是否真正执行', 1
        if 'start_submitted' in status:
            return '🔎 已发送生成指令', '回读实际生成状态与图数，勿重复发送', 1
        if 'generating' in status or 'in_progress' in status or status == 'running':
            return '🔄 回执记录进行中', '先回读现场；仍生成时保持页面', 0
        return '🔎 待核实', '读取该套证据，明确断点后续接', 1

    rows = snapshot['records']
    lines = ['# 口述生产线 · 生产台账', '',
             f"更新时间：{snapshot['updatedAt']}（UTC）｜范围：当前批次", '',
             '> 状态来自已保存回执，不代表实时页面。未知不按零计算；发出指令不算完成。', '',
             '| 产线 | 任务套数 | 已验收归档 | 待验收 | 其余待办 |',
             '|---|---:|---:|---:|---:|']
    for channel in ('GPT A', 'GPT B', 'Codex'):
        group = [r for r in rows if r['executionChannel'] == channel]
        suites = suite_records(group)
        done = sum(state(r)[2] == 2 for r in suites)
        qa = sum('awaiting_visual' in str(r['status']).lower() for r in suites)
        lines.append(f'| {channel} | {len(suites)} | {done} | {qa} | {len(suites)-done-qa} |')
    lines += ['', '## 每套进度', '', '待办排前，已归档排后。图片列为“实际／计划”，文案仅报告文件是否存在。', '',
              '| 产线 · 模板 | 作品 | 当前阶段 | 图片 | 文案文件 | 下一步 | 入口 |',
              '|---|---|---|---|---|---|---|']
    for row in sorted(rows, key=lambda r: (state(r)[2], r['executionChannel'])):
        label, action, _ = state(row)
        actual = row.get('actualPageCount')
        planned = row.get('plannedPageCount')
        pages = f"{actual if actual is not None else '未核实'}／{planned if planned is not None else '未核实'}"
        copy = '📄 已保存' if row.get('copyFileExists') else '未核实'
        entries = ' · '.join((link('执行对话', row.get('productionConversationUrl')), link('模板学习对话', row.get('templateLearningConversationUrl')), link('分发记录', row.get('dispatchedConversationUrl')), link('成品', row.get('productFolderPath')), link('证据', row.get('evidencePath'))))
        lines.append('| ' + ' | '.join(map(cell, [row['executionChannel']+' · '+row['templateId'], row['title'], label, pages, copy, action, entries])) + ' |')
    lines += ['', '## 完整路径与链接', '',
              '- [Excel可打开的CSV](生产台账.csv)：源素材路径、模板路径、三端对话链接、成品路径和任务ID。',
              '- [机器读取JSON](生产台账.json)：同一份数据，供各执行器续接。',
              '- [闭环清单](闭环清单.md)：问题原因、解决方法、验收与历史。', '',
              '更新方式：每次刷新台账，同时生成此页面。网页A、网页B、Codex的额度分别读取各自记录。', '']
    with tempfile.NamedTemporaryFile('w', encoding='utf-8', dir=root, delete=False) as handle:
        temporary = handle.name
        handle.write('\n'.join(lines))
    try:
        os.replace(temporary, root / '生产看板.md')
    except PermissionError:
        # The app may hold the preview handle; keep its path and rewrite contents.
        (root / '生产看板.md').write_text('\n'.join(lines), encoding='utf-8')
        Path(temporary).unlink(missing_ok=True)


def pick(data, *keys):
    for key in keys:
        value = data
        for part in key.split('.'):
            value = value.get(part) if isinstance(value, dict) else None
        if value is not None and value != '':
            return value
    return None


def dedupe_records(rows):
    """Merge mirrored priority/queue rows by channel and concrete workId.

    Keep the newest state as authoritative. On equal timestamps prefer the
    account queue over the global priority mirror. Fill only blank descriptive
    fields from older mirrors, so stale copies cannot revert a rejection or a
    completion state.
    """
    grouped = {}
    queue_rank = {'queue-A.json': 2, 'queue-B.json': 2}

    def key(row):
        work_id = row.get('workId')
        if work_id:
            return row.get('executionChannel', ''), work_id
        return (row.get('executionChannel', ''),
                'unbound:' + str(row.get('evidencePath', '')) + ':' + str(row.get('title', '')))

    def rank(row):
        try:
            observed = datetime.datetime.fromisoformat(
                str(row.get('stateUpdatedAt', '')).replace('Z', '+00:00')).timestamp()
        except (TypeError, ValueError, OSError):
            observed = 0
        evidence = Path(str(row.get('evidencePath', ''))).name
        return observed, queue_rank.get(evidence, 1)

    for row in rows:
        record_key = key(row)
        if record_key not in grouped:
            grouped[record_key] = dict(row)
            continue
        existing = grouped[record_key]
        incoming = dict(row)
        if rank(incoming) > rank(existing):
            winner, older = incoming, existing
        else:
            winner, older = existing, incoming
        merged = dict(winner)
        for field, value in older.items():
            if merged.get(field) in (None, '') and value not in (None, ''):
                merged[field] = value
        grouped[record_key] = merged
    return list(grouped.values())


def suite_records(rows):
    """Page-repair workIds remain visible but do not count as new suites."""
    return [row for row in rows if row.get('recordKind') != 'repair_pages']


def refresh(task_root, template_root):
    root, lib = Path(task_root).resolve(), Path(template_root).resolve()
    if not root.is_dir():
        raise ValueError('Missing task root: ' + str(root))
    warnings, rows = [], []

    def read(path):
        if not path.is_file():
            return {}
        try:
            data = json.loads(path.read_text(encoding='utf-8-sig'))
            if isinstance(data, dict):
                return data
            raise ValueError('Expected JSON object')
        except (OSError, ValueError) as exc:
            warnings.append({'path': str(path), 'error': str(exc)})
            return {}

    def path_value(value, base):
        if not isinstance(value, str) or not value:
            return None
        path = Path(value)
        return path if path.is_absolute() else base / path

    def blank():
        row = dict.fromkeys(('workId sourceTaskId dispatchWorkId title sourceFolderPath templateId templateTitle localTemplatePath codexTemplateUrl gptATemplateUrl gptBTemplateUrl executionChannel productionConversationUrl templateLearningConversationUrl dispatchedConversationUrl status productFolderPath planMessageId startMessageId notificationReceiptPath evidencePath manifestPath copyStatus copyFilePath quotaErrorType quotaResetsAt nextAction stateUpdatedAt acceptanceStatus recordKind parentWorkId').split(), '')
        row.update(actualPageCount=None, acceptedPageCount=None, plannedPageCount=None, storedImageCount=None, copyFileExists=None, usageRecorded=None, linkVerification='registry_record_not_live_verified')
        return row

    def merge(row, data):
        mapping = {
            'workId': ('workId',), 'title': ('source.title', 'title'),
            'sourceFolderPath': ('sourcePath', 'sourceFolderPath', 'source.path'),
            'templateId': ('templateId', 'template.id'), 'templateTitle': ('templateTitle', 'template.name'),
            'localTemplatePath': ('localTemplatePath', 'template.path'),
            'productionConversationUrl': ('conversation.url', 'conversationUrl'),
            'status': ('status',), 'plannedPageCount': ('plannedPageCount', 'plannedPages', 'output.plannedPageCount'),
            'actualPageCount': ('actualPageCount', 'acceptedPages', 'actualImageCount', 'images', 'output.actualPageCount'),
            'copyStatus': ('copyStatus', 'copy.status', 'qa.copyStatus'), 'acceptanceStatus': ('qa.status', 'acceptance.status'),
            'quotaErrorType': ('imagegenAttempt.structuredError.type', 'qa.imagegenAttempt.structuredError.type'),
            'quotaResetsAt': ('imagegenAttempt.resetsAtLocal', 'qa.imagegenAttempt.resetsAtLocal'),
            'nextAction': ('nextAction',), 'stateUpdatedAt': ('updatedAt', 'observedAt')}
        for field, candidates in mapping.items():
            value = pick(data, *candidates)
            if value is None:
                continue
            if field in ('plannedPageCount', 'actualPageCount') and (isinstance(value, bool) or not isinstance(value, int)):
                continue
            row[field] = value

    def template(row):
        path = path_value(row['localTemplatePath'], lib)
        if path is None and row['templateTitle']:
            path = lib / row['templateTitle']
        if path is not None and not path.is_dir() and row['templateTitle']:
            matches = [p for p in lib.rglob(row['templateTitle']) if p.is_dir()]
            path = matches[0] if len(matches) == 1 else path
        if path is not None:
            row['localTemplatePath'] = str(path)
            links = read(path / 'template.json').get('conversationLinks') or {}
            for field, channel in [('codexTemplateUrl', 'codex'), ('gptATemplateUrl', 'gpt1'), ('gptBTemplateUrl', 'gpt2')]:
                row[field] = pick(links, channel + '.url') or row[field]
            row['templateLearningConversationUrl'] = pick(links, 'codex.url') or row['templateLearningConversationUrl']

    def product(row, path):
        if path is None:
            return
        row['productFolderPath'] = str(path)
        manifest_path = path / 'manifest.json'
        manifest = read(manifest_path)
        if manifest:
            if row.get('recordKind') == 'repair_pages':
                # A repair row may point at its parent's accepted package for
                # visibility. Never let that parent manifest overwrite the
                # child workId, status, or 2-page repair counts.
                row['manifestPath'] = str(manifest_path)
            else:
                merge(row, manifest)
            row['manifestPath'] = str(manifest_path)
        if path.is_dir() and row.get('recordKind') != 'repair_pages':
            row['storedImageCount'] = sum(1 for p in path.iterdir() if p.is_file() and p.suffix.lower() in {'.png', '.jpg', '.jpeg', '.webp'})
        copy_path = path_value(pick(manifest, 'copy.file', 'output.copyFile', 'copyFile'), path)
        if copy_path is None and (path / '文案.txt').is_file():
            copy_path = path / '文案.txt'
        if copy_path and row.get('recordKind') != 'repair_pages':
            row['copyFilePath'], row['copyFileExists'] = str(copy_path), copy_path.is_file()
        # Expected output.imageFiles do not prove saved files or visual acceptance.

    def timestamp(item):
        path, data = item
        for field in ('updatedAt', 'observedAt', 'createdAt'):
            try:
                return datetime.datetime.fromisoformat(data[field].replace('Z', '+00:00')).timestamp()
            except (KeyError, TypeError, ValueError):
                pass
        return path.stat().st_mtime

    priority_path = root / '优先生产队列.json'
    for task in read(priority_path).get('tasks', []):
        row = blank()
        row.update({key: value for key, value in task.items() if key in row})
        row['evidencePath'] = str(priority_path)
        template(row)
        rows.append(row)

    for account in ('A', 'B'):
        queue_path = root / f'queue-{account}.json'
        for task in read(queue_path).get('tasks', []):
            row = blank()
            # A plan/source task id can differ from a concrete production workId
            # after a retry or a repair. Preserve both identities so acceptance,
            # source usage, notices, and rolling counts stay idempotent.
            source_task_id = task.get('sourceTaskId') or task.get('id', '')
            work_id = task.get('workId') or task.get('id', '')
            row.update(workId=work_id, sourceTaskId=source_task_id, dispatchWorkId=task.get('dispatchWorkId', ''), title=task.get('title', ''), sourceFolderPath=task.get('sourcePath', ''), templateId=task.get('templateId', ''), templateTitle=task.get('templateTitle', ''), executionChannel='GPT ' + account, productionConversationUrl=task.get('conversationUrl') or task.get('templateUrl', ''), status=task.get('generationStatus') or task.get('status', ''), planMessageId=task.get('planMessageId', ''), startMessageId=task.get('startMessageId', ''), evidencePath=str(queue_path), plannedPageCount=pick(task, 'plannedPageCount', 'planPageCount'), actualPageCount=pick(task, 'actualPageCount', 'acceptedPageCount'), acceptedPageCount=pick(task, 'acceptedPageCount', 'actualPageCount'), productFolderPath=pick(task, 'productFolderPath', 'productPath') or '', manifestPath=task.get('manifestPath', ''), copyFilePath=pick(task, 'copyFilePath', 'copyPath') or '', copyStatus=task.get('copyStatus', ''), nextAction=task.get('nextAction', ''), stateUpdatedAt=pick(task, 'stateUpdatedAt', 'updatedAt') or '', acceptanceStatus=task.get('acceptanceStatus') or task.get('deliveryStatus') or '', notificationReceiptPath=task.get('notificationReceiptPath', ''), recordKind=task.get('recordKind', 'suite'), parentWorkId=task.get('parentWorkId', ''))
            template(row)
            acceptance_path = path_value(task.get("acceptanceReceiptPath"), root) if task.get("acceptanceReceiptPath") else root / f"{account}-{row['templateId']}交付验收.json"
            acceptance = read(acceptance_path)
            if acceptance and acceptance.get('workId') != row['workId']:
                if task.get('acceptanceReceiptPath'):
                    warnings.append({'path': str(acceptance_path), 'error': 'Acceptance workId mismatch; ignored'})
                acceptance = {}
            local = read(root / f"{row['templateId']}本地落地.json") if account == 'A' else {}
            archive_path = path_value(task.get("archiveReceiptPath"), root) if task.get("archiveReceiptPath") else root / f"{account}-{row['templateId']}归档回执.json"
            archive = read(archive_path)
            if archive and archive.get('workId') != row['workId']:
                # Template-level legacy receipts may belong to a previous suite.
                # Warn for an explicit task binding; otherwise skip the unrelated fallback.
                if task.get('archiveReceiptPath'):
                    warnings.append({'path': str(archive_path), 'error': 'Archive workId mismatch; ignored'})
                archive = {}
            product_path = (pick(acceptance, 'productPath') or pick(local, 'productPath') or
                            pick(archive, 'productPath', 'productFolderPath') or row.get('productFolderPath'))
            product(row, path_value(product_path, root))
            if acceptance:
                merge(row, acceptance)
                row['evidencePath'] = str(acceptance_path)
            if archive:
                merge(row, archive)
                if archive.get('status') in ('ACCEPTED_NOTIFIED', 'ACCEPTED_LOCAL'):
                    row['status'] = archive['status']
                row['evidencePath'] = str(archive_path)
            notice_candidates = [root / f"{account}-{row['templateId']}飞书落地通知.json",
                                 root / f"{account}-{row['templateId']}成品通知回执.json"]
            notice = next((p for p in notice_candidates if p.is_file() and read(p).get('workId') == row['workId']), None)
            if notice:
                row['notificationReceiptPath'] = str(notice)
            rows.append(row)

    for folder in sorted(root.glob('Codex-*实测')):
        dispatch_path = folder / '分发回执.json'
        dispatch = read(dispatch_path)
        dispatch_id = dispatch.get('workId', '')
        receipts = [(p, read(p)) for p in folder.rglob('progress*.json')]
        related = [(p, d) for p, d in receipts if d and (not dispatch_id or dispatch_id in (d.get('taskId'), d.get('workId')))]
        if receipts and not related:
            warnings.append({'path': str(folder), 'error': 'Progress identity mismatch; ignored'})
        progress_path, progress = max(related, key=timestamp) if related else (dispatch_path, {})
        if not dispatch and not progress:
            continue
        source_id = dispatch.get('sourceWebTask') or progress.get('sourceTaskId')
        if not source_id and dispatch_id.startswith('CODEX-PILOT-'):
            source_id = dispatch_id.removeprefix('CODEX-PILOT-')
        origin = next((r for r in rows if r['sourceTaskId'] == source_id), None)
        row = blank()
        if origin:
            for field in ('sourceTaskId', 'title', 'sourceFolderPath', 'templateId', 'templateTitle', 'localTemplatePath', 'codexTemplateUrl', 'gptATemplateUrl', 'gptBTemplateUrl'):
                row[field] = origin[field]
        row.update(workId=dispatch_id, dispatchWorkId=dispatch_id, sourceTaskId=source_id or '', templateId=dispatch.get('templateId', row['templateId']), executionChannel='Codex', status='DISPATCHED_DELIVERY_UNVERIFIED', evidencePath=str(dispatch_path))
        def thread_url(value):
            if not isinstance(value, str) or not value:
                return ''
            return value if value.startswith(('codex://', 'https://', 'http://')) else 'codex://threads/' + value

        # The dispatch thread may be the template-learning chat. Keep it separate
        # and only call a thread the production conversation when a generation
        # receipt or an explicit execution field identifies it.
        dispatch_thread = (dispatch.get('dispatchedConversationUrl') or
                           dispatch.get('templateLearningConversationUrl') or dispatch.get('threadId'))
        row['dispatchedConversationUrl'] = thread_url(dispatch.get('dispatchedConversationUrl'))
        if dispatch_thread and not row['templateLearningConversationUrl']:
            row['templateLearningConversationUrl'] = thread_url(dispatch_thread)
        if dispatch.get('threadId'):
            row['dispatchedConversationUrl'] = thread_url(dispatch.get('dispatchedConversationUrl') or dispatch.get('threadId'))
        execution_thread = None  # assigned after manifest/progress merges below
        product_path = path_value(pick(progress, 'productPath', 'productFolderPath', 'output.path'), folder)
        product(row, product_path)
        merge(row, progress)
        if progress:
            row['evidencePath'] = str(progress_path)
            copy_path = path_value(pick(progress, 'copyFile'), product_path) if product_path else None
            if copy_path:
                row['copyFilePath'], row['copyFileExists'] = str(copy_path), copy_path.is_file()
        template(row)
        generation_receipt_path = path_value(pick(progress, 'imagegenAttempt.receiptPath', 'generationReceiptPath'), folder)
        generation_receipt = read(generation_receipt_path) if generation_receipt_path else {}
        execution_thread = (pick(dispatch, 'productionConversationUrl', 'productionExecutionThreadUrl', 'executionThreadUrl') or
                            pick(progress, 'productionExecutionThreadUrl', 'executionThreadUrl', 'imagegenAttempt.executionThreadUrl', 'imagegenAttempt.executionThreadId') or
                            pick(generation_receipt, 'productionConversationUrl', 'executionThreadUrl', 'productionExecutionThreadUrl', 'threadId'))
        # A manifest's generic conversationUrl can be the template-learning chat;
        # replace it only with a generation receipt's explicit execution thread.
        row['productionConversationUrl'] = thread_url(execution_thread)
        rows.append(row)

    # The original Codex API writer can be producing directly into the API
    # staging root before a `Codex-*实测` dispatch/receipt folder exists. Keep
    # that live work visible in the canonical derived ledger using only a
    # same-thread, same-workId manifest readback. Do not mutate its queue.
    current_execution = read(root / '当前执行通路核查.json')
    live_codex = pick(current_execution, 'latestRecheck.Codex') or {}
    codex_writer = current_execution.get('codexWriter') or {}
    thread_id = live_codex.get('threadId')
    work_id = live_codex.get('workId')
    native_hint = path_value(live_codex.get('nativePath'), root)
    manifest_path = path_value(live_codex.get('manifestPath'), root)
    if manifest_path is None and native_hint is not None:
        manifest_path = native_hint.parent.parent / 'manifest.json'
    active_thread_verified = bool(
        thread_id and thread_id == codex_writer.get('threadId')
        and live_codex.get('threadStatus') == 'active')
    if active_thread_verified and work_id and manifest_path and manifest_path.is_file():
        manifest = read(manifest_path)
        if (manifest.get('workId') == work_id
                and manifest.get('status') in ('PRODUCING', 'RUNNING')
                and not any(r.get('workId') == work_id for r in rows)):
            native_root = path_value(pick(manifest, 'nativeRoot'), manifest_path.parent)
            native_files = ([p for p in native_root.iterdir()
                             if p.is_file() and p.suffix.lower() in {'.png', '.jpg', '.jpeg', '.webp'}]
                            if native_root and native_root.is_dir() else [])
            accepted_outputs = manifest.get('acceptedOutputs')
            accepted_outputs = accepted_outputs if isinstance(accepted_outputs, list) else []
            planned_pages = pick(manifest, 'productionPlan.plannedPages')
            generated_pages = pick(manifest, 'generatedPageCount', 'generatedPages')
            source = manifest.get('source') if isinstance(manifest.get('source'), dict) else {}
            selected_template = manifest.get('template') if isinstance(manifest.get('template'), dict) else {}
            row = blank()
            row.update(
                workId=work_id,
                sourceTaskId=manifest.get('taskId', ''),
                dispatchWorkId=manifest.get('queueId', ''),
                title=manifest.get('title', ''),
                sourceFolderPath=source.get('path', ''),
                templateId=selected_template.get('id', ''),
                templateTitle=selected_template.get('name', ''),
                localTemplatePath=selected_template.get('path', ''),
                executionChannel='Codex',
                productionConversationUrl='codex://threads/' + thread_id,
                dispatchedConversationUrl='codex://threads/' + thread_id,
                status='PRODUCING',
                productFolderPath=manifest.get('outputRoot', str(manifest_path.parent)),
                evidencePath=str(manifest_path),
                manifestPath=str(manifest_path),
                nextAction=manifest.get('nextAction', ''),
                stateUpdatedAt=manifest.get('updatedAtUtc') or manifest.get('updatedAt', ''),
                acceptanceStatus=f"{len(accepted_outputs)}/{planned_pages or '?'} pages accepted; suite not archived",
                actualPageCount=generated_pages if isinstance(generated_pages, int) else len(native_files),
                acceptedPageCount=len(accepted_outputs),
                plannedPageCount=planned_pages if isinstance(planned_pages, int) else None,
                storedImageCount=len(native_files),
                copyFileExists=bool((Path(manifest.get('copyRoot') or manifest_path.parent) / '文案.txt').is_file()),
                linkVerification='active_thread_and_workId_manifest_verified',
            )
            template(row)
            rows.append(row)

    rows = dedupe_records(rows)

    for row in rows:
        if row['sourceFolderPath']:
            tags = read(Path(row['sourceFolderPath']) / '.tags.json')
            events = tags.get('usageEvents')
            if isinstance(events, list):
                row['usageRecorded'] = any(isinstance(e, dict) and e.get('workId') == row['workId'] for e in events)
    snapshot = {'schemaVersion': 2, 'updatedAt': datetime.datetime.now(datetime.timezone.utc).isoformat(), 'sourceOfTruth': ['queue-A.json', 'queue-B.json', '本地template.json', 'Codex分发与progress回执', 'active Codex writer thread readback + workId-bound manifest', '成品manifest', '单套验收和通知回执', '源素材usageEvents'], 'scope': '当前批次，非全局所有历史生产', 'warnings': warnings, 'records': rows}
    with tempfile.NamedTemporaryFile('w', encoding='utf-8', dir=root, delete=False) as handle:
        temporary = handle.name
        json.dump(snapshot, handle, ensure_ascii=False, indent=2)
    os.replace(temporary, root / '生产台账.json')
    with tempfile.NamedTemporaryFile('w', encoding='utf-8-sig', newline='', dir=root, delete=False) as handle:
        temporary = handle.name
        # Queue schemas evolve over time. Keep every derived field in the CSV
        # instead of failing the whole refresh when a newer row adds a key.
        fieldnames = list(blank())
        known = set(fieldnames)
        for row in rows:
            for field in row:
                if field not in known:
                    fieldnames.append(field)
                    known.add(field)
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    os.replace(temporary, root / '生产台账.csv')
    render_board(snapshot, root)
    from stage_timing import analyze
    analyze(root)
    from build_simple_panel import build
    build(root)
    return snapshot


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--task-root', required=True)
    parser.add_argument('--template-root', required=True)
    args = parser.parse_args()
    snapshot = refresh(args.task_root, args.template_root)
    print(json.dumps({'records': len(snapshot['records']), 'warnings': len(snapshot['warnings'])}))


if __name__ == '__main__':
    main()

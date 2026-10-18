"""Read-only rolling account view. Never writes production queues or usage ledgers."""
import datetime as dt
import json
from pathlib import Path

def read(path):
    try:
        return json.loads(Path(path).read_text(encoding='utf-8-sig'))
    except (OSError, ValueError):
        return {}

def instant(value):
    try:
        result = dt.datetime.fromtimestamp(value, dt.timezone.utc) if isinstance(value,(int,float)) else dt.datetime.fromisoformat(value.replace('Z','+00:00'))
        return result if result.tzinfo else None
    except (TypeError,ValueError,AttributeError,OverflowError):
        return None

def account_view(root, account, now=None):
    root=Path(root); now=now or dt.datetime.now(dt.timezone.utc); cutoff=now-dt.timedelta(minutes=180)
    ledger=read(root/f'{account}生图滚动账.json'); config=read(root/'循环生产配置.json'); queue=read(root/f'queue-{account}.json')
    ledger_updated=instant(ledger.get('updatedAt'))
    ledger_fresh=bool(ledger_updated and cutoff<=ledger_updated<=now)
    ledger_age_minutes=round((now-ledger_updated).total_seconds()/60) if ledger_updated else None
    images=0; seen=set()
    for event in ledger.get('events',[]):
        at=instant(event.get('at'))
        if at is None or not cutoff<=at<=now: continue
        key=event.get('eventId') or event.get('messageId') or json.dumps(event,sort_keys=True,ensure_ascii=False)
        if key in seen: continue
        seen.add(key); images+=event.get('uniqueOutputs',0) or 0
    uploaded={}; estimated=False; upload_evidence_times=[]
    candidates=list(queue.get('tasks',[]))
    for path in root.glob(f'{account}*上传回执.json'):
        item=read(path)
        if isinstance(item,dict):
            candidates.append(item)
            for field in ('submittedAt','sentAt','uploadedAt','createdAt'):
                stamp=instant(item.get(field))
                if stamp: upload_evidence_times.append(stamp)
    for item in candidates:
        mid=item.get('serverMessageId') or item.get('messageId')
        attachments=item.get('serverAttachments')
        if not mid or not isinstance(attachments,list) or not attachments: continue
        at=instant(item.get('submittedAt')); fallback=False
        if at is None:
            at=instant(item.get('preparedAt')); fallback=at is not None
        if at: upload_evidence_times.append(at)
        if at is not None and cutoff<=at<=now:
            uploaded[mid]=max(uploaded.get(mid,0),len(attachments)); estimated=estimated or fallback
    latest_upload_evidence=max(upload_evidence_times) if upload_evidence_times else None
    upload_fresh=bool(latest_upload_evidence and cutoff<=latest_upload_evidence<=now)
    reserved=int(ledger.get('pendingReservedPages') or 0)
    unknown=sum(1 for t in queue.get('tasks',[]) if 'SUBMISSION_STATUS_UNKNOWN' in str(t.get('generationStatus','')))
    reserved=max(reserved,unknown)
    return dict(images=images,reserved=reserved,uploads=sum(uploaded.values()),
        uploadBudget=config.get('uploadResumePolicy',{}).get('softAttachmentThreshold',80),
        imageBudget=config.get('imageGenerationBudget',{}).get('perAccount',50),
        ledgerUpdatedAt=ledger.get('updatedAt','未取得'),
        ledgerFresh=ledger_fresh,ledgerAgeMinutes=ledger_age_minutes,
        latestUploadEvidenceAt=latest_upload_evidence.isoformat() if latest_upload_evidence else None,
        uploadEvidenceFresh=upload_fresh,
        coverageNote=('本地事件账覆盖当前窗口；平台官方余量与外端用量仍未知' if ledger_fresh and upload_fresh
                      else '本地回执/事件账未覆盖完整近3小时；显示值仅为已登记记录，实际总用量未知'),
        uploadTimingNote='部分提交时间未知，以准备时间保守入窗' if estimated else '仅本地有回执记录，外端未知')

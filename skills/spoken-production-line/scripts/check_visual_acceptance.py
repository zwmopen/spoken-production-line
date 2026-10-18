"""Validate a visual review receipt. This does not perform visual recognition."""
import argparse
import hashlib
import json
from pathlib import Path
from urllib.parse import urlparse


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _read_json(path):
    try:
        return json.loads(Path(path).read_text(encoding='utf-8-sig'))
    except (OSError, UnicodeError, json.JSONDecodeError, TypeError):
        return None


def _migration_binding_errors(receipt, binding):
    """Check the real template-to-production handoff for template migration.

    A migrated production conversation is intentionally separate from the
    template-learning conversation. Requiring equal conversation IDs here
    rejected the workflow the production plan explicitly selected.
    """
    errors = []
    template_url = binding.get('templateConversationUrl') or ''
    production_url = binding.get('productionConversationUrl') or ''
    template_id = binding.get('templateId')
    work_id = receipt.get('workId')
    template_chat = conversation_id(template_url)
    production_chat = conversation_id(production_url)
    if not template_chat or not production_chat:
        return ['模板或生产会话链接无有效会话ID']
    if template_chat == production_chat:
        return errors

    migration = binding.get('templateMigration') or {}
    if migration.get('mode') != 'template_migration' or migration.get('newConversationRequired') is not True:
        return ['模板会话与生产会话不同，缺少明确的新会话迁移授权']

    selection_path = migration.get('selectionReceiptPath') or binding.get('templateLearningReceiptPath')
    selection = _read_json(selection_path) if selection_path else None
    if not selection:
        errors.append('模板迁移选择回执不存在或不可读')
    else:
        if selection.get('receiptType') != 'real_template_migration_selection':
            errors.append('模板迁移选择回执类型不匹配')
        if selection.get('selectedTemplateId') != template_id:
            errors.append('模板迁移选择回执中的模板ID不匹配')
        if selection.get('templateChatReference') != template_url:
            errors.append('模板迁移选择回执中的真实模板会话不匹配')
        if selection.get('repairWorkId') != work_id:
            errors.append('模板迁移选择回执未绑定当前workId')
        if selection.get('newConversationRequired') is not True:
            errors.append('模板迁移选择回执未声明独立新作会话')

    plan_path = migration.get('planReadbackReceiptPath')
    plan = _read_json(plan_path) if plan_path else None
    if not plan:
        errors.append('新作会话计划回读回执不存在或不可读')
    else:
        if plan.get('workId') != work_id:
            errors.append('计划回读回执未绑定当前workId')
        if plan.get('templateId') != template_id:
            errors.append('计划回读回执中的模板ID不匹配')
        if plan.get('conversationId') != production_chat:
            errors.append('计划回读回执中的生产会话ID不匹配')
        if plan.get('status') != 'PLAN_READBACK_VERIFIED':
            errors.append('新作会话计划回读未验证')

    source_path = binding.get('sourceAttachmentReceiptPath')
    source = _read_json(source_path) if source_path else None
    if not source:
        errors.append('来源附件回读回执不存在或不可读')
    else:
        if source.get('workId') != work_id or source.get('conversationId') != production_chat:
            errors.append('来源附件回读未绑定当前workId和生产会话')
        if source.get('sourceImageCount', 0) <= 0 or source.get('textAttachmentCount', 0) <= 0:
            errors.append('来源图片或文案TXT附件回读缺失')
    return errors


def conversation_id(value):
    parsed = urlparse(value or '')
    return parsed.path.rstrip('/').split('/')[-1] if parsed.scheme in ('https', 'codex') else ''


def check(receipt):
    errors = []
    binding = receipt.get('binding') or {}
    for field in ('accountIdentity', 'templateId', 'templateConversationUrl',
                  'productionConversationUrl', 'sourceMessageId', 'planMessageId',
                  'generationMessageId', 'templateLearningReceiptPath',
                  'sourceAttachmentReceiptPath', 'conversationReadbackPath'):
        if not binding.get(field):
            errors.append('缺生产绑定证据: ' + field)
    expected = conversation_id(binding.get('templateConversationUrl'))
    actual = conversation_id(binding.get('productionConversationUrl'))
    for field in ('templateLearningReceiptPath', 'sourceAttachmentReceiptPath', 'conversationReadbackPath'):
        if not Path(binding.get(field) or '').is_file():
            errors.append('绑定回执文件不存在: ' + field)
    if expected and actual and expected != actual:
        errors.extend(_migration_binding_errors(receipt, binding))
    elif not expected or not actual:
        errors.append('模板或生产会话链接无有效会话ID')
    if not receipt.get('workId') or not receipt.get('reviewer') or not receipt.get('reviewedAt'):
        errors.append('缺作品ID、验收执行者或验收时间')
    pages = receipt.get('pages') or []
    if not pages or len(pages) != receipt.get('expectedPageCount'):
        errors.append('逐页验收数量与本次验收范围不一致')
    seen = set()
    for item in pages:
        page = item.get('page')
        if not page or page in seen:
            errors.append('页码缺失或重复')
        seen.add(page)
        for kind in ('output', 'template', 'source'):
            ref = item.get(kind) or {}
            path = Path(ref.get('path') or '')
            if not path.is_file() or not ref.get('sha256'):
                errors.append(f'{page}: 缺{kind}真实文件或哈希')
            elif digest(path) != ref['sha256']:
                errors.append(f'{page}: {kind}文件已变更，旧验收失效')
        for field in ('layout', 'typography', 'decoration', 'sourceFacts', 'naturalness'):
            finding = (item.get('checks') or {}).get(field) or {}
            result = finding.get('result')
            accepted_variation = (field != 'sourceFacts' and result == 'acceptable_variation'
                                  and finding.get('blocking') is False and bool(finding.get('acceptanceReason')))
            if (result != 'pass' and not accepted_variation) or not finding.get('observation'):
                errors.append(f'{page}: {field}未通过或缺具体观察')
        if item.get('unresolvedDefects') != []:
            errors.append(f'{page}: 缺陷未清零或未记录')
        for source in item.get('sourceMappings', []):
            path = Path(source.get('path') or '')
            if not path.is_file() or not source.get('sha256'):
                errors.append(f'{page}: 来源映射缺真实文件或哈希')
            elif digest(path) != source['sha256']:
                errors.append(f'{page}: 来源映射文件已变更，旧验收失效')
    return {'accepted': not errors, 'errors': errors,
            'scope': '证据结构与文件绑定检查，不代替逐页视觉判断'}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('receipt', type=Path)
    args = parser.parse_args()
    result = check(json.loads(args.receipt.read_text(encoding='utf-8-sig')))
    print(json.dumps(result, ensure_ascii=False, indent=2))
    raise SystemExit(0 if result['accepted'] else 2)

"""Add evidence-based metadata to an explicitly scoped audit batch; never invent missing links."""
import argparse
import datetime
import hashlib
import json
import os
from pathlib import Path
import tempfile


def upgrade(audit_path, template_root):
    audit_path, template_root = Path(audit_path), Path(template_root)
    audit = json.loads(audit_path.read_text(encoding='utf-8-sig'))
    results = []
    templates = {}
    for path in template_root.glob('*/template.json'):
        data = json.loads(path.read_text(encoding='utf-8-sig'))
        templates[(data.get('template') or {}).get('id')] = (path.parent, data)
    for item in audit['rows']:
        if not item.get('formal'):
            continue
        folder = Path(item['productPath'])
        path = folder / 'manifest.json'
        original = path.read_bytes()
        data = json.loads(original.decode('utf-8-sig'))
        if data.get('workId') != item['workId']:
            raise ValueError('Work identity mismatch: ' + str(path))
        backup = path.with_name('manifest.before-metadata-v3.json.bak')
        if not backup.exists():
            backup.write_bytes(original)
        now = datetime.datetime.now(datetime.timezone.utc).isoformat()
        template_path, template = templates.get(data.get('template'), (None, {}))
        source_path = data.get('sourceMaterialPath')
        files = [{'path': str(p), 'sha256': hashlib.sha256(p.read_bytes()).hexdigest(), 'bytes': p.stat().st_size}
                 for p in sorted(folder.glob('P*.png'))]
        data.update(metadataSchemaVersion='3.0', localFolderPath=str(folder),
                    metadataUpdatedAt=now, deliveryStatus='REVIEW_FAILED_PENDING_REMAKE')
        data['provenance'] = {
            'declaredChannel': data.get('pipeline'),
            'actualChannelVerification': 'unverified_conversation_binding',
            'accountLabel': 'zwmrpg', 'accountLabelSource': 'legacy_pipeline_label_only',
            'verifiedAccountId': None, 'instance': 'A',
            'productionConversationUrl': None, 'productionConversationId': None,
            'sourceMessageId': None, 'planMessageId': None, 'generationMessageIds': [],
            'copyMessageId': None, 'evidencePath': str(audit_path),
            'generatorScriptPath': item['script'], 'generatorScriptSha256': item['scriptSHA'],
            'missingReason': '旧脚本未留存会话及消息回执；不从模板注册URL推断实际生产URL'}
        data['templateBinding'] = {
            'declaredTemplateId': data.get('template'),
            'localTemplatePath': str(template_path) if template_path else None,
            'registeredConversationLinks': template.get('conversationLinks', {}),
            'registeredLinksAreActualProductionEvidence': False,
            'bindingStatus': 'UNVERIFIED', 'visualMatchStatus': 'FAILED_COVER',
            'referenceFiles': [{'path': str(p), 'sha256': hashlib.sha256(p.read_bytes()).hexdigest()}
                               for p in sorted(template_path.glob('模板-P*')) if p.is_file()] if template_path else []}
        data['sourceBinding'] = {'localFolderPath': source_path,
                                 'pathExistsAtAudit': Path(source_path).is_dir() if source_path else False,
                                 'actualReadOrUploadReceiptPath': None,
                                 'bindingStatus': 'UNVERIFIED_ACTUAL_INPUTS'}
        data['outputInventory'] = {'localFolderPath': str(folder), 'files': files,
                                   'physicalImageCount': len(files),
                                   'acceptedImageCount': None,
                                   'copyPath': str(folder / '文案.txt') if (folder / '文案.txt').is_file() else None}
        data.setdefault('legacyAssessment', {k: data.get(k) for k in ('qualityScore', 'defectCount', 'hasSourceCompare')})
        data.update(qualityScore=None, defectCount=None, hasSourceCompare=False)
        data['acceptance'] = {'status': 'FAILED_TEMPLATE_BINDING_AND_COVER',
                              'reviewedAt': now, 'reviewer': 'Codex',
                              'reviewScope': '脚本溯源与既有封面问题；非整套逐页视觉验收',
                              'evidencePath': str(audit_path),
                              'legacyPerfectScoreValid': False,
                              'unresolvedDefects': ['模板会话及实际输入绑定无证据', '硬编码版式与真实T88不符'],
                              'nextAction': '整套重新绑定真实模板与素材后重做并逐页验收'}
        data.setdefault('attempts', [])
        data.setdefault('timing', {'clockSource': 'UNVERIFIED_LEGACY', 'events': [],
                                   'note': '不以目录时间或文件mtime冒充实际生成时间'})
        data.setdefault('notifications', [])
        data.setdefault('cloudSync', {'status': 'UNVERIFIED', 'folderUrl': None, 'lastVerifiedAt': None})
        history = data.setdefault('metadataChangeLog', [])
        if not any(x.get('event') == 'audit_metadata_v3' for x in history):
            history.append({'event': 'audit_metadata_v3', 'at': now, 'actor': 'Codex',
                            'reason': '补证据链、撤销无依据满分；未知信息不补造', 'backupPath': str(backup)})
        with tempfile.NamedTemporaryFile('w', encoding='utf-8', dir=folder, delete=False) as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2)
            temporary = handle.name
        os.replace(temporary, path)
        saved = json.loads(path.read_text(encoding='utf-8'))
        results.append({'workId': saved['workId'], 'manifestPath': str(path), 'backupPath': str(backup),
                        'status': saved['deliveryStatus'], 'missingConversationPreserved': saved['provenance']['productionConversationUrl'] is None})
    return results


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--audit', required=True)
    parser.add_argument('--template-root', required=True)
    parser.add_argument('--receipt', required=True)
    args = parser.parse_args()
    results = upgrade(args.audit, args.template_root)
    Path(args.receipt).write_text(json.dumps({'results': results}, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({'updated': len(results), 'readback': True}))

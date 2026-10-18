"""Offline checks for source and template-migration binding validation."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from check_visual_acceptance import check


class VisualAcceptanceBindingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.output = self.root / 'P1.png'
        self.template = self.root / 'template-P1.jpg'
        self.source = self.root / 'source.jpg'
        self.output.write_bytes(b'native-output')
        self.template.write_bytes(b'real-template')
        self.source.write_bytes(b'source-material')
        self.selection_path = self.root / 'selection.json'
        self.plan_path = self.root / 'plan.json'
        self.attachments_path = self.root / 'attachments.json'
        self.learning_path = self.root / 'learning.json'
        self.selection_path.write_text(json.dumps({
            'receiptType': 'real_template_migration_selection',
            'status': 'PLAN_READBACK_VERIFIED',
            'repairWorkId': 'W-1',
            'selectedTemplateId': 'T91',
            'templateChatReference': 'https://chatgpt.com/c/template-chat',
            'newConversationRequired': True,
        }), encoding='utf-8')
        self.plan_path.write_text(json.dumps({
            'workId': 'W-1', 'templateId': 'T91',
            'conversationId': 'production-chat',
            'status': 'PLAN_READBACK_VERIFIED',
        }), encoding='utf-8')
        self.attachments_path.write_text(json.dumps({
            'workId': 'W-1', 'conversationId': 'production-chat',
            'sourceImageCount': 1, 'textAttachmentCount': 1,
        }), encoding='utf-8')
        self.learning_path.write_text('{}', encoding='utf-8')

    def tearDown(self):
        self.tmp.cleanup()

    def receipt(self):
        import hashlib

        sha = lambda p: hashlib.sha256(p.read_bytes()).hexdigest()
        checks = {
            key: {'result': 'pass', 'observation': f'{key} visually reviewed'}
            for key in ('layout', 'typography', 'decoration', 'sourceFacts', 'naturalness')
        }
        return {
            'workId': 'W-1', 'reviewer': 'Codex', 'reviewedAt': '2026-10-18T00:00:00Z',
            'expectedPageCount': 1,
            'binding': {
                'accountIdentity': 'account-1', 'templateId': 'T91',
                'templateConversationUrl': 'https://chatgpt.com/c/template-chat',
                'productionConversationUrl': 'https://chatgpt.com/c/production-chat',
                'sourceMessageId': 'source-message', 'planMessageId': 'plan-message',
                'generationMessageId': 'generation-message',
                'templateLearningReceiptPath': str(self.learning_path),
                'sourceAttachmentReceiptPath': str(self.attachments_path),
                'conversationReadbackPath': str(self.plan_path),
                'templateMigration': {
                    'mode': 'template_migration', 'newConversationRequired': True,
                    'selectionReceiptPath': str(self.selection_path),
                    'planReadbackReceiptPath': str(self.plan_path),
                },
            },
            'pages': [{
                'page': 'P1', 'output': {'path': str(self.output), 'sha256': sha(self.output)},
                'template': {'path': str(self.template), 'sha256': sha(self.template)},
                'source': {'path': str(self.source), 'sha256': sha(self.source)},
                'checks': checks, 'unresolvedDefects': [],
            }],
        }

    def test_distinct_template_and_production_conversations_pass_with_verified_migration(self):
        result = check(self.receipt())
        self.assertTrue(result['accepted'], result['errors'])

    def test_distinct_conversations_fail_without_explicit_migration(self):
        receipt = self.receipt()
        receipt['binding'].pop('templateMigration')
        result = check(receipt)
        self.assertFalse(result['accepted'])
        self.assertTrue(any('迁移授权' in error for error in result['errors']))

    def test_same_conversation_keeps_legacy_binding_behavior(self):
        receipt = self.receipt()
        receipt['binding']['productionConversationUrl'] = receipt['binding']['templateConversationUrl']
        receipt['binding'].pop('templateMigration')
        result = check(receipt)
        self.assertTrue(result['accepted'], result['errors'])

    def test_changed_source_mapping_digest_invalidates_receipt(self):
        receipt = self.receipt()
        receipt['pages'][0]['sourceMappings'] = [{
            'path': str(self.source), 'sha256': 'wrong-hash',
        }]
        result = check(receipt)
        self.assertFalse(result['accepted'])
        self.assertTrue(any('来源映射文件已变更' in error for error in result['errors']))


if __name__ == '__main__':
    unittest.main()

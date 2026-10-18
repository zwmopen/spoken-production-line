"""Offline safety tests for fresh custom-GPT plan submission."""
import unittest
import sqlite3
import tempfile
import time
from pathlib import Path

from upload_queue import (custom_gpt_route, conversation_id, exact_attachment_set,
                          reserve_upload_attempt, split_upload_batches, verify_template_target,
                          normalize_prompt, validate_resumable_draft, server_prompt_matches)


GPT = 'g-p-6ac996444008819193cb6317f059c9dc-mo-ban-ku'
ROOT = f'https://chatgpt.com/g/{GPT}'


class UploadQueueRouteTests(unittest.TestCase):
    def test_uploads_more_than_ten_files_in_bounded_batches(self):
        files=[f'file-{i}.jpg' for i in range(21)]
        batches=split_upload_batches(files)
        self.assertEqual([len(x) for x in batches],[10,10,1])
        self.assertEqual([f for batch in batches for f in batch],files)

    def test_upload_batch_limit_must_be_positive(self):
        with self.assertRaises(ValueError):
            split_upload_batches(['one.jpg'],0)

    def test_attachment_readback_requires_exact_cumulative_set(self):
        expected=[f'page-{i}.jpg' for i in range(11)]
        self.assertTrue(exact_attachment_set([f'Remove page-{i}.jpg' for i in range(11)],expected))
        self.assertFalse(exact_attachment_set([f'Remove page-{i}.jpg' for i in range(10)],expected))
        self.assertFalse(exact_attachment_set([f'Remove page-{i}.jpg' for i in range(10)]+['Remove extra.jpg'],expected))

    def test_prompt_normalizer_accepts_editor_whitespace_before_url(self):
        self.assertEqual(normalize_prompt('原始链接：https://example.test/x'),
                         normalize_prompt('原始链接：\nhttps://example.test/x'))
        self.assertNotEqual(normalize_prompt('原始链接：https://example.test/x'),
                            normalize_prompt('原始链接：https://example.test/y'))

    def test_server_prompt_readback_allows_only_surrounding_platform_metadata(self):
        expected='workId repair-01 use exact template images'
        self.assertTrue(server_prompt_matches(expected,'[attachment metadata] '+expected+' [file metadata]'))
        self.assertFalse(server_prompt_matches(expected,'workId repair-01 use different template images'))

    def test_resumable_draft_requires_exact_route_prompt_files_and_reservation(self):
        row={'status':'DRAFT_READY_NOT_SUBMITTED','id':'PLAN-1','currentAttemptId':'PLAN-1-attempt-03',
             'newConversation':True,'expectedGptRoute':GPT,'templateUrl':ROOT,
             'expectedComposerAria':'在“模板库”中新建聊天','files':['one.jpg','two.jpg'],'prompt':'原始链接：https://example.test'}
        state={'quotaNotice':'','editors':[{'aria':row['expectedComposerAria'],'text':'原始链接：\nhttps://example.test'}],
               'removers':[{'label':'Remove one.jpg'},{'label':'Remove two.jpg'}],'send':[{'disabled':False}]}
        reservation={'count':2,'status':'failed_or_unknown'}
        self.assertEqual(validate_resumable_draft(row,state,ROOT,reservation),row['currentAttemptId'])
        bad=dict(state);bad['removers']=[{'label':'Remove one.jpg'}]
        with self.assertRaises(RuntimeError):validate_resumable_draft(row,bad,ROOT,reservation)
        bad=dict(state);bad['quotaNotice']='limit reached'
        with self.assertRaises(RuntimeError):validate_resumable_draft(row,bad,ROOT,reservation)
        with self.assertRaises(RuntimeError):validate_resumable_draft(row,state,ROOT,{'count':1,'status':'reserved'})

    def test_retry_uses_new_reservation_without_double_counting_prior_attempt(self):
        with tempfile.TemporaryDirectory() as temp:
            db=Path(temp)/'uploads.sqlite3'
            files=[f'{i}.jpg' for i in range(21)]
            from upload_budget import reserve
            self.assertTrue(reserve(db,'A','PLAN-1',len(files),80)['allowed'])
            row={'id':'PLAN-1','files':files,'currentAttemptId':None,
                 'uploadStartedAt':time.time()-60,'status':'UPLOAD_BLOCKED_NOT_SUBMITTED',
                 'stage':'wait_attachment_mount','error':'no attachments read back'}
            attempt_id,quota=reserve_upload_attempt(db,'A',row,80)
            self.assertEqual(attempt_id,'PLAN-1-attempt-02')
            self.assertTrue(quota['allowed'])
            self.assertEqual(row['attemptHistory'][0]['attemptId'],'PLAN-1')
            self.assertEqual(row['uploadBudget']['usedBefore'],21)
            con=sqlite3.connect(db)
            try:
                count,total=con.execute('SELECT COUNT(*),SUM(count) FROM uploads WHERE account=?',('A',)).fetchone()
            finally:
                con.close()
            self.assertEqual((count,total),(2,42))

    def test_extracts_exact_custom_gpt_and_conversation(self):
        url = f'{ROOT}/c/6accaf4c-0edc-83e8-9b66-9b8cfa8eb069'
        self.assertEqual(custom_gpt_route(url), GPT)
        self.assertEqual(conversation_id(url), '6accaf4c-0edc-83e8-9b66-9b8cfa8eb069')

    def test_existing_chat_requires_exact_conversation(self):
        row = {'templateUrl': f'{ROOT}/c/chat-one'}
        verify_template_target(row, f'{ROOT}/c/chat-one')
        with self.assertRaises(RuntimeError):
            verify_template_target(row, f'{ROOT}/c/chat-two')

    def test_fresh_chat_stays_on_selected_gpt(self):
        row = {'newConversation': True, 'expectedGptRoute': GPT, 'templateUrl': ROOT}
        verify_template_target(row, ROOT)
        verify_template_target(row, f'{ROOT}/c/new-chat-id', after_send=True)
        with self.assertRaises(RuntimeError):
            verify_template_target(row, 'https://chatgpt.com/g/g-p-other-gpt/c/new-chat-id')

    def test_fresh_chat_must_have_server_conversation_after_send(self):
        row = {'newConversation': True, 'expectedGptRoute': GPT, 'templateUrl': ROOT}
        with self.assertRaises(RuntimeError):
            verify_template_target(row, ROOT, after_send=True)


if __name__ == '__main__':
    unittest.main()

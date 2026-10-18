import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from batch_launch_queue import closed, tick


class BatchLaunchSafetyTests(unittest.TestCase):
    def manifest(self):
        return {'workId': 'PREVIOUS', 'status': 'accepted_archived_and_notified',
                'plannedPageCount': 6, 'acceptedPageCount': 6, 'usageRecorded': True,
                'notificationMessageId': 'om_verified', 'notificationReadbackVerified': True}

    def test_closure_requires_all_pages_usage_and_notification(self):
        self.assertTrue(closed(self.manifest()))
        for field, value in [('plannedPageCount', None), ('plannedPageCount', 0),
                             ('acceptedPageCount', 5), ('usageRecorded', False),
                             ('notificationMessageId', None),
                             ('notificationReadbackVerified', False)]:
            m = self.manifest()
            m[field] = value
            self.assertFalse(closed(m), field)

    def test_submitted_job_waits_for_closure_without_rewriting_worker_queue(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'supervisor').mkdir()
            predecessor = root / 'predecessor.json'
            predecessor.write_text(json.dumps(self.manifest()))
            queue = root / 'queue-A.json'
            queue.write_text(json.dumps({'account': 'A', 'tasks': [
                {'id': 'NEXT', 'status': 'COPY_PENDING'}]}))
            before = queue.read_bytes()
            job = {'account': 'A', 'workId': 'NEXT', 'controllerReviewed': True,
                   'predecessorManifestPath': str(predecessor),
                   'predecessorWorkId': 'PREVIOUS', 'status': 'SUBMITTED_CONFIRMED',
                   'messageId': 'real-user-message'}
            (root / '自动批量接续.json').write_text(json.dumps({'jobs': [job]}))
            with patch('batch_launch_queue.cdp.CDP', side_effect=AssertionError('browser touched')):
                result = tick(root, 'A')
            self.assertEqual(result['status'], 'AWAIT_CURRENT_WORK_CLOSURE')
            self.assertEqual(queue.read_bytes(), before)

    def test_unreviewed_job_never_opens_browser(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'supervisor').mkdir()
            (root / '自动批量接续.json').write_text(json.dumps({'jobs': [
                {'account': 'A', 'workId': 'X', 'controllerReviewed': False}]}))
            with patch('batch_launch_queue.cdp.CDP', side_effect=AssertionError('browser touched')):
                self.assertEqual(tick(root, 'A')['status'], 'REVIEW_REQUIRED')


if __name__ == '__main__':
    unittest.main()

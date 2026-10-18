import unittest

from refresh_current_ledger import dedupe_records, suite_records


class LedgerIdentityTests(unittest.TestCase):
    def test_priority_mirror_does_not_duplicate_queue_work(self):
        rows = [
            {
                'workId': 'W-1', 'executionChannel': 'GPT A',
                'status': 'READY', 'stateUpdatedAt': '2026-10-18T00:00:00+00:00',
                'evidencePath': r'D:\root\优先生产队列.json', 'title': 'old mirror',
            },
            {
                'workId': 'W-1', 'executionChannel': 'GPT A',
                'status': 'COMPLETED', 'stateUpdatedAt': '2026-10-18T01:00:00+00:00',
                'evidencePath': r'D:\root\queue-A.json', 'title': 'current queue',
                'acceptedPageCount': 8,
            },
        ]
        result = dedupe_records(rows)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]['status'], 'COMPLETED')
        self.assertEqual(result[0]['title'], 'current queue')
        self.assertEqual(result[0]['acceptedPageCount'], 8)

    def test_equal_timestamp_prefers_account_queue(self):
        rows = [
            {
                'workId': 'W-1', 'executionChannel': 'GPT A',
                'status': 'STALE', 'stateUpdatedAt': '2026-10-18T01:00:00+00:00',
                'evidencePath': r'D:\root\优先生产队列.json',
            },
            {
                'workId': 'W-1', 'executionChannel': 'GPT A',
                'status': 'FRESH', 'stateUpdatedAt': '2026-10-18T01:00:00+00:00',
                'evidencePath': r'D:\root\queue-A.json',
            },
        ]
        self.assertEqual(dedupe_records(rows)[0]['status'], 'FRESH')

    def test_page_repair_remains_visible_but_is_not_a_suite(self):
        rows = [
            {'workId': 'SUITE-1', 'recordKind': 'suite'},
            {'workId': 'REPAIR-1', 'recordKind': 'repair_pages'},
        ]
        self.assertEqual(len(dedupe_records(rows)), 2)
        self.assertEqual([r['workId'] for r in suite_records(rows)], ['SUITE-1'])


if __name__ == '__main__':
    unittest.main()

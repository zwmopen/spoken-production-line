"""Deterministic recovery tests for production_supervisor.

All adapter responses are simulated. These tests never open a browser, send a
generation request, or touch the live production queues.
"""
from __future__ import annotations

import datetime as dt
import json
import tempfile
import threading
import unittest
from pathlib import Path

from production_supervisor import (
    Supervisor, account_lock, next_wake_seconds, save,
)


def due(supervisor: Supervisor, account: str) -> dict:
    path = supervisor.directory / f'{account}.json'
    state = json.loads(path.read_text(encoding='utf-8'))
    state['nextRunAt'] = (dt.datetime.now(dt.timezone.utc) -
                          dt.timedelta(seconds=1)).isoformat()
    save(path, state)
    return state


def config():
    return {'commands': {name: ['mock'] for name in (
        'discover', 'submit', 'reconcile', 'quota', 'recover', 'download',
        'verify', 'copy', 'archive', 'notify')}}


def epoch_for_test(value: str) -> float:
    return dt.datetime.fromisoformat(value.replace('Z', '+00:00')).timestamp()


class ProductionSupervisorRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.calls = []
        self.lock = threading.Lock()
        self.respond = lambda account, operation, request: {
            'account': account, 'workId': request.get('workId'),
            'page': request.get('page'), 'status': 'pending'}

        def runner(cfg, operation, request):
            account = request['account']
            with self.lock:
                self.calls.append((account, operation, request.copy()))
            return self.respond(account, operation, request)

        self.runner = runner
        self.supervisor = Supervisor(self.root, runner=runner)

    def tearDown(self):
        self.tmp.cleanup()

    def set_state(self, account, **changes):
        value = {
            'account': account, 'stage': 'READY', 'workId': f'W-{account}',
            'page': 1, 'totalPages': 2, 'bindingVerified': True,
            'owner': None, 'lastProgressAt': None, 'intent': None,
            'messageId': None, 'nextRunAt': None,
        }
        value.update(changes)
        save(self.supervisor.directory / f'{account}.json', value)
        return value

    def test_submit_timeout_is_reconciled_without_resubmission(self):
        self.set_state('A')
        fail_once = {'submit': True}

        def answer(account, operation, request):
            if operation == 'submit' and fail_once['submit']:
                fail_once['submit'] = False
                raise TimeoutError('simulated acknowledgement timeout')
            status = 'submitted' if operation == 'reconcile' else 'pending'
            return {'account': account, 'workId': request.get('workId'),
                    'page': request.get('page'), 'status': status,
                    'messageId': 'server-message-1'}

        self.respond = answer
        first = self.supervisor.tick('A', config())
        self.assertEqual(first['stage'], 'SUBMISSION_UNKNOWN')
        self.assertTrue(first['intent']['id'])
        due(self.supervisor, 'A')
        second = self.supervisor.tick('A', config())
        self.assertEqual(second['stage'], 'GENERATING')
        self.assertEqual(second['messageId'], 'server-message-1')
        self.assertEqual([op for _, op, _ in self.calls].count('submit'), 1)
        self.assertEqual([op for _, op, _ in self.calls].count('reconcile'), 1)

    def test_restart_reconciles_existing_generation(self):
        self.set_state('A', stage='GENERATING', messageId='known-start-id')
        self.respond = lambda account, operation, request: {
            'account': account, 'workId': request['workId'],
            'page': request['page'], 'status': 'generated',
            'messageId': 'known-start-id'}
        restarted = Supervisor(self.root, runner=self.runner)
        result = restarted.tick('A', config())
        self.assertEqual(result['stage'], 'DOWNLOAD')
        self.assertEqual([op for _, op, _ in self.calls], ['reconcile'])

    def test_unverified_discovery_never_submits(self):
        self.set_state('A', stage='DISCOVER', workId=None, page=None,
                       bindingVerified=False)
        self.respond = lambda account, operation, request: {
            'account': account, 'workId': 'WRONG-WORK', 'page': 1,
            'totalPages': 1, 'bindingVerified': False, 'status': 'ready'}
        result = self.supervisor.tick('A', config())
        self.assertEqual(result['stage'], 'ACTIVE_RECOVERY')
        self.assertNotIn('submit', [op for _, op, _ in self.calls])

    def test_quota_wait_keeps_resume_stage_and_other_account_runs(self):
        now = dt.datetime.now(dt.timezone.utc)
        observed = now.isoformat()
        reset = (now + dt.timedelta(seconds=60)).isoformat()
        self.set_state('A', stage='QUOTA_WAIT', resumeStage='SUBMISSION_UNKNOWN',
                       quota={'observedAt': observed, 'resetAt': reset,
                              'evidence': 'simulated platform limit'},
                       nextRunAt=(now - dt.timedelta(seconds=1)).isoformat())
        self.set_state('B', stage='READY', workId='W-B', page=1)

        def answer(account, operation, request):
            if account == 'A' and operation == 'quota':
                return {'account': 'A', 'workId': request['workId'],
                        'page': request['page'], 'status': 'quota',
                        'observedAt': observed, 'resetAt': reset,
                        'evidence': 'still limited'}
            if account == 'B' and operation == 'submit':
                return {'account': 'B', 'workId': request['workId'],
                        'page': request['page'], 'status': 'submitted',
                        'messageId': 'b-message'}
            return {'account': account, 'workId': request.get('workId'),
                    'page': request.get('page'), 'status': 'pending'}

        self.respond = answer
        output = self.supervisor.cycle({'accounts': {'A': config(), 'B': config()}})
        states = {x['account']: x for x in output}
        self.assertEqual(states['A']['stage'], 'QUOTA_WAIT')
        self.assertEqual(states['A']['resumeStage'], 'SUBMISSION_UNKNOWN')
        self.assertEqual(states['B']['stage'], 'GENERATING')
        self.assertEqual(states['B']['messageId'], 'b-message')

    def test_reset_time_resumes_original_stage_after_quota(self):
        now = dt.datetime.now(dt.timezone.utc)
        old_observed = (now - dt.timedelta(minutes=10)).isoformat()
        reset = (now - dt.timedelta(seconds=1)).isoformat()
        self.set_state('A', stage='QUOTA_WAIT', resumeStage='SUBMISSION_UNKNOWN',
                       quota={'observedAt': old_observed, 'resetAt': reset,
                              'evidence': 'simulated platform limit'},
                       nextRunAt=reset)
        self.respond = lambda account, operation, request: {
            'account': account, 'workId': request['workId'],
            'page': request['page'], 'status': 'available',
            'latestSuccessAt': (now + dt.timedelta(seconds=1)).isoformat()}
        first = self.supervisor.tick('A', config())
        self.assertEqual(first['stage'], 'SUBMISSION_UNKNOWN')
        due(self.supervisor, 'A')
        self.respond = lambda account, operation, request: {
            'account': account, 'workId': request['workId'],
            'page': request['page'], 'status': 'submitted',
            'messageId': 'after-reset'}
        second = self.supervisor.tick('A', config())
        self.assertEqual(second['stage'], 'GENERATING')
        self.assertEqual(second['messageId'], 'after-reset')
        self.assertEqual([op for _, op, _ in self.calls].count('submit'), 0)
        self.assertEqual([op for _, op, _ in self.calls].count('reconcile'), 1)

    def test_same_quota_reset_keeps_original_event_and_checks_current_receipt(self):
        now = dt.datetime.now(dt.timezone.utc)
        original_observed = (now - dt.timedelta(minutes=5)).isoformat()
        reset = (now + dt.timedelta(minutes=5)).isoformat()
        latest_observed = now.isoformat()
        self.set_state('A', stage='QUOTA_WAIT', resumeStage='READY',
                       quota={'observedAt': original_observed, 'resetAt': reset,
                              'evidence': 'same platform limit'},
                       nextRunAt=(now - dt.timedelta(seconds=1)).isoformat())
        self.respond = lambda account, operation, request: {
            'account': account, 'workId': request['workId'],
            'page': request['page'], 'status': 'quota',
            'observedAt': latest_observed, 'resetAt': reset,
            'evidence': 'same platform limit'}

        result = self.supervisor.tick('A', config())

        self.assertEqual(result['stage'], 'QUOTA_WAIT')
        self.assertEqual(result['quota']['observedAt'], original_observed)
        self.assertEqual(result['quotaLastReceipt']['observedAt'], latest_observed)
        self.assertEqual(result['quotaHistory'], [])
        self.assertEqual(result['nextRunAt'], reset)

    def test_new_quota_reset_is_recorded_without_losing_old_event(self):
        now = dt.datetime.now(dt.timezone.utc)
        old_observed = (now - dt.timedelta(minutes=20)).isoformat()
        old_reset = (now - dt.timedelta(minutes=1)).isoformat()
        new_observed = now.isoformat()
        new_reset = (now + dt.timedelta(minutes=10)).isoformat()
        self.set_state('A', stage='QUOTA_WAIT', resumeStage='SUBMISSION_UNKNOWN',
                       quota={'observedAt': old_observed, 'resetAt': old_reset,
                              'evidence': 'older limit'},
                       nextRunAt=(now - dt.timedelta(seconds=1)).isoformat())
        self.respond = lambda account, operation, request: {
            'account': account, 'workId': request['workId'],
            'page': request['page'], 'status': 'quota',
            'observedAt': new_observed, 'resetAt': new_reset,
            'evidence': 'new platform limit'}

        result = self.supervisor.tick('A', config())

        self.assertEqual(result['quota']['resetAt'], new_reset)
        self.assertEqual(result['quotaHistory'][0]['observedAt'], old_observed)
        self.assertEqual(result['nextRunAt'], new_reset)

    def test_stale_quota_reset_uses_bounded_recheck_not_past_timestamp(self):
        now = dt.datetime.now(dt.timezone.utc)
        observed = (now - dt.timedelta(minutes=20)).isoformat()
        reset = (now - dt.timedelta(minutes=1)).isoformat()
        self.set_state('A', stage='QUOTA_WAIT', resumeStage='SUBMISSION_UNKNOWN',
                       quota={'observedAt': observed, 'resetAt': reset,
                              'evidence': 'same expired event'},
                       nextRunAt=(now - dt.timedelta(seconds=1)).isoformat())
        self.respond = lambda account, operation, request: {
            'account': account, 'workId': request['workId'],
            'page': request['page'], 'status': 'quota',
            'observedAt': now.isoformat(), 'resetAt': reset,
            'evidence': 'same expired event', 'recheckAfterSeconds': 90}

        result = self.supervisor.tick('A', config())

        self.assertEqual(result['quota']['observedAt'], observed)
        self.assertGreaterEqual(epoch_for_test(result['nextRunAt']), now.timestamp() + 89)

    def test_daemon_wait_uses_nearest_next_run_with_bounds(self):
        now = 1_000_000.0
        results = [
            {'account': 'A', 'nextRunAt': dt.datetime.fromtimestamp(
                now + 17, dt.timezone.utc).isoformat()},
            {'account': 'B', 'nextRunAt': dt.datetime.fromtimestamp(
                now + 200, dt.timezone.utc).isoformat()},
        ]
        self.assertEqual(next_wake_seconds(results, {'maxSleepSeconds': 60}, now), 17)
        self.assertEqual(next_wake_seconds(results, {'maxSleepSeconds': 10}, now), 10)
        self.assertEqual(next_wake_seconds([], {'idlePollSeconds': 45}, now), 45)

    def test_account_lock_leaves_one_initialized_byte(self):
        path = self.root / 'empty.lock'
        with account_lock(path):
            self.assertTrue(path.exists())
        self.assertEqual(path.read_bytes(), b'0')

    def test_copy_failure_retries_copy_without_regenerating(self):
        self.set_state('A', stage='COPY', messageId='generated-id')
        attempts = {'copy': 0}

        def answer(account, operation, request):
            if operation == 'copy':
                attempts['copy'] += 1
                if attempts['copy'] == 1:
                    raise OSError('simulated copy write failure')
                return {'account': account, 'workId': request['workId'],
                        'page': request['page'], 'status': 'done'}
            return {'account': account, 'workId': request.get('workId'),
                    'page': request.get('page'), 'status': 'pending'}

        self.respond = answer
        first = self.supervisor.tick('A', config())
        self.assertEqual(first['stage'], 'COPY')
        due(self.supervisor, 'A')
        second = self.supervisor.tick('A', config())
        self.assertEqual(second['stage'], 'ARCHIVE')
        self.assertEqual(attempts['copy'], 2)
        self.assertNotIn('submit', [op for _, op, _ in self.calls])


if __name__ == '__main__':
    unittest.main()

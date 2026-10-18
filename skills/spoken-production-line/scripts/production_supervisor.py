"""Account-isolated durable scheduler. Adapters own browser/queue reconciliation.

Config: TASK_ROOT/production-supervisor.json, {"accounts":{"A":{"commands":
{"discover":["python", "adapter.py", "discover"], ...},"timeoutSeconds":90}}}.
Commands receive one JSON request on stdin and return exactly one JSON object.
Every receipt echoes account, workId and page; discover returns bindingVerified,
workId, page, totalPages. Adapter operations must be idempotent by intent.id.
reconcile is read-only and may return not_submitted ONLY with authoritative=true.
Existing writers must honor the same account lock or be positively quiesced by
the adapter before bindingVerified=true. Merely holding this lock is insufficient.
"""
import argparse
import concurrent.futures
import contextlib
import datetime as dt
import json
import math
import os
from pathlib import Path
import re
import subprocess
import time
import uuid


def stamp():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def epoch(value):
    if not value:
        return 0
    return dt.datetime.fromisoformat(value.replace('Z', '+00:00')).timestamp()


def save(path, value):
    tmp = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    with tmp.open('w', encoding='utf-8') as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(tmp, path)


@contextlib.contextmanager
def account_lock(path):
    """OS-held lock released after crash; never delete a stale lockfile."""
    stream = path.open('a+b')
    acquired = False
    try:
        # Windows cannot lock byte 0 beyond EOF. Seed a marker without reading
        # the lock range; concurrent initializers may append harmless markers,
        # but only one process can acquire byte 0 below.
        stream.seek(0, os.SEEK_END)
        if stream.tell() == 0:
            stream.write(b'0')
            stream.flush()
            os.fsync(stream.fileno())
        stream.seek(0)
        # On Windows, touching a byte covered by another process's byte-range
        # lock can raise PermissionError. Acquire the range before reading it
        # so duplicate launches cleanly fail at lock.
        if os.name == 'nt':
            import msvcrt
            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        acquired = True
        stream.seek(0)
        if not stream.read(1):
            raise OSError('Lockfile marker missing after acquiring byte lock')
        stream.seek(0)
        yield
    finally:
        if acquired:
            stream.seek(0)
            if os.name == 'nt':
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream, fcntl.LOCK_UN)
        stream.close()


def next_wake_seconds(results, config, now=None):
    """Bound daemon sleep by the nearest durable account nextRunAt."""
    now = time.time() if now is None else now
    minimum = max(1, min(30, int(config.get('minSleepSeconds', 2))))
    maximum = max(minimum, min(900, int(config.get('maxSleepSeconds', 300))))
    default = max(minimum, min(maximum, int(config.get('idlePollSeconds', 30))))
    due = [epoch(item.get('nextRunAt')) for item in results
           if isinstance(item, dict) and item.get('nextRunAt')]
    if not due:
        return default
    wait = min(due) - now
    if wait <= 0:
        return minimum
    return max(minimum, min(maximum, int(math.ceil(wait))))


def record_quota_event(state, receipt, operation, now=None):
    """Keep the original reset event; record new reset times as new events."""
    now = time.time() if now is None else now
    old = state.get('quota')
    history = state.setdefault('quotaHistory', [])
    same_event = bool(old and old.get('resetAt') == receipt.get('resetAt'))
    if old and not same_event:
        old_key = (old.get('eventId'), old.get('resetAt'), old.get('evidence'))
        if not any((item.get('eventId'), item.get('resetAt'), item.get('evidence')) == old_key
                   for item in history):
            history.append(old)
    event = dict(receipt)
    event.setdefault('account', state.get('account'))
    event.setdefault('phase', state.get('resumeStage') or operation)
    if same_event:
        # A repeated observation of the same reset must not rewrite the
        # original observedAt. Keep the newest observation separately.
        state['quotaLastReceipt'] = receipt
        state['quotaLastCheckedAt'] = stamp()
        reset_at = epoch(old.get('resetAt'))
    else:
        state['quota'] = event
        state['quotaLastReceipt'] = receipt
        state['quotaLastCheckedAt'] = stamp()
        reset_at = epoch(event.get('resetAt'))
    if reset_at > now:
        state['nextRunAt'] = event.get('resetAt') if not same_event else old.get('resetAt')
    else:
        explicit_next = epoch(receipt.get('nextRunAt'))
        # A stale reset timestamp is evidence to recheck, not a reason to
        # poll every second or to claim the platform is still at that reset.
        state['nextRunAt'] = (receipt.get('nextRunAt') if explicit_next > now else
                              dt.datetime.fromtimestamp(now + int(receipt.get('recheckAfterSeconds', 300)),
                                                        dt.timezone.utc).isoformat())
    state.update(stage='QUOTA_WAIT', resumeStage=state.get('resumeStage') or 'SUBMISSION_UNKNOWN')


def command_runner(config, operation, request):
    argv = config.get('commands', {}).get(operation)
    if not isinstance(argv, list) or not argv or not all(isinstance(x, str) for x in argv):
        raise LookupError('Missing explicit argv adapter: ' + operation)
    result = subprocess.run(argv, input=json.dumps(request), text=True,
                            encoding='utf-8', capture_output=True, shell=False,
                            timeout=max(1, min(300, config.get('timeoutSeconds', 90))))
    if result.returncode:
        raise RuntimeError('Adapter exit ' + str(result.returncode))
    return json.loads(result.stdout)


class Supervisor:
    def __init__(self, root, runner=command_runner):
        self.root = Path(root).resolve()
        self.directory = self.root / 'supervisor'
        self.directory.mkdir(parents=True, exist_ok=True)
        self.runner = runner

    def tick(self, account, config):
        if not re.fullmatch(r'[A-Za-z0-9_-]+', account):
            return {'account': account, 'stage': 'UNCONFIGURED', 'error': 'Invalid account identifier'}
        try:
            with account_lock(self.directory / (account + '.lock')):
                return self._tick(account, config)
        except (BlockingIOError, PermissionError, OSError) as error:
            return {'account': account, 'stage': 'LOCKED_OR_IO_ERROR', 'error': type(error).__name__}

    def _tick(self, account, config):
        path = self.directory / (account + '.json')
        state = json.loads(path.read_text(encoding='utf-8')) if path.exists() else {
            'account': account, 'stage': 'DISCOVER', 'workId': None, 'page': None,
            'owner': None, 'lastProgressAt': None, 'intent': None, 'messageId': None,
            'nextRunAt': None}
        state.update(owner=f'{os.getpid()}:{account}', checkedAt=stamp())
        stage = state['stage']
        if not config.get('commands'):
            state.update(stage='UNCONFIGURED', resumeStage=state.get('resumeStage', stage))
            save(path, state)
            return state
        if stage == 'UNCONFIGURED':
            stage = state['stage'] = state.pop('resumeStage', 'DISCOVER')
        if epoch(state.get('nextRunAt')) > time.time():
            save(path, state)
            return state
        operation = {'DISCOVER': 'discover', 'READY': 'submit',
                     'SUBMISSION_UNKNOWN': 'reconcile', 'GENERATING': 'reconcile',
                     'DOWNLOAD': 'download', 'VERIFY': 'verify', 'COPY': 'copy',
                     'ARCHIVE': 'archive', 'NOTIFY': 'notify',
                     'QUOTA_WAIT': 'quota', 'ACTIVE_RECOVERY': 'recover',
                     'WAIT_MATERIAL': 'discover', 'BLOCKED_HUMAN_ACTION': 'reconcile'}[stage]
        if operation == 'submit':
            if not state.get('bindingVerified'):
                state.update(stage='ACTIVE_RECOVERY', resumeStage='DISCOVER', error='Binding not verified')
                save(path, state)
                return state
            state['intent'] = {'id': uuid.uuid4().hex, 'workId': state['workId'],
                               'page': state['page'], 'createdAt': stamp()}
            state['stage'] = 'SUBMISSION_UNKNOWN'
            save(path, state)  # durable BEFORE any possible send
        request = {'version': 1, 'taskRoot': str(self.root), 'operation': operation, **state}
        try:
            receipt = self.runner(config, operation, request)
            if receipt.get('account') != account:
                raise ValueError('Account receipt mismatch')
            if operation not in ('discover',) and (receipt.get('workId') != state.get('workId') or receipt.get('page') != state.get('page')):
                raise ValueError('Work/page receipt mismatch')
            status = receipt.get('status')
            state['lastReceiptAt'] = stamp()
            state['nextRunAt'] = receipt.get('nextRunAt')
            if status == 'quota':
                if not receipt.get('evidence') or not receipt.get('observedAt') or not receipt.get('resetAt'):
                    raise ValueError('Quota requires evidence and absolute timestamps')
                epoch(receipt['resetAt'])
                if operation == 'quota':
                    resume_stage = state.get('resumeStage') or 'SUBMISSION_UNKNOWN'
                elif operation == 'submit':
                    resume_stage = 'SUBMISSION_UNKNOWN'
                else:
                    resume_stage = stage
                state['resumeStage'] = resume_stage
                record_quota_event(state, receipt, operation)
            elif status == 'human_action':
                state.update(stage='BLOCKED_HUMAN_ACTION', error=receipt.get('reason'))
            elif operation == 'discover' and status == 'empty':
                state['stage'] = 'WAIT_MATERIAL'
            elif operation == 'discover' and status == 'ready':
                if not receipt.get('bindingVerified') or not receipt.get('workId') or not (1 <= receipt.get('page', 0) <= receipt.get('totalPages', 0)):
                    raise ValueError('Unverified discovery binding')
                state.update({k: receipt[k] for k in ('workId', 'page', 'totalPages', 'bindingVerified')})
                state.update(stage='READY', intent=None, messageId=None)
            elif operation in ('submit', 'reconcile') and status in ('submitted', 'generating', 'generated'):
                if not receipt.get('messageId'):
                    raise ValueError('Missing messageId')
                state.update(messageId=receipt['messageId'], stage='DOWNLOAD' if status == 'generated' else 'GENERATING')
            elif operation == 'reconcile' and status == 'not_submitted' and receipt.get('authoritative'):
                state.update(stage='READY', intent=None)
            elif operation == 'quota' and (status == 'available' or epoch(receipt.get('latestSuccessAt')) > epoch(state['quota']['observedAt'])):
                state.update(stage=state.get('resumeStage', 'SUBMISSION_UNKNOWN'), nextRunAt=None)
            elif operation == 'recover' and status == 'recovered':
                state['stage'] = state.get('resumeStage', 'SUBMISSION_UNKNOWN')
            elif status == 'done' and operation in ('download', 'verify', 'copy', 'archive', 'notify'):
                if operation == 'verify':
                    if state['page'] < state['totalPages']:
                        state.update(page=state['page'] + 1, stage='READY', intent=None, messageId=None)
                    else:
                        state['stage'] = 'COPY'
                elif operation == 'notify':
                    if not receipt.get('readbackVerified') or not receipt.get('notificationId'):
                        raise ValueError('Notification readback missing')
                    state.update(stage='DISCOVER', lastCompletedWorkId=state['workId'], completedAt=stamp())
                else:
                    state['stage'] = {'download': 'VERIFY', 'copy': 'ARCHIVE', 'archive': 'NOTIFY'}[operation]
            elif status not in ('pending', 'unknown', 'limited'):
                raise ValueError('Unsupported adapter outcome')
            if state['stage'] != stage or status == 'done':
                state['lastProgressAt'] = stamp()
        except Exception as error:
            # Retrying these phases never submits generation. Unknown sends ONLY reconcile.
            state['error'] = type(error).__name__ + ': ' + str(error)[:200]
            if operation == 'submit' or stage in ('SUBMISSION_UNKNOWN', 'GENERATING'):
                state['stage'] = 'SUBMISSION_UNKNOWN'
            elif stage not in ('DOWNLOAD', 'VERIFY', 'COPY', 'ARCHIVE', 'NOTIFY', 'QUOTA_WAIT'):
                state.update(stage='ACTIVE_RECOVERY', resumeStage=state.get('resumeStage', stage))
        if not state.get('nextRunAt'):
            immediate = state['stage'] != stage and state['stage'] in (
                'READY', 'SUBMISSION_UNKNOWN', 'GENERATING', 'DOWNLOAD',
                'VERIFY', 'COPY', 'ARCHIVE', 'NOTIFY', 'DISCOVER')
            state['nextRunAt'] = dt.datetime.fromtimestamp(time.time() + (0 if immediate else 30), dt.timezone.utc).isoformat()
        save(path, state)
        return state

    def cycle(self, config):
        accounts = config.get('accounts', {})
        if not accounts:
            return [{'stage': 'UNCONFIGURED', 'error': 'No account adapters configured'}]
        with concurrent.futures.ThreadPoolExecutor(max_workers=min(8, len(accounts))) as pool:
            futures = [pool.submit(self.tick, a, c) for a, c in accounts.items()]
            output = []
            for future in futures:
                try:
                    output.append(future.result())
                except Exception as error:
                    output.append({'stage': 'ACTIVE_RECOVERY', 'error': type(error).__name__})
            return output


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--task-root', required=True)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument('--once', action='store_true')
    group.add_argument('--daemon', action='store_true')
    args = parser.parse_args()
    supervisor = Supervisor(args.task_root)
    while True:
        config_path = supervisor.root / 'production-supervisor.json'
        config = json.loads(config_path.read_text(encoding='utf-8-sig')) if config_path.exists() else {}
        results = supervisor.cycle(config)
        print(json.dumps(results, ensure_ascii=True), flush=True)
        if args.once:
            return
        time.sleep(next_wake_seconds(results, config))


if __name__ == '__main__':
    main()

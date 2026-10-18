"""Keep the derived panel current; never operate a production browser or queue."""
import argparse
import datetime
import json
import msvcrt
import subprocess
import sys
import time
import traceback
from pathlib import Path
from refresh_current_ledger import refresh
from build_simple_panel import build, read_live_runtime


def code_signature():
    """Track code imported by this long-lived watcher so edits take effect."""
    script_dir = Path(__file__).resolve().parent
    paths = [script_dir / name for name in (
        'watch_status_panel.py', 'refresh_current_ledger.py',
        'build_simple_panel.py', 'quota_panel_view.py',
        'check_visual_acceptance.py')]
    result = []
    for path in paths:
        try:
            stat = path.stat()
            result.append((str(path), stat.st_mtime_ns, stat.st_size))
        except OSError:
            result.append((str(path), None, None))
    return tuple(result)


def restart_for_code_change(lock):
    """Release the singleton lock and start the updated read-only watcher."""
    lock.seek(0)
    msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
    lock.close()
    subprocess.Popen(
        [sys.executable, str(Path(__file__).resolve()), *sys.argv[1:]],
        cwd=str(Path(__file__).resolve().parent), close_fds=True)


def signature(root):
    derived = {'生产台账.json', '会话可复用状态.json', '阶段耗时统计.json',
               '面板同步器状态.json', '面板同步器错误.log'}
    paths = [p for p in root.glob('*.json') if p.name not in derived]
    try:
        ledger = json.loads((root / '生产台账.json').read_text(encoding='utf-8-sig'))
        for row in ledger.get('records', []):
            folder = row.get('productFolderPath')
            if folder and Path(folder).is_dir():
                paths.extend(p for p in Path(folder).iterdir() if p.is_file())
    except (OSError, ValueError):
        pass
    result = []
    for path in paths:
        try:
            stat = path.stat()
            result.append((str(path), stat.st_mtime_ns, stat.st_size))
        except OSError:
            pass
    # Runtime state can change while queue/ledger files remain untouched. Include
    # only stable control-plane markers so the derived panel refreshes on a real
    # runtime revision, without rebuilding on every poll.
    live = read_live_runtime()
    result.extend((f'runtime:{account}', item.get('available'), item.get('source'), item.get('revision'),
                   item.get('queueRunning'), item.get('paused'), item.get('taskCount'))
                  for account, item in sorted(live.items()))
    return sorted(result, key=lambda item: str(item))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--task-root', type=Path, required=True)
    parser.add_argument('--template-root', type=Path, required=True)
    args = parser.parse_args()
    root = args.task_root.resolve()
    lock = (root / '面板同步器.lock').open('a+b')
    try:
        # Acquire the byte-range lock before reading it. On Windows, a second
        # process reading byte 0 while the owner holds this lock can receive
        # PermissionError instead of a clean duplicate-instance exit.
        lock.seek(0)
        msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError:
        lock.close()
        return
    lock.seek(0)
    if lock.read(1) == b'':
        lock.seek(0)
        lock.write(b'0')
        lock.flush()
    previous, rendered = None, 0
    last_success_at = None
    last_error_at = None
    last_error_summary = None
    loaded_code = code_signature()
    while True:
        try:
            current_code = code_signature()
            if current_code != loaded_code:
                restart_for_code_change(lock)
                return
            current = signature(root)
            if current != previous:
                refresh(root, args.template_root)
                previous = signature(root)
                rendered = time.monotonic()
            elif time.monotonic() - rendered >= 60:
                build(root)
                rendered = time.monotonic()
            now = datetime.datetime.now().astimezone().isoformat()
            last_success_at = now
            status = {'observedAt': now, 'lastSuccessfulCheckAt': last_success_at,
                      'state': 'running', 'intervalSeconds': 15,
                      'scope': 'local_files_and_receipts_only', 'browserWrites': False}
            if last_error_at:
                status.update(lastErrorAt=last_error_at,
                              lastErrorSummary=last_error_summary)
            (root / '面板同步器状态.json').write_text(json.dumps(status, ensure_ascii=False, indent=2), encoding='utf-8')
        except Exception as exc:
            last_error_at = datetime.datetime.now().astimezone().isoformat()
            last_error_summary = f'{type(exc).__name__}: {exc}'[:500]
            with (root / '面板同步器错误.log').open('a', encoding='utf-8') as log:
                log.write(datetime.datetime.now().isoformat() + '\n' + traceback.format_exc() + '\n')
            status = {'observedAt': last_error_at,
                      'lastSuccessfulCheckAt': last_success_at,
                      'lastErrorAt': last_error_at,
                      'lastErrorSummary': last_error_summary,
                      'state': 'degraded', 'intervalSeconds': 15,
                      'scope': 'local_files_and_receipts_only', 'browserWrites': False}
            (root / '面板同步器状态.json').write_text(json.dumps(status, ensure_ascii=False, indent=2), encoding='utf-8')
        time.sleep(15)


if __name__ == '__main__':
    main()

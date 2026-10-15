"""Explicit offline maintenance; never sends HTTP or changes original bytes."""

import argparse
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .config import Config
from .reporting_service import ReportingService
from .reporting_models import FileStats
from .reporting_store import safe_relative_path
from .worksite_timezone import WorksiteTimezone


def utc_time(value):
    parsed = datetime.fromisoformat(value.replace('Z','+00:00'))
    if parsed.tzinfo is None or parsed.utcoffset().total_seconds() != 0:
        raise ValueError('event times must explicitly use UTC')
    return parsed.astimezone(timezone.utc)


def run(argv, root, config):
    parser = argparse.ArgumentParser(description=__doc__)
    subs = parser.add_subparsers(dest='command', required=True)
    subs.add_parser('status')
    retry = subs.add_parser('retry')
    retry.add_argument('--file-key', required=True)
    imp = subs.add_parser('import')
    imp.add_argument('--path', required=True)
    imp.add_argument('--device-id', required=True)
    imp.add_argument('--opened-at', required=True)
    imp.add_argument('--closed-at', required=True)
    imp.add_argument('--reason', required=True, choices=('IDLE_TIMEOUT','SERVER_SHUTDOWN','ABORTED'))
    args = parser.parse_args(argv)
    root = Path(root).resolve()
    if args.command == 'status':
        database = root / 'reporting/outbox.sqlite3'
        with sqlite3.connect(database.as_uri() + '?mode=ro', uri=True) as db:
            db.row_factory = sqlite3.Row
            result = {'recordings':[dict(r) for r in db.execute('SELECT file_key,path,state,error FROM recordings')],
                      'notifications':[dict(r) for r in db.execute('SELECT file_key,phase,state,attempts,error,next_at FROM outbox WHERE state!=\'SENT\'')]}
        print(json.dumps(result, ensure_ascii=True, indent=2))
        return 0
    if not config.enabled:
        raise ValueError('Web reporting must be configured for maintenance')
    # Exclusive lease prevents recovery/maintenance from racing the live collector.
    service = ReportingService(root, config)
    try:
        if args.command == 'retry':
            service.store.retry_blocked(args.file_key)
        else:
            safe_relative_path(args.path)
            path = root / args.path
            if not path.resolve().is_relative_to(root) or path.suffix not in ('.jsonl','.bin'):
                raise ValueError('import requires an in-root JSONL recording')
            opened, closed = utc_time(args.opened_at), utc_time(args.closed_at)
            if closed < opened:
                raise ValueError('closed-at precedes opened-at')
            # Inspect before registering so a malformed operator command creates no new identity.
            stats = FileStats(args.device_id, WorksiteTimezone())
            stats.scan(path, final=True)
            if not stats.valid and args.reason != 'ABORTED':
                raise ValueError('file is not a valid recording; use ABORTED only for explicit abnormal archival')
            row = service.register(args.path, args.device_id, args.opened_at)
            service.store.mark_closed(args.path, args.reason, args.closed_at)
            service.worker.prepare()
            print(json.dumps({'fileKey':row['file_key'],'rakingRecordingTaskId':row['task_id']}))
    finally:
        service.stop()
    return 0


def main():
    import sys
    config = Config.from_environ()
    try:
        return run(sys.argv[1:], config.data_dir, config.reporting)
    except (ValueError, OSError, RuntimeError, sqlite3.Error) as error:
        print('Reporting maintenance failed: ' + str(error), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())

"""WAL 의 커밋까지 복사하고 검증한 뒤에만 완성된 덤프를 공개한다."""
import argparse
from contextlib import closing
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sqlite3
import sys


def backup(data, out, keep_days, now=None):
    source, out = Path(data) / 'skills.db', Path(out)
    if not source.is_file():
        raise FileNotFoundError('skills.db 가 없다')
    if keep_days < 0:
        raise ValueError('keep-days 는 0 이상이어야 한다')
    now = now or datetime.now(timezone.utc)
    out.mkdir(parents=True, exist_ok=True)
    final = out / f"skills-{now.astimezone(timezone.utc):%Y%m%dT%H%M%SZ}.dump"
    part = final.with_suffix('.dump.part')
    try:
        part.unlink(missing_ok=True)
        with closing(sqlite3.connect(source.resolve().as_uri() + '?mode=ro', uri=True)) as src:
            with closing(sqlite3.connect(part)) as dst:
                src.backup(dst)
                if dst.execute('PRAGMA integrity_check').fetchall() != [('ok',)]:
                    raise RuntimeError('백업 integrity_check 실패')
                dst.execute('PRAGMA journal_mode=DELETE')
        with part.open('rb') as stream:
            os.fsync(stream.fileno())
        os.replace(part, final)
        directory = os.open(out, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    except Exception:
        part.unlink(missing_ok=True)
        raise
    for path in out.glob('skills-*.dump'):
        if path != final and path.is_file() and path.stat().st_mtime < now.timestamp() - keep_days * 86400:
            path.unlink()
    for path in out.glob('*.part'):
        if path.is_file():
            path.unlink()
    print(json.dumps({'backup': final.name, 'bytes': final.stat().st_size,
                      'kept': len(list(out.glob('skills-*.dump')))}))
    return final


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument('--data', required=True)
    parser.add_argument('--out', required=True)
    parser.add_argument('--keep-days', type=int, default=7)
    args = parser.parse_args(argv)
    try:
        backup(args.data, args.out, args.keep_days)
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())

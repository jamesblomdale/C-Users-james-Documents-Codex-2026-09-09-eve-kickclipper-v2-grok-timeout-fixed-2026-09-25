"""Retention helper for generated customer artifacts.

Default mode is dry-run. Production should invoke this from a scheduler once a
day. It intentionally skips owner output and hidden checkpoint/cache folders;
those have separate operational cleanup policies.
"""
import argparse, shutil, time
from pathlib import Path
from config import Config


def expired_customer_dirs(root: Path, days: int):
    cutoff = time.time() - max(1, days) * 86400
    if not root.exists():
        return []
    found = []
    for user_dir in root.glob('u*'):
        if not user_dir.is_dir():
            continue
        for mode in ('vod', 'vod_stream', 'live', '_uploads'):
            base = user_dir / mode
            if not base.exists():
                continue
            for child in base.iterdir():
                if child.is_dir() and child.stat().st_mtime < cutoff:
                    found.append(child)
                elif child.is_file() and child.stat().st_mtime < cutoff:
                    found.append(child)
    return found


def cleanup(root: Path, days: int, dry_run=True):
    targets = expired_customer_dirs(root, days)
    for target in targets:
        print(('WOULD DELETE ' if dry_run else 'DELETE ') + str(target))
        if not dry_run:
            shutil.rmtree(target, ignore_errors=True) if target.is_dir() else target.unlink(missing_ok=True)
    return len(targets)


if __name__ == '__main__':
    p = argparse.ArgumentParser()
    p.add_argument('--apply', action='store_true', help='actually delete expired customer artifacts')
    p.add_argument('--days', type=int, default=Config.RETENTION_DAYS)
    args = p.parse_args()
    count = cleanup(Path(Config.OUTPUT_DIR), args.days, dry_run=not args.apply)
    print(f'{count} expired artifact(s) matched')

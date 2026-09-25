"""Reversible, account-scoped library removal."""
import uuid


def move_library_to_trash(base, commit):
    base = base.resolve()
    trash = base / '.trash' / uuid.uuid4().hex
    moved = []
    try:
        for mode in ('vod', 'live', 'vod_stream'):
            for folder in sorted((base / mode).glob('*/clips/*')):
                if not folder.is_dir() or not (folder / 'clip.mp4').is_file():
                    continue
                if folder.is_symlink() or base not in folder.resolve().parents or folder.resolve() != folder.absolute():
                    raise ValueError('Refusing a redirected clip folder')
                target = trash / folder.relative_to(base)
                target.parent.mkdir(parents=True, exist_ok=True)
                folder.rename(target)
                moved.append((folder, target))
        commit([folder for folder, _ in moved])
    except Exception:
        for folder, target in reversed(moved):
            target.rename(folder)
        raise
    return len(moved)

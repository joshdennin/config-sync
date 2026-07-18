"""fsops — safe filesystem mutations. These are the only writers in the tool
(besides the tidy move that routes through safe_move). Every primitive refuses
to overwrite or delete: it raises FsError rather than clobber existing state,
and creates parent directories as needed. Dry-run and reporting live in the
action layer (an action surveys a plan, then calls these to execute it) — the
primitives themselves always act.
"""

import os
import shutil


class FsError(Exception):
    """A safe-write primitive refused to proceed — it would have overwritten or
    deleted existing state, or the source/target was not as expected."""


def ensure_parent(path):
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)


def safe_copy(src, dst, ignore=()):
    """Copy a file or directory tree src -> dst. Refuses if dst already exists.
    `ignore` is a set of basename patterns dropped at every level of a directory
    copy (e.g. a program's own .git, virtualenvs, bytecode caches)."""
    if os.path.lexists(dst):
        raise FsError(f"refusing to overwrite existing path: {dst}")
    ensure_parent(dst)
    if os.path.isdir(src) and not os.path.islink(src):
        ignore_fn = shutil.ignore_patterns(*ignore) if ignore else None
        shutil.copytree(src, dst, symlinks=True, ignore=ignore_fn)
    else:
        shutil.copy2(src, dst, follow_symlinks=False)
    return dst


def safe_move(src, dst):
    """Move src -> dst. Refuses if dst already exists."""
    if os.path.lexists(dst):
        raise FsError(f"refusing to overwrite existing path: {dst}")
    ensure_parent(dst)
    shutil.move(src, dst)
    return dst


def safe_symlink(target, link_path):
    """Create a symlink at link_path pointing to target. Refuses if link_path
    already exists."""
    if os.path.lexists(link_path):
        raise FsError(f"refusing to overwrite existing path: {link_path}")
    ensure_parent(link_path)
    os.symlink(target, link_path)
    return link_path


def remove_symlink(link_path):
    """Remove link_path, but only if it is a symlink — never a real file or dir."""
    if not os.path.islink(link_path):
        raise FsError(f"refusing to remove non-symlink: {link_path}")
    os.unlink(link_path)


def backup(path, backups_root, home):
    """Move path aside into backups_root, mirroring its location under home.
    Returns the backup path. (Used by `link` before it replaces an original
    with a symlink, so `unlink` can restore it.)"""
    if not path.startswith(home + os.sep):
        raise FsError(f"cannot back up a path outside home: {path}")
    dst = os.path.join(backups_root, os.path.relpath(path, home))
    return safe_move(path, dst)


def restore(backup_path, orig):
    """Move a backup back to its original location. Refuses if orig exists."""
    return safe_move(backup_path, orig)

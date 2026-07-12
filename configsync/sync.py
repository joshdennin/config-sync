"""sync — the mutating actions (tidy, adopt, link, unlink) and the manifest they
share. This is the only module that imports fsops (the safe-write primitives)
and the only one that runs writing-git; the read-only engine (inventory) and the
reporters never do. Every filesystem change routes through fsops, so the
overwrite/delete guarantees hold uniformly.
"""

import contextlib
import os
import subprocess
import tomllib
from datetime import datetime

import tomli_w

from .fsops import (FsError, backup, ensure_parent, remove_symlink, restore,
                    safe_copy, safe_move, safe_symlink)
from .inventory import (die, display_path, is_adoptable, repo_path_for,
                        repo_root, tilde)


# --------------------------------------------------------------------------
# Manifest — the persisted home<->repo mapping and link state for the managed
# repo, written by `adopt` and consumed by `link`/`unlink`. Read with stdlib
# tomllib and written with tomli_w. TOML has no null, so absent values
# (unattributed program, not-yet-set backup path) are stored as "".

MANIFEST_NAME = "manifest.toml"
MANIFEST_VERSION = 1


def manifest_path(conf_home):
    return os.path.join(repo_root(conf_home), MANIFEST_NAME)


def empty_manifest():
    return {"version": MANIFEST_VERSION, "entries": []}


def manifest_entry(program, home_path, repo_path, kind):
    """One manifest row; `linked`/`backup_path` are filled in by `link`. TOML
    cannot hold null, so an unattributed program is stored as ""."""
    return {"program": program or "", "home_path": home_path,
            "repo_path": repo_path, "kind": kind, "linked": False,
            "backup_path": ""}


def load_manifest(conf_home):
    """Read the manifest, or an empty one if the repo has none yet."""
    path = manifest_path(conf_home)
    try:
        with open(path, "rb") as f:
            data = tomllib.load(f)
    except FileNotFoundError:
        return empty_manifest()
    except (OSError, tomllib.TOMLDecodeError) as e:
        die(f"cannot read manifest {path!r}: {e}")
    if not isinstance(data, dict) or not isinstance(data.get("entries"), list):
        die(f"{path!r} is not a valid manifest (expected a {{version, entries}} object)")
    return data


def save_manifest(conf_home, manifest):
    path = manifest_path(conf_home)
    ensure_parent(path)
    with open(path, "wb") as f:
        tomli_w.dump(manifest, f)
    return path


# --------------------------------------------------------------------------
# Tidy — report (and optionally perform) safe XDG relocations.
#
# Checks $HOME for a conservative "Tier 1" set of config files: those whose
# program reads the ~/.config location *automatically* — no environment
# variable, wrapper, or sourced stub required — so the file can simply be moved
# there without breaking the application. Programs that only relocate via a
# pointer ($ZDOTDIR, $GNUPGHOME, a bash `source` shim, …) are left out: those
# are not transparent moves. A candidate is only moved when its target does not
# already exist; a symlinked source is reported but never moved. Never
# overwrites or deletes.

# program -> [(HOME-relative source, ~/.config-relative target)].
# INVARIANT: only programs that read the target automatically. Each pair is a
# promise that moving source -> target is transparent to the program; keep it
# that way when adding rows (verify against the program's own docs, not a guess).
TIER1 = {
    "git": [
        (".gitconfig", "git/config"),          # $XDG_CONFIG_HOME/git/config
        (".gitignore_global", "git/ignore"),   # git's default XDG excludes file
    ],
    "tmux": [
        (".tmux.conf", "tmux/tmux.conf"),      # tmux >= 3.1 reads the XDG path
    ],
}

# Status -> (label, note). "movable" is the only actionable-by-move state.
TIDY_STATUS = {
    "movable": ("MOVABLE", ""),
    "merge":   ("MERGE  ", "target exists — merge by hand"),
    "symlink": ("SYMLINK", "source is a symlink — left for your dotfiles tool"),
    "done":    ("DONE   ", "already at the target"),
}


def tidy_classify(src, dst):
    """Movement state for a HOME source and its ~/.config target."""
    src_here = os.path.lexists(src)  # lexists so a dangling/symlink source counts
    dst_here = os.path.lexists(dst)
    if not src_here:
        return "done" if dst_here else "absent"
    if os.path.islink(src):
        return "symlink"
    return "merge" if dst_here else "movable"


def tidy_survey(home, conf_home):
    rows = []  # (program, src_rel, dst_rel, src_abs, dst_abs, status)
    for prog, pairs in sorted(TIER1.items()):
        for src_rel, dst_rel in pairs:
            src = os.path.join(home, src_rel)
            dst = os.path.join(conf_home, dst_rel)
            status = tidy_classify(src, dst)
            if status != "absent":  # nothing to say about files you don't have
                rows.append((prog, src_rel, dst_rel, src, dst, status))
    return rows


def tidy_report(rows, moved=False):
    verb = "Moved" if moved else "Tier 1 config relocations"
    print(f"tidy — {verb} (HOME → ~/.config)\n")
    if not rows:
        print("  nothing to report — no known Tier 1 config files in $HOME.")
        return
    width = max(len(f"~/{r[1]}") for r in rows)
    for prog, src_rel, dst_rel, _, _, status in rows:
        label, note = TIDY_STATUS[status]
        arrow = f"~/{src_rel:<{width}} → ~/.config/{dst_rel}"
        print(f"  {label}  {arrow}" + (f"   ({note})" if note else ""))
    movable = sum(1 for r in rows if r[5] == "movable")
    if not moved:
        print()
        if movable:
            print(f"{movable} movable · run with --move to relocate them.")
        else:
            print("Nothing to move.")


def tidy_move(rows):
    done = []
    for prog, src_rel, dst_rel, src, dst, status in rows:
        if status != "movable":
            continue
        safe_move(src, dst)  # status=="movable" guarantees dst is absent
        done.append((prog, src_rel, dst_rel, src, dst, "done"))
    if done:
        tidy_report(done, moved=True)
    else:
        print("tidy — nothing to move.")
    return done


# --------------------------------------------------------------------------
# Adopt — build a dotfiles repo from discovered configs, in two phases. Phase
# one (`adopt`) writes an editable plan file and copies nothing; phase two
# (`adopt --apply`) reads the edited plan and materializes the repo. The plan
# is the review surface, so the tier is only a starting breadth — nothing is
# dropped without the user seeing it.

# Relevance floor per tier (on top of the always-on is_adoptable gate). Tunable.
ADOPT_TIERS = {"curated": 50, "extended": 15, "everything": 0}
ADOPT_PLAN_VERSION = 1


def _adopt_match(rec, names):
    """True if the entry's program or category is named."""
    return rec["program"] in names or rec["category"] in names


def adopt_candidates(inv, tier, include, exclude, conf_home):
    """Entries eligible for the plan at `tier`, after include/exclude. Applies
    the safety gate, the tier's relevance floor, and adopt policy (skip anything
    already under version control — it is managed elsewhere)."""
    floor = ADOPT_TIERS[tier]
    out = []
    for rec in inv["entries"]:
        if not is_adoptable(rec, conf_home) or rec["is_git_repo"]:
            continue
        if (rec["relevance"] or 0) < floor:
            continue
        if include and not _adopt_match(rec, include):
            continue
        if exclude and _adopt_match(rec, exclude):
            continue
        out.append(rec)
    return out


def adopt_plan_row(rec):
    return {"program": rec["program"] or "", "path": display_path(rec),
            "kind": rec["kind"], "category": rec["category"] or "",
            "relevance": rec["relevance"] or 0, "adopt": True}


def write_adopt_plan(path, rows, tier):
    header = (f'# config-sync adopt plan — "{tier}" tier, {datetime.now():%Y-%m-%d}\n'
              "# Edit before applying: set adopt = false (or delete a block) to skip an entry.\n"
              f"# Then run:  config-sync adopt --apply --plan {path}\n\n")
    data = {"version": ADOPT_PLAN_VERSION, "tier": tier, "entries": rows}
    with open(path, "wb") as f:
        f.write(header.encode())
        tomli_w.dump(data, f)
    return path


def load_adopt_plan(path):
    try:
        with open(path, "rb") as f:
            data = tomllib.load(f)
    except FileNotFoundError:
        die(f"adopt plan not found: {path}\n"
            "  run `config-sync adopt` first to generate one")
    except (OSError, tomllib.TOMLDecodeError) as e:
        die(f"cannot read adopt plan {path!r}: {e}")
    if not isinstance(data, dict) or not isinstance(data.get("entries"), list):
        die(f"{path!r} is not a valid adopt plan (expected a {{version, entries}} object)")
    return data


def expand_home(path, home):
    return os.path.join(home, path[2:]) if path.startswith("~/") else path


def git_init_commit(repo, message):
    """Best-effort init + add + commit for the freshly built repo. Commit can
    fail (e.g. no git identity configured); that is reported, not fatal."""
    for args in (["init", "-q"], ["add", "-A"]):
        subprocess.run(["git", "-C", repo, *args], capture_output=True, text=True)
    r = subprocess.run(["git", "-C", repo, "commit", "-q", "-m", message],
                       capture_output=True, text=True)
    return r.returncode == 0


def adopt_apply(plan, home, conf_home, cfg):
    """Copy the plan's adopt=true entries into the repo, merge the manifest, and
    commit. Re-runnable: entries already recorded or already present are skipped."""
    manifest = load_manifest(conf_home)
    known = {e["home_path"] for e in manifest["entries"]}
    copied, skipped = [], []
    for row in plan["entries"]:
        if not row.get("adopt", False):
            continue
        home_path = expand_home(row["path"], home)
        program = row["program"] or None
        repo_path = repo_path_for(home_path, row["kind"], program, cfg, conf_home)
        if home_path in known or os.path.lexists(repo_path):
            skipped.append(row["path"])
            continue
        try:
            safe_copy(home_path, repo_path)
        except (FsError, OSError) as e:
            skipped.append(f"{row['path']} ({e})")
            continue
        manifest["entries"].append(manifest_entry(program, home_path, repo_path, row["kind"]))
        known.add(home_path)
        copied.append(row["path"])
    committed = False
    if copied:
        save_manifest(conf_home, manifest)
        ensure_repo_gitignore(repo_root(conf_home))  # keep backups out of git
        committed = git_init_commit(repo_root(conf_home),
                                    f"adopt {len(copied)} config(s) via config-sync")
    return {"copied": copied, "skipped": skipped, "committed": committed,
            "repo": repo_root(conf_home)}


# --------------------------------------------------------------------------
# Link — deploy the repo back into the filesystem: for each adopted entry, move
# the home original aside into the backups tree and replace it with a symlink
# into the repo. Reversible via `unlink`, which restores the backup. Backups
# live under the repo but are git-ignored so they never get committed.

BACKUP_DIRNAME = ".backups"

# status -> (label, note); "link"/"link-missing" are the actionable states.
LINK_STATUS = {
    "link":         ("LINK", "back up original, then symlink"),
    "link-missing": ("LINK", "original absent — symlink only"),
    "done":         ("DONE", "already linked into the repo"),
    "conflict":     ("SKIP", "home is a symlink elsewhere — left as-is"),
    "no-source":    ("SKIP", "repo content missing"),
}


def backups_root(conf_home):
    return os.path.join(repo_root(conf_home), BACKUP_DIRNAME)


def ensure_repo_gitignore(repo):
    """Keep the backups tree out of the repo's git history (idempotent)."""
    path = os.path.join(repo, ".gitignore")
    line = BACKUP_DIRNAME + "/"
    existing = ""
    if os.path.exists(path):
        with open(path) as f:
            existing = f.read()
        if line in existing.split():
            return
    ensure_parent(path)
    with open(path, "a") as f:
        if existing and not existing.endswith("\n"):
            f.write("\n")
        f.write(line + "\n")


def link_status(entry):
    """Where a manifest entry stands relative to being linked (pure inspection)."""
    home_path, repo_path = entry["home_path"], entry["repo_path"]
    if not os.path.lexists(repo_path):
        return "no-source"
    if os.path.islink(home_path):
        if os.path.realpath(home_path) == os.path.realpath(repo_path):
            return "done"      # already a symlink into the repo
        return "conflict"      # a symlink to something else — leave it alone
    if os.path.lexists(home_path):
        return "link"          # a real file/dir — back it up, then symlink
    return "link-missing"      # original gone — just place the symlink


def link_survey(manifest):
    return [(e, link_status(e)) for e in manifest["entries"]]


def link_apply(manifest, home, conf_home):
    """Back up + symlink every actionable entry; record link state in the
    manifest. Idempotent — already-linked entries are left untouched."""
    ensure_repo_gitignore(repo_root(conf_home))
    backups = backups_root(conf_home)
    linked, skipped = [], []
    for entry in manifest["entries"]:
        status = link_status(entry)
        home_path, repo_path = entry["home_path"], entry["repo_path"]
        if status not in ("link", "link-missing"):
            skipped.append((home_path, status))
            continue
        bpath = ""
        try:
            bpath = backup(home_path, backups, home) if status == "link" else ""
            safe_symlink(repo_path, home_path)
        except (FsError, OSError) as e:
            # Keep the step atomic: if the original was already moved aside but
            # the symlink failed, put it back. Otherwise it would sit orphaned
            # in the backups tree with nothing in the manifest to restore it.
            if bpath:
                with contextlib.suppress(FsError, OSError):
                    restore(bpath, home_path)
            skipped.append((home_path, f"error: {e}"))
            continue
        entry["linked"] = True
        entry["backup_path"] = bpath
        linked.append(home_path)
    if linked:
        save_manifest(conf_home, manifest)
    return {"linked": linked, "skipped": skipped}


def link_report(rows, home, applied=False):
    print(f"link — {'Linked' if applied else 'Link plan (home → repo)'}\n")
    if not rows:
        print("  nothing to link — no adopted configs.")
        return
    for entry, status in rows:
        label, note = LINK_STATUS[status]
        arrow = f"{tilde(entry['home_path'], home)} → {tilde(entry['repo_path'], home)}"
        print(f"  {label:<5} {arrow}" + (f"   ({note})" if note else ""))
    todo = sum(1 for _, s in rows if s in ("link", "link-missing"))
    if not applied:
        print()
        print(f"{todo} to link · run with --apply to create the symlinks."
              if todo else "Nothing to link.")


# --------------------------------------------------------------------------
# Unlink — reverse of link: remove the symlink and restore the backed-up
# original, then clear the link state. The reversibility guarantee made
# operational; it undoes `link` but leaves the repo (the `adopt` copy) intact.

# status -> (label, note); "restore"/"unlink-only" are the actionable states.
UNLINK_STATUS = {
    "restore":     ("RESTORE", "remove symlink, restore backup"),
    "unlink-only": ("UNLINK", "remove symlink (nothing was backed up)"),
    "not-linked":  ("SKIP", "not linked"),
    "changed":     ("SKIP", "home is not the symlink config-sync created — left as-is"),
}


def unlink_status(entry):
    """Where a manifest entry stands relative to being unlinked (pure inspection)."""
    if not entry.get("linked"):
        return "not-linked"
    home_path, repo_path = entry["home_path"], entry["repo_path"]
    if not os.path.islink(home_path) or \
            os.path.realpath(home_path) != os.path.realpath(repo_path):
        return "changed"       # not the symlink we made — do not touch it
    return "restore" if entry.get("backup_path") else "unlink-only"


def unlink_survey(manifest):
    return [(e, unlink_status(e)) for e in manifest["entries"]]


def unlink_apply(manifest, home, conf_home):
    """Remove each symlink config-sync created and restore its backup, clearing
    link state. Idempotent — entries that are not linked are left untouched."""
    restored, skipped = [], []
    for entry in manifest["entries"]:
        status = unlink_status(entry)
        home_path = entry["home_path"]
        if status not in ("restore", "unlink-only"):
            skipped.append((home_path, status))
            continue
        try:
            remove_symlink(home_path)
            if status == "restore":
                restore(entry["backup_path"], home_path)
        except (FsError, OSError) as e:
            skipped.append((home_path, f"error: {e}"))
            continue
        entry["linked"] = False
        entry["backup_path"] = ""
        restored.append(home_path)
    if restored:
        save_manifest(conf_home, manifest)
    return {"restored": restored, "skipped": skipped}


def unlink_report(rows, home, applied=False):
    print(f"unlink — {'Restored' if applied else 'Unlink plan (restore originals)'}\n")
    if not rows:
        print("  nothing to unlink — no adopted configs.")
        return
    for entry, status in rows:
        label, note = UNLINK_STATUS[status]
        print(f"  {label:<7} {tilde(entry['home_path'], home)}"
              + (f"   ({note})" if note else ""))
    todo = sum(1 for _, s in rows if s in ("restore", "unlink-only"))
    if not applied:
        print()
        print(f"{todo} to restore · run with --apply to undo the links."
              if todo else "Nothing to unlink.")

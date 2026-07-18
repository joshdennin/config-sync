"""sync — the mutating actions (tidy, adopt, link, unlink) and the manifest they
share. This is the only module that imports fsops (the safe-write primitives)
and the only one that runs writing-git; the read-only engine (inventory) and the
reporters never do. Every filesystem change routes through fsops, so the
overwrite/delete guarantees hold uniformly.
"""

import contextlib
import os
import shutil
import subprocess
import textwrap
import tomllib
from datetime import datetime

import tomli_w

from .fsops import (FsError, backup, ensure_parent, remove_symlink, restore,
                    safe_copy, safe_move, safe_symlink)
from .inventory import (UNCATEGORIZED, check_installed, default_config_path,
                        die, display_path, is_adoptable, ordered_categories,
                        repo_config_path, repo_path_for, repo_root, tilde)


# --------------------------------------------------------------------------
# Manifest — the persisted home<->repo mapping and link state for the managed
# repo, written by `adopt` and consumed by `link`/`unlink`. Read with stdlib
# tomllib and written with tomli_w. TOML has no null, so absent values
# (unattributed program, not-yet-set backup path) are stored as "".

MANIFEST_NAME = "manifest.toml"
MANIFEST_VERSION = 1
BACKUP_DIRNAME = ".backups"  # link's originals tree, under the repo (git-ignored)


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


def _resolve_entry(entry, home, conf_home):
    """Portable (on-disk) -> absolute (in-memory): expand the ~/-relative home
    path and the repo-relative repo path against this machine's roots."""
    return {**entry,
            "home_path": expand_home(entry["home_path"], home),
            "repo_path": os.path.join(repo_root(conf_home), entry["repo_path"])}


def _relativize_entry(entry, home, conf_home):
    """Absolute (in-memory) -> portable (on-disk): store the home path ~/-relative
    and the repo path relative to the repo root, so the manifest resolves on any
    machine. `backup_path` is machine-local runtime state and left untouched."""
    return {**entry,
            "home_path": tilde(entry["home_path"], home),
            "repo_path": os.path.relpath(entry["repo_path"], repo_root(conf_home))}


def load_manifest(conf_home, home):
    """Read the manifest, or an empty one if the repo has none yet. Entries are
    stored portable and resolved to absolute paths for the rest of the tool."""
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
    data["entries"] = [_resolve_entry(e, home, conf_home) for e in data["entries"]]
    return data


def save_manifest(conf_home, manifest, home):
    path = manifest_path(conf_home)
    ensure_parent(path)
    out = {**manifest,
           "entries": [_relativize_entry(e, home, conf_home)
                       for e in manifest["entries"]]}
    with open(path, "wb") as f:
        tomli_w.dump(out, f)
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
ADOPT_PLAN_NAME = "config-sync-adopt.toml"  # default plan file, captured in the repo

# Basenames never copied into the repo when adopting a directory (matched at
# every level of the tree). A managed config's own git metadata is stripped so
# the repo does not swallow a nested repo; virtualenvs and bytecode caches are
# bulky and regenerable, so they are dropped for every program.
ADOPT_IGNORE = ("__pycache__", ".venv", ".git", ".gitignore")

# Repo entries that are the tool's own bookkeeping, not adopted program content.
# Their presence does not make the repo "populated" for the adopt guard, so the
# config/plan written in the plan phase never blocks the first `adopt --apply`.
REPO_BOOKKEEPING = frozenset({
    ".git", ".gitignore", BACKUP_DIRNAME, MANIFEST_NAME,
    os.path.basename(repo_config_path("")), ADOPT_PLAN_NAME})


def repo_has_adopted_content(repo):
    """True if the repo holds adopted configs (a program dir, or a cloned repo's
    content) — anything beyond the tool's own bookkeeping files."""
    try:
        return any(name not in REPO_BOOKKEEPING for name in os.listdir(repo))
    except OSError:
        return False  # no repo yet


def default_plan_path(conf_home):
    """Where `adopt` writes its plan by default — inside the managed repo, so it
    is captured and shared along with the configs it adopted."""
    return os.path.join(repo_root(conf_home), ADOPT_PLAN_NAME)


def ensure_repo_scaffold(conf_home):
    """Create the repo dir (before any plan/config lands in it) and capture the
    classification config there if absent, so a clone carries the registry it was
    built with. Never clobbers an existing (customized or cloned) config."""
    repo = repo_root(conf_home)
    os.makedirs(repo, exist_ok=True)
    dst = repo_config_path(conf_home)
    if not os.path.exists(dst):
        shutil.copy2(default_config_path(), dst)
    return repo


def _adopt_match(rec, names):
    """True if the entry's program or category is named."""
    return rec["program"] in names or rec["category"] in names


def adopt_candidates(inv, tier, include, exclude, conf_home):
    """Entries eligible for the plan at `tier`, after include/exclude. Applies
    the safety gate and the tier's relevance floor. A config that is already its
    own git repo is a strong, explicit signal, so it bypasses the floor and
    surfaces in every tier — flagged `managed` with `adopt` defaulted off (see
    adopt_plan_rows), for the user to opt in rather than adopt by default."""
    floor = ADOPT_TIERS[tier]
    out = []
    for rec in inv["entries"]:
        if not is_adoptable(rec, conf_home):
            continue
        if not rec["is_git_repo"] and (rec["relevance"] or 0) < floor:
            continue
        if include and not _adopt_match(rec, include):
            continue
        if exclude and _adopt_match(rec, exclude):
            continue
        out.append(rec)
    return out


def adopt_plan_rows(cands, cfg, conf_home):
    """Group adopt candidates into one plan entry per program, each listing every
    path it owns as a {home, repo} pair — where the file lives now and where it
    lands, relative to the repo root (recorded as `repo` at the top of the plan).
    Unattributed entries key on their own path so distinct files never merge.
    Ordered by category — health's category order — then program name, so the
    plan is stable and reads like the health report."""
    root = repo_root(conf_home)
    groups = {}  # group key -> plan entry
    for rec in cands:
        program = rec["program"] or None
        key = program or rec["path"]
        g = groups.get(key)
        if g is None:
            g = {"program": rec["program"] or "",
                 "category": rec["category"] or UNCATEGORIZED,
                 "adopt": True, "managed": False, "paths": []}
            groups[key] = g
        if rec["is_git_repo"]:
            # A versioned config: mark it managed and default adopt off, so it is
            # surfaced for review but not copied unless the user opts in.
            g["managed"] = True
            g["adopt"] = False
        repo_path = repo_path_for(rec["path"], rec["kind"], program, cfg, conf_home)
        g["paths"].append({"home": display_path(rec),
                           "repo": os.path.relpath(repo_path, root)})
    cat_rank = {c: i for i, c in enumerate(ordered_categories(cands))}
    rows = sorted(groups.values(),
                  key=lambda g: (cat_rank.get(g["category"], 0), g["program"].lower()))
    for g in rows:
        g["paths"].sort(key=lambda p: p["home"])
    return rows


def omitted_programs(inv, rows):
    """Attributed programs found in the scan but left out of the plan — listed in
    a comment so nothing drops silently. Secrets are excluded (never adoptable,
    and not worth advertising); [exclude] entries never enter the inventory, so
    they are already absent. Unattributed entries have no name to show."""
    included = {r["program"] for r in rows if r["program"]}
    omitted = {rec["program"] for rec in inv["entries"]
               if rec.get("program") and rec["program"] not in included
               and "secret" not in (rec.get("flags") or ())}
    return sorted(omitted, key=str.lower)


def _omitted_comment(omitted, tier):
    """Comment block naming the omitted programs, or "" when there are none."""
    if not omitted:
        return ""
    intro = (f'# Not in this "{tier}" plan — discovered but below the tier\'s\n'
             "# relevance floor or not adoptable. Add any by hand if you want them\n"
             "# (secrets and [exclude] entries are intentionally not listed):\n")
    body = textwrap.fill(", ".join(omitted), width=76,
                         initial_indent="#   ", subsequent_indent="#   ")
    return intro + body + "\n"


def write_adopt_plan(path, rows, tier, repo, omitted=()):
    header = (f'# config-sync adopt plan — "{tier}" tier, {datetime.now():%Y-%m-%d}\n'
              "# Edit before applying: set adopt = false (or remove a path, or\n"
              "# delete a block) to skip it. Each entry is one program; paths lists\n"
              "# every file/dir it owns as { home = where it lives now, repo = its\n"
              "# path inside the repo (below) }. managed = true marks a config that\n"
              "# is already its own git repo; adopt is off for those by default —\n"
              "# set adopt = true to copy it in anyway.\n"
              f"# Then run:  config-sync adopt --apply --plan {path}\n"
              + _omitted_comment(omitted, tier) + "\n")
    data = {"version": ADOPT_PLAN_VERSION, "tier": tier, "repo": repo, "entries": rows}
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


def adopt_apply(plan, home, conf_home, cfg, force=False):
    """Copy the plan's adopt=true entries into the repo, merge the manifest, and
    commit. Re-runnable: entries already recorded or already present are skipped.
    Refuses to add to a repo that already holds adopted configs (e.g. one cloned
    from another machine) unless `force`, so a shared repo is not polluted."""
    repo = repo_root(conf_home)
    if repo_has_adopted_content(repo) and not force:
        die(f"{tilde(repo, home)} already holds adopted configs — refusing to "
            "copy local configs into it.\n"
            "  This repo looks shared (built elsewhere or already populated). "
            "Deploy it with `config-sync sync` instead, or pass --force to add "
            "local configs anyway.")
    manifest = load_manifest(conf_home, home)
    known = {e["home_path"] for e in manifest["entries"]}
    copied, skipped = [], []
    for entry in plan["entries"]:
        if not entry.get("adopt", False):
            continue
        program = entry.get("program") or None
        for p in entry.get("paths", []):
            disp = p["home"]
            home_path = expand_home(disp, home)
            # kind is read from the filesystem at apply time (the plan no longer
            # stores it) so it always matches what is actually on disk now.
            kind = "dir" if os.path.isdir(home_path) else "file"
            repo_path = repo_path_for(home_path, kind, program, cfg, conf_home)
            if home_path in known or os.path.lexists(repo_path):
                skipped.append(disp)
                continue
            try:
                safe_copy(home_path, repo_path, ignore=ADOPT_IGNORE)
            except (FsError, OSError) as e:
                skipped.append(f"{disp} ({e})")
                continue
            manifest["entries"].append(manifest_entry(program, home_path, repo_path, kind))
            known.add(home_path)
            copied.append(disp)
    committed = False
    if copied:
        save_manifest(conf_home, manifest, home)
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

# status -> (label, note); "link"/"link-missing" are the actionable states.
# "not-installed" is not a link_status — it is the skip reason `sync` records for
# an entry whose program is absent on this machine.
LINK_STATUS = {
    "link":          ("LINK", "back up original, then symlink"),
    "link-missing":  ("LINK", "original absent — symlink only"),
    "done":          ("DONE", "already linked into the repo"),
    "conflict":      ("SKIP", "home is a symlink elsewhere — left as-is"),
    "no-source":     ("SKIP", "repo content missing"),
    "not-installed": ("SKIP", "program not installed here"),
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


def link_apply(manifest, home, conf_home, should_link=None):
    """Back up + symlink every actionable entry; record link state in the
    manifest. Idempotent — already-linked entries are left untouched. When
    `should_link` is given (used by `sync`), entries it rejects are skipped as
    "not-installed" while still kept in the saved manifest."""
    ensure_repo_gitignore(repo_root(conf_home))
    backups = backups_root(conf_home)
    linked, skipped = [], []
    for entry in manifest["entries"]:
        home_path, repo_path = entry["home_path"], entry["repo_path"]
        if should_link is not None and not should_link(entry):
            skipped.append((home_path, "not-installed"))
            continue
        status = link_status(entry)
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
        save_manifest(conf_home, manifest, home)
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
# Sync — deploy a repo (typically one cloned from another machine) onto this
# host: for each adopted config, check its program is actually installed here,
# then symlink the installed ones into place, reusing link's backup+symlink. The
# read-only survey doubles as "what does this repo hold, and can I use it here?".

def program_installed(entry, qq, cfg):
    """Whether the entry's program is available on this machine. An unattributed
    entry (program == "") has nothing to gate on, so it counts as installed."""
    program = entry["program"]
    return check_installed(program, qq, cfg) if program else True


def sync_survey(manifest, qq, cfg):
    """Per entry: (entry, installed, link_status). link_status re-reads the
    filesystem, so a freshly cloned repo reports what would actually happen."""
    return [(e, program_installed(e, qq, cfg), link_status(e))
            for e in manifest["entries"]]


def sync_apply(manifest, home, conf_home, qq, cfg, force=False):
    """Symlink every installed, actionable entry into place (backing up any
    original first), exactly as `link`. Entries whose program is missing here are
    skipped as "not-installed" unless `force`, but stay in the manifest."""
    should_link = None if force else lambda e: program_installed(e, qq, cfg)
    return link_apply(manifest, home, conf_home, should_link=should_link)


def sync_report(rows, home):
    print("sync — deploy plan (repo → home)\n")
    if not rows:
        print("  nothing to sync — the repo holds no adopted configs.")
        return
    width = max(len(tilde(e["home_path"], home)) for e, _, _ in rows)
    to_link = 0
    for entry, installed, status in rows:
        if not installed:
            label, note = LINK_STATUS["not-installed"]
        else:
            label, note = LINK_STATUS[status]
            if status in ("link", "link-missing"):
                to_link += 1
        home_disp = tilde(entry["home_path"], home)
        prog = entry["program"] or "—"
        print(f"  {label:<5} {home_disp:<{width}}  [{prog}]"
              + (f"   ({note})" if note else ""))
    print()
    print(f"{to_link} to link · run with --apply to create the symlinks."
          if to_link else "Nothing to link.")


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
        save_manifest(conf_home, manifest, home)
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

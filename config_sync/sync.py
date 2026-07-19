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
from .inventory import (CONFIG_NAME, INVENTORY_NAME, UNCATEGORIZED, capture,
                        check_installed, default_config_path, die, display_path,
                        is_adoptable, ordered_categories, repo_config_path,
                        repo_path_for, repo_root, status_counts, tilde)


# --------------------------------------------------------------------------
# Manifest — the persisted home<->repo mapping and link state for the managed
# repo, written by `adopt` and consumed by `link`/`unlink`. Read with stdlib
# tomllib and written with tomli_w. TOML has no null, so absent values
# (unattributed program, not-yet-set backup path) are stored as "".

MANIFEST_NAME = "manifest.toml"
MANIFEST_VERSION = 1
BACKUP_DIRNAME = ".backups"  # link's originals tree, under the repo (git-ignored)
# Machine-local link state (which entries are linked here, and where their
# originals were backed up). Kept out of the committed manifest so deploying on
# one machine never dirties the shared repo or conflicts on `git pull`.
LINK_STATE_NAME = ".link-state.toml"
LINK_STATE_VERSION = 1
GIT_DIR = ".git"            # a program's own repo metadata / the managed repo's
GITIGNORE_NAME = ".gitignore"

# The committed manifest row is the portable home<->repo mapping only; `linked`
# and `backup_path` are machine-local and live in the link-state file instead
# (see _relativize_entry, which emits exactly these keys).

# Marker for a home path that lives under ~/.config (config_home): stored
# relative to config_home so it resolves against the *target* machine's
# $XDG_CONFIG_HOME, not a hard-coded ~/.config. Home-root dotfiles stay ~/-relative.
CONFIG_PREFIX = "$CONFIG/"


def manifest_path(conf_home):
    return os.path.join(repo_root(conf_home), MANIFEST_NAME)


def link_state_path(conf_home):
    return os.path.join(repo_root(conf_home), LINK_STATE_NAME)


def empty_manifest():
    return {"version": MANIFEST_VERSION, "entries": []}


def manifest_entry(program, home_path, repo_path, kind):
    """One manifest row; `linked`/`backup_path` are machine-local runtime state
    (persisted separately by `link`). TOML cannot hold null, so an unattributed
    program is stored as ""."""
    return {"program": program or "", "home_path": home_path,
            "repo_path": repo_path, "kind": kind, "linked": False,
            "backup_path": ""}


def _relativize_home(home_path, home, conf_home):
    """Absolute home path -> portable string. A path under config_home is stored
    config-relative ($CONFIG/…) so it follows the target's $XDG_CONFIG_HOME; any
    other path is stored ~/-relative."""
    if home_path == conf_home or home_path.startswith(conf_home + os.sep):
        return CONFIG_PREFIX + os.path.relpath(home_path, conf_home)
    return tilde(home_path, home)


def _resolve_home(stored, home, conf_home):
    """Portable string -> absolute home path, the inverse of _relativize_home."""
    if stored.startswith(CONFIG_PREFIX):
        return os.path.join(conf_home, stored[len(CONFIG_PREFIX):])
    return expand_home(stored, home)


def _resolve_entry(entry, home, conf_home):
    """Portable (on-disk) -> absolute (in-memory): resolve the home path against
    this machine's $HOME/$XDG_CONFIG_HOME and the repo path against the repo."""
    return {**entry,
            "home_path": _resolve_home(entry["home_path"], home, conf_home),
            "repo_path": os.path.join(repo_root(conf_home), entry["repo_path"])}


def _relativize_entry(entry, home, conf_home):
    """Absolute (in-memory) -> portable (on-disk mapping): home path config- or
    ~/-relative, repo path relative to the repo root, so it resolves anywhere."""
    return {"program": entry["program"],
            "home_path": _relativize_home(entry["home_path"], home, conf_home),
            "repo_path": os.path.relpath(entry["repo_path"], repo_root(conf_home)),
            "kind": entry["kind"]}


def _load_link_state(conf_home):
    """Read the machine-local link state as {portable_home_path: {linked,
    backup_path}}, or empty when the repo has none (e.g. a fresh clone)."""
    path = link_state_path(conf_home)
    try:
        with open(path, "rb") as f:
            data = tomllib.load(f)
    except FileNotFoundError:
        return {}
    except (OSError, tomllib.TOMLDecodeError) as e:
        die(f"cannot read link state {path!r}: {e}")
    entries = data.get("entries", {})
    return entries if isinstance(entries, dict) else {}


def load_manifest(conf_home, home):
    """Read the manifest (portable mapping) and merge the machine-local link
    state, resolving every path to absolute for the rest of the tool. Returns an
    empty manifest if the repo has none yet."""
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
    state = _load_link_state(conf_home)
    repo = repo_root(conf_home)
    entries = []
    for e in data["entries"]:
        st = state.get(e["home_path"], {})
        bpath = st.get("backup_path", "")
        merged = {**e, "linked": bool(st.get("linked", False)),
                  "backup_path": os.path.join(repo, bpath) if bpath else ""}
        entries.append(_resolve_entry(merged, home, conf_home))
    data["entries"] = entries
    return data


def save_manifest(conf_home, manifest, home):
    """Write the committed manifest — the portable mapping only. Link state is
    persisted separately (save_link_state), so this never records machine-local
    detail into the shared repo."""
    path = manifest_path(conf_home)
    ensure_parent(path)
    out = {"version": manifest.get("version", MANIFEST_VERSION),
           "entries": [_relativize_entry(e, home, conf_home)
                       for e in manifest["entries"]]}
    with open(path, "wb") as f:
        tomli_w.dump(out, f)
    return path


def save_link_state(conf_home, manifest, home):
    """Persist the machine-local link state (git-ignored) for the linked entries,
    keyed by the same portable home path the manifest uses. Backup paths are
    stored repo-relative; unlinked entries are simply omitted."""
    repo = repo_root(conf_home)
    entries = {}
    for e in manifest["entries"]:
        if not e.get("linked"):
            continue
        key = _relativize_home(e["home_path"], home, conf_home)
        bpath = e.get("backup_path") or ""
        entries[key] = {"linked": True,
                        "backup_path": os.path.relpath(bpath, repo) if bpath else ""}
    path = link_state_path(conf_home)
    ensure_parent(path)
    with open(path, "wb") as f:
        tomli_w.dump({"version": LINK_STATE_VERSION, "entries": entries}, f)
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
# Adopt — build a dotfiles repo from discovered configs, in two phases across
# two commands. Phase one (`plan`) writes an editable plan file and copies
# nothing; phase two (`adopt`) reads the edited plan and materializes the repo.
# The plan is the review surface, so the tier is only a starting breadth —
# nothing is dropped without the user seeing it.

# Relevance floor per tier (on top of the always-on is_adoptable gate). Tunable.
ADOPT_TIERS = {"curated": 50, "extended": 15, "everything": 0}
ADOPT_PLAN_VERSION = 1
ADOPT_PLAN_NAME = "config-sync-adopt.toml"  # default plan file, captured in the repo

# Basenames never copied into the repo when adopting a directory (matched at
# every level of the tree). A managed config's own git metadata is stripped so
# the repo does not swallow a nested repo; virtualenvs and bytecode caches are
# bulky and regenerable, so they are dropped for every program.
ADOPT_IGNORE = ("__pycache__", ".venv", GIT_DIR, GITIGNORE_NAME)

# Repo entries that are the tool's own bookkeeping, not adopted program content.
# Their presence does not make the repo "populated" for the adopt guard, so the
# config/plan/inventory written by `plan`/`scan` never blocks the first `adopt`.
REPO_BOOKKEEPING = frozenset({
    GIT_DIR, GITIGNORE_NAME, BACKUP_DIRNAME, MANIFEST_NAME, LINK_STATE_NAME,
    CONFIG_NAME, ADOPT_PLAN_NAME, INVENTORY_NAME})

# Repo-relative paths kept out of git: the backups tree, the machine-local link
# state, and the scan snapshot (all per-machine — they must not travel with the
# shared repo).
GITIGNORE_LINES = (BACKUP_DIRNAME + "/", LINK_STATE_NAME, INVENTORY_NAME)


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


# Comment marking an entry whose config is already its own git repo. It is not a
# configurable field — the state is discovered, not chosen — so it reads as a
# comment above the entry rather than a `managed = …` key the user might edit.
_MANAGED_COMMENT = (b"# already tracked in its own git repo "
                    b"(adopt defaults off; set it true to copy in anyway)\n")


def write_adopt_plan(path, rows, tier, repo, omitted=()):
    header = (f'# config-sync adopt plan — "{tier}" tier, {datetime.now():%Y-%m-%d}\n'
              "# Edit before applying: set adopt = false (or remove a path, or\n"
              "# delete a block) to skip it. Each entry is one program; paths lists\n"
              "# every file/dir it owns as { home = where it lives now, repo = its\n"
              "# path inside the repo (below) }. A program already tracked in its\n"
              "# own git repo is marked with a comment and defaults adopt = false —\n"
              "# set adopt = true to copy it in anyway.\n"
              f"# Then run:  config-sync adopt {path}\n"
              + _omitted_comment(omitted, tier) + "\n")
    # Entries are dumped one at a time (each a valid [[entries]] block that TOML
    # appends to the array) so a per-entry comment can precede a managed one.
    # `managed` drives the comment but is not itself written to the file.
    meta = {"version": ADOPT_PLAN_VERSION, "tier": tier, "repo": repo}
    if not rows:
        meta["entries"] = []  # keep the key present so an empty plan still loads
    with open(path, "wb") as f:
        f.write(header.encode())
        tomli_w.dump(meta, f)
        for row in rows:
            f.write(b"\n")
            if row.get("managed"):
                f.write(_MANAGED_COMMENT)
            tomli_w.dump({"entries": [{k: v for k, v in row.items()
                                       if k != "managed"}]}, f)
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


def git_init(repo):
    """`git init` the repo if it is not one already, so the user has a versionable
    dotfiles repo. That is the only git config-sync runs — staging, committing,
    pulling, and pushing are always left to the user. Returns whether it inited;
    a no-op (and False) when git is not installed, so adopt still succeeds."""
    if os.path.isdir(os.path.join(repo, GIT_DIR)) or shutil.which("git") is None:
        return False
    subprocess.run(["git", "-C", repo, "init", "-q"], capture_output=True, text=True)
    return True


def adopt_apply(plan, home, conf_home, cfg, force=False):
    """Copy the plan's adopt=true entries into the repo and merge the manifest.
    Re-runnable: entries already recorded or already present are skipped. The only
    git it runs is `git init` on a not-yet-versioned repo — staging, committing,
    and pushing are left to the user. Refuses to add to a repo that already holds
    adopted configs (e.g. one cloned from another machine) unless `force`, so a
    shared repo is not polluted."""
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
    initialized = False
    if copied:
        save_manifest(conf_home, manifest, home)
        ensure_repo_gitignore(repo_root(conf_home))  # keep backups/state out of git
        initialized = git_init(repo_root(conf_home))
    return {"copied": copied, "skipped": skipped, "initialized": initialized,
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
    """Keep the backups tree and machine-local link state out of the repo's git
    history (idempotent — only appends lines that are missing)."""
    path = os.path.join(repo, GITIGNORE_NAME)
    existing = ""
    if os.path.exists(path):
        with open(path) as f:
            existing = f.read()
    present = set(existing.split())
    missing = [ln for ln in GITIGNORE_LINES if ln not in present]
    if not missing:
        return
    ensure_parent(path)
    with open(path, "a") as f:
        if existing and not existing.endswith("\n"):
            f.write("\n")
        for ln in missing:
            f.write(ln + "\n")


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


def _link_one(entry, home, backups):
    """Back up a real original and symlink the repo copy over it, mutating the
    entry's link state. Returns ("linked", home_path) or ("skipped", (path,
    reason)). Atomic: a failed symlink restores any just-made backup."""
    home_path, repo_path = entry["home_path"], entry["repo_path"]
    status = link_status(entry)
    if status not in ("link", "link-missing"):
        return "skipped", (home_path, status)
    bpath = ""
    try:
        bpath = backup(home_path, backups, home) if status == "link" else ""
        safe_symlink(repo_path, home_path)
    except (FsError, OSError) as e:
        if bpath:
            with contextlib.suppress(FsError, OSError):
                restore(bpath, home_path)
        return "skipped", (home_path, f"error: {e}")
    entry["linked"] = True
    entry["backup_path"] = bpath
    return "linked", home_path


def link_apply(manifest, home, conf_home):
    """Back up + symlink every actionable entry; record link state in the
    (git-ignored) link-state file. Idempotent — already-linked entries are left
    untouched."""
    ensure_repo_gitignore(repo_root(conf_home))
    backups = backups_root(conf_home)
    linked, skipped = [], []
    changed = False
    for entry in manifest["entries"]:
        outcome, detail = _link_one(entry, home, backups)
        if outcome == "linked":
            linked.append(detail)
            changed = True
        else:
            skipped.append(detail)
    if changed:
        save_link_state(conf_home, manifest, home)
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
# host, reconciling the symlinks with what's installed: symlink the configs
# whose program is present (reusing link's backup+symlink), and undo the symlink
# for a program that is no longer installed but that config-sync had linked. The
# read-only survey doubles as "what does this repo hold, and can I use it here?".

# The action sync would take for one entry -> (label, note). Installed entries
# reuse the LINK_STATUS actions; a missing program is either unlinked (if we made
# the symlink) or left alone.
SYNC_STATUS = {
    **LINK_STATUS,
    "unlink": ("UNLINK", "program not installed — remove the symlink config-sync made"),
}
_SYNC_ACTIONABLE = ("link", "link-missing", "unlink")


def program_installed(entry, qq, cfg):
    """Whether the entry's program is available on this machine. An unattributed
    entry (program == "") has nothing to gate on, so it counts as installed."""
    program = entry["program"]
    return check_installed(program, qq, cfg) if program else True


def sync_action(entry, installed):
    """What sync would do to one entry. Installed -> its link_status; missing ->
    "unlink" when config-sync created the current symlink, else "not-installed"."""
    if installed:
        return link_status(entry)
    return "unlink" if unlink_status(entry) in ("restore", "unlink-only") \
        else "not-installed"


def sync_survey(manifest, qq, cfg):
    """Per entry: (entry, installed, action). Re-reads the filesystem, so a
    freshly cloned repo reports what would actually happen."""
    rows = []
    for e in manifest["entries"]:
        installed = program_installed(e, qq, cfg)
        rows.append((e, installed, sync_action(e, installed)))
    return rows


def sync_apply(manifest, home, conf_home, qq, cfg, force=False):
    """Reconcile symlinks with what's installed: link installed configs (backing
    up any original), and undo the symlink of a config whose program is gone but
    that config-sync had linked. `force` links everything regardless. Link state
    is persisted to the git-ignored link-state file, never the shared manifest."""
    ensure_repo_gitignore(repo_root(conf_home))
    backups = backups_root(conf_home)
    linked, unlinked, skipped = [], [], []
    changed = False
    for entry in manifest["entries"]:
        if force or program_installed(entry, qq, cfg):
            outcome, detail = _link_one(entry, home, backups)
            (linked if outcome == "linked" else skipped).append(detail)
            changed = changed or outcome == "linked"
        else:
            # Program not installed here: undo a symlink config-sync created,
            # otherwise leave the entry alone.
            outcome, detail = _unlink_one(entry, home)
            if outcome == "restored":
                unlinked.append(detail)
                changed = True
            else:
                skipped.append((entry["home_path"], "not-installed"))
    if changed:
        save_link_state(conf_home, manifest, home)
    return {"linked": linked, "unlinked": unlinked, "skipped": skipped}


def sync_report(rows, home):
    print("sync — deploy plan (repo → home)\n")
    if not rows:
        print("  nothing to sync — the repo holds no adopted configs.")
        return
    width = max(len(tilde(e["home_path"], home)) for e, _, _ in rows)
    todo = 0
    for entry, _installed, action in rows:
        label, note = SYNC_STATUS[action]
        if action in _SYNC_ACTIONABLE:
            todo += 1
        home_disp = tilde(entry["home_path"], home)
        prog = entry["program"] or "—"
        print(f"  {label:<6} {home_disp:<{width}}  [{prog}]"
              + (f"   ({note})" if note else ""))
    print()
    print(f"{todo} change(s) · run with --apply to reconcile the symlinks."
          if todo else "Nothing to do — already in sync.")


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


def _unlink_one(entry, home):
    """Remove the symlink config-sync created and restore its backup, mutating
    the entry's link state. Returns ("restored", home_path) or ("skipped",
    (path, reason))."""
    home_path = entry["home_path"]
    status = unlink_status(entry)
    if status not in ("restore", "unlink-only"):
        return "skipped", (home_path, status)
    try:
        remove_symlink(home_path)
        if status == "restore":
            restore(entry["backup_path"], home_path)
    except (FsError, OSError) as e:
        return "skipped", (home_path, f"error: {e}")
    entry["linked"] = False
    entry["backup_path"] = ""
    return "restored", home_path


def unlink_apply(manifest, home, conf_home):
    """Remove each symlink config-sync created and restore its backup, clearing
    link state. Idempotent — entries that are not linked are left untouched."""
    restored, skipped = [], []
    changed = False
    for entry in manifest["entries"]:
        outcome, detail = _unlink_one(entry, home)
        if outcome == "restored":
            restored.append(detail)
            changed = True
        else:
            skipped.append(detail)
    if changed:
        save_link_state(conf_home, manifest, home)
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


# --------------------------------------------------------------------------
# Repo health — live status of the managed repo and the symlinks config-sync
# manages, gathered for the `health` report. Unlike the inventory (a snapshot
# that may have been taken on another machine), this reads the repo and the
# filesystem here and now, so it answers "has the repo been built, is it
# committed/pushed, and are the adopted configs actually linked into place?".
# It returns a plain findings dict (no rendering), keeping report.py free of any
# sync/fsops import; the CLI hands the result to the health reporter.

def _repo_git_state(repo):
    """Read-only git snapshot of the managed repo: current branch, whether it
    has any commits yet, dirty counts, and ahead/behind vs its upstream. Every
    query goes through the read-only `capture` seam."""
    def g(*args):
        return capture(["git", "-C", repo, *args])

    branch = g("symbolic-ref", "--short", "HEAD") or "(detached)"  # works pre-commit
    no_commits = g("rev-parse", "--verify", "--quiet", "HEAD") is None
    counts = status_counts(repo) or {}
    ahead = behind = None
    if g("rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"):
        c = g("rev-list", "--left-right", "--count", "HEAD...@{u}")
        if c:
            ahead, behind = (int(n) for n in c.split())
    return {"branch": branch, "no_commits": no_commits,
            "modified": counts.get("modified", 0),
            "untracked": counts.get("untracked", 0),
            "ahead": ahead, "behind": behind}


def _link_findings(entry, home):
    """Health findings for one adopted entry's symlink, from its live link status.
    `linked` is the machine-local record; link_status re-reads the filesystem, so
    the two disagreeing is itself a finding (a link removed or replaced by hand)."""
    hp = tilde(entry["home_path"], home)
    prog = entry["program"] or os.path.basename(entry["home_path"])
    status = link_status(entry)
    if status == "done":
        return "done", []
    if status == "no-source":
        return status, [("ERROR", f"{hp}: repo copy missing ({prog})", None)]
    if status == "conflict":
        return status, [("WARN", f"{hp} is a symlink pointing outside the repo "
                         f"({prog})", None)]
    # A real file or an absent original — not linked. If link state claims it is,
    # the symlink was removed/replaced by hand; otherwise it is simply not linked.
    if entry.get("linked"):
        return status, [("WARN", f"{hp} is recorded as linked but is not the "
                         f"symlink config-sync made ({prog})", "config-sync link --apply")]
    return status, [("INFO", f"{hp} adopted but not linked ({prog})",
                     "config-sync link --apply")]


def repo_health(conf_home, home):
    """Live health of the managed repo and its symlinks. Returns
    {path, exists, findings} where findings is [(severity, text, suggestion|None)]
    in the same shape section_findings produces, so the reporter renders it
    uniformly. Never writes."""
    repo = repo_root(conf_home)
    disp = tilde(repo, home)
    if not os.path.isdir(repo):
        return {"path": disp, "exists": False, "findings": [
            ("INFO", f"no managed repo at {disp} yet — `config-sync plan` then "
             "`config-sync adopt` builds one", "config-sync plan")]}

    findings = []
    if os.path.isdir(os.path.join(repo, GIT_DIR)):
        git = _repo_git_state(repo)
        findings.append(("OK", f"managed repo at {disp} (git branch {git['branch']})",
                         None))
        if git["no_commits"]:
            findings.append(("WARN", "repo has no commits yet",
                             f"git -C {disp} add -A && git commit -m 'adopt configs'"))
        n = git["modified"] + git["untracked"]
        if n and not git["no_commits"]:
            findings.append(("WARN", f"{n} uncommitted change{'s' if n != 1 else ''} "
                             "in the repo", f"git -C {disp} status"))
        if git["ahead"]:
            findings.append(("INFO", f"{git['ahead']} commit(s) not pushed",
                             f"git -C {disp} push"))
        if git["behind"]:
            findings.append(("INFO", f"{git['behind']} commit(s) behind upstream",
                             f"git -C {disp} pull"))
    else:
        findings.append(("WARN", f"managed repo at {disp} is not a git repo "
                         "(adopt normally git-inits it)", "config-sync adopt"))

    entries = load_manifest(conf_home, home)["entries"]
    if not entries:
        findings.append(("INFO", "no configs adopted into the repo yet", None))
    else:
        linked = 0
        for e in entries:
            status, fs = _link_findings(e, home)
            findings += fs
            if status == "done":
                linked += 1
        findings.append(("OK" if linked == len(entries) else "INFO",
                         f"{linked}/{len(entries)} adopted config(s) linked into place",
                         None))
    return {"path": disp, "exists": True, "findings": findings}

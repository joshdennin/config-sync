#!/usr/bin/env python3
"""Read-only inventory of user config/dotfiles on an Arch/CachyOS system.

scan   — discover config entries under home, classify them, print a report
health — read a saved `scan --json` inventory and print a Markdown
         checkhealth-style report (inspired by Neovim's :checkhealth)
tidy   — report (and with --move, perform) safe XDG relocations of a
         conservative "Tier 1" set of HOME config files into ~/.config
adopt  — write an editable plan of discovered configs (curated/extended/
         everything tier); with --apply, copy the plan's entries into the
         managed repo at ~/.config/config-sync/, write the manifest, git commit
link   — report (and with --apply, perform) symlinking of adopted configs from
         the repo back into place, backing up each original first
unlink — reverse `link`: report (and with --apply, perform) removing the
         symlinks and restoring the backed-up originals

scan flags:
  --json               emit the complete structured inventory to stdout
  --generated          show machine-generated entries in the listing
  --all                also scan the state and cache roots (implies --generated)
  --secrets            show secret-flagged entries in the listing
  --only-orphans       restrict the listing to orphan entries
  --min-relevance N    hide entries scoring below N from the listing (default 0)
  --root PATH          add an extra scan root, categorized "unknown" (repeatable)
  --config PATH        TOML config with the classification tables
                       (default: inventory-config.toml next to the script)

health arguments:
  inventory            path to an inventory file written by `scan --json`

tidy flags:
  --move               move the safe candidates into ~/.config (default: report)

adopt flags:
  --select TIER        plan breadth: curated | extended | everything (default curated)
  --include NAME       restrict the plan to these programs/categories (repeatable)
  --exclude NAME       drop these programs/categories from the plan (repeatable)
  --plan PATH          plan file to write, then read with --apply
  --apply              build the repo from the (edited) plan
  --config PATH        TOML classification config

link flags:
  --apply              create the symlinks (default: report the plan only)

unlink flags:
  --apply              restore the originals (default: report the plan only)

The classification tables (the known-dotfiles registry, the shell/secret/noise
lists, and the machine-generated denylists) live in a TOML config, not in the
code. `scan` loads inventory-config.toml from next to this script by default,
or the file given to --config; it is required, so a missing or malformed config
is a hard error. The shipped file documents every section — [programs],
[shell], [secrets], [noise], [exclude], [generated] — and is edited by hand to
teach the tool new programs or stores. [exclude] names home-dir basenames (glob
patterns) that are dropped from the scan entirely, so they never reach the
inventory — for pure state/cache/runtime junk that is not config at all.

Example — save a structured inventory, then render a health report to Markdown:
  inventory.py scan --json > inventory.json
  inventory.py health inventory.json > health.md

`scan` and `health` never write, move, or delete anything; their only side
effects are filesystem reads and read-only `pacman` / `git` queries. `tidy` is
read-only unless given --move; `adopt` is read-only unless given --apply (it
only writes a plan file otherwise). Every write goes through the fsops
primitives, which refuse to overwrite or delete: `adopt` copies (originals are
left untouched) and `tidy --move` only relocates when the target is absent.
"""

import argparse
import json
import os
import platform
import shutil
import subprocess
import sys
import tomllib

import tomli_w
from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import datetime
from fnmatch import fnmatch

VERSION = "0.1.0"

# --------------------------------------------------------------------------
# Classification tables — loaded from the TOML config into a Config, which is
# threaded through the scan and health paths (no module-level mutable state).
# There are no built-in defaults; the shipped inventory-config.toml is the
# source of truth. (Structural constants that are code behavior, not curated
# data — HOME_EXCLUDE, CAT_ORDER, _TEXT_BYTES — stay below.)


@dataclass(frozen=True)
class Config:
    """Classification tables from inventory-config.toml, immutable once built.

    A default-constructed Config is empty and classifies nothing; that is the
    form `health` uses, since a saved inventory already carries every derived
    field it needs.
    """
    programs: dict = field(default_factory=dict)          # program -> {"paths": [...], "pkgs"?, "bin"?, "category"?}
    registry_by_path: dict = field(default_factory=dict)  # rc-file / config-dir basename -> program (derived)
    program_category: dict = field(default_factory=dict)  # program -> category (derived)
    shell_files: frozenset = field(default_factory=frozenset)    # home-dir rc files that get location "shell"
    secret_home: frozenset = field(default_factory=frozenset)    # sensitive home-dir basenames (never content-sniffed)
    secret_config: frozenset = field(default_factory=frozenset)  # sensitive ~/.config basenames
    noise_dirs: frozenset = field(default_factory=frozenset)     # state/cache dirs living under ~/.config
    generated_exts: frozenset = field(default_factory=frozenset)       # machine-generated file extensions (.-prefixed)
    generated_dir_names: frozenset = field(default_factory=frozenset)  # machine-generated directory basenames
    exclude_home: tuple = ()  # home-dir basename globs never recorded (state/junk)


HOME_EXCLUDE = {".config", ".cache", ".local"}  # scanned as their own roots

CAT_ORDER = ["config", "shell", "home", "data", "state", "cache", "unknown"]

# Bytes considered "text" when sniffing content (7-bit printable, common
# whitespace, and anything >= 0x80 so UTF-8 passes).
_TEXT_BYTES = bytes(range(0x20, 0x7F)) + b"\t\n\r\x0b\x0c" + bytes(range(0x80, 0x100))


def die(msg):
    print(f"inventory.py: error: {msg}", file=sys.stderr)
    sys.exit(1)


def require_home():
    """$HOME or a hard error — every command needs it."""
    home = os.environ.get("HOME")
    if not home:
        die("$HOME is not set")
    return home


def config_home(home):
    """The ~/.config root, honoring $XDG_CONFIG_HOME."""
    return os.environ.get("XDG_CONFIG_HOME") or os.path.join(home, ".config")


# --------------------------------------------------------------------------
# Configuration (required TOML — the source of truth for the tables above)

def default_config_path():
    """The inventory-config.toml shipped alongside this script."""
    return os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "inventory-config.toml")


def _table(cfg, name):
    sec = cfg.get(name, {})
    if not isinstance(sec, dict):
        die(f"config: [{name}] must be a table")
    return sec


def _str_list(table, key, ctx):
    vals = table.get(key, [])
    if not isinstance(vals, list) or not all(isinstance(v, str) for v in vals):
        die(f"config: {ctx} must be an array of strings")
    return vals


def load_config(path):
    """Build a Config from a TOML file. The config is required: a missing or
    malformed file is a hard error, since without it there are no tables to
    classify against.
    """
    try:
        with open(path, "rb") as f:
            cfg = tomllib.load(f)
    except FileNotFoundError:
        die(f"config file not found: {path}\n"
            "  inventory.py requires its TOML config (ships as "
            "inventory-config.toml next to the script); pass --config PATH "
            "to use another copy")
    except (OSError, tomllib.TOMLDecodeError) as e:
        die(f"cannot read config {path!r}: {e}")

    programs = _table(cfg, "programs")
    parsed = {}
    for name, info in programs.items():
        if not isinstance(info, dict) or not isinstance(info.get("paths"), list):
            die(f"config: program {name!r} needs a 'paths' array")
        if "category" in info and not isinstance(info["category"], str):
            die(f"config: program {name!r} 'category' must be a string")
        parsed[name] = info

    secrets = _table(cfg, "secrets")
    generated = _table(cfg, "generated")
    return Config(
        programs=parsed,
        registry_by_path={p: prog for prog, i in parsed.items() for p in i["paths"]},
        program_category={prog: i["category"] for prog, i in parsed.items()
                          if isinstance(i.get("category"), str)},
        shell_files=frozenset(_str_list(_table(cfg, "shell"), "files", "[shell].files")),
        secret_home=frozenset(_str_list(secrets, "home", "[secrets].home")),
        secret_config=frozenset(_str_list(secrets, "config", "[secrets].config")),
        noise_dirs=frozenset(_str_list(_table(cfg, "noise"), "dirs", "[noise].dirs")),
        exclude_home=tuple(_str_list(_table(cfg, "exclude"), "home", "[exclude].home")),
        generated_dir_names=frozenset(_str_list(generated, "dir_names",
                                                "[generated].dir_names")),
        # Normalize to the leading-dot lowercase form probe_file compares against.
        generated_exts=frozenset((e if e.startswith(".") else "." + e).lower()
                                 for e in _str_list(generated, "exts", "[generated].exts")),
    )


# --------------------------------------------------------------------------
# Shell-out seam (tests monkeypatch these)

def capture(cmd, cwd=None):
    """Run a read-only command; return stripped stdout, or None on any failure."""
    try:
        r = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return r.stdout.strip() if r.returncode == 0 else None


def status_counts(top, cap=500):
    """Dirty counts from `git status --porcelain`, reading at most `cap` lines."""
    try:
        p = subprocess.Popen(["git", "-C", top, "status", "--porcelain"],
                             stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
    except OSError:
        return None
    modified = untracked = 0
    saturated = False
    with p.stdout:
        for i, line in enumerate(p.stdout):
            if i >= cap:
                saturated = True
                p.terminate()
                break
            if line.startswith("??"):
                untracked += 1
            else:
                modified += 1
    p.wait()
    d = {"modified": modified, "untracked": untracked}
    if saturated:
        d["saturated"] = True
    return d


# --------------------------------------------------------------------------
# Content probes (bounded, shallow)

def is_generated_name(name, cfg):
    return (name.endswith("~")
            or os.path.splitext(name)[1].lower() in cfg.generated_exts
            or name in {"lock", "LOCK", "lockfile"})


def probe_file(path, name, cfg, skip_read=False):
    """Classify one file: text | json | binary | generated | unknown."""
    if is_generated_name(name, cfg):
        return "generated"
    if name.endswith(".json"):
        return "json"
    if skip_read:
        return "unknown"
    # Only open regular files — open() on a FIFO blocks until a writer shows up.
    if not os.path.isfile(path):
        return "generated"
    try:
        with open(path, "rb") as f:
            chunk = f.read(8192)
    except OSError:
        return "unknown"
    if not chunk:
        return "text"
    if b"\0" in chunk:
        return "binary"
    nontext = len(chunk.translate(None, delete=_TEXT_BYTES))
    return "text" if nontext / len(chunk) <= 0.30 else "binary"


def probe_dir(path, cfg, skip_read=False, entry_budget=4000, sample_cap=40):
    """Bounded walk: (size estimate, Counter of sampled file classes, capped)."""
    size = seen = 0
    stats = Counter()
    capped = False
    for dirpath, dirnames, filenames in os.walk(path, onerror=lambda e: None):
        in_generated = dirpath != path and os.path.basename(dirpath) in cfg.generated_dir_names
        for fname in filenames:
            fp = os.path.join(dirpath, fname)
            try:
                size += os.lstat(fp).st_size
            except OSError:
                continue
            seen += 1
            if not skip_read and sum(stats.values()) < sample_cap:
                cls = "generated" if in_generated else probe_file(fp, fname, cfg)
                stats[cls] += 1
            if seen >= entry_budget:
                capped = True
                break
        seen += len(dirnames)
        if capped or seen >= entry_budget:
            capped = True
            break
    return size, stats, capped


# --------------------------------------------------------------------------
# Package cross-reference

def load_pacman_qq():
    out = capture(["pacman", "-Qq"])
    return set(out.splitlines()) if out else set()


def attribute(name, qq, cfg):
    """Resolve an entry name to (program, registry_hit)."""
    prog = cfg.registry_by_path.get(name)
    if prog:
        return prog, True
    for cand in (name, name.lstrip(".").lower()):
        if cand in qq:
            return cand, False
    return None, False


def check_installed(program, qq, cfg):
    """True when the program is found via pacman or on PATH (best-effort)."""
    info = cfg.programs.get(program, {})
    if ({program, *info.get("pkgs", ())} & qq):
        return True
    return shutil.which(info.get("bin", program)) is not None


def pacman_owner(path):
    out = capture(["pacman", "-Qo", path])
    # "path is owned by <pkg> <ver>" — "no owner" is a normal outcome (None).
    return out.split()[-2] if out else None


# --------------------------------------------------------------------------
# Git awareness

_git_cache = {}


def find_git_anchor(path):
    """Nearest ancestor (or self) containing a .git entry; pure os.path, no forks."""
    d = path if os.path.isdir(path) else os.path.dirname(path)
    prev = None
    while d != prev:
        if os.path.exists(os.path.join(d, ".git")):
            return d
        prev, d = d, os.path.dirname(d)
    return None


def git_record(anchor):
    """Read-only git sub-record, memoized per repo toplevel."""
    top = capture(["git", "-C", anchor, "rev-parse", "--show-toplevel"])
    if not top:
        return None
    if top in _git_cache:
        return _git_cache[top]

    def g(*args):
        return capture(["git", "-C", top, *args])

    remotes = []
    for line in (g("remote", "-v") or "").splitlines():
        parts = line.split()
        if len(parts) >= 3 and parts[2] == "(fetch)":
            remotes.append({"name": parts[0], "url": parts[1]})
    remotes.sort(key=lambda r: r["name"] != "origin")

    branch = g("rev-parse", "--abbrev-ref", "HEAD")
    if branch == "HEAD":
        branch = "(detached)"
    upstream = g("rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}")

    ahead = behind = None
    if upstream:
        counts = g("rev-list", "--left-right", "--count", "HEAD...@{u}")
        if counts:
            ahead, behind = (int(n) for n in counts.split())

    sym = g("symbolic-ref", "refs/remotes/origin/HEAD")
    if sym:
        default = sym.rsplit("/", 1)[-1]
    else:
        default = next((b for b in ("main", "master")
                        if g("rev-parse", "--verify", "--quiet", b)), "main")

    vs_default = {"ahead": None, "behind": None, "is_default": branch == default}
    base = next((b for b in (default, f"origin/{default}")
                 if g("rev-parse", "--verify", "--quiet", b)), None)
    if base and branch != "(detached)":
        counts = g("rev-list", "--left-right", "--count", f"{base}...HEAD")
        if counts:
            b, a = (int(n) for n in counts.split())
            vs_default.update(ahead=a, behind=b)

    name = os.path.basename(top)
    if not name and remotes:
        name = os.path.basename(remotes[0]["url"]).removesuffix(".git")

    head = g("log", "-1", "--format=%h|%cI")
    last_commit = None
    if head and "|" in head:
        sha, date = head.split("|", 1)
        last_commit = {"sha": sha, "date": date}

    rec = {"root": top, "name": name, "remotes": remotes, "branch": branch,
           "upstream": upstream, "ahead": ahead, "behind": behind,
           "default_branch": default, "vs_default": vs_default,
           "dirty": status_counts(top), "last_commit": last_commit}
    _git_cache[top] = rec
    return rec


# --------------------------------------------------------------------------
# Entry model — the per-entry record shape, defined once so `analyze` and
# `dangling_entry` cannot drift. Records are stored and emitted as plain dicts
# (the `scan --json` interchange format read back by `health`), so the builders
# construct an Entry and return `asdict(...)`; every consumer still sees a dict.

@dataclass
class Entry:
    path: str                          # resolved real path
    rel: str                           # home-relative (or absolute if outside home)
    location: str                      # config|shell|home|data|state|cache|unknown
    kind: str | None = None            # file | dir (None for a dangling link)
    via_symlink: list | None = None    # link paths that resolve to this entry
    size: int | None = None
    mtime: str | None = None
    editable: bool | None = None       # human-editable config (False = generated/cache/state)
    is_git_repo: bool = False
    git: dict | None = None
    program: str | None = None
    category: str | None = None
    installed: bool | None = None
    flags: list = field(default_factory=list)  # secret | noise | dangling
    relevance: int = 0
    relevance_terms: list = field(default_factory=list)


# --------------------------------------------------------------------------
# Scan

def resolve_roots(args, home):
    env = os.environ
    roots = [
        (config_home(home), "config"),
        (home, "home"),
        (env.get("XDG_DATA_HOME") or os.path.join(home, ".local/share"), "data"),
    ]
    if args.all:
        roots += [
            (env.get("XDG_STATE_HOME") or os.path.join(home, ".local/state"), "state"),
            (env.get("XDG_CACHE_HOME") or os.path.join(home, ".cache"), "cache"),
        ]
    roots += [(os.path.abspath(r), "unknown") for r in args.root]
    return roots


def categorize(name, root_cat, cfg):
    # Precedence: shell > config > home > root default.
    if root_cat == "home":
        if name in cfg.shell_files:
            return "shell"
        if name in cfg.registry_by_path:
            return "config"
        return "home"
    return root_cat


def rel_home(path, home):
    return os.path.relpath(path, home) if path.startswith(home + os.sep) else path


def iso(ts):
    return datetime.fromtimestamp(ts).astimezone().isoformat(timespec="seconds")


def score(name, kind, program, installed, is_git_repo, registry_hit,
          text_only, json_heavy):
    terms = []
    if installed:
        terms.append({"label": f"installed package ({program})", "delta": 30})
    if is_git_repo:
        terms.append({"label": "git repo", "delta": 25})
    if registry_hit:
        terms.append({"label": "known-dotfiles registry", "delta": 25})
    if text_only:
        terms.append({"label": "text-only tree", "delta": 15})
    if registry_hit and kind == "file" and name.startswith("."):
        terms.append({"label": f"known rc file ({name})", "delta": 10})
    if json_heavy:
        terms.append({"label": "json-heavy", "delta": -10})
    if installed is False:
        terms.append({"label": f"orphan: {program} not found (pacman or PATH)",
                      "delta": -20})
    total = max(0, min(100, sum(t["delta"] for t in terms)))
    return total, terms


def analyze(lpath, real, root_cat, home, qq, cfg):
    name = os.path.basename(lpath)
    location = categorize(name, root_cat, cfg)
    secret = ((root_cat == "home" and name in cfg.secret_home)
              or (root_cat == "config" and name in cfg.secret_config))
    noise = root_cat == "config" and name in cfg.noise_dirs
    flags = [f for f, on in (("secret", secret), ("noise", noise)) if on]

    kind = "dir" if os.path.isdir(real) else "file"
    st = os.stat(real)

    program, registry_hit = attribute(name, qq, cfg)
    installed = check_installed(program, qq, cfg) if program else None
    if program is None and root_cat == "unknown":
        owner = pacman_owner(real)
        if owner:
            program, installed = owner, True

    anchor = find_git_anchor(real)
    git = git_record(anchor) if anchor else None

    if kind == "dir":
        size, stats, _ = probe_dir(real, cfg, skip_read=secret)
        files = sum(stats.values())
        text_only = (stats["text"] > 0 and stats["binary"] == 0
                     and stats["generated"] == 0 and size < 5 * 1024 * 1024)
        json_heavy = stats["json"] > 0 and stats["json"] > stats["text"]
        content_ok = files == 0 or files > stats["binary"] + stats["generated"]
    else:
        size = st.st_size
        cls = probe_file(real, name, cfg, skip_read=secret)
        text_only = cls == "text"
        json_heavy = cls == "json"
        content_ok = cls != "binary" and cls != "generated"

    if location in ("cache", "state") or noise:
        editable = False
    elif secret or git is not None or registry_hit:
        editable = True  # content sniffing is skipped for secrets
    else:
        editable = content_ok

    relevance, terms = score(name, kind, program, installed, git is not None,
                             registry_hit, text_only, json_heavy)
    return asdict(Entry(
        path=real,
        rel=rel_home(real, home),
        location=location,
        kind=kind,
        via_symlink=[lpath] if lpath != real else None,
        size=size,
        mtime=iso(st.st_mtime),
        editable=editable,
        is_git_repo=git is not None,
        git=git,
        program=program,
        category=cfg.program_category.get(program),
        installed=installed,
        flags=flags,
        relevance=relevance,
        relevance_terms=terms,
    ))


def dangling_entry(lpath, root_cat, home, cfg):
    return asdict(Entry(
        path=lpath,
        rel=rel_home(lpath, home),
        location=categorize(os.path.basename(lpath), root_cat, cfg),
        flags=["dangling"],
    ))


def build_inventory(args, home, cfg):
    qq = load_pacman_qq()
    roots = resolve_roots(args, home)
    entries = {}  # keyed on resolved real path -> dedup collapses links + target
    for root, root_cat in roots:
        try:
            names = sorted(os.listdir(root))
        except OSError:
            continue
        for name in names:
            if root_cat == "home" and (not name.startswith(".") or name in HOME_EXCLUDE
                                       or any(fnmatch(name, p) for p in cfg.exclude_home)):
                continue
            lpath = os.path.join(root, name)
            real = os.path.realpath(lpath)
            if not os.path.exists(real):
                if os.path.islink(lpath):
                    entries[lpath] = dangling_entry(lpath, root_cat, home, cfg)
                continue
            if real in entries:
                # Roots are scanned in priority order, so the existing entry
                # already carries the winning location; just note the link.
                rec = entries[real]
                if lpath != real:
                    rec["via_symlink"] = (rec["via_symlink"] or []) + [lpath]
                continue
            entries[real] = analyze(lpath, real, root_cat, home, qq, cfg)
    meta = {"tool": "inventory.py", "version": VERSION, "host": platform.node(),
            "scanned_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "roots": [r for r, _ in roots], "all": args.all}
    return {"meta": meta, "entries": list(entries.values())}


def is_adoptable(rec, conf_home):
    """True when an entry is safe and sensible to copy into the managed repo.

    A hard safety gate for `adopt`, not a display filter. Refuses:
      - secrets (editable is True for them since sniffing is skipped, so this
        must be checked explicitly — the single most important exclusion),
      - dangling links and any non-editable entry (generated / cache / state /
        noise),
      - the managed repo itself (never adopt ~/.config/config-sync recursively).
    """
    if "secret" in rec["flags"] or "dangling" in rec["flags"]:
        return False
    if rec["editable"] is not True:
        return False
    if rec["location"] in ("cache", "state"):
        return False
    root = repo_root(conf_home)
    return rec["path"] != root and not rec["path"].startswith(root + os.sep)


# --------------------------------------------------------------------------
# Human-readable listing

def display_path(rec):
    rel = rec["rel"]
    return rel if rel.startswith("/") else "~/" + rel


def shorten(path, rec):
    """Abbreviate the scanned home to ~ (derived from the record, so health
    output is right even for an inventory taken on another machine)."""
    rel = rec["rel"]
    if not rel.startswith("/") and rec["path"].endswith(os.sep + rel):
        home = rec["path"][:-len(rel) - 1]
        if path.startswith(home + os.sep):
            return "~" + path[len(home):]
    return path


def git_token(git):
    if not git:
        return ""
    dirty = git.get("dirty") or {}
    tok = f"{git['name']}: {git['branch']}"
    if dirty.get("modified") or dirty.get("untracked"):
        tok += " ✎"
    if git.get("ahead"):
        tok += f" ↑{git['ahead']}"
    if git.get("behind"):
        tok += f"{'' if git.get('ahead') else ' '}↓{git['behind']}"
    return f"({tok})"


def badges(rec):
    out = list(rec["flags"])
    if rec["is_git_repo"]:
        out.append("git-repo")
    if rec["editable"] is False:
        out.append("generated")
    return out


def is_visible(rec, args):
    if args.only_orphans and rec["installed"] is not False:
        return False
    if "dangling" in rec["flags"]:
        return True  # always a cleanup signal
    if "secret" in rec["flags"] and not args.secrets:
        return False
    if rec["editable"] is False and not (args.generated or args.all):
        return False
    return (rec["relevance"] or 0) >= args.min_relevance


def report_listing(inv, args, cfg=None):
    entries = inv["entries"]
    shown = [e for e in entries if is_visible(e, args)]
    lines = []
    for cat in CAT_ORDER:
        group = sorted((e for e in shown if e["location"] == cat),
                       key=lambda e: (-(e["relevance"] or 0), e["rel"]))
        if not group:
            continue
        lines.append(cat.upper())
        for e in group:
            prog = e["program"] or "?"
            if e["installed"] is True:
                prog += " [installed]"
            elif e["installed"] is False:
                prog += " [orphan]"
            parts = [f"{e['relevance'] if e['relevance'] is not None else '-':>4}",
                     f"{display_path(e):<44}", f"{e['kind'] or '?':<5}", prog]
            if badges(e):
                parts.append(" ".join(badges(e)))
            if e["git"]:
                parts.append(git_token(e["git"]))
            lines.append("  " + "  ".join(parts))
        lines.append("")

    cats = Counter(e["location"] for e in shown)
    orphans = sum(1 for e in entries if e["installed"] is False)
    secrets = sum(1 for e in entries if "secret" in e["flags"])
    repos = len({e["git"]["root"] for e in entries if e["git"]})
    parts = [f"Summary: {len(shown)} shown / {len(entries)} recorded",
             *(f"{c} {n}" for c, n in cats.items()),
             f"orphans {orphans}", f"secrets {secrets}", f"git repos {repos}"]
    lines.append(" · ".join(parts))
    hidden = len(entries) - len(shown)
    if hidden:
        lines.append(f"{hidden} hidden from listing (show with --generated / --secrets; "
                     f"--json is always complete)")
    return "\n".join(lines)


# --------------------------------------------------------------------------
# Health (pure functions of stored records)

def section_key(rec, cfg):
    prog = rec["program"]
    if not prog:
        return None
    return cfg.programs.get(prog, {}).get("bin", prog)  # command name reads best


def section_findings(recs):
    """Findings for one program's records: [(severity, text, suggestion|None)]."""
    f = []
    prog = recs[0]["program"]
    present = [r for r in recs if "dangling" not in r["flags"]]
    # Location checks apply to config proper, not a program's data/state dirs
    # that happen to attribute to it.
    conf = [r for r in present if r["location"] in ("config", "shell", "home", "unknown")]

    if prog:
        if any(r["installed"] for r in recs):
            f.append(("OK", f"program installed ({prog})", None))
        elif any(r["installed"] is False for r in recs):
            f.append(("WARN", "config present but program not found (pacman or PATH)",
                      f"likely stale; verify before removing {display_path(recs[0])}"))

    for r in conf:
        via = ""
        if r["via_symlink"]:
            links = ", ".join(shorten(l, r) for l in r["via_symlink"])
            via = f"  (via {links} → symlink)"
        f.append(("OK", f"config at {display_path(r)}{via}", None))
    if prog and len(conf) > 1:
        f.append(("WARN", f"config present at {len(conf)} known paths at once", None))
    for r in recs:
        if "dangling" in r["flags"]:
            f.append(("ERROR", f"dangling symlink: {display_path(r)} (target missing)",
                      f"broken link; verify and clean up {display_path(r)}"))

    for r in {r["git"]["root"]: r for r in recs if r["git"]}.values():
        git = r["git"]
        root = shorten(git["root"], r)
        f.append(("OK", f"git: {git['name']} @ {git['branch']}", None))
        dirty = git.get("dirty") or {}
        n = (dirty.get("modified") or 0) + (dirty.get("untracked") or 0)
        if n:
            plus = "+" if dirty.get("saturated") else ""
            f.append(("WARN", f"git: {n}{plus} uncommitted change{'s' if n != 1 else ''}"
                      " in the working tree", f"git -C {root} status"))
        ahead, behind = git.get("ahead"), git.get("behind")
        if ahead and behind:
            f.append(("WARN", f"git: diverged from {git['upstream']} "
                      f"({ahead} ahead / {behind} behind)", f"git -C {root} status"))
        elif ahead:
            f.append(("WARN", f"git: {ahead} commit{'s' if ahead != 1 else ''} ahead of "
                      f"{git['upstream']} (unpushed)", f"git -C {root} push"))
        elif behind:
            f.append(("WARN", f"git: {behind} commit{'s' if behind != 1 else ''} behind "
                      f"{git['upstream']} (needs pull)", f"git -C {root} pull"))
        if git["branch"] == "(detached)":
            f.append(("WARN", "git: detached HEAD", None))
        elif git.get("vs_default") and not git["vs_default"]["is_default"]:
            f.append(("INFO", f"git: on non-default branch ({git['branch']}, "
                      f"default {git['default_branch']})", None))
        if not git.get("upstream") or not git.get("remotes"):
            f.append(("INFO", "git: no remote/upstream configured", None))

    candidates = [r for r in conf if r["editable"]]
    if candidates and not any(r["is_git_repo"] for r in candidates):
        f.append(("INFO", "not under version control — candidate for a dotfiles repo",
                  None))
    if any("secret" in r["flags"] for r in recs):
        f.append(("WARN", "contains secrets — do not sync to a public repo", None))
    return f


# Severity → Markdown bullet icon, echoing Neovim's :checkhealth glyphs.
SEV_ICON = {"OK": "✅", "INFO": "ℹ️", "WARN": "⚠️", "ERROR": "❌"}

# Command-shaped suggestions get wrapped in inline code; prose is left plain.
_CMD_PREFIXES = ("git ", "pacman ", "rm ", "ln ", "cp ", "mv ")


def md_suggestion(text):
    return f"`{text}`" if text.startswith(_CMD_PREFIXES) else text


UNCATEGORIZED = "Uncategorized"  # health group for programs with no category


def render_health(inv, source, cfg=Config()):
    sections = {}  # section title -> records, in inventory order; unattributed last
    for rec in inv["entries"]:
        sections.setdefault(section_key(rec, cfg) or "unattributed", []).append(rec)
    if "unattributed" in sections:
        sections["unattributed"] = sections.pop("unattributed")

    # Group each program's section under its category (baked into the entries at
    # scan time, so this works from the stored inventory alone). Findings are
    # computed up front so the summary can lead the document.
    groups = {}  # category -> [(section title, findings)], first-appearance order
    totals = Counter()
    attention = []
    programs = 0
    for name, recs in sections.items():
        findings = section_findings(recs)
        cat = (name != "unattributed" and recs[0].get("category")) or UNCATEGORIZED
        groups.setdefault(cat, []).append((name, findings))
        for sev, _, _ in findings:
            totals[sev] += 1
        bad = [t for s, t, _ in findings if s in ("WARN", "ERROR")]
        if bad:
            attention.append((name, "; ".join(t.removeprefix("git: ") for t in bad)))
        if name != "unattributed":
            programs += 1
    # Uncategorized (and the unattributed section it holds) always sorts last.
    ordered_cats = ([c for c in groups if c != UNCATEGORIZED]
                    + ([UNCATEGORIZED] if UNCATEGORIZED in groups else []))

    meta = inv.get("meta", {})
    out = [
        "# config inventory — health check",
        "",
        f"`{meta.get('host', '?')}` · source `{source}` · "
        f"scanned `{meta.get('scanned_at', '?')}` · "
        f"checked `{datetime.now():%Y-%m-%d %H:%M}`",
        "",
        "## Summary",
        "",
        f"**{programs} program{'' if programs == 1 else 's'} checked** — "
        f"{SEV_ICON['OK']} {totals['OK']} OK · "
        f"{SEV_ICON['WARN']} {totals['WARN']} WARN · "
        f"{SEV_ICON['ERROR']} {totals['ERROR']} ERROR · "
        f"{SEV_ICON['INFO']} {totals['INFO']} INFO",
        "",
    ]
    if attention:
        out += ["### Needs attention", ""]
        out += [f"- **{n}** — {msg}" for n, msg in attention]
        out.append("")

    for cat in ordered_cats:
        out += [f"# {cat}", ""]
        for name, findings in groups[cat]:
            out += [f"## {name}", ""]
            for sev, text, suggestion in findings:
                out.append(f"- {SEV_ICON[sev]} {text}")
                if suggestion:
                    out.append(f"  - ↳ {md_suggestion(suggestion)}")
            out.append("")

    return "\n".join(out).rstrip() + "\n"


# --------------------------------------------------------------------------
# Reporters — read-only renderings of an inventory. All share the signature
# (inv, args, cfg) -> str (some ignore args/cfg) so a format is selected by
# name from REPORTERS; adding one is a single registry entry. The inventory
# stays a plain JSON-serializable dict — that is the contract these consume.

def report_json(inv, args=None, cfg=None):
    return json.dumps(inv, indent=2)


def report_health(inv, args, cfg=Config()):
    return render_health(inv, getattr(args, "inventory", "?"), cfg)


REPORTERS = {
    "listing": report_listing,   # scan default: the human-readable table
    "json": report_json,         # scan --json: the complete structured inventory
    "health": report_health,     # health: the Markdown checkhealth report
}


# --------------------------------------------------------------------------
# fsops — safe filesystem mutations. These are the only writers in the tool
# (besides the tidy move that now routes through safe_move). Every primitive
# refuses to overwrite or delete: it raises FsError rather than clobber
# existing state, and creates parent directories as needed. Dry-run and
# reporting live in the action layer (an action surveys a plan, then calls
# these to execute it) — the primitives themselves always act.

class FsError(Exception):
    """A safe-write primitive refused to proceed — it would have overwritten or
    deleted existing state, or the source/target was not as expected."""


def _ensure_parent(path):
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)


def safe_copy(src, dst):
    """Copy a file or directory tree src -> dst. Refuses if dst already exists."""
    if os.path.lexists(dst):
        raise FsError(f"refusing to overwrite existing path: {dst}")
    _ensure_parent(dst)
    if os.path.isdir(src) and not os.path.islink(src):
        shutil.copytree(src, dst, symlinks=True)
    else:
        shutil.copy2(src, dst, follow_symlinks=False)
    return dst


def safe_move(src, dst):
    """Move src -> dst. Refuses if dst already exists."""
    if os.path.lexists(dst):
        raise FsError(f"refusing to overwrite existing path: {dst}")
    _ensure_parent(dst)
    shutil.move(src, dst)
    return dst


def safe_symlink(target, link_path):
    """Create a symlink at link_path pointing to target. Refuses if link_path
    already exists."""
    if os.path.lexists(link_path):
        raise FsError(f"refusing to overwrite existing path: {link_path}")
    _ensure_parent(link_path)
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


# --------------------------------------------------------------------------
# Repo mapping — where a discovered config is mirrored inside the managed repo
# at ~/.config/config-sync/. Each program gets its own directory (named by its
# command, the same key `health` groups by); within it the program's own
# structure is preserved. A directory entry maps to the program directory
# itself (its tree copied in); a file entry maps to a file beneath it.

REPO_DIRNAME = "config-sync"  # the managed repo, under ~/.config


def repo_root(conf_home):
    return os.path.join(conf_home, REPO_DIRNAME)


def program_dirname(program, cfg):
    """The per-program repo directory name — the command name reads best."""
    return cfg.programs.get(program, {}).get("bin", program)


def repo_path_for(home_path, kind, program, cfg, conf_home):
    """The repo destination for a discovered config. Representation-agnostic
    (takes fields, not a record) so it survives the later Entry refactor."""
    dirname = program_dirname(program, cfg) if program else os.path.basename(home_path)
    prog_dir = os.path.join(repo_root(conf_home), dirname)
    if kind == "dir":
        return prog_dir
    return os.path.join(prog_dir, os.path.basename(home_path))


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
    _ensure_parent(path)
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
# into the repo. Reversible via `unlink` (step 8), which restores the backup.
# Backups live under the repo but are git-ignored so they never get committed.

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
    _ensure_parent(path)
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
        try:
            bpath = backup(home_path, backups, home) if status == "link" else ""
            safe_symlink(repo_path, home_path)
        except (FsError, OSError) as e:
            skipped.append((home_path, f"error: {e}"))
            continue
        entry["linked"] = True
        entry["backup_path"] = bpath
        linked.append(home_path)
    if linked:
        save_manifest(conf_home, manifest)
    return {"linked": linked, "skipped": skipped}


def _tilde(path, home):
    return "~" + path[len(home):] if path.startswith(home + os.sep) else path


def link_report(rows, home, applied=False):
    print(f"link — {'Linked' if applied else 'Link plan (home → repo)'}\n")
    if not rows:
        print("  nothing to link — no adopted configs.")
        return
    for entry, status in rows:
        label, note = LINK_STATUS[status]
        arrow = f"{_tilde(entry['home_path'], home)} → {_tilde(entry['repo_path'], home)}"
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
        print(f"  {label:<7} {_tilde(entry['home_path'], home)}"
              + (f"   ({note})" if note else ""))
    todo = sum(1 for _, s in rows if s in ("restore", "unlink-only"))
    if not applied:
        print()
        print(f"{todo} to restore · run with --apply to undo the links."
              if todo else "Nothing to unlink.")


# --------------------------------------------------------------------------
# Entry points

def cmd_scan(args):
    home = require_home()
    if shutil.which("pacman") is None:
        die("pacman not found on PATH — this tool needs Arch's pacman for "
            "package-ownership queries (on Arch: sudo pacman -S pacman)")
    cfg = load_config(args.config or default_config_path())
    inv = build_inventory(args, os.path.abspath(home), cfg)
    print(REPORTERS["json" if args.json else "listing"](inv, args, cfg))
    return 0


def cmd_health(args):
    try:
        with open(args.inventory) as f:
            inv = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        die(f"cannot read inventory {args.inventory!r}: {e}")
    if not isinstance(inv, dict) or not isinstance(inv.get("entries"), list):
        die(f"{args.inventory!r} is not a valid inventory "
            "(expected the {meta, entries} object written by `scan --json`)")
    sys.stdout.write(REPORTERS["health"](inv, args))
    return 0


def cmd_tidy(args):
    home = require_home()
    rows = tidy_survey(home, config_home(home))
    if args.move:
        tidy_move(rows)
    else:
        tidy_report(rows)
    return 0


def cmd_adopt(args):
    home = os.path.abspath(require_home())
    conf = config_home(home)
    cfg = load_config(args.config or default_config_path())
    if args.apply:
        result = adopt_apply(load_adopt_plan(args.plan), home, conf, cfg)
        if result["copied"]:
            print(f"Adopted {len(result['copied'])} config(s) into {result['repo']}:")
            for p in result["copied"]:
                print(f"  + {p}")
            print("Committed." if result["committed"]
                  else "  (git commit skipped — set git user.name/email, then commit by hand)")
        if result["skipped"]:
            print(f"Skipped {len(result['skipped'])} (already adopted or unavailable):")
            for p in result["skipped"]:
                print(f"  - {p}")
        if not result["copied"] and not result["skipped"]:
            print("Nothing to adopt — no entries marked adopt = true in the plan.")
        return 0
    # Plan phase: scan, filter to the tier, write the editable plan.
    if shutil.which("pacman") is None:
        die("pacman not found on PATH — this tool needs Arch's pacman for "
            "package-ownership queries (on Arch: sudo pacman -S pacman)")
    scan_ns = argparse.Namespace(all=False, root=[])
    inv = build_inventory(scan_ns, home, cfg)
    rows = [adopt_plan_row(rec) for rec in
            adopt_candidates(inv, args.select, args.include, args.exclude, conf)]
    write_adopt_plan(args.plan, rows, args.select)
    print(f"Wrote {len(rows)} candidate(s) to {args.plan} ({args.select} tier).")
    print(f"Edit the file, then run:  config-sync adopt --apply --plan {args.plan}")
    return 0


def cmd_link(args):
    home = os.path.abspath(require_home())
    conf = config_home(home)
    manifest = load_manifest(conf)
    if not manifest["entries"]:
        die("nothing to link — no adopted configs "
            "(run `config-sync adopt --apply` first)")
    if args.apply:
        result = link_apply(manifest, home, conf)
        for p in result["linked"]:
            print(f"  linked {_tilde(p, home)}")
        for p, status in result["skipped"]:
            print(f"  skipped {_tilde(p, home)} ({LINK_STATUS.get(status, ('', status))[1] or status})")
        if not result["linked"]:
            print("Nothing to link.")
    else:
        link_report(link_survey(manifest), home)
    return 0


def cmd_unlink(args):
    home = os.path.abspath(require_home())
    conf = config_home(home)
    manifest = load_manifest(conf)
    if not manifest["entries"]:
        die("nothing to unlink — no adopted configs")
    if args.apply:
        result = unlink_apply(manifest, home, conf)
        for p in result["restored"]:
            print(f"  restored {_tilde(p, home)}")
        for p, status in result["skipped"]:
            print(f"  skipped {_tilde(p, home)} "
                  f"({UNLINK_STATUS.get(status, ('', status))[1] or status})")
        if not result["restored"]:
            print("Nothing to unlink.")
    else:
        unlink_report(unlink_survey(manifest), home)
    return 0


def parse_args(argv):
    p = argparse.ArgumentParser(prog="inventory.py", description=__doc__.split("\n")[0])
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("scan", help="inspect the system and emit an inventory")
    s.add_argument("--json", action="store_true",
                   help="emit the complete structured inventory (stdout)")
    s.add_argument("--generated", action="store_true",
                   help="show machine-generated entries in the listing")
    s.add_argument("--all", action="store_true",
                   help="also scan the state and cache roots (implies --generated)")
    s.add_argument("--secrets", action="store_true",
                   help="show secret-flagged entries in the listing")
    s.add_argument("--only-orphans", action="store_true",
                   help="restrict the listing to orphan entries")
    s.add_argument("--min-relevance", type=int, default=0, metavar="N",
                   help="hide entries scoring below N from the listing")
    s.add_argument("--root", action="append", default=[], metavar="PATH",
                   help="add an extra scan root (repeatable)")
    s.add_argument("--config", metavar="PATH",
                   help="TOML config with the classification tables "
                        "(default: inventory-config.toml next to the script)")
    s.set_defaults(func=cmd_scan)

    h = sub.add_parser("health",
                       help="render a Markdown health report for a saved "
                            "`scan --json` inventory")
    h.add_argument("inventory", help="inventory file written by `scan --json`")
    h.set_defaults(func=cmd_health)

    t = sub.add_parser("tidy",
                       help="report (and optionally perform) safe XDG "
                            "relocations of Tier 1 config files")
    t.add_argument("--move", action="store_true",
                   help="move the safe candidates into ~/.config (default: report only)")
    t.set_defaults(func=cmd_tidy)

    a = sub.add_parser("adopt",
                       help="generate an editable adopt plan (and with --apply, "
                            "build the dotfiles repo from it)")
    a.add_argument("--select", choices=list(ADOPT_TIERS), default="curated",
                   help="breadth of the generated plan (default: curated)")
    a.add_argument("--include", action="append", default=[], metavar="NAME",
                   help="restrict the plan to these programs/categories (repeatable)")
    a.add_argument("--exclude", action="append", default=[], metavar="NAME",
                   help="drop these programs/categories from the plan (repeatable)")
    a.add_argument("--plan", default="config-sync-adopt.toml", metavar="PATH",
                   help="plan file to write, then read back with --apply "
                        "(default: ./config-sync-adopt.toml)")
    a.add_argument("--apply", action="store_true",
                   help="copy the plan's adopt=true entries into the repo, write "
                        "the manifest, and git commit")
    a.add_argument("--config", metavar="PATH",
                   help="TOML classification config (default: next to the script)")
    a.set_defaults(func=cmd_adopt)

    ln = sub.add_parser("link",
                        help="symlink adopted configs from the repo back into "
                             "place (backs up each original first)")
    ln.add_argument("--apply", action="store_true",
                    help="create the symlinks (default: report the plan only)")
    ln.set_defaults(func=cmd_link)

    ul = sub.add_parser("unlink",
                        help="reverse `link`: remove the symlinks and restore "
                             "the backed-up originals")
    ul.add_argument("--apply", action="store_true",
                    help="restore the originals (default: report the plan only)")
    ul.set_defaults(func=cmd_unlink)
    return p.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())

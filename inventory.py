#!/usr/bin/env python3
"""Read-only inventory of user config/dotfiles on an Arch/CachyOS system.

scan   — discover config entries under home, classify them, print a report
health — read a saved `scan --json` inventory and print a Markdown
         checkhealth-style report (inspired by Neovim's :checkhealth)

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

The script never writes, moves, or deletes anything; its only side effects are
filesystem reads and read-only `pacman` / `git` queries.
"""

import argparse
import json
import os
import platform
import shutil
import subprocess
import sys
import tomllib
from collections import Counter
from datetime import datetime
from fnmatch import fnmatch

VERSION = "0.1.0"

# --------------------------------------------------------------------------
# Classification tables — populated from the TOML config by load_config().
# There are no built-in defaults; the shipped inventory-config.toml is the
# source of truth. (Structural constants that are code behavior, not curated
# data — HOME_EXCLUDE, CAT_ORDER, _TEXT_BYTES — stay below.)

PROGRAMS = {}          # program -> {"paths": [...], "pkgs"?, "bin"?, "category"?}
REGISTRY_BY_PATH = {}  # rc-file / config-dir basename -> program (derived)
PROGRAM_CATEGORY = {}  # program -> category (from TOML), stored on each entry (derived)
SHELL_FILES = set()    # home-dir rc files that get location "shell"
SECRET_HOME = set()    # sensitive home-dir basenames (never content-sniffed)
SECRET_CONFIG = set()  # sensitive ~/.config basenames
NOISE_DIRS = set()     # state/cache dirs living under ~/.config
GENERATED_EXTS = set()       # machine-generated file extensions (.-prefixed)
GENERATED_DIR_NAMES = set()  # machine-generated directory basenames
EXCLUDE_HOME = []            # home-dir basename globs never recorded (state/junk)

HOME_EXCLUDE = {".config", ".cache", ".local"}  # scanned as their own roots

CAT_ORDER = ["config", "shell", "home", "data", "state", "cache", "unknown"]

# Bytes considered "text" when sniffing content (7-bit printable, common
# whitespace, and anything >= 0x80 so UTF-8 passes).
_TEXT_BYTES = bytes(range(0x20, 0x7F)) + b"\t\n\r\x0b\x0c" + bytes(range(0x80, 0x100))


def die(msg):
    print(f"inventory.py: error: {msg}", file=sys.stderr)
    sys.exit(1)


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
    """Populate the classification tables from a TOML config, replacing whatever
    they held. The config is required: a missing or malformed file is a hard
    error, since without it there are no tables to classify against.
    """
    global PROGRAMS, REGISTRY_BY_PATH, PROGRAM_CATEGORY, SHELL_FILES
    global SECRET_HOME, SECRET_CONFIG
    global NOISE_DIRS, GENERATED_EXTS, GENERATED_DIR_NAMES, EXCLUDE_HOME
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
    PROGRAMS = parsed
    REGISTRY_BY_PATH = {p: prog for prog, i in PROGRAMS.items() for p in i["paths"]}
    PROGRAM_CATEGORY = {prog: i["category"] for prog, i in PROGRAMS.items()
                        if isinstance(i.get("category"), str)}

    SHELL_FILES = set(_str_list(_table(cfg, "shell"), "files", "[shell].files"))
    secrets = _table(cfg, "secrets")
    SECRET_HOME = set(_str_list(secrets, "home", "[secrets].home"))
    SECRET_CONFIG = set(_str_list(secrets, "config", "[secrets].config"))
    NOISE_DIRS = set(_str_list(_table(cfg, "noise"), "dirs", "[noise].dirs"))
    EXCLUDE_HOME = _str_list(_table(cfg, "exclude"), "home", "[exclude].home")
    generated = _table(cfg, "generated")
    GENERATED_DIR_NAMES = set(_str_list(generated, "dir_names",
                                        "[generated].dir_names"))
    # Normalize to the leading-dot lowercase form probe_file compares against.
    GENERATED_EXTS = {(e if e.startswith(".") else "." + e).lower()
                      for e in _str_list(generated, "exts", "[generated].exts")}


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

def is_generated_name(name):
    return (name.endswith("~")
            or os.path.splitext(name)[1].lower() in GENERATED_EXTS
            or name in {"lock", "LOCK", "lockfile"})


def probe_file(path, name, skip_read=False):
    """Classify one file: text | json | binary | generated | unknown."""
    if is_generated_name(name):
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


def probe_dir(path, skip_read=False, entry_budget=4000, sample_cap=40):
    """Bounded walk: (size estimate, Counter of sampled file classes, capped)."""
    size = seen = 0
    stats = Counter()
    capped = False
    for dirpath, dirnames, filenames in os.walk(path, onerror=lambda e: None):
        in_generated = dirpath != path and os.path.basename(dirpath) in GENERATED_DIR_NAMES
        for fname in filenames:
            fp = os.path.join(dirpath, fname)
            try:
                size += os.lstat(fp).st_size
            except OSError:
                continue
            seen += 1
            if not skip_read and sum(stats.values()) < sample_cap:
                cls = "generated" if in_generated else probe_file(fp, fname)
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


def attribute(name, qq):
    """Resolve an entry name to (program, registry_hit)."""
    prog = REGISTRY_BY_PATH.get(name)
    if prog:
        return prog, True
    for cand in (name, name.lstrip(".").lower()):
        if cand in qq:
            return cand, False
    return None, False


def check_installed(program, qq):
    """True when the program is found via pacman or on PATH (best-effort)."""
    info = PROGRAMS.get(program, {})
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
# Scan

def resolve_roots(args, home):
    env = os.environ
    roots = [
        (env.get("XDG_CONFIG_HOME") or os.path.join(home, ".config"), "config"),
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


def categorize(name, root_cat):
    # Precedence: shell > config > home > root default.
    if root_cat == "home":
        if name in SHELL_FILES:
            return "shell"
        if name in REGISTRY_BY_PATH:
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


def analyze(lpath, real, root_cat, home, qq):
    name = os.path.basename(lpath)
    location = categorize(name, root_cat)
    secret = ((root_cat == "home" and name in SECRET_HOME)
              or (root_cat == "config" and name in SECRET_CONFIG))
    noise = root_cat == "config" and name in NOISE_DIRS
    flags = [f for f, on in (("secret", secret), ("noise", noise)) if on]

    kind = "dir" if os.path.isdir(real) else "file"
    st = os.stat(real)

    program, registry_hit = attribute(name, qq)
    installed = check_installed(program, qq) if program else None
    if program is None and root_cat == "unknown":
        owner = pacman_owner(real)
        if owner:
            program, installed = owner, True

    anchor = find_git_anchor(real)
    git = git_record(anchor) if anchor else None

    if kind == "dir":
        size, stats, _ = probe_dir(real, skip_read=secret)
        files = sum(stats.values())
        text_only = (stats["text"] > 0 and stats["binary"] == 0
                     and stats["generated"] == 0 and size < 5 * 1024 * 1024)
        json_heavy = stats["json"] > 0 and stats["json"] > stats["text"]
        content_ok = files == 0 or files > stats["binary"] + stats["generated"]
    else:
        size = st.st_size
        cls = probe_file(real, name, skip_read=secret)
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
    return {
        "path": real,
        "rel": rel_home(real, home),
        "location": location,
        "kind": kind,
        "via_symlink": [lpath] if lpath != real else None,
        "size": size,
        "mtime": iso(st.st_mtime),
        "editable": editable,
        "is_git_repo": git is not None,
        "git": git,
        "program": program,
        "category": PROGRAM_CATEGORY.get(program),
        "installed": installed,
        "flags": flags,
        "relevance": relevance,
        "relevance_terms": terms,
    }


def dangling_entry(lpath, root_cat, home):
    return {
        "path": lpath,
        "rel": rel_home(lpath, home),
        "location": categorize(os.path.basename(lpath), root_cat),
        "kind": None, "via_symlink": None, "size": None, "mtime": None,
        "editable": None, "is_git_repo": False, "git": None,
        "program": None, "category": None, "installed": None,
        "flags": ["dangling"], "relevance": 0, "relevance_terms": [],
    }


def build_inventory(args, home):
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
                                       or any(fnmatch(name, p) for p in EXCLUDE_HOME)):
                continue
            lpath = os.path.join(root, name)
            real = os.path.realpath(lpath)
            if not os.path.exists(real):
                if os.path.islink(lpath):
                    entries[lpath] = dangling_entry(lpath, root_cat, home)
                continue
            if real in entries:
                # Roots are scanned in priority order, so the existing entry
                # already carries the winning location; just note the link.
                rec = entries[real]
                if lpath != real:
                    rec["via_symlink"] = (rec["via_symlink"] or []) + [lpath]
                continue
            entries[real] = analyze(lpath, real, root_cat, home, qq)
    meta = {"tool": "inventory.py", "version": VERSION, "host": platform.node(),
            "scanned_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "roots": [r for r, _ in roots], "all": args.all}
    return {"meta": meta, "entries": list(entries.values())}


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


def render_listing(inv, args):
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
    print("\n".join(lines))


# --------------------------------------------------------------------------
# Health (pure functions of stored records)

def section_key(rec):
    prog = rec["program"]
    if not prog:
        return None
    return PROGRAMS.get(prog, {}).get("bin", prog)  # command name reads best


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


def render_health(inv, source):
    sections = {}  # section title -> records, in inventory order; unattributed last
    for rec in inv["entries"]:
        sections.setdefault(section_key(rec) or "unattributed", []).append(rec)
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
# Entry points

def cmd_scan(args):
    home = os.environ.get("HOME")
    if not home:
        die("$HOME is not set")
    if shutil.which("pacman") is None:
        die("pacman not found on PATH — this tool needs Arch's pacman for "
            "package-ownership queries (on Arch: sudo pacman -S pacman)")
    load_config(args.config or default_config_path())
    inv = build_inventory(args, os.path.abspath(home))
    if args.json:
        json.dump(inv, sys.stdout, indent=2)
        print()
    else:
        render_listing(inv, args)
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
    sys.stdout.write(render_health(inv, args.inventory))
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
    return p.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())

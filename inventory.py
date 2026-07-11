#!/usr/bin/env python3
"""Read-only inventory of user config/dotfiles on an Arch/CachyOS system.

scan   — discover config entries under home, classify them, print a report
health — read a saved `scan --json` inventory and print a checkhealth report

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
from collections import Counter
from datetime import datetime

VERSION = "0.1.0"

# --------------------------------------------------------------------------
# Static tables

# Curated known-dotfiles registry: program -> home-relative rc files and
# config dir basenames; "pkgs" lists pacman package names when they differ
# from the program name, "bin" the executable name when it differs.
PROGRAMS = {
    "bash": {"paths": [".bashrc", ".bash_profile", ".bash_logout", ".profile"]},
    "zsh": {"paths": [".zshrc", ".zprofile", ".zshenv", ".zlogin", ".zlogout", "zsh"]},
    "fish": {"paths": ["fish"]},
    "readline": {"paths": [".inputrc"], "bin": "bash"},
    "git": {"paths": [".gitconfig", ".gitignore_global", "git"]},
    "tmux": {"paths": [".tmux.conf", "tmux"]},
    "vim": {"paths": [".vimrc", ".vim"], "pkgs": ["gvim"]},
    "neovim": {"paths": ["nvim"], "bin": "nvim"},
    "emacs": {"paths": [".emacs", ".emacs.d", "emacs"]},
    "kitty": {"paths": ["kitty"]},
    "alacritty": {"paths": ["alacritty"]},
    "ghostty": {"paths": ["ghostty"]},
    "wezterm": {"paths": ["wezterm", ".wezterm.lua"]},
    "foot": {"paths": ["foot"]},
    "starship": {"paths": ["starship.toml"]},
    "hyprland": {"paths": ["hypr"], "bin": "Hyprland"},
    "sway": {"paths": ["sway"]},
    "i3": {"paths": ["i3"], "pkgs": ["i3-wm"]},
    "waybar": {"paths": ["waybar"]},
    "polybar": {"paths": ["polybar"]},
    "rofi": {"paths": ["rofi"]},
    "wofi": {"paths": ["wofi"]},
    "dunst": {"paths": ["dunst"]},
    "mako": {"paths": ["mako"]},
    "picom": {"paths": ["picom"]},
    "xorg": {"paths": [".xinitrc", ".xprofile", ".Xresources", ".Xdefaults"],
             "pkgs": ["xorg-server"], "bin": "Xorg"},
    "gtk": {"paths": ["gtk-3.0", "gtk-4.0", ".gtkrc-2.0"],
            "pkgs": ["gtk3", "gtk4"], "bin": "gtk-launch"},
    "qt": {"paths": ["qt5ct", "qt6ct"], "pkgs": ["qt5ct", "qt6ct"], "bin": "qt6ct"},
    "fontconfig": {"paths": ["fontconfig"], "bin": "fc-list"},
    "mpv": {"paths": ["mpv"]},
    "htop": {"paths": ["htop"]},
    "btop": {"paths": ["btop"]},
    "fastfetch": {"paths": ["fastfetch"]},
    "ranger": {"paths": ["ranger"]},
    "yazi": {"paths": ["yazi"]},
    "lf": {"paths": ["lf"]},
    "lazygit": {"paths": ["lazygit"]},
    "zathura": {"paths": ["zathura"]},
    "openssh": {"paths": [".ssh"], "bin": "ssh"},
    "gnupg": {"paths": [".gnupg"], "bin": "gpg"},
    "pass": {"paths": [".password-store"]},
    "aws-cli": {"paths": [".aws"], "pkgs": ["aws-cli-v2"], "bin": "aws"},
    "github-cli": {"paths": ["gh"], "pkgs": ["github-cli"], "bin": "gh"},
    "docker": {"paths": [".docker"]},
    "kubectl": {"paths": [".kube"]},
    "npm": {"paths": [".npmrc"]},
    "cargo": {"paths": [".cargo"], "pkgs": ["rust", "rustup"]},
    "wget": {"paths": [".wgetrc"]},
    "curl": {"paths": [".curlrc"]},
}

# Reverse map: rc-file / config-dir basename -> program.
REGISTRY_BY_PATH = {p: prog for prog, info in PROGRAMS.items() for p in info["paths"]}

SHELL_FILES = {".bashrc", ".bash_profile", ".bash_logout", ".profile",
               ".zshrc", ".zprofile", ".zshenv", ".zlogin", ".zlogout", ".inputrc"}

# Sensitive credential stores: never content-sniffed, hidden unless --secrets.
SECRET_HOME = {".ssh", ".gnupg", ".password-store", ".netrc", ".aws",
               ".git-credentials", ".docker", ".kube", ".pki"}
SECRET_CONFIG = {"gh"}

# Known state/cache dirs that live under .config despite not being config.
NOISE_DIRS = {"Code", "Code - OSS", "VSCodium", "chromium", "google-chrome",
              "BraveSoftware", "vivaldi", "opera", "discord", "Slack", "spotify",
              "Electron", "teams", "Signal", "session", "pulse", "dconf",
              "ibus", "Trolltech.conf"}

GENERATED_EXTS = {".db", ".sqlite", ".sqlite3", ".log", ".lock", ".pid", ".bak",
                  ".old", ".tmp", ".sock", ".socket", ".ldb", ".dat", ".pyc"}
GENERATED_DIR_NAMES = {"logs", "log", "Cache", "cache", "CachedData", "Code Cache",
                       "GPUCache", "DawnCache", "ShaderCache", "GrShaderCache",
                       "Crashpad", "blob_storage", "Service Worker",
                       "Session Storage", "Local Storage", "IndexedDB", "databases"}

HOME_EXCLUDE = {".config", ".cache", ".local"}  # scanned as their own roots

CAT_ORDER = ["config", "shell", "home", "data", "state", "cache", "unknown"]

# Bytes considered "text" when sniffing content (7-bit printable, common
# whitespace, and anything >= 0x80 so UTF-8 passes).
_TEXT_BYTES = bytes(range(0x20, 0x7F)) + b"\t\n\r\x0b\x0c" + bytes(range(0x80, 0x100))


def die(msg):
    print(f"inventory.py: error: {msg}", file=sys.stderr)
    sys.exit(1)


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
    category = categorize(name, root_cat)
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

    if category in ("cache", "state") or noise:
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
        "category": category,
        "kind": kind,
        "via_symlink": [lpath] if lpath != real else None,
        "size": size,
        "mtime": iso(st.st_mtime),
        "editable": editable,
        "is_git_repo": git is not None,
        "git": git,
        "program": program,
        "installed": installed,
        "flags": flags,
        "relevance": relevance,
        "relevance_terms": terms,
    }


def dangling_entry(lpath, root_cat, home):
    return {
        "path": lpath,
        "rel": rel_home(lpath, home),
        "category": categorize(os.path.basename(lpath), root_cat),
        "kind": None, "via_symlink": None, "size": None, "mtime": None,
        "editable": None, "is_git_repo": False, "git": None,
        "program": None, "installed": None,
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
            if root_cat == "home" and (not name.startswith(".") or name in HOME_EXCLUDE):
                continue
            lpath = os.path.join(root, name)
            real = os.path.realpath(lpath)
            if not os.path.exists(real):
                if os.path.islink(lpath):
                    entries[lpath] = dangling_entry(lpath, root_cat, home)
                continue
            if real in entries:
                # Roots are scanned in priority order, so the existing entry
                # already carries the winning category; just note the link.
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
        group = sorted((e for e in shown if e["category"] == cat),
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

    cats = Counter(e["category"] for e in shown)
    orphans = sum(1 for e in entries if e["installed"] is False)
    secrets = sum(1 for e in entries if "secret" in e["flags"])
    repos = len({e["git"]["root"] for e in entries if e["git"]})
    lines.append(f"Summary: {len(shown)} shown / {len(entries)} recorded · "
                 + " · ".join(f"{c} {n}" for c, n in cats.items())
                 + f" · orphans {orphans} · secrets {secrets} · git repos {repos}")
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

    if prog:
        if any(r["installed"] for r in recs):
            f.append(("OK", f"program installed ({prog})", None))
        elif any(r["installed"] is False for r in recs):
            f.append(("WARN", "config present but program not found (pacman or PATH)",
                      f"likely stale; verify before removing {display_path(recs[0])}"))

    for r in present:
        via = ""
        if r["via_symlink"]:
            links = ", ".join(shorten(l, r) for l in r["via_symlink"])
            via = f"  (via {links} → symlink)"
        f.append(("OK", f"config at {display_path(r)}{via}", None))
    if len(present) > 1:
        f.append(("WARN", f"config present at {len(present)} known paths at once", None))
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

    if present and not any(r["is_git_repo"] for r in present):
        f.append(("INFO", "not under version control — candidate for a dotfiles repo",
                  None))
    if any("secret" in r["flags"] for r in recs):
        f.append(("WARN", "contains secrets — do not sync to a public repo", None))
    return f


def render_health(inv, source):
    sections = {}  # name -> records, in inventory order; unattributed last
    for rec in inv["entries"]:
        sections.setdefault(section_key(rec) or "unattributed", []).append(rec)
    if "unattributed" in sections:
        sections["unattributed"] = sections.pop("unattributed")

    meta = inv.get("meta", {})
    out = [f"config inventory — health check       "
           f"{datetime.now():%Y-%m-%d %H:%M}   host: {meta.get('host', '?')}",
           f"source: {source}  (scanned {meta.get('scanned_at', '?')})", ""]
    totals = Counter()
    attention = []
    for name, recs in sections.items():
        findings = section_findings(recs)
        out.append(name)
        for sev, text, suggestion in findings:
            totals[sev] += 1
            out.append(f"  {sev:<7}{text}")
            if suggestion:
                out.append(f"         → {suggestion}")
        out.append("")
        bad = [t for s, t, _ in findings if s in ("WARN", "ERROR")]
        if bad:
            attention.append((name, "; ".join(t.removeprefix("git: ") for t in bad)))

    programs = sum(1 for n in sections if n != "unattributed")
    out.append(f"Summary: {programs} programs · {totals['OK']} OK · {totals['WARN']} WARN"
               f" · {totals['ERROR']} ERROR · {totals['INFO']} INFO")
    if attention:
        out.append("Needs attention:")
        width = max(len(n) for n, _ in attention)
        out += [f"  • {n:<{width}}  — {msg}" for n, msg in attention]
    return "\n".join(out)


# --------------------------------------------------------------------------
# Entry points

def cmd_scan(args):
    home = os.environ.get("HOME")
    if not home:
        die("$HOME is not set")
    if shutil.which("pacman") is None:
        die("pacman not found on PATH — this tool needs Arch's pacman for "
            "package-ownership queries (on Arch: sudo pacman -S pacman)")
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
    print(render_health(inv, args.inventory))
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
    s.set_defaults(func=cmd_scan)

    h = sub.add_parser("health", help="report on a saved `scan --json` inventory")
    h.add_argument("inventory", help="inventory file written by `scan --json`")
    h.set_defaults(func=cmd_health)
    return p.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())

"""The read-only inventory engine: discover config/dotfiles under $HOME on an
Arch/CachyOS system, classify them against the TOML config, and build the
structured inventory that the reporters render and the actions consume.

This module never writes, moves, or deletes anything — its only side effects
are filesystem reads and read-only `pacman` / `git` queries. It imports stdlib
only (never `fsops`), so the inspection core stays provably read-only. The repo
**mapping** lives here too (pure path logic that `is_adoptable` needs); the
manifest and the mutating actions live in `sync`, the reporters in `report`,
and the CLI in `cli`.

The classification tables (the known-dotfiles registry, the shell/secret/noise
lists, and the machine-generated denylists) live in a TOML config, not in the
code — see `load_config` and the shipped inventory-config.toml.
"""

import os
import platform
import shutil
import subprocess
import tomllib
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
    noise_home: frozenset = field(default_factory=frozenset)     # state/cache dirs in $HOME
    noise_config: frozenset = field(default_factory=frozenset)   # state/cache dirs under ~/.config
    generated_exts: frozenset = field(default_factory=frozenset)       # machine-generated file extensions (.-prefixed)
    generated_dir_names: frozenset = field(default_factory=frozenset)  # machine-generated directory basenames
    exclude_home: tuple = ()    # home-dir basename globs never recorded (state/junk)
    exclude_config: tuple = ()  # ~/.config basename globs never recorded (state/junk)


HOME_EXCLUDE = {".config", ".cache", ".local"}  # scanned as their own roots

CAT_ORDER = ["config", "shell", "home", "data", "state", "cache", "unknown"]

UNCATEGORIZED = "Uncategorized"  # program category for programs with none set


def ordered_categories(recs):
    """Program categories in health's grouping order: first appearance across
    `recs`, with the Uncategorized bucket always last. Shared so the health
    report and the adopt plan order their categories identically."""
    seen = []
    for rec in recs:
        cat = rec.get("category") or UNCATEGORIZED
        if cat not in seen:
            seen.append(cat)
    return ([c for c in seen if c != UNCATEGORIZED]
            + ([UNCATEGORIZED] if UNCATEGORIZED in seen else []))

# Bytes considered "text" when sniffing content (7-bit printable, common
# whitespace, and anything >= 0x80 so UTF-8 passes).
_TEXT_BYTES = bytes(range(0x20, 0x7F)) + b"\t\n\r\x0b\x0c" + bytes(range(0x80, 0x100))


class ConfigSyncError(Exception):
    """A recoverable, user-facing error raised by the library layer (bad config,
    unreadable manifest/plan, missing $HOME, …). The CLI entry point catches it
    and turns it into a stderr message plus a non-zero exit; no library code
    calls sys.exit itself, so these modules stay importable and testable."""


def die(msg):
    """Raise a user-facing error. Kept as a terse helper so call sites read as
    `die(...)`; the message is surfaced by the CLI, not printed here."""
    raise ConfigSyncError(msg)


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
    noise = _table(cfg, "noise")
    generated = _table(cfg, "generated")
    return Config(
        programs=parsed,
        registry_by_path={p: prog for prog, i in parsed.items() for p in i["paths"]},
        program_category={prog: i["category"] for prog, i in parsed.items()
                          if isinstance(i.get("category"), str)},
        shell_files=frozenset(_str_list(_table(cfg, "shell"), "files", "[shell].files")),
        secret_home=frozenset(_str_list(secrets, "home", "[secrets].home")),
        secret_config=frozenset(_str_list(secrets, "config", "[secrets].config")),
        noise_home=frozenset(_str_list(noise, "home", "[noise].home")),
        noise_config=frozenset(_str_list(noise, "config", "[noise].config")),
        exclude_home=tuple(_str_list(_table(cfg, "exclude"), "home", "[exclude].home")),
        exclude_config=tuple(_str_list(_table(cfg, "exclude"), "config", "[exclude].config")),
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


def attribute(name, qq, cfg, key=None):
    """Resolve an entry to (program, registry_hit). `key` is the registry lookup
    key when it differs from the basename — a registered sub-path like
    "Code - OSS/User/settings.json", whose basename ("settings.json") would not
    match. The pacman-name fallback always keys off the basename."""
    prog = cfg.registry_by_path.get(key or name)
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


def categorize(name, root_cat, cfg, key=None):
    # Precedence: shell > config > home > root default. `key` is the registry
    # lookup key for a registered sub-path (see attribute).
    if root_cat == "home":
        if name in cfg.shell_files:
            return "shell"
        if (key or name) in cfg.registry_by_path:
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
    if json_heavy and not registry_hit:
        # The json-heavy penalty deprioritizes incidental app-state json; a
        # registered path is a hand-picked config, so it is not penalized (this
        # is what keeps registered settings.json files above the curated floor).
        terms.append({"label": "json-heavy", "delta": -10})
    if installed is False:
        terms.append({"label": f"orphan: {program} not found (pacman or PATH)",
                      "delta": -20})
    total = max(0, min(100, sum(t["delta"] for t in terms)))
    return total, terms


def analyze(lpath, real, root_cat, home, qq, cfg, registry_key=None):
    """Build one entry. `registry_key` names a registered sub-path (a `paths`
    entry containing a separator) when this is one — it drives attribution and
    location, since the basename alone cannot match the sub-path registration."""
    name = os.path.basename(lpath)
    location = categorize(name, root_cat, cfg, key=registry_key)
    secret = ((root_cat == "home" and name in cfg.secret_home)
              or (root_cat == "config" and name in cfg.secret_config))
    noise = ((root_cat == "home" and name in cfg.noise_home)
             or (root_cat == "config" and name in cfg.noise_config))
    flags = [f for f, on in (("secret", secret), ("noise", noise)) if on]

    kind = "dir" if os.path.isdir(real) else "file"
    st = os.stat(real)

    program, registry_hit = attribute(name, qq, cfg, key=registry_key)
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
            if root_cat == "config" and any(fnmatch(name, p) for p in cfg.exclude_config):
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
    # Registered sub-paths — `paths` entries containing a separator, e.g.
    # "Code - OSS/User/settings.json" — are not reached by the top-level
    # enumeration above. Resolve each explicitly against the scanned roots (in
    # priority order) so a single tracked file inside an otherwise-untracked
    # folder becomes its own entry; the parent stays hidden via [noise]. Bounded:
    # one existence check per registered sub-path, no directory walk.
    for relpath in (p for p in cfg.registry_by_path if os.sep in p):
        for root, root_cat in roots:
            lpath = os.path.join(root, relpath)
            if not os.path.lexists(lpath):
                continue
            real = os.path.realpath(lpath)
            if os.path.exists(real) and real not in entries:
                entries[real] = analyze(lpath, real, root_cat, home, qq, cfg,
                                        registry_key=relpath)
            break  # first root where the sub-path exists wins
    meta = {"tool": "config-sync", "version": VERSION, "host": platform.node(),
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
      - anything outside the config-proper locations — data/state/cache dirs
        (e.g. ~/.local/share/<program>) are program data, not hand-edited
        config, even when a registry hit forces editable=True on them,
      - the managed repo itself (never adopt ~/.config/config-sync recursively).
    """
    if "secret" in rec["flags"] or "dangling" in rec["flags"]:
        return False
    if rec["editable"] is not True:
        return False
    if rec["location"] not in ("config", "shell", "home", "unknown"):
        return False
    root = repo_root(conf_home)
    return rec["path"] != root and not rec["path"].startswith(root + os.sep)


# --------------------------------------------------------------------------
# Shared display helpers (used by the reporters, the actions, and adopt's plan).
# `tilde` is the one home-abbreviation primitive; `display_path` is the variant
# for when only a stored record is in hand (no live $HOME — e.g. a `health`
# report rendered from an inventory taken on another machine), working off the
# precomputed `rel` instead.

def tilde(path, home):
    """Abbreviate an absolute path under `home` to its ~-prefixed form."""
    return "~" + path[len(home):] if path.startswith(home + os.sep) else path


def display_path(rec):
    rel = rec["rel"]
    return rel if rel.startswith("/") else "~/" + rel


# --------------------------------------------------------------------------
# Repo mapping — where a discovered config is mirrored inside the managed repo
# at ~/.config/config-sync/. Each program gets its own directory (named by its
# command, the same key `health` groups by); within it the program's own
# structure is preserved. A directory entry maps to the program directory
# itself (its tree copied in); a file entry maps to a file beneath it.

REPO_DIRNAME = "config-sync"  # the managed repo, under ~/.config


def repo_root(conf_home):
    return os.path.join(conf_home, REPO_DIRNAME)


def repo_config_path(conf_home):
    """The classification config captured inside the managed repo. `adopt` writes
    it there so a clone carries the registry it was built with; repo-centric
    commands (`sync`) prefer it over the package copy."""
    return os.path.join(repo_root(conf_home), "inventory-config.toml")


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
    return os.path.join(prog_dir, _repo_leaf(home_path, program, cfg))


def _repo_leaf(home_path, program, cfg):
    """The file's location *within* its program dir. A plain file lands at its
    basename, and a registered sub-path is flattened to its basename too so it
    sits directly under the program dir like every other file. Structure is kept
    only when flattening would collide: a leading dir shared by all of the
    program's sub-paths is dropped (.claude/settings.json -> settings.json), and
    a full sub-path is preserved only when two sub-paths share a basename in
    different dirs (gtk-3.0/settings.ini and gtk-4.0/settings.ini)."""
    subpaths = [p for p in cfg.programs.get(program, {}).get("paths", ())
                if os.sep in p] if program else []
    match = max((p for p in subpaths if home_path.endswith(os.sep + p)),
                key=len, default=None)
    if match is None:
        return os.path.basename(home_path)
    if len({p.split(os.sep, 1)[0] for p in subpaths}) == 1:
        return match.split(os.sep, 1)[1]  # shared leading dir duplicates prog dir
    basenames = [os.path.basename(p) for p in subpaths]
    if len(basenames) == len(set(basenames)):
        return os.path.basename(match)  # unique basenames -> flat, no collision
    return match  # keep the full sub-path to disambiguate a basename collision



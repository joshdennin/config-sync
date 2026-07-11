# Dotfile Inventory Script — Plan / Spec

## Purpose

A **read-only** Python script that discovers and catalogs the config/dotfiles on
an Arch / CachyOS install: what exists, where, which installed program it belongs
to, and whether it is already under version control. It is the discovery
counterpart to `config-sync` — you inventory the box first, then decide what is
worth putting in a `config-sync` config.

The script **never writes, moves, or deletes anything.** Its only side effects
are reading the filesystem and shelling out to `pacman` for ownership queries.
Everything it produces is a report.

**Core stance: human-editable config only.** The default human-readable output is
the set of files a person would actually hand-edit and want under version
control. Anything that looks machine-generated — caches, state, logs, lock files,
databases, binary blobs, compiled artifacts — is **hidden from that listing by
default**, not merely deprioritized.

**Filtering happens at render time, not collection time.** The structured
(`--json`) inventory always contains *every* discovered entry with its flags;
`--generated`, `--secrets`, `--only-orphans`, and `--min-relevance` only shape
the human-readable listing and the `--config-sync` skeleton. This keeps
inventories complete and comparable across runs regardless of which flags were
active, and it is what lets the `health` subcommand work as a pure function of
the stored records (its secret and dangling checks depend on those entries being
recorded).

**Symlinks resolve to their source.** When an entry is a symlink, the script
follows it to the real file and inventories *that* — the user-editable original —
rather than the link. See "Symlink resolution."

The goal is the same minimalism as `config-sync`: do only the work needed to
classify each entry correctly, surface what it finds, and let the user decide.

## Invocation

Two subcommands: **scan** (inspect the system and emit an inventory) and
**health** (read an inventory emitted earlier and report on it). Health never
scans. Subcommands (argparse subparsers) keep the two flag sets separate — scan
flags do not parse in health mode at all.

```
# scan — build an inventory
python3 inventory.py scan [--json | --config-sync] [--generated] [--all]
                          [--secrets] [--only-orphans] [--min-relevance N]
                          [--root PATH ...]

# health — read an inventory built above and print a checkhealth-style report
python3 inventory.py health <inventory.json>
```

- **`scan`** takes no positional arguments — it scans the current user's home by
  default. Output goes to stdout; nothing is written to disk unless the user
  redirects it.
- **`health`** takes exactly one positional argument: the path to an inventory
  file created by a prior `scan --json` run (redirected to a file). Health does
  **no** scanning and takes none of the scan flags — it reports exactly what the
  stored inventory contains. See "Health check."
- Exit non-zero only on a hard failure (scan: `pacman` missing, `$HOME` unset;
  health: the inventory file is missing, unreadable, or not a valid inventory).

## Runtime & dependencies

- **`Target runtime: Python 3`** favor stdlib where it is a reasonable option.
- **`pacman`** on `PATH` for package-ownership queries. A startup preflight
  checks for it and, if absent, prints `sudo pacman -S pacman`-style guidance and
  aborts — same shape as `config-sync`'s `git` preflight. (In practice pacman is
  always present on Arch; the check keeps the failure legible if run elsewhere.)
- **Native filesystem inspection** via `os` / `pathlib` (`os.scandir`,
  `os.path.islink`, `os.readlink`, `os.stat`, `os.walk`). Only `pacman` and `git`
  shell out (via `subprocess`), mirroring the "git-only shell-out" split in
  `config-sync.py`.
- **Arch-only assumption.** Ownership and package names are pacman-specific; no
  other package manager is probed. (`shutil.which` is used as a secondary
  installed-check for programs that arrived outside pacman — see Package
  cross-reference.)

## Scan scope

Roots are scanned in this priority order. Each root carries a default category
(see Classification); XDG environment variables override the defaults.

| Root | Env override | Default category | Scanned by default? |
|------|--------------|------------------|---------------------|
| `~/.config`        | `$XDG_CONFIG_HOME` | `config` | yes |
| Home-dir dotfiles  | —                  | `home`   | yes |
| `~/.local/share`   | `$XDG_DATA_HOME`   | `data`   | yes (low relevance) |
| `~/.local/state`   | `$XDG_STATE_HOME`  | `state`  | no (needs `--all`) |
| `~/.cache`         | `$XDG_CACHE_HOME`  | `cache`  | no (needs `--all`) |

- **Home-dir dotfiles** = entries matching `~/.*` (files and dirs) *excluding*
  `.` / `..` and the XDG dirs handled as their own roots (`.config`, `.cache`,
  `.local`). This is where the shell rc files and classic dotfiles live
  (`.bashrc`, `.zshrc`, `.gitconfig`, `.vimrc`, `.tmux.conf`, `.xinitrc`,
  `.Xresources`, …).
- **Top-level enumeration, shallow probing.** Each root is enumerated one level
  deep — one inventory entry per top-level child (`~/.config/nvim`,
  `~/.config/kitty`, …). The script does **not** recurse fully; it descends only
  as far as needed for the shallow probes below (git-repo / symlink detection,
  a bounded size estimate). `~/.cache` in particular is never deep-walked.
- `--root PATH` adds an extra root to scan (repeatable); useful for a
  non-standard config location under home. Custom roots default to category
  `unknown`. (System paths like `/etc` are deliberately out of scope for now —
  see Out of scope.)

## Classification

Each entry is assigned exactly one **category** and, orthogonally, a set of
**flags**.

**Category** (drives default visibility and relevance):

- `config` — under `.config`, or a known rc file → the primary target.
- `home` — a home-dir dotfile not otherwise classified.
- `shell` — shell startup files (`.bashrc`, `.zshrc`, `.zprofile`, `.profile`,
  `.bash_profile`, `.inputrc`, …). A `home` refinement worth its own bucket.
- `data` / `state` / `cache` — XDG data/state/cache; machine-managed, rarely
  hand-edited, deprioritized or hidden.
- `unknown` — custom roots and anything unclassifiable.

**Category precedence.** Several rules can match one entry (`.zshrc` is both a
shell startup file and a known rc file). The most specific wins:
`shell` > `config` > `home` > root default. The **category comes from where the
entry was found** (the link's location / the scanned root); all *analysis* runs
against the resolved symlink target (see Symlink resolution). When deduplication
collapses several links onto one target, the surviving entry takes the category
of the link whose root is highest in the Scan-scope priority order.

**Flags** (any combination). Flags record only what is *not* derivable from the
scalar record fields — `secret`, `noise`, `dangling`. Derived states
(`orphan` ⇐ `installed == false`, `generated` ⇐ `editable == false`,
`git-repo` ⇐ `is_git_repo`) are rendered as badges by the display layer but are
**not** stored redundantly, so there is a single source of truth per fact.

- `secret` — sensitive credential stores: `.ssh`, `.gnupg`, `.password-store`,
  `.netrc`, `.aws`, `.config/gh`, `.docker/config.json`, `.git-credentials`, …
  Recorded in the inventory (path and flags only — **content sniffing is skipped
  for secret entries**), hidden from the human listing unless `--secrets`, and
  **never** emitted into a `config-sync` skeleton.
- `dangling` — reached via a symlink whose target does not exist (no editable
  source). **Shown in the default listing** — a broken config link is exactly the
  kind of cleanup signal the tool exists to surface — and reported as `ERROR` by
  health.
- `noise` — matches a denylist of known state/cache dirs that live *under*
  `.config` despite not being hand-editable config (e.g. `.config/Code/`,
  `.config/chromium/`, `.config/discord/`, `.config/BraveSoftware/`).
  Deprioritized.

Derived badges (display-only, from record fields):

- `orphan` — a `config`-category entry whose owning program appears in neither
  pacman nor `PATH` (cleanup candidate); `installed == false`.
- `generated` — failed the human-editable filter; `editable == false`. Hidden
  from the human listing by default, shown with `--generated`.
- `git-repo` — `is_git_repo == true`.

## Human-editable filter (the primary job)

`~/.config` and the home dir are full of machine-generated content; the value of
the tool is separating hand-editable, version-control-worthy config from state and
cache masquerading as config. This is a **filter first**, not just an ordering:
an entry classified as machine-generated is dropped from the default
human-readable listing (shown with `--generated`). It is always present in the
`--json` inventory with `editable: false` — the filter shapes the display, never
the data.

**What is treated as machine-generated (hidden from the listing by default):**

- Category `cache` / `state`, and anything `noise`-flagged (known state/cache dirs
  living under `.config`, e.g. `.config/Code/`, `.config/chromium/`).
- **Binary or non-text content** — a bounded, shallow content sniff (read the
  first few KB; NUL bytes or a high non-printable ratio ⇒ binary). Covers images,
  fonts, compiled blobs.
- **Databases and serialized state** — sqlite files, `*.db`, `*.sqlite`.
- **Logs, locks, sockets, pid files, backups** — `*.log`, `*.lock`, `*.pid`,
  `*.bak`, `*.old`, `*.tmp`, `*~`, `*.socket`, and dir names like `logs/`,
  `Cache/`, `CachedData/`, `Crashpad/`, `blob_storage/`, `Service Worker/`.
- A directory whose contents, after the sniff, are *entirely* of the above kinds
  (looks like a pure state dir even if it lives under `.config`).

**What is kept as human-editable:** text files with config-like names/extensions
(`*.conf`, `*.toml`, `*.ini`, `*.cfg`, `*.yaml`/`*.yml`, `*.lua`, `*.rc`, rc
dotfiles, shell startup files, etc.), the curated known-dotfiles registry entries,
and any `git-repo` (the user is clearly managing it by hand). `*.json` is kept but
weighted down — many programs write state as JSON — leaving ownership and the
noise denylist to break the tie.

Within the kept set, each entry still gets a coarse **relevance score** for
*ordering* and the `--min-relevance` filter — a transparent sum of labeled
contributions (always itemized in `--json`), **clamped to [0, 100]** after
summing:

- **+** owned by an installed package · already a `git-repo` · matches the
  known-dotfiles registry · small, text-only tree.
- **−** `*.json`-heavy · orphan (owning program not found → probably stale).

## Package cross-reference

Attributing a config to a package is the interesting problem: pacman owns files it
*installed*, but user configs under `~` are almost never pacman-owned, so
`pacman -Qo ~/.config/nvim` usually says "no package owns it." The script uses a
layered strategy, cheapest first:

1. **Name match against installed packages.** Load the installed set once
   (`pacman -Qq`). Match the entry's basename against it directly and via a small
   alias map (`nvim`→`neovim`, `kitty`→`kitty`, `Code`→`code`/`vscode`,
   `zsh`/`.zshrc`→`zsh`, …). This resolves the common case without per-path forks.
2. **Curated known-dotfiles registry.** A built-in table mapping program →
   home-relative rc files and config dir names — a superset of `config-sync`'s
   `KNOWN_PATHS`, covering shells, editors, terminals, WMs/DEs, and the common
   CLI tools. Provides the canonical program name and the expected paths, so a
   `.tmux.conf` in the home dir is attributed to `tmux` even though it isn't under
   `.config`.
3. **`shutil.which` secondary check.** `pacman -Qq` covers repo and AUR packages,
   but not flatpak, pip/pipx, cargo, npm, appimages, or curl-installed tools —
   all common even on Arch, and all of which leave config dirs behind. Before
   concluding a resolved program is absent, check whether its binary is on
   `PATH`; if so, `installed` is `true` (installed, just not via pacman). Only
   when a program is found in **neither** pacman **nor** `PATH` is the entry an
   orphan, and the wording everywhere is "not found," not "not installed" — the
   check is best-effort, not proof of absence.
4. **`pacman -Qo` fallback** for genuine on-disk files under custom roots,
   guarded so the "no owner" case is a normal outcome, not an error.

An entry whose resolved program is found in neither the installed set nor `PATH`
gets `installed: false` (rendered as the `orphan` badge).

## Symlink resolution

Programs search a hierarchy of paths for their config, and a symlink high in that
search path often points *down* to the file the user actually edits (a dotfiles
repo, a stow tree, a hand-linked source). The inventory must catalog the **real
editable source**, not the link that shadows it.

- When an entry is a symlink, **follow the whole chain** (`os.path.realpath`) to
  the final target and inventory *that path* as the canonical entry. All the
  per-entry analysis — human-editable sniff, package attribution, git detection,
  relevance — runs against the resolved source; the **category** still comes from
  the link's location (see Category precedence).
- Record how it was reached: the resolving link path(s) are kept in
  `via_symlink` so the relationship is visible, but the entry's identity is the
  real file. This favors the user-editable original over the shadowing link.
- **Deduplicate.** If both a link and its target (or two links to one target)
  would be listed, they collapse to a single entry keyed on the resolved real
  path. The resolved source wins even when it sits outside the scanned roots
  (e.g. `~/.config/nvim` → `~/dotfiles/nvim`): the source is inventoried and the
  link is noted, not the reverse. When the collapsing links came from different
  roots, the category tiebreak in Classification applies.
- **Dangling symlink** — target does not exist. There is no editable source to
  analyze, so the entry is recorded with the `dangling` flag, analysis fields
  left `null`, and it is shown in the default listing as a cleanup signal (and as
  an `ERROR` in health).

## Git awareness

An entry is `git-repo` if it lives **inside a git work tree** — its own root, or
any ancestor. Detection is **native first**: walk the resolved path's ancestors
looking for a `.git` entry (pure `os.path`, no subprocess), so the common
not-in-a-repo case costs zero forks across the ~100+ top-level entries. Only when
a `.git` ancestor is found does the script shell out —
`git -C <dir> rev-parse --show-toplevel` to confirm and canonicalize the
toplevel (this also correctly handles `.git` *files* in worktrees/submodules).

For every such entry the script captures a `git` sub-record so a discovered
dotfiles repo maps cleanly onto `config-sync` and the user can see how far the
working copy has drifted from its remote.

All queries are **read-only plumbing** and run **once per repo root**, memoized by
toplevel — several inventoried entries in one dotfiles monorepo share a single set
of git calls. Any field that a given query can't answer is left `null`; a missing
remote, no upstream, or a detached HEAD are normal outcomes, not errors.
`git status --porcelain` output is read up to a bounded number of lines; past the
cap the dirty counts are reported as saturated (e.g. `100+`) rather than walking
a huge repo's full status.

Captured per repo (`git` sub-record):

| Field | Source (read-only) |
|-------|--------------------|
| `root` | `git -C <dir> rev-parse --show-toplevel` |
| `name` | basename of `root` (fallback: derived from the `origin` URL) |
| `remotes` | `git remote -v` → `[{name, url}]`; `origin` listed first when present |
| `branch` | `git rev-parse --abbrev-ref HEAD` (`HEAD` ⇒ detached, noted) |
| `upstream` | `git rev-parse --abbrev-ref --symbolic-full-name @{u}` (e.g. `origin/main`), or `null` |
| `ahead` / `behind` | `git rev-list --left-right --count HEAD...@{u}` — commits ahead of / behind the tracked upstream |
| `default_branch` | resolved from `git symbolic-ref refs/remotes/origin/HEAD` (fallback `main`, then `master`) |
| `vs_default` | current branch's divergence from the default branch: `git rev-list --left-right --count <default>...HEAD` → commits ahead / behind, and whether it *is* the default branch |
| `dirty` | `git status --porcelain` non-empty ⇒ uncommitted changes; counts of modified / untracked (bounded, see above) |
| `last_commit` | short SHA + ISO date of `HEAD` (`git log -1 --format=%h\|%cI`) |

This mirrors the state a person reads before deciding whether a config is safe to
sync: *which repo, which remote, which branch, is it a feature branch off `main`,
is it ahead/behind, and is the working tree clean.* `git` and `pacman` remain the
only shell-outs, both read-only.

## Output

- **Default (human-readable):** entries grouped by category, ordered by relevance
  within each group, one line per entry: path, kind, relevance, owning
  program (+ `[installed]` / `[orphan]`), and flag/derived badges. Git-tracked
  entries append a compact status token — repo name, branch, dirty marker, and
  drift — e.g. `(dotfiles: main ✎ ↑2↓1)` meaning branch `main`, uncommitted
  changes, 2 ahead / 1 behind upstream. A trailing summary counts entries per
  category and highlights orphan / `secret` / git-repo sets. This listing is
  where the display filters (`--generated`, `--secrets`, `--only-orphans`,
  `--min-relevance`) apply.
- **`--json`:** the canonical structured output — a single object
  `{ "meta": {...}, "entries": [...] }`, where `meta` carries run-level context
  (scan time, resolved roots, tool version) and `entries` is the full per-entry
  record list (see the record shape and worked example below), including the
  itemized relevance contributions. **Always complete:** the display filters do
  not remove records from `--json` (only unscanned roots are absent — `--all`
  still controls whether `state`/`cache` are walked at all). Written to stdout so
  it pipes into `jq` / the `--config-sync` generator, and diffing two saved runs
  with `jq` covers the cross-run comparison story.
- **`--config-sync`:** emit a ready-to-edit `config-sync` config skeleton
  (defaults to the Python `CONFIG = [...]` form; the format could be selected
  later) prefilled from the strongest candidates — git-repo entries (with
  discovered `repo`/`dest`/`mode`) and high-relevance package-matched configs.
  `secret`-flagged entries are **never** included. Emitted commented-out so the
  user opts each one in deliberately rather than syncing by accident.

A SQLite export was considered and deferred — see Out of scope.

## Per-entry data (record shape)

```
path            absolute path of the resolved editable source
rel             path relative to $HOME (or absolute if outside home)
category        config | home | shell | data | state | cache | unknown
kind            file | dir   (resolved; the link itself is not a kind)
via_symlink     [link paths that resolved to this entry], or null
size            bounded byte estimate (dirs: shallow du, capped)
mtime           last-modified (ISO 8601)
editable        bool          (passed the human-editable filter)
is_git_repo     bool          (inside a git work tree — own root or an ancestor)
git             sub-record below, or null when not in a repo
program         resolved owning program, or null
installed       bool | null   (found via pacman or PATH; null = couldn't attribute)
flags           [secret, noise, dangling]  (non-derivable markers only —
                orphan/generated/git-repo are derived from installed /
                editable / is_git_repo at display time)
relevance       int, clamped to 0–100
relevance_terms [{label, delta}, ...]   (itemized in --json)
```

`git` sub-record (present when `is_git_repo`; see Git awareness):

```
root            repo toplevel (absolute)
name            repo name (basename of root, or derived from origin URL)
remotes         [{name, url}, ...], origin first; [] if none
branch          current branch, or "(detached)"
upstream        tracked upstream ref (e.g. origin/main), or null
ahead / behind  commits ahead of / behind upstream, or null when no upstream
default_branch  resolved default (origin/HEAD → main → master)
vs_default      {ahead, behind, is_default}  — divergence from default_branch
dirty           {modified, untracked}  — {0, 0} means a clean working tree
last_commit     {sha, date}            — HEAD short SHA + ISO commit date
```

## Example `--json` output

A run finding four entries — an nvim config symlinked out to a dotfiles repo, a
plain ghostty config, a tmux config kept as a home-dir rc file, and an orphaned
polybar config whose program isn't found:

```json
{
  "meta": {
    "tool": "inventory.py",
    "version": "0.1.0",
    "host": "cachyos",
    "scanned_at": "2026-07-11T14:32:07-05:00",
    "roots": ["/home/jd/.config", "/home/jd", "/home/jd/.local/share"],
    "all": false
  },
  "entries": [
    {
      "path": "/home/jd/dotfiles/nvim",
      "rel": "dotfiles/nvim",
      "category": "config",
      "kind": "dir",
      "via_symlink": ["/home/jd/.config/nvim"],
      "size": 48211,
      "mtime": "2026-07-09T21:14:03-05:00",
      "editable": true,
      "is_git_repo": true,
      "git": {
        "root": "/home/jd/dotfiles",
        "name": "dotfiles",
        "remotes": [
          {"name": "origin", "url": "git@github.com:joshdennin/dotfiles.git"}
        ],
        "branch": "main",
        "upstream": "origin/main",
        "ahead": 2,
        "behind": 0,
        "default_branch": "main",
        "vs_default": {"ahead": 0, "behind": 0, "is_default": true},
        "dirty": {"modified": 1, "untracked": 0},
        "last_commit": {"sha": "a1b2c3d", "date": "2026-07-09T21:14:03-05:00"}
      },
      "program": "neovim",
      "installed": true,
      "flags": [],
      "relevance": 95,
      "relevance_terms": [
        {"label": "installed package (neovim)", "delta": 30},
        {"label": "git repo", "delta": 25},
        {"label": "known-dotfiles registry", "delta": 25},
        {"label": "text-only tree", "delta": 15}
      ]
    },
    {
      "path": "/home/jd/.config/ghostty",
      "rel": ".config/ghostty",
      "category": "config",
      "kind": "dir",
      "via_symlink": null,
      "size": 2480,
      "mtime": "2026-07-02T09:15:41-05:00",
      "editable": true,
      "is_git_repo": false,
      "git": null,
      "program": "ghostty",
      "installed": true,
      "flags": [],
      "relevance": 70,
      "relevance_terms": [
        {"label": "installed package (ghostty)", "delta": 30},
        {"label": "known-dotfiles registry", "delta": 25},
        {"label": "text-only tree", "delta": 15}
      ]
    },
    {
      "path": "/home/jd/.tmux.conf",
      "rel": ".tmux.conf",
      "category": "config",
      "kind": "file",
      "via_symlink": null,
      "size": 1840,
      "mtime": "2026-05-21T18:03:12-05:00",
      "editable": true,
      "is_git_repo": false,
      "git": null,
      "program": "tmux",
      "installed": true,
      "flags": [],
      "relevance": 80,
      "relevance_terms": [
        {"label": "installed package (tmux)", "delta": 30},
        {"label": "known-dotfiles registry", "delta": 25},
        {"label": "text-only tree", "delta": 15},
        {"label": "known rc file (.tmux.conf)", "delta": 10}
      ]
    },
    {
      "path": "/home/jd/.config/polybar",
      "rel": ".config/polybar",
      "category": "config",
      "kind": "dir",
      "via_symlink": null,
      "size": 6044,
      "mtime": "2025-11-18T13:40:22-05:00",
      "editable": true,
      "is_git_repo": false,
      "git": null,
      "program": "polybar",
      "installed": false,
      "flags": [],
      "relevance": 20,
      "relevance_terms": [
        {"label": "known-dotfiles registry", "delta": 25},
        {"label": "text-only tree", "delta": 15},
        {"label": "orphan: polybar not found (pacman or PATH)", "delta": -20}
      ]
    }
  ]
}
```

The relevance sums land inside [0, 100] here; the clamp only matters when the
contributions push past either bound. Note the `nvim` and `polybar` entries carry
no stored flags — `git-repo` and `orphan` are derived from `is_git_repo` and
`installed` when displayed.

## Health check (`health`)

`health <inventory.json>` is a **reader, not a scanner.** It loads an inventory
previously written by `scan --json` (redirected to a file) and renders a
human-friendly, per-program diagnostic modeled on Neovim's `:checkhealth`. It
collects no data of its own — every check is a pure function of the stored record
fields — so it is fast, offline, and reproducible. Because the scan always
records the complete inventory (secrets and dangling links included), every
check below can fire regardless of which display flags the scan ran with. It
reflects the **snapshot** taken when the inventory was built; re-scan to refresh
a stale git status.

Separating collection from reporting means the slower scan runs once, while the
report can be re-run, diffed, or shared without touching the system again. This
is why health takes an existing inventory as input rather than scanning on the
fly.

Each program becomes a section; entries with no attributed program
(`program: null`) are grouped in a trailing **unattributed** section rather than
dropped. Each finding is a status line with a severity marker and, where useful,
a suggested command. Findings derive from stored fields:

| Check | Condition (from the record) | Status |
|-------|------------------------------|--------|
| Program present | owning program `installed` | `OK` |
| | config present but program not found in pacman or on `PATH` (orphan) | `WARN` |
| Location | single resolved config found | `OK` |
| | config present at several known paths at once | `WARN` |
| | reached via a `dangling` symlink | `ERROR` |
| Git — clean | working tree clean, level with upstream | `OK` |
| Git — dirty | `dirty` has uncommitted changes | `WARN` |
| Git — ahead | `ahead` > 0 (unpushed commits) | `WARN` |
| Git — behind | `behind` > 0 (needs pull) | `WARN` |
| Git — diverged | both `ahead` and `behind` > 0 | `WARN` |
| Git — branch | on a non-default branch (`vs_default.is_default` false) | `INFO` |
| Git — detached | `branch` is detached | `WARN` |
| Git — remote | no `upstream` / empty `remotes` | `INFO` |
| Not tracked | `is_git_repo` false | `INFO` (candidate for version control) |
| Safety | `secret`-flagged config present | `WARN` (do not sync to a public repo) |

Because the "not found" check is best-effort (a program installed some unusual
way may simply be invisible to it), orphan findings suggest *verifying* before
removing — never a bare removal command.

Severities roll up into a summary: counts of `OK` / `WARN` / `ERROR` / `INFO`,
then the list of items needing attention (everything `WARN` or `ERROR`) so nothing
is missed.

### Example `health` output

Reading the four-entry inventory from the JSON example above:

```text
config inventory — health check       2026-07-11 14:32   host: cachyos
source: inventory.json  (scanned 2026-07-11 14:32:07)

nvim
  OK     program installed (neovim)
  OK     config at ~/dotfiles/nvim  (via ~/.config/nvim → symlink)
  OK     git: dotfiles @ main
  WARN   git: 1 uncommitted change in the working tree
         → git -C ~/dotfiles status
  WARN   git: 2 commits ahead of origin/main (unpushed)
         → git -C ~/dotfiles push

ghostty
  OK     program installed (ghostty)
  OK     config at ~/.config/ghostty
  INFO   not under version control — candidate for a dotfiles repo

tmux
  OK     program installed (tmux)
  OK     config at ~/.tmux.conf
  INFO   not under version control — candidate for a dotfiles repo

polybar
  WARN   config present but program not found (pacman or PATH)
         → likely stale; verify before removing ~/.config/polybar
  INFO   not under version control

Summary: 4 programs · 7 OK · 3 WARN · 0 ERROR · 3 INFO
Needs attention:
  • nvim     — 1 uncommitted change, 2 commits unpushed to origin/main
  • polybar  — orphan config (program not found)
```

## Flags summary

Display filters (`--generated`, `--secrets`, `--only-orphans`,
`--min-relevance`) shape the human-readable listing and the `--config-sync`
skeleton only; the `--json` inventory is always complete.

| Flag | Effect |
|------|--------|
| `--generated`       | Show machine-generated (`editable: false`) entries in the listing. |
| `--all`             | Also scan the `state` and `cache` roots (implies `--generated`). |
| `--secrets`         | Show `secret`-flagged entries in the listing (they are always recorded in `--json`, never in `--config-sync`). |
| `--only-orphans`    | Restrict the listing to orphan entries (config whose program was not found). |
| `--min-relevance N` | Hide entries scoring below `N` from the listing. |
| `--root PATH`       | Add an extra scan root (repeatable). |
| `--json`            | Canonical, complete structured output (`{meta, entries}`) to stdout. |
| `--config-sync`     | Emit a commented `config-sync` skeleton from top candidates. |

`health` is a subcommand, not a flag: `inventory.py health <inventory.json>`.

## Performance

- One-level enumeration + shallow, bounded probes only; no full recursive walk of
  large trees. `~/.cache` is never deep-walked and is off by default.
- Directory sizes are a **capped** estimate (stop after a byte/entry budget) so a
  giant `data` dir can't stall the run; the cap is noted in the record.
- `pacman -Qq` is loaded **once** into a set; per-entry attribution is then
  in-memory (including the `shutil.which` fallback). `pacman -Qo` is only invoked
  in the custom-root fallback.
- Git repo membership is detected **natively** (ancestor walk for `.git`) before
  any subprocess is spawned; git plumbing then runs once per repo root, memoized,
  with `git status --porcelain` output bounded.

## Testing

- **Fixture home tree.** Build a synthetic `$HOME` in a temp dir — rc files,
  a `.config` with real-ish program dirs, a dotfiles git repo with symlinks into
  `.config`, a dangling link, a secret dir, noise dirs, binary blobs — and run
  the scanner against it with `--root`/env overrides pointing at the fixture.
- **Golden JSON.** Compare the scan's `--json` output (volatile fields like
  `mtime`/`scanned_at` normalized) against a checked-in expected document; the
  worked example above is the seed for this golden file.
- **Seams for shell-outs.** `pacman` and `git` invocations go through
  module-level helper functions (the `capture`/`run` shape used in
  `config-sync.py`) so tests can monkeypatch them and unit-test attribution and
  git-record assembly without the real tools.
- **Health as a pure function.** Health checks take record dicts in and findings
  out, so they are unit-tested directly with hand-built records; the example
  `health` output above doubles as a golden rendering test.

## Out of scope

- **No modification of anything** — no dedup, cleanup, moving, or deleting. Purely
  read-only discovery. (Acting on the inventory is `config-sync`'s job.)
- **SQLite export (`--sqlite`) — deferred.** It would only be a relational
  projection of the JSON (the records stay the source of truth), and `jq` over
  two saved `--json` runs already covers cross-run diffing. It returns only if a
  real recurring-query need shows up; stdlib `sqlite3` keeps the door open.
- **`/etc` scanning — deferred.** One-level enumeration of `/etc` would mostly
  list pacman-owned defaults; the meaningful signal there is *modified from
  package defaults*, which is a `pacman -Qkk`/mtree comparison, not an ownership
  lookup. A future `/etc` mode should be specified around that check rather than
  bolted on via `--root`.
- No non-Arch package managers (`shutil.which` is the only concession to
  software installed outside pacman).
- No deep semantic parsing of config file *contents*; classification is by path,
  ownership, git/symlink state, and a shallow content sniff only.
- No daemon / watch mode — it's a one-shot snapshot.

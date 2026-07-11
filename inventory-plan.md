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

**Core stance: human-editable config only.** The default output is the set of
files a person would actually hand-edit and want under version control. Anything
that looks machine-generated — caches, state, logs, lock files, databases, binary
blobs, compiled artifacts — is **excluded by default**, not merely deprioritized.
Exclusion is best-effort and reversible: a flag re-includes the excluded set so
nothing is truly hidden, but the tool errs toward a clean, edit-worthy list.

**Symlinks resolve to their source.** When an entry is a symlink, the script
follows it to the real file and inventories *that* — the user-editable original —
rather than the link. See "Symlink resolution."

The goal is the same minimalism as `config-sync`: do only the work needed to
classify each entry correctly, surface what it finds, and let the user decide.

## Invocation

```
python3 inventory.py [--json | --config-sync | --sqlite PATH] [--generated]
                     [--all] [--secrets] [--orphans] [--min-relevance N]
                     [--root PATH ...]
```

- No positional arguments required — it scans the current user's home by default.
- Output goes to stdout; nothing is written to disk unless the user redirects it.
- Exit non-zero only on a hard failure (e.g. `pacman` missing, `$HOME` unset).

## Runtime & dependencies

- **Target runtime: Python 3 (stdlib only)** — matches `config-sync.py`. No
  third-party modules.
- **`pacman`** on `PATH` for package-ownership queries. A startup preflight
  checks for it and, if absent, prints `sudo pacman -S pacman`-style guidance and
  aborts — same shape as `config-sync`'s `git` preflight. (In practice pacman is
  always present on Arch; the check keeps the failure legible if run elsewhere.)
- **Native filesystem inspection** via `os` / `pathlib` (`os.scandir`,
  `os.path.islink`, `os.readlink`, `os.stat`, `os.walk`). Only `pacman` shells out
  (via `subprocess`), mirroring the "git-only shell-out" split in `config-sync.py`.
- **Arch-only assumption.** Ownership and package names are pacman-specific; no
  other package manager is probed.

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
- `--root PATH` adds an extra root to scan (repeatable); useful for `/etc` or a
  non-standard config location. Custom roots default to category `unknown`.

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

**Flags** (any combination):

- `secret` — sensitive credential stores: `.ssh`, `.gnupg`, `.password-store`,
  `.netrc`, `.aws`, `.config/gh`, `.docker/config.json`, `.git-credentials`, …
  Hidden unless `--secrets`; **never** emitted into a `config-sync` skeleton.
- `git-repo` — the entry is (or contains at its root) a git work tree.
- `dangling` — reached via a symlink whose target does not exist (no editable
  source; hidden by default, see Symlink resolution).
- `generated` — failed the human-editable filter (machine-generated); hidden by
  default, re-included with `--generated`.
- `orphan` — a `config`-category entry whose owning program does **not** appear to
  be installed (cleanup candidate).
- `noise` — matches a denylist of known state/cache dirs that live *under*
  `.config` despite not being hand-editable config (e.g. `.config/Code/`,
  `.config/chromium/`, `.config/discord/`, `.config/BraveSoftware/`). Deprioritized.

## Human-editable filter (the primary job)

`~/.config` and the home dir are full of machine-generated content; the value of
the tool is separating hand-editable, version-control-worthy config from state and
cache masquerading as config. This is a **filter first**, not just an ordering:
an entry classified as machine-generated is dropped from the default output
entirely (re-included with `--generated`).

**What is treated as machine-generated (excluded by default):**

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

Within the kept set, each entry still gets a coarse **relevance score (0–100)**
for *ordering* and the `--min-relevance` filter — a transparent sum of labeled
contributions (printed with `--json`):

- **+** owned by an installed package · already a `git-repo` · matches the
  known-dotfiles registry · small, text-only tree.
- **−** `*.json`-heavy · `orphan` (owning program not installed → probably stale).

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
3. **`pacman -Qo` fallback** for genuine on-disk files (mostly relevant to
   `--root /etc`), guarded so the "no owner" case is a normal outcome, not an error.

An entry whose resolved program is *not* in the installed set is flagged `orphan`.

## Symlink resolution

Programs search a hierarchy of paths for their config, and a symlink high in that
search path often points *down* to the file the user actually edits (a dotfiles
repo, a stow tree, a hand-linked source). The inventory must catalog the **real
editable source**, not the link that shadows it.

- When an entry is a symlink, **follow the whole chain** (`os.path.realpath`) to
  the final target and inventory *that path* as the canonical entry. All the
  per-entry analysis — human-editable sniff, package attribution, git detection,
  relevance — runs against the resolved source, not the link.
- Record how it was reached: the resolving link path(s) are kept in
  `via_symlink` so the relationship is visible, but the entry's identity is the
  real file. This favors the user-editable original over the shadowing link.
- **Deduplicate.** If both a link and its target (or two links to one target)
  would be listed, they collapse to a single entry keyed on the resolved real
  path. The resolved source wins even when it sits outside the scanned roots
  (e.g. `~/.config/nvim` → `~/dotfiles/nvim`): the source is inventoried and the
  link is noted, not the reverse.
- **Dangling symlink** — target does not exist. There is no editable source to
  inventory, so it is dropped from the default output and surfaced only under
  `--generated`/`--all` with a `dangling` flag (a broken link is a cleanup
  signal, not editable config).

## Git awareness

An entry is `git-repo` if it lives **inside a git work tree** — its own root, or
any ancestor. Detection is `git -C <dir> rev-parse --show-toplevel`; a success
means the resolved source is tracked (the common case after symlink resolution:
`~/.config/nvim` → `~/dotfiles/nvim`, where the repo root is `~/dotfiles`, above
the entry). For every such entry the script captures a `git` sub-record so a
discovered dotfiles repo maps cleanly onto `config-sync` and the user can see how
far the working copy has drifted from its remote.

All queries are **read-only plumbing** and run **once per repo root**, memoized by
toplevel — several inventoried entries in one dotfiles monorepo share a single set
of git calls. Any field that a given query can't answer is left `null`; a missing
remote, no upstream, or a detached HEAD are normal outcomes, not errors.

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
| `dirty` | `git status --porcelain` non-empty ⇒ uncommitted changes; counts of modified / untracked |
| `last_commit` | short SHA + ISO date of `HEAD` (`git log -1 --format=%h\|%cI`) |

This mirrors the state a person reads before deciding whether a config is safe to
sync: *which repo, which remote, which branch, is it a feature branch off `main`,
is it ahead/behind, and is the working tree clean.* `git` and `pacman` remain the
only shell-outs, both read-only.

## Output

- **Default (human-readable):** entries grouped by category, ordered by relevance
  within each group, one line per entry: path, kind, relevance, owning
  program (+ `[installed]` / `[orphan]`), and flags. Git-tracked entries append a
  compact status token — repo name, branch, dirty marker, and drift — e.g.
  `(dotfiles: main ✎ ↑2↓1)` meaning branch `main`, uncommitted changes, 2 ahead /
  1 behind upstream. A trailing summary counts entries per category and highlights
  `orphan` / `secret` / `git-repo` sets.
- **`--json`:** the full per-entry record list (see below) for machine
  consumption, including the itemized relevance contributions.
- **`--config-sync`:** emit a ready-to-edit `config-sync` config skeleton
  (defaults to the Python `CONFIG = [...]` form; the format could be selected
  later) prefilled from the strongest candidates — `git-repo` entries (with
  discovered `repo`/`dest`/`mode`) and high-relevance package-matched configs.
  `secret`-flagged entries are **never** included. Emitted commented-out so the
  user opts each one in deliberately rather than syncing by accident.

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
installed       bool | null   (null = couldn't attribute)
flags           [secret, git-repo, orphan, noise, dangling, generated, ...]
relevance       int 0–100
relevance_terms [{label, delta}, ...]   (only with --json)
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

## Flags summary

| Flag | Effect |
|------|--------|
| `--generated`       | Re-include machine-generated entries (`generated`/`dangling`), hidden by default. |
| `--all`             | Also scan the `state` and `cache` roots (implies `--generated`). |
| `--secrets`         | Include `secret`-flagged entries (hidden by default). |
| `--orphans`         | Show **only** `orphan` entries (config with program not installed). |
| `--min-relevance N` | Hide kept entries scoring below `N`. |
| `--root PATH`       | Add an extra scan root (repeatable). |
| `--json`            | Machine-readable output. |
| `--config-sync`     | Emit a commented `config-sync` skeleton from top candidates. |

## Performance

- One-level enumeration + shallow, bounded probes only; no full recursive walk of
  large trees. `~/.cache` is never deep-walked and is off by default.
- Directory sizes are a **capped** estimate (stop after a byte/entry budget) so a
  giant `data` dir can't stall the run; the cap is noted in the record.
- `pacman -Qq` is loaded **once** into a set; per-entry attribution is then
  in-memory. `pacman -Qo` is only invoked in the `/etc`-style fallback.

## Out of scope

- **No modification of anything** — no dedup, cleanup, moving, or deleting. Purely
  read-only discovery. (Acting on the inventory is `config-sync`'s job.)
- No non-Arch package managers.
- No deep semantic parsing of config file *contents*; classification is by path,
  ownership, git/symlink state, and a shallow content sniff only.
- No daemon / watch mode — it's a one-shot snapshot.

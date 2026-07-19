# config-sync — Design

`config-sync` inventories, health-checks, and manages the config/dotfiles on an
Arch / CachyOS install. It answers "what config do I have, where, which program
owns it, and is it under version control?" — and then, on request, builds a
dotfiles repo from what it finds, symlinks that repo back into place, and can
undo either step. Discovery is always read-only; every mutation is opt-in,
copy-first, and reversible.

The console script is `config-sync` (`config_sync.cli:main`); `python -m config_sync`
is equivalent. Target runtime is Python 3.11+ (stdlib `tomllib`); the only
third-party dependency is `tomli_w`, used to *write* TOML.

---

## Principles

**Two axes, one safety boundary.** Everything divides into read-only
*inspection* and mutating *actions*, and the split is structural, not just
conventional:

- **Inspection** — `scan` builds an inventory; the reporters (`listing`, `json`,
  `health`) render it. Pure functions of the filesystem and the stored records.
- **Actions** — `tidy`, `adopt`, `link`, `unlink` change the filesystem and
  return a result.

`inventory.py` and `report.py` never import `fsops` or `sync`; `sync.py` is the
only module that imports the write primitives (`fsops`) or runs mutating git.
That one invariant keeps the inspection core provably read-only.

**Filtering happens at render time, not collection time.** The structured
`--json` inventory always contains *every* discovered entry with its flags;
`--generated`, `--secrets`, `--only-orphans`, and `--min-relevance` only shape
the human-readable listing. Inventories stay complete and comparable across runs
regardless of which flags were active, and `health`'s inventory checks work as a
pure function of the stored records (its secret and dangling checks depend on
those entries being present). `health` also overlays a small *live* check — the
managed repo's git state and whether each adopted config is currently linked —
which the CLI gathers on the spot (it can't come from an inventory that may have
been scanned on another machine) and hands to the reporter as plain findings, so
`report.py` still imports neither `fsops` nor `sync`.

**Human-editable config is the default lens.** `~/.config` and `$HOME` are full
of caches, state, logs, lock files, databases, and binary blobs. The default
listing shows the files a person would hand-edit and want under version control;
machine-generated content is *hidden* from that listing (not merely
deprioritized), while remaining in `--json` with `editable: false`.

**Symlinks resolve to their source.** A symlink high in a program's search path
usually points down to the file the user actually edits (a dotfiles repo, a stow
tree). The inventory catalogs the real editable target and notes the link in
`via_symlink`, rather than cataloging the shadowing link.

**Reversible by construction.** `adopt` *copies* (originals stay in place, so
adopt reverses by deleting the repo). `link` *backs up the original, then
symlinks*; `unlink` restores the backup. No operation ever destroys the only
copy of anything.

**Config is data, not code.** Which programs exist, which files are shell
startup files, which stores are secret, which names are machine-generated — all
live in a TOML config, so teaching the tool a new program or secret store is a
file edit, not a code change.

---

## Module layout

```
config_sync/
  inventory.py  # READ-ONLY engine: Config/load_config, content probes, the
                #   capture/status_counts/git_record shell-out seam, the scan
                #   (build_inventory/analyze/score/categorize), the Entry model,
                #   the safe_to_adopt gate, and repo path-mapping. Stdlib only.
  report.py     # read-only reporters: listing, json, health — pure functions
  fsops.py      # safe copy/move/symlink/backup/restore — never overwrite/delete
  sync.py       # ALL mutation: manifest + the tidy/adopt/link/unlink actions;
                #   the sole importer of fsops and the sole home of mutating git
  cli.py        # argparse dispatch: scan · health · tidy · adopt · link · unlink
```

**Dependency graph (acyclic, enforced):**

```
      cli
     ╱ │ ╲
report │  sync
     ╲ │ ╱ ╲
  inventory fsops
```

- `fsops` — stdlib only; the leaf, isolated because it is the code most likely
  to clobber `$HOME` if wrong.
- `inventory` — stdlib only, the read-only engine; **never imports `fsops`**.
- `report` — imports `inventory`; no `fsops`, no manifest.
- `sync` — imports `inventory` + `fsops`; the **sole** importer of `fsops` and
  the sole home of mutating git (`init`/`add`/`commit`). The safety boundary.
- `cli` — imports all three and dispatches.

**Why repo mapping lives in `inventory`, manifest in `sync`.** `safe_to_adopt`
(read-only, evaluated during the scan) needs `repo_root` to exclude the managed
repo, so the repo *path-mapping* (`repo_root`, `program_dirname`,
`repo_path_for`) is pure path logic that stays in `inventory`. The *manifest* (load/save) is used only by the
actions and writes through `fsops.ensure_parent`, so it lives in `sync`. This
keeps `inventory` free of any `fsops`/`sync` import and the graph acyclic.

**Errors.** Library code never calls `sys.exit`. A recoverable, user-facing
problem (bad config, unreadable manifest/plan, missing `$HOME`, `pacman` absent)
raises `ConfigSyncError` via the terse `die()` helper; `cli.main` catches it,
prints `config-sync: error: …` to stderr, and returns exit 1. This keeps the
library modules importable and unit-testable.

---

## Commands

```
config-sync scan   [--json] [--out [PATH]] [--generated] [--all] [--secrets]
                   [--only-orphans] [--min-relevance N] [--root PATH ...] [--config PATH]
config-sync health [inventory]
config-sync tidy   [--apply]
config-sync plan   [PATH] [--select curated|extended|everything]
                   [--include NAME ...] [--exclude NAME ...] [--config PATH]
config-sync adopt  [PATH] [--apply] [--force] [--config PATH]
config-sync link   [--apply]
config-sync sync   [--apply] [--force] [--config PATH]
config-sync unlink [--apply]
```

Read-only unless told otherwise: `scan` streams (or, with `--out`, writes) an
inventory but never touches configs, and `health` never writes; `plan` writes
only a plan file. Every mutating command follows the same rule — **report by
default, act only with `--apply`** — so `tidy`, `adopt`, `link`, `sync`, and
`unlink` are all dry-run by default. (`adopt` also has the edited plan file as a
prior review surface, and a populated-repo guard the dry run surfaces up front.)

| Command | Role |
|---------|------|
| `scan` | Inspect the system and emit an inventory (default: human listing; `--json`: full structured record to stdout; `--out`: that record to a file). |
| `health` | Read a saved `scan --out` inventory (default `<repo>/inventory.json`) and render a Markdown `:checkhealth`-style report, plus a live "Managed repo" section (repo built/committed? each adopted config linked?). Never scans. |
| `tidy` | Report (and with `--apply`, perform) a conservative set of transparent XDG relocations of `$HOME` config files into `~/.config`. |
| `plan` | Scan discovered configs and write an editable adopt plan. Copies nothing. |
| `adopt` | Report (and with `--apply`, materialize) the repo from the edited plan: copy the plan's entries in, write the manifest, `git init` (never commit). |
| `sync` | Deploy a repo onto this host: link the configs whose program is installed, unlink one whose program is gone. Report without `--apply`. |
| `link` | Deploy the repo back into place: back up each home original and replace it with a symlink into the repo. |
| `unlink` | Reverse `link`: remove the symlink and restore the backed-up original. |

`scan` and `adopt` require `pacman` on `PATH` (a startup preflight aborts with
guidance if absent). `tidy`/`health`/`link`/`unlink` do not.

---

## Configuration

The classification tables ship as `inventory-config.toml` (package data, loaded
on every `scan`; `--config PATH` selects another copy). The config is the single
source of truth — there are **no built-in defaults**, and a missing or malformed
config is a hard error. Consistent with the read-only stance, the tool never
writes this file; it is maintained by hand.

| Section | Shape | Feeds |
|---------|-------|-------|
| `[programs]` | `name = { paths = [...], pkgs = [...]?, bin = str?, category = str? }` | known-dotfiles registry + reverse path map; `category` groups the program in `health` |
| `[shell]` | `files = [...]` | `shell`-location rc files |
| `[secrets]` | `home = [...]`, `config = [...]` | `secret` flag — home-dir and `~/.config` basenames |
| `[noise]` | `dirs = [...]` | `noise` flag — state/cache dirs living under `~/.config` |
| `[exclude]` | `home = [...]` (globs) | home-dir basenames dropped from the scan entirely |
| `[generated]` | `exts = [...]`, `dir_names = [...]` | machine-generated denylist |

`[generated].exts` accept a leading dot or not and match case-insensitively.
Everything is grouped under named tables (never bare top-level keys) so a key's
scope cannot shift with its position in the file.

What stays in **code**, deliberately, because it is behavior rather than curated
data: the root/category wiring and the home-exclusion set (`.config`, `.cache`,
`.local`, scanned as their own roots), the category display order, the
text-vs-binary sniff byte set, and the relevance weights.

---

## Scan

### Scope

Roots are scanned in priority order; XDG env vars override the defaults.

| Root | Env override | Default location | Default? |
|------|--------------|------------------|----------|
| `~/.config` | `$XDG_CONFIG_HOME` | `config` | yes |
| Home-dir dotfiles (`~/.*`) | — | `home` | yes |
| `~/.local/share` | `$XDG_DATA_HOME` | `data` | yes (low relevance) |
| `~/.local/state` | `$XDG_STATE_HOME` | `state` | `--all` |
| `~/.cache` | `$XDG_CACHE_HOME` | `cache` | `--all` |
| `--root PATH` (repeatable) | — | `unknown` | when given |

Enumeration is **one level deep** — one entry per top-level child — with only
shallow, bounded probes below it (git/symlink detection, a capped size
estimate). `~/.cache` is never deep-walked. Home-dir dotfiles exclude the XDG
dirs handled as their own roots, and drop anything matching `[exclude].home`.

### Classification

Each entry gets exactly one **location** and, orthogonally, a set of **flags**.

**Location** (drives default visibility and relevance): `config` (under
`.config`, or a known rc file — the primary target), `shell` (shell startup
files), `home` (a home-dir dotfile not otherwise classified), `data` / `state` /
`cache` (machine-managed, deprioritized or hidden), `unknown` (custom roots and
the unclassifiable). Precedence when several rules match: `shell` > `config` >
`home` > root default. The location comes from *where the entry was found* (the
link's root); all analysis runs against the resolved symlink target.

> Note: in the stored record this field is named `location`. The separate
> `category` field holds the *program's* display category from `[programs]`
> (e.g. "Editors"), used to group the `health` report.

**Flags** record only what is *not* derivable from the scalar fields:

- `secret` — sensitive credential store (`.ssh`, `.gnupg`, `.aws`, `.config/gh`,
  …). Recorded (path + flags only — **content sniffing is skipped**) and hidden
  from the listing unless `--secrets`.
- `noise` — a known state/cache dir living under `.config` (`.config/Code/`,
  `.config/chromium/`, …). Deprioritized.
- `dangling` — reached via a symlink whose target is gone. **Shown by default**
  (a broken link is exactly the cleanup signal the tool exists to surface) and
  reported as `ERROR` by `health`.

Derived states are rendered as display-only badges, never stored redundantly, so
each fact has one source of truth: `orphan` ⇐ `installed == false`, `generated`
⇐ `editable == false`, `git-repo` ⇐ `is_git_repo`.

### Human-editable filter

An entry is treated as machine-generated (dropped from the default listing;
`editable: false` in `--json`) when it is `cache`/`state`/`noise`, has binary or
high-non-printable content (bounded first-KB sniff), is a database / log / lock /
socket / pid / backup by extension, sits in a generated dir name (`Cache/`,
`logs/`, `Crashpad/`, …), or is a directory whose sampled contents are entirely
of those kinds. Kept as human-editable: text config, the curated registry
entries, and anything inside a git repo (the user is clearly managing it).
`*.json` is kept but weighted down, since many programs write state as JSON.

Within the kept set each entry gets a coarse **relevance score** (0–100 after
clamping) for ordering and `--min-relevance` — a transparent sum of labeled
contributions, always itemized in `--json`:

- **+** owned by an installed package · already a git-repo · matches the
  known-dotfiles registry · small text-only tree · known rc file.
- **−** `*.json`-heavy · orphan (owning program not found → probably stale).

### Package cross-reference

Attributing a config to a program, cheapest first:

1. **Registry path match** — the `[programs]` reverse map resolves a basename
   (`.tmux.conf` → `tmux`) to its canonical program and expected paths.
2. **Installed-set name match** — `pacman -Qq` loaded once into a set; match the
   basename directly and via a normalized (`.`-stripped, lowercased) form.
3. **`shutil.which` secondary check** — `pacman -Qq` misses
   flatpak/pip/cargo/npm/appimage/curl-installed tools; if the program's binary
   is on `PATH`, `installed` is `true`. Only found in **neither** pacman **nor**
   `PATH` makes an entry an orphan — wording is always "not found," not "not
   installed."
4. **`pacman -Qo` fallback** for genuine on-disk files under custom roots, with
   the "no owner" case treated as normal.

### Git awareness

An entry is `git-repo` if it lives inside a git work tree. Detection is **native
first**: walk the resolved path's ancestors for a `.git` entry (pure `os.path`,
no fork), so the common not-in-a-repo case costs zero subprocesses across ~100+
entries. Only when a `.git` ancestor is found does it shell out to read-only
plumbing, **once per repo toplevel** (memoized). `git status --porcelain` is read
up to a line cap; past it the dirty counts are reported as saturated.

| Field | Source (read-only) |
|-------|--------------------|
| `root` | `rev-parse --show-toplevel` |
| `name` | basename of `root` (fallback: `origin` URL) |
| `remotes` | `remote -v` → `[{name, url}]`, `origin` first |
| `branch` | `rev-parse --abbrev-ref HEAD` (`HEAD` ⇒ `(detached)`) |
| `upstream` | `@{u}` symbolic name, or null |
| `ahead` / `behind` | `rev-list --left-right --count HEAD...@{u}` |
| `default_branch` | `origin/HEAD` → `main` → `master` |
| `vs_default` | `{ahead, behind, is_default}` vs the default branch |
| `dirty` | `{modified, untracked}` (bounded; `{0,0}` = clean) |
| `last_commit` | `{sha, date}` of HEAD |

---

## Data model

The scan builds an `Entry` dataclass per top-level child and emits `asdict(...)`,
so records are plain JSON-serializable dicts (the `scan --json` interchange
format). Both scan builders share the dataclass, so they cannot drift.

```
path            absolute path of the resolved editable source
rel             path relative to $HOME (absolute if outside home)
location        config | shell | home | data | state | cache | unknown
kind            file | dir  (null for a dangling link)
via_symlink     [link paths that resolved to this entry], or null
size            bounded byte estimate (dirs: shallow, capped)
mtime           last-modified (ISO 8601)
editable        bool   (passed the human-editable filter)
adoptable       bool   (safe+sensible to copy into the managed repo; safe_to_adopt)
is_git_repo     bool
git             sub-record (see above), or null
program         resolved owning program, or null
category        program's display category (from [programs]), or null
installed       bool | null  (found via pacman or PATH; null = unattributed)
flags           subset of [secret, noise, dangling]  (non-derivable only)
relevance       int, 0–100
relevance_terms [{label, delta}, ...]  (itemized)
```

Deduplication is keyed on the resolved real path: a link and its target (or two
links to one target) collapse to a single entry; the resolved source wins even
when it sits outside the scanned roots, and the surviving entry takes the
location of the link whose root ranks highest.

### Example `--json` entry

```json
{
  "path": "/home/jd/dotfiles/nvim",
  "rel": "dotfiles/nvim",
  "location": "config",
  "kind": "dir",
  "via_symlink": ["/home/jd/.config/nvim"],
  "size": 48211,
  "mtime": "2026-07-09T21:14:03-05:00",
  "editable": true,
  "is_git_repo": true,
  "git": {
    "root": "/home/jd/dotfiles", "name": "dotfiles",
    "remotes": [{"name": "origin", "url": "git@github.com:joshdennin/dotfiles.git"}],
    "branch": "main", "upstream": "origin/main", "ahead": 2, "behind": 0,
    "default_branch": "main",
    "vs_default": {"ahead": 0, "behind": 0, "is_default": true},
    "dirty": {"modified": 1, "untracked": 0},
    "last_commit": {"sha": "a1b2c3d", "date": "2026-07-09T21:14:03-05:00"}
  },
  "program": "neovim",
  "category": "Editors",
  "installed": true,
  "flags": [],
  "relevance": 95,
  "relevance_terms": [
    {"label": "installed package (neovim)", "delta": 30},
    {"label": "git repo", "delta": 25},
    {"label": "known-dotfiles registry", "delta": 25},
    {"label": "text-only tree", "delta": 15}
  ]
}
```

The entry carries no stored flags — `git-repo` is derived from `is_git_repo` at
display time.

---

## Reporters

All three share the signature `(inv, args, cfg) -> str` and are selected by name
from a `REPORTERS` registry, so adding a format is one entry.

- **`listing`** (scan default) — entries grouped by location, ordered by
  relevance, one line each: relevance, path, kind, owning program (+
  `[installed]`/`[orphan]`), flag/derived badges, and a compact git token
  (`(dotfiles: main ✎ ↑2↓1)`). A trailing summary counts per location and
  highlights orphan/secret/git-repo sets. The display filters apply here only.
- **`json`** — the canonical `{meta, entries}` object to stdout; always complete
  (only unscanned roots are absent). `meta` carries run context (host, scan
  time, resolved roots, version).
- **`health`** — a Markdown `:checkhealth`-style report, a pure function of a
  saved inventory (no rescanning; reflects the snapshot). Each program is a
  section grouped under its category; unattributed entries trail in their own
  section. Findings, each a severity + optional suggested command:

| Check | Condition | Status |
|-------|-----------|--------|
| Program present | owning program `installed` | `OK` |
| | config present but program not found (pacman or PATH) | `WARN` |
| Location | single resolved config | `OK` |
| | config at several known paths at once | `WARN` |
| | reached via a dangling symlink | `ERROR` |
| Git — clean / dirty / ahead / behind / diverged | from the git sub-record | `OK` / `WARN` |
| Git — non-default branch | `vs_default.is_default` false | `INFO` |
| Git — detached | branch detached | `WARN` |
| Git — remote | no upstream / empty remotes | `INFO` |
| Not tracked | `is_git_repo` false | `INFO` (version-control candidate) |
| Safety | `secret`-flagged config present | `WARN` (do not sync publicly) |

Severities roll up into a summary (`OK`/`WARN`/`ERROR`/`INFO` counts) and a
"needs attention" list of everything `WARN`/`ERROR`. Because the "not found"
check is best-effort, orphan findings suggest *verifying* before removing, never
a bare removal command.

---

## Actions

### Managed repo and manifest

The managed repo lives at `~/.config/config-sync/`. Each adopted program gets its
own directory named by its command (`nvim`, `tmux`, `git`); a directory entry
maps to that program directory (its tree copied in, structure preserved), a file
entry to a file beneath it.

Program-grouping discards the original home location, so a **manifest**
(`~/.config/config-sync/manifest.toml`) is the source of truth for reversal. Per
adopted entry it records `program`, `home_path`, `repo_path`, `kind`, and link
state (`linked`, `backup_path`). `adopt` writes it; `link`/`unlink` consume and
update it. It is TOML (stdlib `tomllib` to read, `tomli_w` to write); since TOML
has no null, absent values (unattributed program, unset backup path) are stored
as `""`.

### tidy

Reports (and with `--apply`, performs) a conservative "Tier 1" set of XDG
relocations: `$HOME` config files whose program reads the `~/.config` location
*automatically* — no env var, wrapper, or sourced stub — so the move is
transparent (e.g. `.gitconfig` → `git/config`, `.tmux.conf` → `tmux/tmux.conf`).
A candidate moves only when its target is absent; a symlinked source is reported
but never moved. Orthogonal to the repo workflow, and read-only without `--apply`.

### plan / adopt

Two-phase, `terraform`-style, split across two commands. `plan` scans, filters
to a breadth tier, and writes an **editable plan file** (copying nothing); the
plan is the review-and-choose surface, so nothing is silently dropped. `adopt`
reads the edited plan and materializes the repo.

Tiers seed the plan on top of the always-on `safe_to_adopt` gate, as a tunable
relevance floor: `curated` (≥ 50, default), `extended` (≥ 15), `everything`
(≥ 0). An entry already its own git repo bypasses the floor and is surfaced
with `adopt = false` and a comment marking it as already tracked in its own repo
(the managed state is discovered, not chosen, so it is a comment rather than an
editable field) — visible for review but opt-in, never copied unless the user
turns `adopt` on. `--include` / `--exclude`
(program or category names) pre-narrow candidates; the plan's per-entry
`adopt = true/false` is the final say. `adopt` is dry-run by default — it reports
the copy/skip split (`adopt_survey`, writing nothing) and flags up front whether
a populated-repo guard would block it. With `--apply` it **copies** each
`adopt=true` entry into the repo via `fsops` (originals untouched), merges the
manifest, and `git init`s a not-yet-versioned repo — the only git it runs;
staging, commits, and pushes are the user's. It is re-runnable (entries already
in the manifest or present in the repo are skipped) and refuses to add to a repo
that already holds adopted content unless `--force`, protecting a shared/cloned
repo. Both commands default their plan path to `<repo>/config-sync-adopt.toml`; a
path argument overrides.

### link

Deploys the repo back into the filesystem. Per manifest entry, `link_status`
classifies it (`link` / `link-missing` / `done` / `conflict` / `no-source`) and
`link_apply` backs up the home original into `~/.config/config-sync/.backups/`
(mirroring the home path), records `backup_path`, symlinks home → repo, and marks
`linked`. Never overwrites: a home symlink pointing elsewhere is a `conflict`,
left as-is. Idempotent: a symlink already resolving into the repo is `done`. The
step is **atomic** — if the original is backed up but the symlink then fails, the
backup is rolled back, so a failed link never orphans the original. The
`.backups/` tree is git-ignored (written at adopt time, defensively re-checked at
link time) so backups never enter history. Dry-run by default; `--apply` acts.

### unlink

Reverses `link`. Per entry, `unlink_status` classifies it (`restore` /
`unlink-only` / `not-linked` / `changed`) and `unlink_apply` removes the symlink
and restores the backup, clearing link state. Safety: only a home path that is
still the exact symlink config-sync created is touched — a `changed` entry (a
real file the user put back) is left alone. Idempotent; leaves the repo (the
adopt copy) intact. This is the reversibility guarantee made operational.

---

## Safety model

- **`fsops` primitives** (`safe_copy`, `safe_move`, `safe_symlink`,
  `remove_symlink`, `backup`, `restore`) are the only writers. Every one refuses
  to overwrite or delete: it raises `FsError` rather than clobber existing state,
  `remove_symlink` refuses a non-symlink, `backup` refuses a path outside home,
  and parents are created as needed. Dry-run/reporting lives in the action layer;
  the primitives always act. `ensure_parent` is shared internal support.
- **The adopt safety gate has one source of truth.** `safe_to_adopt` is a pure
  predicate evaluated **once per entry at scan time** and stored as the
  `adoptable` field; it refuses secrets, dangling links, any non-editable entry
  (generated / cache / state / noise), and anything at or under the managed repo
  path. `adopt` consults it through the thin `is_adoptable(rec)` accessor, which
  only *reads* the stored bool — no consumer reconstructs the decision. This is
  deliberate: secrets carry `editable = True` (their content is never sniffed),
  so deciding adoptability from `editable` downstream would be a latent footgun.
  Computing it up front, from the raw fields, closes that gap. `editable` is left
  to mean strictly "human-editable content."
- **The read-only boundary** is import-enforced: `inventory`/`report` cannot
  reach the write primitives, so `scan`/`health` are read-only by construction.

Remaining backlog (latency on repo-heavy homes, the Arch-only package
dependency) is tracked in `ISSUES.md`.

---

## Testing

- **Fixture home tree.** A synthetic `$HOME` in a temp dir — rc files, a
  `.config` with program dirs, a dotfiles repo with symlinks into `.config`, a
  dangling link, a secret dir, noise dirs, binary blobs — scanned via `--root` /
  env overrides.
- **Shell-out seams.** `pacman` and `git` go through module-level helpers
  (`capture`, `status_counts`, `git_record`) so attribution and git-record
  assembly are unit-tested without the real tools. The per-repo git cache lives
  on the `Scan` context (one per `build_inventory`), so tests are isolated by
  construction — no module global to clear between runs.
- **Pure functions.** Reporters and health checks take records in and strings out,
  so they are tested directly with hand-built records; the manifest/plan I/O and
  the `fsops` guards have their own units.
- **Action round-trips.** `adopt → link → unlink` is exercised on a temp home,
  including the link partial-failure rollback path.

The suite runs via `python -m pytest` (or
`python -m unittest discover -t . -s tests`).

---

## Out of scope

- **No modification during discovery** — `scan`/`health` never write. Mutation is
  confined to the opt-in actions.
- **No non-Arch package managers** — ownership is pacman-specific; `shutil.which`
  is the only concession to software installed outside pacman.
- **No deep semantic parsing of config contents** — classification is by path,
  ownership, git/symlink state, and a shallow content sniff only.
- **No `/etc` scanning** — one-level enumeration would mostly list pacman-owned
  defaults; the meaningful signal there is *modified from package defaults* (a
  `pacman -Qkk`/mtree comparison), which a future `/etc` mode should be built
  around rather than bolted on via `--root`.
- **No SQLite export** — it would only be a relational projection of the JSON, and
  `jq` over two saved runs already covers cross-run diffing. `sqlite3` stays in
  stdlib if a real recurring-query need appears.
- **No daemon / watch mode** — a one-shot snapshot.
